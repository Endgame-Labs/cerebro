"""One live run of the gtm template on real models: `cerebro init --template gtm` in a temporary folder with a daily
cap of $3, seven short synthetic notes about three invented companies and their people under `local/`, then a first
run whose lists it approves, then passes until one runs nothing. On a list it plays a person who takes each line the
model ticked whole, with everything under it, since the questions ask about everything the workspace knows of those
accounts. Not part of the test suite: it
calls real models and costs some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_gtm.py

It prints what each pass did, every article as a
person reads it, what was refused, skipped or failed, what it cost and how long it took, then PASS, or FAIL with every
reason, and who decided each admit that landed and how many wait. It never prints a key. It passes only when the
first run built what its approved list holds, no part of a pass failed, a source that needs the Salesforce CLI among
them, at least one account article and one person article exist, every
sentence of each cites a fact or links an article the workspace holds, but a line saying the evidence holds none, and a
settled pass runs nothing. Its notes source routes `local/calls/` and `local/meetings/` into calls, and
one more call note lands after the second pass, once the accounts are settled, so it is held: the calls curator reads
it on its held list and proposes its account to the accounts curator. With `--keep <folder>` the workspace is made there
and left, to read it after. With `--see-first <words>` the first run is given the person's words on what to see
first. At its end it asks the wiki the questions in `gtm_questions.yaml` and prints their
score, and fails under `--min-score`, 0.85 by default. It fails too unless Brightwater's or Kestrel's article,
whose notes name their people, has a Stakeholders part, and when any account's Stakeholders part holds a fence: a
reader is owed sentences there, not data. With `--decision <provider>:<model>`, such as
`typesafe:jev-1.13.0`, the workspace decides and grounds on that model in place of the default. The questions are judged
by Rune whatever the workspace decides on, so two runs compare. With `--summary <file>` it writes what a
run is compared by as JSON: each article, its sentences per part and the facts they cite, by text, what grounding
dropped and why, the question scores, and the cost.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from _first_run import FIRST, placed, taken_whole
from _no_sf import refuse_sf
from _paths import paths, ran_lines
from spend import spent_line, table

from cerebro import evals, rendered, scaffold
from cerebro.app import Cerebro
from cerebro.article import cut, passages
from cerebro.budget import LOG
from cerebro.evidence import CITE
from cerebro.first_run import run as first_run
from cerebro.first_run import see_first
from cerebro.ground import ABSENCE, link_id, split_sentences
from cerebro.models import HttpDecider, ModelSpec
from cerebro.records import proposals
from cerebro.runner import PassResult

MAX_PASSES = 8
LOCAL = """kind: files
window: { behind: all }
select:
  - glob: "local/calls/**/*.md"
    extract: facts
    into: calls
  - glob: "local/meetings/**/*.md"
    extract: facts
    into: calls
    fields: { status: past }
  - glob: "local/email/**/*.md"
    extract: facts
"""
LATE = ("local/calls/2026-11-18-tidewater-intro.md", """Account: Tidewater Freight
Attendees: Lina Brooks <lina@ourco.example>
Date: 2026-11-18

Ravi Menon, head of finance operations at Tidewater Freight, took an intro call with Lina Brooks from our side.
Ravi said Tidewater matches about 4,000 carrier invoices a month by hand.
Lina Brooks will send Ravi a demo recording by 25 November.
""")
MIN_SCORE = 0.85  # the question score a healthy run clears with room
QUESTIONS = Path(__file__).resolve().parent / "gtm_questions.yaml"
NOTES = {
    "local/calls/2026-11-03-brightwater-discovery.md": """Account: Brightwater Logistics
Attendees: Nessa Torvik <nessa@brightwater.example>, Sam Okoye <sam@ourco.example>
Date: 2026-11-03

Nessa Torvik, VP of Operations at Brightwater Logistics, said their warehouse team loses about six hours a week
reconciling shipment exceptions by hand.
Nessa wants a pilot in the Reno warehouse before the end of January.
Sam Okoye from our side agreed to send a pilot plan by 10 November.
""",
    "local/email/2026-11-10-brightwater-pilot-plan.md": """Account: Brightwater Logistics
Attendees: Nessa Torvik <nessa@brightwater.example>, Sam Okoye <sam@ourco.example>
Date: 2026-11-10

