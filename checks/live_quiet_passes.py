"""One live check of the passes after a first run on real models: the gtm workspace of `live_gtm.py` with a made-up
export of CRM contacts beside its notes, one record a row with its fields, held for the people curator, its first run
approved as there. Then one new contact and one new call note arrive: a VP of Finance at an account the workspace
keeps, who joins the call and signs off on the purchase, and that call. The people and calls curators may admit them on
their own authority, and five passes run. Not part of the test suite: it calls real models and costs a few
dollars.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_quiet_passes.py

What each pass is expected to hold:
- pass 1: the two new records are listed and held, after the curators' turn, beside whatever the first run's build
  left for curators to read;
- pass 2: the people and calls curators wake on them, admit them on their own authority, and writers write;
- pass 3: no curator wakes on its own admits alone; a wake on what a writer wrote is printed and does not fail;
- passes 4 and 5: nothing new, so nothing should start.

For each quiet pass it prints every run the pass
started, by role, with what woke it, then the records read and facts made, and what the pass spent in dollars and
seconds. For a curator woken by change it prints which of its evidence lines moved since it last read them: the items
added, removed and changed in place, each counted by who last changed it: `own`, the landing of a proposal the
curator filed and decided on its authority; `decided`, one it filed that a caller decided; `writer`; or `other`.
It then lists each thing two curators handed back and forth (A, B, A), which it reports and never fails on, and
ends with a table of the passes, how many passes each curator that admitted on its own ran after its admit and
what those passes cost, the lines `self wakes: <n>` and `other wakes: <n>` that `side_by_side.py`
reads, then PASS, or FAIL with every reason. It never prints a key. It passes only when:
- the first run built what its approved list holds;
- no curator wakes by change on nothing but the landing of its own proposals, `own` or `decided`, in any quiet
  pass: every other wake, such as on a field a writer set, is printed and does not fail;
- nothing of a quiet pass fails, and no daily cap stops one.
With `--keep <folder>` the workspace is made there and left, to read it after.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from _first_run import taken_whole
from _no_sf import refuse_sf
from _paths import ran_lines
from live_gtm import LOCAL, NOTES
from spend import dollars

from cerebro import curate, scaffold
from cerebro.app import Cerebro
from cerebro.article import compiled_at
from cerebro.first_run import run as first_run
from cerebro.identity import ADMITTED_FROM
from cerebro.records import proposals

QUIET = 5
CONTACTS = """kind: files
window: { behind: all }
select:
  - glob: "local/crm/contacts.csv"
    rows: csv
    id: "{row.contact_id}"
    at: "{row.last_activity}"
    into: people
    identity: { email: "{row.email}" }
    fields: { name: "{row.name}", title: "{row.title}", account: "{row.account}", phone: "{row.phone}" }
"""
EXPORT = """contact_id,name,email,title,account,phone,last_activity
003A01,Nessa Torvik,nessa@brightwater.example,VP of Operations,Brightwater Logistics,+1 555 0101,2026-11-14
003A02,Priya Nair,priya@brightwater.example,IT Security Lead,Brightwater Logistics,+1 555 0102,2026-11-14
003A03,Marco Feld,marco@brightwater.example,Procurement Manager,Brightwater Logistics,+1 555 0103,2026-11-10
003A04,Ellen Tran,ellen@brightwater.example,Warehouse Supervisor,Brightwater Logistics,+1 555 0104,2026-10-02
003B01,Omar Haddad,omar@kestrel.example,CFO,Kestrel Foods,+1 555 0201,2026-11-12
003B02,Ruth Abara,ruth@kestrel.example,Accounts Payable Manager,Kestrel Foods,+1 555 0202,2026-11-05
003C01,Grace Liu,grace@novaline.example,Director of IT,Novaline Health,+1 555 0301,2026-11-17
003C02,Tom Becker,tom@novaline.example,Clinic Operations Lead,Novaline Health,+1 555 0302,2026-11-07
"""
LATE_CONTACT = "003B03,Nadia Osei,nadia@kestrel.example,VP of Finance,Kestrel Foods,+1 555 0203,2026-11-20\n"
LATE_CALL = ("local/calls/2026-11-20-kestrel-pricing.md", """Account: Kestrel Foods
Attendees: Nadia Osei <nadia@kestrel.example>, Omar Haddad <omar@kestrel.example>, Sam Okoye <sam@ourco.example>
Date: 2026-11-20

