"""One live check of a whole pass on real models: a blank workspace, two short notes with header lines, one agent file.
Not part of the test suite: it calls real models and costs a few cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_pass.py

The workspace is a temporary folder made as `cerebro init`
makes one, with its config folder beside it, so nothing is left behind and no call is replayed. It runs three passes:
the first on the new notes, a second with nothing changed, and a third after one note is edited. It prints what each
pass did, the facts and the article with how many sentences grounding marked, what the workspace spent, then PASS,
or FAIL with every reason. It never prints a key. It passes only when:
- each note gave more than one fact, and no fact quotes a header line;
- no fact's text holds `cerebro:`;
- the article has a part, and every citation in it is a fact the workspace holds;
- the second pass runs nothing;
- after the edit, the third pass rewrites the article.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from _paths import ran_lines
from spend import spent_line, table

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import MARK, cut, passages
from cerebro.evidence import CITE
from cerebro.ground import unmarked
from cerebro.runner import PassResult

NOTES = {
    "local/2026-11-12-launch-sync.md": (
        "Project: Launch\nDate: 2026-11-12\n\n"
        "Priya Raman said the Halcyon launch moves from 1 December to 8 December, because the payments review is not "
        "finished.\nTomas Weber owns the payments review and expects to sign it off by 3 December.\n"
        "The launch budget stays at $40,000.\n"),
    "local/2026-11-16-launch-risks.md": (
        "Project: Launch\nDate: 2026-11-16\n\n"
        "Tomas Weber said the payments review found two open issues with refunds, and one of them blocks sign-off.\n"
        "Priya Raman asked for a go or no-go call on 4 December.\n"
        "The press embargo lifts on 8 December at 9am Pacific.\n"),
}
EDIT = ("local/2026-11-16-launch-risks.md",
        "Tomas Weber said the payments review found two open issues with refunds, and one of them blocks sign-off.\n",
        "Tomas Weber said both refund issues are fixed, and the payments review was signed off on 17 November.\n")
AGENT = """---
evidence:
  - facts: { window: last_90_days }
heartbeat: [change]
---

# Instructions

Write where the Halcyon launch stands for someone joining the project today:
the date, what blocks it, who owns what, and what happens next. Every sentence
cites a fact. If facts disagree, say so and cite both. Leave out anything you
cannot cite.

# Output

## Where it stands

## What blocks it

## What happens next
"""
ARTICLE = "projects/launch"
HEADER = re.compile(r"^(Project|Date):", re.MULTILINE)


def show(n: int, result: PassResult) -> None:
    print(f"pass {n}: listed {result.listed}, {result.read} records read, {result.extracted} facts extracted, "
          f"{result.linked} linked, compiled {result.compiled}, {len(result.runs)} runs")
    for run in result.runs:
        print(f"  run {run.agent}: {run.status}{': ' + run.note if run.note else ''}, wrote {([h for h, _ in cut(run.result.text)] if run.result else [])}, "
              f"{spent_line(run)}")
        said = [e.text for e in run.transcript if e.role == "model" and e.text.strip()]
        if not run.result and said:
            print(f"  it said: {' '.join(said[-1].split())[:600]}")
        if not run.result and os.environ.get("LIVE_PASS_PROMPT"):
            print("  it was given:\n" + next((e.text for e in run.transcript if e.role == "user"), ""))
    for line in ran_lines(result):
        print(line)
    for key, why in {**result.failed}.items():
        print(f"  failed  {key}: {why}")
    for line in result.warnings:
        print(f"  warning {line}")


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    reasons: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # the workspace's secrets folder, outside the workspace
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        done = scaffold.init(root, daily_usd=1, environ=os.environ)
        print(f"init: {root.name}, missing keys {done.missing_keys or 'none'}")
        for rel, text in NOTES.items():
            (root / rel).write_text(text, encoding="utf-8")
        (root / "agents" / "projects").mkdir(parents=True)
        (root / "agents" / "projects" / "launch.md").write_text(AGENT, encoding="utf-8")
        with Cerebro(root, trusted=True) as workspace:
            first = workspace.run_once()
            show(1, first)
            facts = workspace.extraction.facts()
            print(f"facts: {len(facts)}")
            for fact in facts:
                print(f"- {fact.id} [{fact.record}] {fact.text}")
                for quote in fact.quotes:
                    print(f"    quote {quote.text!r}")
            for rel in NOTES:
                mine = [f for f in facts if f.record == rel]
                if len(mine) < 2:
                    reasons.append(f"{rel} gave {len(mine)} facts, not more than one")
            for fact in facts:
                if any(HEADER.search(q.text) for q in fact.quotes) or HEADER.search(fact.text):
                    reasons.append(f"{fact.id} is made from a header line: {fact.text!r}")
                if "cerebro:" in fact.text:
                    reasons.append(f"{fact.id} names an internal address: {fact.text!r}")
            held = {f.id for f in facts}
            article = workspace.store.article(ARTICLE)
            linked = workspace.store.facts_of(ARTICLE)
            print(f"linked to {ARTICLE}: {len(linked)} facts")
            if article is None or not article.compilations:
                reasons.append(f"{ARTICLE} has no part")
            for part in (passages(article) if article else []):
                print(f"## {part.heading}\n{part.text}\n")
                reasons += [f"{part.heading}: [^{id}] is no fact the wiki holds"
                            for id in CITE.findall(unmarked(part.text)) if id not in held]
            print(f"grounding marked {sum(s.text.count(MARK) for s in passages(article)) if article else 0} sentences")
            before = [(s.heading, s.text) for s in passages(article)] if article else []
            second = workspace.run_once()
            show(2, second)
            if second.runs or second.compiled or second.read or second.extracted:
                reasons.append(f"the second pass, with nothing changed, ran {len(second.runs)} runs, compiled "
                               f"{second.compiled} and read {second.read} records")
            path, old, new = EDIT
            note = root / path
            note.write_text(note.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
            third = workspace.run_once()
            show(3, third)
            after = workspace.store.article(ARTICLE)
            wrote = [run for run in third.runs if run.target == ARTICLE and run.result]
            if ARTICLE not in third.compiled or not wrote or after is None or \
                    [(s.heading, s.text) for s in passages(after)] == before:
                reasons.append(f"after {path} was edited, {ARTICLE} was not rewritten: its run wrote "
                               f"{[([h for h, _ in cut(run.result.text)] if run.result else []) for run in third.runs]}")
            for part in (passages(after) if after else []):
                print(f"## {part.heading}\n{part.text}\n")
            held = {f.id for f in workspace.extraction.facts()}
            for part in (passages(after) if after else []):
                reasons += [f"after the edit, {part.heading}: [^{id}] is no fact the wiki holds"
                            for id in CITE.findall(unmarked(part.text)) if id not in held]
            print(f"grounding marked {sum(s.text.count(MARK) for s in passages(after)) if after else 0} sentences")
            print("\n" + table({a.id: a.role for a in workspace.agents}, first.runs + second.runs + third.runs))
            now = workspace.clock()
            print(f"spent: ${workspace.ledger.spent(now):.4f} (extraction ${workspace.ledger.spent(now, 'extraction'):.4f}, "
                  f"compilation ${workspace.ledger.spent(now, 'compilation'):.4f})")
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