Sam Okoye sent Nessa Torvik the pilot plan for the Reno warehouse, priced at $18,000 for eight weeks.
Nessa Torvik replied that the plan looks right and that procurement needs a security questionnaire first.
""",
    "local/calls/2026-11-14-brightwater-security.md": """Account: Brightwater Logistics
Attendees: Nessa Torvik <nessa@brightwater.example>, Priya Nair <priya@brightwater.example>, Sam Okoye <sam@ourco.example>
Date: 2026-11-14

Priya Nair, Brightwater's IT security lead, joined the call and asked for SSO and a data retention policy.
Sam Okoye said the security questionnaire will be back to Priya by 21 November.
Nessa Torvik said the pilot can start on 1 December if security signs off.
""",
    "local/calls/2026-11-05-kestrel-intro.md": """Account: Kestrel Foods
Attendees: Omar Haddad <omar@kestrel.example>, Lina Brooks <lina@ourco.example>
Date: 2026-11-05

Omar Haddad, CFO of Kestrel Foods, said they are comparing us with one other vendor for invoice matching.
Omar's budget for the project is $40,000 this fiscal year.
Lina Brooks from our side will send a reference customer by 12 November.
""",
    "local/email/2026-11-12-kestrel-reference.md": """Account: Kestrel Foods
Attendees: Omar Haddad <omar@kestrel.example>, Lina Brooks <lina@ourco.example>
Date: 2026-11-12

Lina Brooks introduced Omar Haddad to a reference customer at a regional grocery chain.
Omar Haddad said Kestrel will decide between the two vendors by 15 December.
Omar Haddad said the final call on the vendor is his, and he signs the contract himself.
""",
    "local/meetings/2026-11-07-novaline-kickoff.md": """Account: Novaline Health
Attendees: Grace Liu <grace@novaline.example>, Sam Okoye <sam@ourco.example>
Date: 2026-11-07

Grace Liu, director of IT at Novaline Health, kicked off the rollout to their three clinics.
The first clinic, in Tacoma, goes live on 2 December.
Grace Liu asked for weekly status calls on Tuesdays.
""",
    "local/calls/2026-11-17-novaline-status.md": """Account: Novaline Health
Attendees: Grace Liu <grace@novaline.example>, Sam Okoye <sam@ourco.example>
Date: 2026-11-17