Nadia Osei, VP of Finance at Kestrel Foods, joined Omar Haddad and Sam Okoye from our side to review pricing.
Nadia said she signs off on the purchase and owns the budget for invoice matching.
Sam Okoye will send Nadia the final pricing by 28 November.
""")
SEEN: dict[str, dict[str, dict[str, tuple[str, str]]]] = {}  # each curator's evidence as last read, by line
MOVED: dict[str, dict[str, Counter[str]]] = {}  # what moved in each curator's evidence this pass, and who moved it
FILED: dict[str, int] = {}  # the pass each proposal was first seen in, the first run as 0
WHO = ("own", "decided", "writer", "other")  # who last changed a moved item: see `changed_by`


def spy(digest: Any, workspace: Cerebro) -> Any:
    """The hash a curator's change wake compares, keeping what moved in a curator's evidence since its digest was last taken on a gather, by
    line: each item added, removed, or changed in place, such as a field a writer set, with who last changed it, as
    it stands when the curator reads it."""
    def taken(agent: Any, evidence: list[Any]) -> str:
        now = {e.spelled or e.heading: {i.key: (i.counts or i.text, i.left)
                                        for i in (e.items if e.counted is None else e.counted)} for e in evidence}
        if agent.id not in MOVED:  # the first digest of a pass is the gather's: one after a run restates it
            before = SEEN.get(agent.id, {})
            MOVED[agent.id] = {}
            for line in {**before, **now}:
                old, new = before.get(line, {}), now.get(line, {})
                moved = Counter()
                for key in old.keys() | new.keys():
                    if old.get(key) != new.get(key):
                        moved["+" if key not in old else "-" if key not in new else "~"] += 1
                        moved[changed_by(workspace, agent.id, key, (new.get(key) or old[key])[1])] += 1
                if moved:
                    MOVED[agent.id][line] = moved
            SEEN[agent.id] = now
        return digest(agent, evidence)
    return taken


def own_acts(workspace: Cerebro, curator: str) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """What the landing of a curator's own proposals touched, whoever decided them: each article, with when it last
    landed one and how, and each record they named or made an article of, with how. How is `own` when the curator
    decided it on its authority, `decided` when a caller did."""
    articles: dict[str, tuple[str, str]] = {}
    records: dict[str, str] = {}
    for p in proposals(workspace.store):
        if p.by.name != curator or p.status != "accepted":
            continue
        how = "own" if p.decided_by == p.by else "decided"
        for id in (p.target, str(p.payload.get("with") or "")):
            if id and p.at >= articles.get(id, ("", ""))[0]:
                articles[id] = (p.at, how)
        for r in [p.payload.get("record"), *(p.payload.get("records") or [])]:
            if isinstance(r, dict):
                records[f"{r.get('source')}:{r.get('id')}"] = how
    for key, id in (workspace.store.state(ADMITTED_FROM) or {}).items():
        if id in articles:
            records.setdefault(key, articles[id][1])
    return articles, records


def changed_by(workspace: Cerebro, curator: str, key: str, left: str) -> str:
    """Who last changed one item that moved in a curator's evidence: the landing of its own proposal, `own` on its
    authority or `decided` by a caller, a `writer`, or `other`."""
    articles, records = own_acts(workspace, curator)
    kind, _, rest = key.partition(":")
    if kind == "article":
        article = workspace.store.article(rest)
        if article is not None and compiled_at(article) > articles.get(rest, ("", ""))[0]:
            return "writer"
        return articles[rest][1] if rest in articles else "other"
    if kind == "held":
        return records.get(left, "other")
    if kind == "fact":
        fact = next(iter(workspace.extraction.facts(ids=[rest])), None) if workspace.extraction else None
        return records.get(f"{fact.source}:{fact.record}", "other") if fact is not None else "other"
    if kind == "unlinked":
        return next((articles[id][1] for id in workspace.store.named(rest) if id in articles), "other")
    return "other"


def said(moved: dict[str, Counter[str]]) -> str:
    """What moved in a curator's evidence, line by line, and who last changed it."""
    return "; ".join(f"{line} +{c['+']} -{c['-']} ~{c['~']} (" + ", ".join(f"{who} {c[who]}" for who in WHO if c[who])
                     + ")" for line, c in moved.items()) or "nothing it compares"


