"""How often the sales template's open curators, calls and people, admit what does not hang off an article already in
the working set, on real models. Not part of the test suite: it calls real models and costs some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_hangs_off.py --trials 3

It makes the gtm template in a temporary folder with a
daily cap of $3, keeps only its accounts, calls and people curators, so no article is written, and writes made-up
notes of two companies. A first run approves its list whole, so both companies are accounts in the working set. Then
new calls land for six companies: the two in the working set, two whose account an accounts curator proposed and a
person has not decided, and two no article or proposal names. Each trial starts from that same workspace and runs the
calls curator, then the people curator, as their daily runs. An admit that lands for a company outside the working set
is counted as a leak: neither curator's file holds an admit back until its
account is in the working set. It prints each curator's calls and replies, what landed for which company, the leak
count over every trial against the chances to leak (each curator meets four companies outside the working set a
trial), what each trial cost, and the total from the workspace's spend log. It prints which new calls are held for the
calls curator, and counts each trial in which the new call of a company in the working set does not land on an article
of its own: such a call must, and one that lands on an earlier call's article is printed as such and counts as missed.
It never prints a key. It exits 0 when it measured, whatever the rate, and 1 when the setup did not hold, such as a
first run that left a company out of the working set.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from _first_run import whole
from _no_sf import refuse_sf
from spend import dollars

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.budget import LOG
from cerebro.caller import Caller
from cerebro.clock import stamp
from cerebro.first_run import run as first_run
from cerebro.held import held_for
from cerebro.records import Proposal, proposals, propose

LOCAL = """kind: files
window: { behind: all }
select:
  - glob: "local/calls/**/*.md"
    extract: facts
    into: calls
  - glob: "local/email/**/*.md"
    extract: facts
"""
KEPT = ("accounts/curator.md", "calls/curator.md", "people/curator.md")
IN_SET = {"Alder Mills": "alder.example", "Birch Labs": "birch.example"}
PROPOSED = {"Cobalt Ridge": "cobalt.example", "Dunmore Textiles": "dunmore.example"}
NOTHING = {"Elmstead Bakery": "elmstead.example", "Fenwick Optics": "fenwick.example"}
COMPANIES = IN_SET | PROPOSED | NOTHING
PEOPLE = {"Alder Mills": ("Ada Moss", "Ana Ortiz"), "Birch Labs": ("Ben Hale", "Bea Lund"),
          "Cobalt Ridge": ("", "Cy Park"), "Dunmore Textiles": ("", "Dee Shaw"),
          "Elmstead Bakery": ("", "Eli Grant"), "Fenwick Optics": ("", "Fay Imes")}


def note(company: str, who: str, day: str, said: str) -> str:
    domain = COMPANIES[company]
    first = who.split()[0].lower()
    return (f"Account: {company}\nAttendees: {who} <{first}@{domain}>, Sam Okoye <sam@ourco.example>\nDate: {day}\n\n"
            f"{said}\n")


def first_notes() -> dict[str, str]:
    """Two companies the first run admits: a call and a reply both ways each."""
    out = {}
    for company in IN_SET:
        who, slug = PEOPLE[company][0], company.split()[0].lower()
        out[f"local/calls/2026-11-03-{slug}-discovery.md"] = note(
            company, who, "2026-11-03", f"{who}, head of operations at {company}, asked for a pilot in January. Sam "
            "Okoye from our side will send a pilot plan by 10 November.")
        out[f"local/email/2026-11-10-{slug}-plan.md"] = note(
            company, who, "2026-11-10", f"Sam Okoye sent {who} the pilot plan. {who} replied that the plan looks right "
            "and asked for a security questionnaire.")
    return out


def later_notes() -> dict[str, str]:
    """A new call for each of the six companies, each with a person of theirs who has a role."""
    out = {}
    for company in COMPANIES:
        who, slug = PEOPLE[company][1], company.split()[0].lower()
        out[f"local/calls/2026-11-18-{slug}-call.md"] = note(
            company, who, "2026-11-18", f"{who}, finance lead at {company}, joined a call with Sam Okoye from our side "
            f"and asked for pricing for three sites. {who} said a decision comes by 15 December.")
    return out


def company_of(p: Proposal) -> str:
    """The company an admit is about, by a domain, a name or a note's file name it carries, or else its reason, which
    names a person's one account."""
    text = json.dumps([p.target, p.payload, p.reason], default=str).lower()
    return next((c for c, d in COMPANIES.items() if d in text or c.lower() in text
                 or f"-{c.split()[0].lower()}-" in text or c.split()[0].lower() in text), "unknown")


def company_of_text(text: str) -> str:
    """The company a held record's id names, by its note's file name."""
    return next((c for c in COMPANIES if f"-{c.split()[0].lower()}-" in text.lower()), "unknown")