Grace Liu said the Tacoma clinic's staff training is done.
Grace Liu raised that the second clinic's network upgrade slips to January, which moves its go-live.
""",
}


def show(n: int | str, result: PassResult, seconds: float, reasons: list[str]) -> None:
    print(f"pass {n} ({seconds:.0f}s): listed {result.listed}, {result.read} records read, {result.extracted} facts, "
          f"{result.linked} linked, compiled {result.compiled}, {len(result.runs)} runs")
    for run in result.runs:
        print(f"  run {run.agent} on {run.target}: {run.status}{': ' + run.note if run.note else ''}, wrote "
              f"{([h for h, _ in cut(run.result.text)] if run.result else [])}, {spent_line(run)}")
        if line := paths(run):  # a reply taken, nothing handed in, a floor met or a call refused for room
            print(f"  {line}")
        if run.agent.endswith("/curator"):  # what a curator decided, as a person reads it: each call, then its last line
            for entry in run.transcript:
                if entry.role == "call" and entry.tool != "fetch":
                    print(f"    {entry.tool} {json.dumps(entry.args)[:240]}")
                elif entry.role == "tool" and entry.tool != "fetch":
                    print(f"      -> {entry.text[:200]}")
            last = [e.text for e in run.transcript if e.role == "model" and e.text.strip()]
            if last:
                print(f"    last: {last[-1].strip()[:400]}")
    for line in ran_lines(result):
        print(line)
    for key, why in result.failed.items():
        print(f"  failed  {key}: {why}")
        reasons.append(f"pass {n}: {key} failed: {why}")
    for key, why in result.held.items():  # held for room under the cap: it runs on the next pass
        print(f"  held    {key}: {why}")
    for line in result.notices:
        if "with no model" in line:
            print(f"  notice  {line}")
    for line in result.warnings:
        print(f"  warning {line[:300]}")


def sentences(text: str) -> list[str]:
    out = []
    lines = [line.strip() for line in text.splitlines()]
    fence = False
    for n, line in enumerate(lines):
        if line.startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        if not line or line.startswith(("#", ">", "_")) or re.fullmatch(r"\|[-:| ]+\|", line):
            continue
        if line.startswith("|"):  # a table row is one claim, but for its header, which names the columns
            if not (n + 1 < len(lines) and re.fullmatch(r"\|[-:| ]+\|", lines[n + 1])):
                out.append(line)
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"([.!?])((?:\s*\[\^[^\]\s]+\])+)", r"\2\1", line)  # a citation after the stop is the sentence's
        out += [s.text for s in split_sentences(line)]
    return out


def read_articles(workspace: Cerebro, reasons: list[str]) -> dict[str, list[str]]:
    """Each collection's articles with content, printed; every sentence checked for a citation the workspace holds."""
    held = {f.id for f in workspace.extraction.facts()}  # a fact the workspace holds, or an article it keeps, as a brief cites
    held |= {a.identity.id for c in workspace.store.collections() for a in workspace.store.query(c)}
    written: dict[str, list[str]] = {}
    opens = {a.identity.id for c in workspace.store.collections() for a in workspace.store.query(c)
             if workspace.store.article(a.identity.id).compilations}  # an article a link may open: one that has been written
    opens |= {f.record.removesuffix(".md") for f in workspace.extraction.facts()}
    for collection in workspace.store.collections():
        for article in workspace.store.query(collection):
            full = workspace.store.article(article.identity.id)
            print(f"\n=== {full.identity.id} ({full.identity.title}, {full.identity.status}): "
                  f"{len(full.compilations)} parts, {len(workspace.store.facts_of(full.identity.id))} facts linked")
            for part in passages(full):
                print(f"## {part.heading}\n{part.text}")
                if part.by.startswith(rendered.MARK):
                    continue  # written by code from what the store holds: it cites nothing, and is never grounded
                for path in re.findall(r"(?<!!)\[[^\]^][^\]]*\]\(([^)\s#]+)", part.text):
                    if not re.match(r"^[a-z][a-z0-9+.-]*:", path, re.IGNORECASE) and not path.endswith("/") and \
                            link_id(path) not in opens:
                        reasons.append(f"{full.identity.id}: {part.heading}: links {path}, which no article that "
                                       "has been written and no record is")
                for sentence in sentences(part.text):
                    cited = CITE.findall(sentence) + [link.removeprefix("../").removesuffix(".md") for link in
                                                      re.findall(r"\]\((\.\./[^)\s]+\.md)\)", sentence)]
                    if not cited and not ABSENCE.search(sentence):  # a line saying the evidence holds none stands
                        reasons.append(f"{full.identity.id}: {part.heading}: cites nothing: {sentence[:120]!r}")
                    reasons += [f"{full.identity.id}: [^{id}] is no fact or article the wiki holds" for id in cited
                                if id not in held]
            if full.compilations:
                written.setdefault(collection, []).append(full.identity.id)
    return written


def stakeholders(workspace: Cerebro, reasons: list[str]) -> None:
    """Brightwater's or Kestrel's article has a Stakeholders part, and no account's Stakeholders part holds a fence."""
    landed = []
    for found in workspace.store.query("accounts"):
        article = workspace.store.article(found.identity.id)
        for part in passages(article):
            if part.heading == "Stakeholders" and "```" in part.text:
                reasons.append(f"{article.identity.id}: the Stakeholders part holds a fence, which a reader sees as data")
            elif part.heading == "Stakeholders":
                landed.append(article.identity.title)
    if not any(name in title for title in landed for name in ("Brightwater", "Kestrel")):
        reasons.append("no account whose notes name its people has a Stakeholders part")


OWN_DOMAIN = "ourco.example"  # the workspace's own domain, as the check makes the workspace