def quiet_pass(workspace: Cerebro, n: int, reasons: list[str]) -> dict[str, Any]:
    """Run one pass with nothing new, print every run it started, and give the row the table prints. A curator woken
    by change on nothing but what its own acts changed fails the check."""
    MOVED.clear()
    roles = {a.id: a.role for a in workspace.agents}
    spent, began = workspace.ledger.spent(workspace.clock()), time.monotonic()
    result = workspace.run_once()
    seconds = time.monotonic() - began
    for p in proposals(workspace.store):
        FILED.setdefault(p.id, n)
    admits = [p for p in proposals(workspace.store) if p.kind == "article.admit" and p.status == "accepted"
              and roles.get(p.by.name) == "curator" and FILED[p.id] == n]
    row = {"pass": n, "curators": [], "curator_usd": 0.0, "facts": result.extracted, "read": result.read,
           "writers": 0, "self": 0, "other": 0, "seconds": seconds, "ran": [r.agent for r in result.runs],
           "admitted": sorted({p.by.name for p in admits if p.decided_by == p.by})}
    print(f"\nquiet pass {n}: {result.read} records read, {result.extracted} facts, compiled {len(result.compiled)}")
    for run in result.runs:
        role, why, usd = roles.get(run.agent, "?"), result.due.get(run.agent, "?"), dollars(run)
        if role != "curator":
            row["writers"] += 1
            print(f"  ran {role} {run.agent}: woke by {why}; {run.status}; ${usd:.4f}")
            continue
        moved = MOVED.get(run.agent, {})
        self_wake = why == "change" and bool(moved) and all(set(c) <= {"+", "-", "~", "own", "decided"} for c in moved.values())
        mine = [p for p in admits if p.by.name == run.agent]
        print(f"  ran curator {run.agent}: woke by {why}" + (f" ({said(moved)})" if why == "change" else "")
              + (" SELF WAKE: only its own proposals' landing moved" if self_wake else "") + f"; admitted {len(mine)}; ${usd:.4f}")
        for p in mine:
            print(f"    admit {p.target}: {json.dumps(p.payload.get('fields') or {})[:160]} {p.reason[:100]!r}")
        if why == "change":
            row["self" if self_wake else "other"] += 1
        if self_wake:
            reasons.append(f"quiet pass {n}: {run.agent} woke on nothing but its own proposals: {said(moved)}")
        row["curators"].append(f"{run.agent.split('/')[0]} ({why}{', SELF WAKE' if self_wake else ''}, {len(mine)}, "
                               f"${usd:.2f})")
        row["curator_usd"] += usd
    for line in ran_lines(result):
        print(line)
    for key, why in result.failed.items():
        reasons.append(f"quiet pass {n}: {key} failed: {why}")
    for line in result.stops:
        reasons.append(f"quiet pass {n}: a daily cap stopped it: {line}")
    row["usd"] = workspace.ledger.spent(workspace.clock()) - spent
    print(f"  curators ${row['curator_usd']:.4f}, the pass ${row['usd']:.4f} in {seconds:.0f}s")
    return row