def trial(root: Path, n: int, since: str) -> dict[str, Any]:
    with Cerebro(root, trusted=True) as workspace:
        workspace.sf_run = refuse_sf
        before = spent(workspace)
        for id in ("calls/curator", "people/curator"):
            done = workspace.curate(workspace.agent(id), wake="daily")
            run = done.run
            print(f"  trial {n} {id}: {run.status if run else 'skipped'}, ${dollars(run) if run else 0:.4f}")
            for entry in run.transcript if run else []:
                if entry.role == "call" and entry.tool in ("admit", "leave_out"):
                    print(f"    {entry.tool} {json.dumps(entry.args)[:220]}")
                elif entry.role == "tool" and entry.tool in ("admit", "leave_out"):
                    print(f"      -> {entry.text[:180]}")
        landed = [p for p in proposals(workspace.store, "accepted") if p.kind == "article.admit" and p.at >= since
                  and p.decided_by is not None and p.decided_by.kind == "agent"
                  and p.target.split("/")[0] in ("calls", "people")]
        proposed = [p for p in proposals(workspace.store, "waiting") if p.kind == "article.admit" and p.at >= since]
        cost = spent(workspace) - before
    by = Counter((p.target.split("/")[0], company_of(p)) for p in landed)
    onto = [p for p in landed if p.target.startswith("calls/") and not p.applied.get("created")]
    for p in onto:  # a new call is its own article: one that lands on an earlier call's is wrong
        print(f"  trial {n} WRONG ARTICLE: a new call of {company_of(p)} landed on {p.target}, which exists already")
    missed = [c for c in IN_SET if ("calls", c) not in Counter((p.target.split("/")[0], company_of(p))
                                                               for p in landed if p not in onto)]
    if missed:
        print(f"  trial {n} MISSED: no new call landed for {', '.join(missed)}, in the working set")
    leaks = [p for p in landed if company_of(p) not in IN_SET]
    print(f"  trial {n} landed: {dict(by)}")
    print(f"  trial {n} waiting, filed this trial: {sorted(p.target for p in proposed)}")
    for p in leaks:
        print(f"  trial {n} LEAK {p.target} ({company_of(p)}): {p.reason[:160]}")
    return {"landed": len(landed), "leaks": len(leaks), "in_set": len(landed) - len(leaks), "spent": cost,
            "missed": len(missed)}


def spent(workspace: Cerebro) -> float:
    """What the workspace's spend log holds, every day and job."""
    return sum(float(row.data.get("usd") or 0) for row in workspace.store.rows(LOG))


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    trials = int(sys.argv[sys.argv.index("--trials") + 1]) if "--trials" in sys.argv else 3
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, template="gtm", daily_usd=3, domains=["ourco.example"], environ=os.environ)
        for path in sorted((root / "agents").rglob("*.md"), reverse=True):
            if path.relative_to(root / "agents").as_posix() not in KEPT:
                path.unlink()  # nothing is written: only the three curators run
        (root / "sources" / "local.yaml").write_text(LOCAL, encoding="utf-8")
        for rel, text in first_notes().items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        with Cerebro(root, trusted=True) as workspace:
            workspace.sf_run = refuse_sf
            first_run(workspace, choose=whole, say=print)
            accounts = {a.identity.title for a in workspace.store.query("accounts") if a.identity.status == "active"}
            print(f"working set accounts after the first run: {sorted(accounts)}")
            if not set(IN_SET) <= accounts:
                print("FAIL: the first run left a company out of the working set, so nothing is measured")
                return 1
            for rel, text in later_notes().items():
                (root / rel).write_text(text, encoding="utf-8")
            workspace.run_once(agents=False)  # the sources only: the new calls are held for their curators
            held = {company_of_text(h.id): h.id for h in held_for(workspace.store, "calls", sorted(workspace.connectors), ours=workspace.ours,
                                                                   limit=None).items}
            print(f"new calls held for the calls curator, by company: {dict(sorted(held.items()))}")
            for company in IN_SET:  # a call of an account in the working set must reach the curator, and land
                if company not in held:
                    print(f"  not held: the new call of {company}, whose account is in the working set, so no "
                          "curator weighs it")
            since = stamp(workspace.clock())
            setup = spent(workspace)
            for company, domain in PROPOSED.items():  # proposed by the accounts curator, undecided by the person
                slug = company.lower().replace(" ", "-")
                propose(workspace.store, Proposal(
                    id=f"p_check{slug[:6]}", kind="article.admit", target=f"accounts/{slug}",
                    by=Caller("accounts/curator", "agent"), at=since, reason=f"{company}: calls both ways this month",
                    payload={"name": company, "keys": {"domain": [domain]}}))
        print(f"setup spent ${setup:.4f}")
        kept = Path(folder) / "kept"
        shutil.copytree(root, kept)
        results = []
        for n in range(1, trials + 1):
            shutil.rmtree(root)
            shutil.copytree(kept, root)
            results.append(trial(root, n, since))
        landed = sum(r["landed"] for r in results)
        leaks = sum(r["leaks"] for r in results)
        total = sum(r["spent"] for r in results)
        rate = leaks / landed if landed else 0.0
        chances = trials * 2 * len(PROPOSED | NOTHING)
        print(f"\n{trials} trials: {landed} admits landed, "
              f"{sum(r['in_set'] for r in results)} for companies in the working set and {leaks} outside it, "
              f"{leaks} of {chances} chances to leak ({leaks / chances:.0%}), {rate:.0%} of what landed; trials spent "
              f"${total:.4f}, setup ${setup:.4f}, total ${total + setup:.4f}; the new calls of the {len(IN_SET)} "
              f"companies in the working set missed {sum(r['missed'] for r in results)} of {trials * len(IN_SET)} "
              "times")
    return 0


if __name__ == "__main__":
    from spend import watch

    watch()
    sys.exit(main())