def links(workspace: Cerebro, notes: dict[str, str]) -> dict[str, str]:
    """How many articles that the notes tie to an admitted account hold that tie in their linked list, as `k of n`:
    a person on a note's Attendees: line at an outside address, and a call whose facts come from a note with an Account:
    line. Then the most briefs that link one account or one deal, and each thing with more than one. A measure, never
    a failure."""
    def working(collection: str) -> list:
        return [a for a in workspace.store.query(collection) if a.identity.status == "active"]

    def linked(article) -> set[str]:
        prop = article.properties.get("linked")
        values = (prop.value if isinstance(prop.value, list) else [prop.value]) if prop is not None else []
        return {str(v).strip().lower() for v in values}

    account = {_folded(a.identity.title): a.identity.id for a in working("accounts")}
    of_note = {rel: account[_folded(name)] for rel, name in accounts_of(notes).items() if _folded(name) in account}
    at = {_folded(name): of_note[rel] for rel, text in notes.items() if rel in of_note
          for name, address in re.findall(r"([^,<>\n:]+?)\s*<([^>\s]+)>", text)
          if not address.lower().endswith("@" + OWN_DOMAIN)}
    people = [(p, at[_folded(p.identity.title)]) for p in working("people") if _folded(p.identity.title) in at]
    records = {f.id: f.record for f in workspace.extraction.facts()}
    calls = [(c, found[0]) for c in working("calls") if (found := sorted({of_note[records[f]] for f in
             workspace.store.facts_of(c.identity.id) if records.get(f) in of_note}))]
    out = {name: f"{sum(id.lower() in linked(a) for a, id in pairs)} of {len(pairs)}"
           for name, pairs in (("people", people), ("calls", calls))}
    print("links: " + ", ".join(f"{k} {v}" for k, v in out.items()))
    per = {c: Counter(id for b in working(c) for id in linked(b) if id.startswith(prefix))
           for c, prefix in (("risk-radar", "accounts/"), ("open-pipeline", "opportunities/"))}
    print("briefs per thing: " + ", ".join(f"{c} max {max(n.values(), default=0)}" for c, n in per.items()))
    for c, n in per.items():
        for id, count in sorted(n.items()):
            if count > 1:
                print(f"  {c}: {count} briefs link {id}")
    return out


def _folded(name: str) -> str:
    return " ".join(name.casefold().split())


def own_people(notes: dict[str, str]) -> set[str]:
    """The workspace's own people, as its setup makes them: each attendee of the notes at its own domain."""
    return {" ".join(name.split()) for text in notes.values()
            for name, address in re.findall(r"([^,<>\n:]+?)\s*<([^>\s]+)>", text)
            if address.lower().endswith("@" + OWN_DOMAIN)}


def accounts_of(notes: dict[str, str]) -> dict[str, str]:
    """Each note's record id, as the files source gives it, and the account its `Account:` line keys it to."""
    return {rel: found.group(1).strip() for rel, text in notes.items()
            if (found := re.search(r"(?m)^Account:\s*(.+)$", text))}


def own_side_articles(workspace: Cerebro, own: set[str], prefix: list[str], reasons: list[str]) -> None:
    """Fail in one line each article, in any collection, for one of the workspace's own people, or keyed by an
    address at its own domain: they get no article. `prefix` heads each line, such as the run it is of."""
    mine = {_folded(name) for name in own}
    for collection in workspace.store.collections():
        for article in workspace.store.query(collection):
            title = article.identity.title
            keyed = [v for vs in article.identity.keys.values() for v in vs if str(v).lower().endswith("@" + OWN_DOMAIN)]
            if _folded(title) in mine or keyed:
                said = f"{article.identity.id}: an article for {title}, who is on the workspace's own side"
                reasons.append(" ".join([*prefix, said]))


def borrowed_facts(workspace: Cerebro, accounts: dict[str, str], reasons: list[str]) -> None:
    """Fail an account's article that links a fact of a note keyed to another account, or more than half of the
    workspace's facts: an account holds its own facts, never the workspace's."""
    facts = {f.id: f for f in workspace.extraction.facts()}
    for article in workspace.store.query("accounts"):
        id, title = article.identity.id, article.identity.title
        linked = workspace.store.facts_of(id)
        if len(facts) >= 4 and len(linked) * 2 > len(facts):
            reasons.append(f"{id}: links {len(linked)} of the workspace's {len(facts)} facts, more than half")
        others = sorted({account for fact in linked if (f := facts.get(fact)) is not None
                         and (account := accounts.get(f.record, "")) and _folded(account) != _folded(title)})
        if others:
            reasons.append(f"{id}: links facts of a note keyed to {' and '.join(others)}")