def settled(rows: list[dict[str, Any]]) -> None:
    """For each curator that admitted on its own: the pass it admitted in, the passes after it that ran it, and what
    the passes from its admit to the first that started nothing cost."""
    print()
    for curator in sorted({c for row in rows for c in row["admitted"]}):
        first = next(row["pass"] for row in rows if curator in row["admitted"])
        again = [row["pass"] for row in rows if row["pass"] > first and curator in row["ran"]]
        quiet = next((row["pass"] for row in rows if row["pass"] > first and not row["ran"]), None)
        spell = [row for row in rows if first <= row["pass"] < (quiet or QUIET + 1)]
        print(f"settled {curator}: admitted in pass {first}, ran again in {again or 'no pass'}, "
              + (f"the first pass to start nothing was {quiet}" if quiet else "no pass started nothing after it")
              + f"; passes {first} to {spell[-1]['pass']} cost ${sum(r['usd'] for r in spell):.4f} in "
              f"{sum(r['seconds'] for r in spell):.0f}s")


def back_and_forth(workspace: Cerebro) -> None:
    """Each thing curators handed back and forth: by the last part of its target, the curators that filed its
    proposals change and change back (A, B, A). Reported, never a failure."""
    roles = {a.id: a.role for a in workspace.agents}
    things: dict[str, list[Any]] = {}
    for p in sorted({p.id: p for p in proposals(workspace.store)}.values(), key=lambda p: p.at):
        if roles.get(p.by.name) == "curator":
            things.setdefault(p.target.rpartition("/")[2], []).append(p)
    handed = {}
    for thing, rows in things.items():
        order = [name for k, name in enumerate(p.by.name for p in rows) if not k or rows[k - 1].by.name != name]
        if any(order[k] == order[k + 2] for k in range(len(order) - 2)):
            handed[thing] = rows
    print(f"\nhanded back and forth: {len(handed)}")
    for thing, rows in handed.items():
        print(f"  {thing}: " + ", ".join(f"{p.by.name} {p.kind} {p.status} (pass {FILED.get(p.id, '?')})"
                                         for p in rows))


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    keep = sys.argv[sys.argv.index("--keep") + 1] if "--keep" in sys.argv else ""
    reasons: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(keep) if keep else Path(folder) / "workspace"
        scaffold.init(root, template="gtm", daily_usd=10, domains=["ourco.example"], environ=os.environ)
        (root / "sources" / "local.yaml").write_text(LOCAL, encoding="utf-8")
        (root / "sources" / "crm.yaml").write_text(CONTACTS, encoding="utf-8")
        (root / "local" / "crm").mkdir(parents=True)
        (root / "local" / "crm" / "contacts.csv").write_text(EXPORT, encoding="utf-8")
        for rel, text in NOTES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        with Cerebro(root, trusted=True) as workspace:
            workspace.sf_run = refuse_sf
            curate.fingerprint = spy(curate.fingerprint, workspace)  # what a curator's wake compares
            out = first_run(workspace, choose=taken_whole, say=lambda line: None)
            if out is None or out.result is None or out.waits or out.quit:
                reasons.append("the first run did not build what its approved list holds")
            first = workspace.ledger.spent(workspace.clock())
            print(f"first run: {Counter(r.agent.split('/')[0] for r in (out.result.runs if out and out.result else []))}"
                  f", ${first:.4f}")
            FILED.update({p.id: 0 for p in proposals(workspace.store)})
            with (root / "local" / "crm" / "contacts.csv").open("a", encoding="utf-8") as export:
                export.write(LATE_CONTACT)
            (root / LATE_CALL[0]).write_text(LATE_CALL[1], encoding="utf-8")
            rows = [quiet_pass(workspace, n, reasons) for n in range(1, QUIET + 1)]
            back_and_forth(workspace)
            settled(rows)
            print("\n| pass | curators that ran (woke by, admits, cost) | writers | curators | pass | seconds | facts |")
            print("|---|---|---|---|---|---|---|")
            for row in rows:
                print(f"| {row['pass']} | {', '.join(row['curators']) or 'none'} | {row['writers']} | "
                      f"${row['curator_usd']:.2f} | ${row['usd']:.2f} | {row['seconds']:.0f} | {row['facts']} |")
            print(f"self wakes: {sum(row['self'] for row in rows)}")
            print(f"other wakes: {sum(row['other'] for row in rows)}")
            print(f"spent: ${workspace.ledger.spent(workspace.clock()):.4f}, the first run ${first:.4f}")
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    from spend import watch

    watch()
    sys.exit(main())