def summary(workspace: Cerebro, dropped: list[str], report: evals.Report) -> dict:
    """What one run is compared by: each article with content, its sentences per part and the facts they cite,
    by text so two runs compare, what grounding dropped, the question scores, and what the run spent."""
    texts = {f.id: f.text for f in workspace.extraction.facts()}
    articles = {}
    for collection in workspace.store.collections():
        for found in workspace.store.query(collection):
            article = workspace.store.article(found.identity.id)
            if not article.compilations or article.identity.status != "active":
                continue  # an archived article is out of the working set a reader asks
            articles[article.identity.id] = {
                "parts": {s.heading: len(sentences(s.text)) for s in passages(article)},
                "cites": sorted({texts.get(id, id) for s in passages(article) for id in CITE.findall(s.text)}),
                "linked": len(workspace.store.facts_of(article.identity.id))}
    now = workspace.clock()
    return {"articles": articles, "facts": sorted(texts.values()), "dropped": dropped,
            "score": report.score, "questions": {s.id: s.score for s in report.scored},
            "spent": round(workspace.ledger.spent(now), 4),
            "spent_evaluation": round(workspace.ledger.spent(now, "evaluation"), 4)}


def handins(workspace: Cerebro, runs: list) -> dict:
    """How the runs that write handed in their text: by status, the turns they took, how often the model was told its
    text cited nothing or that text outside a hand-in is not kept, how much text stood before the first heading, and how
    often a run with headings under # Output used each of them, in their order, and the tool calls the runs refused,
    as each run's row in the agent-runs log counts them, by tool."""
    by_id = {a.id: a for a in workspace.agents}
    writers = [r for r in runs if by_id.get(r.agent) is not None and by_id[r.agent].role != "curator"]
    said = [e.text for r in writers for e in r.transcript]
    with_output = [(by_id[r.agent], r) for r in writers if r.result and by_id[r.agent].output
                   and by_id[r.agent].role != "subagent"]
    ids = {r.id for r in runs}  # each run's first row in the agent-runs log, which counts the calls it refused
    logged: dict[str, dict] = {}
    for row in workspace.store.rows("agent-runs"):
        if row.data.get("run") in ids:
            logged.setdefault(row.data["run"], row.data)
    return {
        "runs": len(writers), "statuses": {s: sum(r.status == s for r in writers) for s in {r.status for r in writers}},
        "turns": round(sum(r.usage.requests for r in writers) / max(len(writers), 1), 2),
        "told_uncited": sum("Your text cites nothing" in t for t in said),
        "told_text_not_kept": sum("Your reply handed in nothing" in t for t in said),  # asked once to hand in
        "tool_calls_refused": sum(row.get("refused", 0) for row in logged.values()),
        "refused_by_tool": dict(Counter(tool for row in logged.values() for tool in row.get("refused_tools", []))),
        "text_before_first_heading": sum(1 for _, r in with_output if cut(r.result.text)[0][0] == ""),
        "output_headings_in_order": sum(1 for a, r in with_output
                                        if [h for h, _ in cut(r.result.text) if h in a.output] == a.output),
        "with_output": len(with_output),
        "by_agent_with_subagents": {a.id: sum(r.agent == a.id for r in writers) for a in workspace.agents if a.subagents},
    }


def settled_pass(result: PassResult) -> bool:
    """Whether a pass leaves nothing to do: it ran, read and compiled nothing, held no run, which waits, and listed no
    record, which a curator weighs on the next pass when it is held for its collection."""
    return not (result.runs or result.read or result.compiled or result.held or any(result.listed.values()))


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    keep = sys.argv[sys.argv.index("--keep") + 1] if "--keep" in sys.argv else ""
    floor = float(sys.argv[sys.argv.index("--min-score") + 1]) if "--min-score" in sys.argv else MIN_SCORE
    reasons: list[str] = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(keep) if keep else Path(folder) / "workspace"
        scaffold.init(root, template="gtm", daily_usd=3, domains=["ourco.example"], environ=os.environ)
        if "--decision" in sys.argv:  # another decision model, as provider:model, such as typesafe:jev-1.13.0
            provider, _, model = sys.argv[sys.argv.index("--decision") + 1].partition(":")
            settings = root / "settings.yaml"
            settings.write_text(re.sub(r"(?m)^  decision: .*$", f"  decision: {{ provider: {provider}, model: {model} }}",
                                       settings.read_text(encoding="utf-8")), encoding="utf-8")
        (root / "sources" / "local.yaml").write_text(LOCAL, encoding="utf-8")
        for rel, text in NOTES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        with Cerebro(root, trusted=True) as workspace:
            workspace.sf_run = refuse_sf
            print(f"agents: {len(workspace.agents)} loaded, refused {workspace.refused}")
            for line in workspace.notices:
                print(f"  notice {line}")
            settled = False
            runs = []
            dropped: list[str] = []
            waited: list[str] = []
            began = time.monotonic()
            if "--see-first" in sys.argv:  # the person's words on what to see first, as the run asks at a terminal
                see_first(workspace, sys.argv[sys.argv.index("--see-first") + 1])
            out = first_run(workspace, choose=taken_whole, say=print)
            where = placed(FIRST["pile"], NOTES, OWN_DOMAIN) if "pile" in FIRST else "no list was shown"
            print(f"placed    {where}")
            if FIRST.get("selected") == 0:
                reasons.append(f"the first run's list ticked nothing, so nothing was admitted: {where}")
            if out is None or out.result is None or out.waits or out.quit:
                reasons.append("the first run did not build what its approved list holds")
            else:
                show("first run", out.result, time.monotonic() - began, reasons)
                runs += out.result.runs
            for line in out.lines if out is not None else []:
                print(f"  {line}")
            for n in range(1, MAX_PASSES + 1):
                began = time.monotonic()
                result = workspace.run_once()
                show(n, result, time.monotonic() - began, reasons)
                runs += result.runs
                waited += [f"pass {n}: {k}: {why}" for k, why in [*result.skipped.items(), *(
                    line.split(": ", 1) for line in result.warnings if ": " in line)] if "so it waits until" in why]
                dropped += [f"pass {n}: {line}" for line in result.warnings if ": dropped " in line or
                            ": removed the lead" in line]
                if n == 1:
                    for agent, why in sorted(result.skipped.items()):
                        print(f"  skipped {agent}: {why}")
                if n == 2:  # a call that lands once the accounts are settled, so it is held for the calls curator
                    (root / LATE[0]).write_text(LATE[1], encoding="utf-8")
                    continue
                if settled_pass(result):
                    settled = True
                    break
            if not settled:
                reasons.append(f"no pass in {MAX_PASSES} ran nothing")
            admits = [p for p in proposals(workspace.store) if p.kind == "article.admit"]
            by = Counter(f"{p.decided_by.kind} {p.decided_by.name}" for p in admits
                         if p.status == "accepted" and p.decided_by is not None)
            print(f"admits: accepted by {dict(by)}, {sum(p.status == 'waiting' for p in admits)} waiting")
            written = read_articles(workspace, reasons)
            stakeholders(workspace, reasons)
            links(workspace, NOTES | dict([LATE]))
            notes = NOTES | dict([LATE])
            own_side_articles(workspace, own_people(notes), [], reasons)
            borrowed_facts(workspace, accounts_of(notes), reasons)
            print(f"\nwritten: { {c: len(ids) for c, ids in written.items()} }")
            for collection in ("accounts", "people"):
                if not written.get(collection):
                    reasons.append(f"no {collection} article was written")
            print("\n" + table({a.id: a.role for a in workspace.agents}, runs))
            judge = ModelSpec("fireworks", "rune", job="decision")  # one judge of the questions, whatever the workspace decides on
            report = evals.run(workspace, evals.load(QUESTIONS), decider=HttpDecider(judge, key=workspace.model_key(judge)),
                               model=judge, ledger=workspace.ledger, cap_usd=workspace.ledger.caps.evaluation, clock=workspace.clock)
            print()
            for line in evals.lines(report):
                print(line)
            if report.score is None or report.score < floor:
                reasons.append(f"the questions scored {report.score}, under the floor of {floor:g}")
            now = workspace.clock()
            judged = workspace.store.rows(LOG, {"run": "keep judge"})
            counted = handins(workspace, runs) | {"waited_after_a_failed_run": waited}
            print(f"hand-ins: {json.dumps(counted)}")
            print(f"keep judge: {len(judged)} calls, ${sum(float(r.data.get('usd', 0)) for r in judged):.4f}")
            print(f"spent: ${workspace.ledger.spent(now):.4f} (extraction ${workspace.ledger.spent(now, 'extraction'):.4f}, "
                  f"compilation ${workspace.ledger.spent(now, 'compilation'):.4f}, evaluation "
                  f"${workspace.ledger.spent(now, 'evaluation'):.4f}), {time.monotonic() - started:.0f}s")
            if "--summary" in sys.argv:
                Path(sys.argv[sys.argv.index("--summary") + 1]).write_text(json.dumps(
                    summary(workspace, dropped, report) | {"handins": counted}, indent=1), encoding="utf-8")
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
