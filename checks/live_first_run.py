"""One live check of a workspace's first run on real models: the gtm template in a temporary folder with a daily cap
of $3 and a few made-up notes, run twice, once approving what the list ticks, as `--yes` does, and once with two of
its top-level lines unticked and one candidate the curators left out ticked. Not part of the test suite: it calls
real models and costs some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_first_run.py
    uv run python checks/live_first_run.py --fake

`--fake` runs the same steps and the same checks on the
stand-ins, a small workspace with one scripted curator that also leaves one row out, one writer, a calls curator whose
one call sits under an account, and one curator that proposes only once facts exist, as a smoke test of the check
itself. It prints each run's discovery lines, the list as `--plain` shows it, how many people and calls sit under the
right account, what was decided and written, then the late round's list, if one came, and what it spent, then PASS,
or FAIL with every reason, or NOTHING TO CHECK with each part that had nothing in it, which exits 0. It never prints a
key. It passes only when, on each run:
- before the decision, no article exists, no writer ran, every admit the curators filed waits, and every dollar
  spent was a curator's or the call that arranged the list;
- the list ticks something: a list that ticks nothing fails in one line, which says where people and calls sit;
- after the decision, each ticked row reads accepted, or withdrawn when its article was there already, and the
  curators carry it out as the build runs: its article exists once they have run, and at least one was written;
- on the second run, what the two unticked lines held waits, undecided, and the first left-out candidate the list
  offers as a tick is ticked, as a person admits one a curator left out: it is admitted, filed with its curator's
  reason. It ticks the first candidate not of the workspace's own side, so an article for the own side is a
  curator's;
- no article exists for someone on the workspace's own side, each such article a line of its own;
- no source failed, and no pass asked to start the Salesforce CLI, which no workspace here reads through.
It also reports, as counts that pass or fail nothing: the list's rows per collection, own-side rows apart; each
curator's opening in tokens, its largest; and, after one more pass, for each curator's proposal on another collection
that was made under another path, how many of its links moved to the made path and how many still name the one it
proposed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from _first_run import placed, whole
from _no_sf import refuse_sf
from _paths import Watching, paths, ran_lines

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.caller import Caller
from cerebro.first_run import Choices, lines, run
from cerebro.records import proposals
from cerebro.run import opening, tokens
from cerebro.tree import PROMPT

YOU = Caller("you", "person")
DECLINED = 2  # top-level lines the second run leaves unticked


def gtm(root: Path) -> Cerebro:
    """A gtm workspace with live_gtm's notes source and notes, on real models."""
    from live_gtm import LOCAL, NOTES

    scaffold.init(root, template="gtm", daily_usd=3, domains=["ourco.example"], environ=os.environ)
    (root / "sources" / "local.yaml").write_text(LOCAL, encoding="utf-8")
    for rel, text in NOTES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    workspace = Cerebro(root, trusted=True)
    workspace.sf_run = refuse_sf
    return workspace


def stand_in(root: Path) -> Cerebro:
    """A small workspace on the stand-ins: one curator that admits four made-up accounts and leaves one out, one
    writer that cites every fact it reads, and a topics curator that reads facts, so it has nothing to read at
    discovery and proposes one topic in the late round."""
    import re
    from datetime import UTC, datetime, timedelta

    from cerebro.clock import stamp
    from cerebro.connector import Record
    from cerebro.fakes import FakeConnector, FakeExtraction, FakeRewriter, ScriptedRun, every_item_ticked
    from cerebro.run import AgentRun

    names = [("Brightwater Logistics", "brightwater.example"), ("Kestrel Foods", "kestrel.example"),
             ("Tidewater Freight", "tidewater.example"), ("Larkspur Media", "larkspur.example"),
             ("Quillon Supply", "quillon.example")]
    now = datetime.now(UTC)  # the records of the last days, which a first run reads
    records = [Record(id=f"r{n}", source="crm", revision="1", at=stamp(now - timedelta(days=7)),
                      keys={"domain": [domain], "name": [name]}, fields={"name": name, "subject": name},
                      body=f"{name} asked for a pilot plan.", into="accounts") for n, (name, domain) in enumerate(names)]
    calls = [("admit", {"record": {"source": "crm", "id": f"r{n}"}, "reason": f"Two calls with {name}.",
                        "group": "you own (crm)"}) for n, (name, _) in enumerate(names[:4])]
    calls.append(("leave_out", {"reason": "vendor", "record": {"source": "crm", "id": "r4"}}))
    records.append(Record(id="c1", source="crm", revision="1", at=stamp(now - timedelta(days=3)),
                          keys={"account": ["Brightwater Logistics"]}, fields={"subject": "Brightwater security call"},
                          body="Brightwater Logistics asked for SSO.", into="calls"))
    call = [("admit", {"record": {"source": "crm", "id": "c1"}, "name": "Brightwater security call",
                       "reason": "a call in your notes", "group": "calls in your notes",
                       "fields": {"linked": ["accounts/brightwater-logistics"]}})]

    class Scripted(AgentRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            def cite(prompt: str) -> dict[str, str]:
                facts = re.findall(r"\[\^(fact_\w+)\] ([^(\n]+)", prompt)
                return {"Summary": " ".join(f"{text.strip()} [^{id}]" for id, text in facts) or "Nothing yet."}

            rows = {p.id: p for p in proposals(workspace.store)}
            listed = [rows.get(i.key.removeprefix("proposal:")) for entry in evidence for i in entry.items
                      if i.key.startswith("proposal:")]
            yes = [p for p in listed if p is not None and p.status == "accepted"]
            if yes:  # the build: each accepted row carried out at its name, as the curator's list shows it
                script = [("admit", {"name": p.payload.get("name") or p.target.rpartition("/")[2],
                                     "reason": "Approved.", "proposals": [p.id]}) for p in yes]
            elif agent.id == "calls/curator":  # a call whose header line names its account: it sits under that row
                script = list(call)
            elif agent.id == "topics/curator":  # nothing to read before the first facts exist
                found = any("fact_" in line for entry in evidence for line in entry.lines)
                script = [("admit", {"name": "Pilot plans", "reason": "Pilots asked for by four accounts.",
                                     "group": "named in your facts"}),
                          # a proposal of an account: in the working set on the first run, declined on the second
                          ("admit", {"name": "Kestrel Foods", "collection": "accounts",
                                     "keys": {"domain": ["kestrel.example"]},
                                     "reason": "Kestrel Foods asked for a pilot plan."})] if found else []
            else:
                script = list(calls) if agent.role == "curator" else []
            done = ScriptedRun(lambda p: {} if agent.role == "curator" else cite(p), script).run(
                agent, evidence, tools, budget)
            return replace(done, id=f"run_{uuid.uuid4().hex[:8]}")

    (root / "agents" / "accounts").mkdir(parents=True)
    (root / "agents" / "accounts" / "curator.md").write_text(
        "---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - held: {}\nheartbeat: [daily]\n---\n\nKeep the accounts.\n")
    (root / "agents" / "accounts" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: crm }\n---\n\nWrite the account.\n\n# Output\n\n## Summary\n")
    (root / "agents" / "calls").mkdir(parents=True)
    (root / "agents" / "calls" / "curator.md").write_text(
        "---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - held: {}\nheartbeat: [daily]\n---\n\nKeep the calls.\n")
    (root / "agents" / "topics").mkdir(parents=True)
    (root / "agents" / "topics" / "curator.md").write_text(
        "---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - query: { from: facts, source: crm }\nheartbeat: [daily]\n---\n\n"
        "Keep the topics the facts name.\n")
    (root / "settings.yaml").write_text("{}\n")
    workspace = Cerebro(root, agent_run=Scripted(), extraction=FakeExtraction(),
                        connectors=[FakeConnector("crm", records)], trusted=True)
    workspace.asker = FakeRewriter(lambda data: every_item_ticked(json.dumps(data)))  # the list's arrangement
    return workspace


class Openings:
    """An agent run that keeps each curator's largest opening, in tokens, as it starts."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.tokens = inner, dict[str, int]()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def run(self, agent: Any, evidence: Any, *args: Any, **kwargs: Any) -> Any:
        if agent.role == "curator":
            from datetime import UTC, datetime

            size = tokens(opening(evidence, datetime.now(UTC)))
            self.tokens[agent.id] = max(size, self.tokens.get(agent.id, 0))
        return self.inner.run(agent, evidence, *args, **kwargs)


def counts(workspace: Cerebro, pile: Any, openings: Openings, label: str) -> None:
    """The counts the check reports and judges nothing by: see the docstring above."""
    for listed in pile.lists:
        own = sum(bool(row.own) for row in listed.rows)
        print(f"rows      {label}: {listed.collection}: {len(listed.rows)} on the list, {own} of them own-side")
    for agent, size in sorted(openings.tokens.items()):
        print(f"opening   {label}: {agent}: {size} tokens at most")
    workspace.run_once()
    store = workspace.store
    for p in proposals(store, "accepted"):
        made, mine = p.applied.get("article"), p.by.name.partition("/")[0]
        if p.kind != "article.admit" or p.by.kind != "agent" or made in (None, p.target) or \
                p.target.partition("/")[0] == mine:
            continue
        stale = [id for id in store.linking_to("linked", p.target) if id.startswith(mine + "/")]
        moved = [id for id in store.linking_to("linked", made) if id.startswith(mine + "/")]
        print(f"made      {label}: {p.by.name} proposed {p.target}, made as {made}: {len(moved)} of its links name "
              f"{made}, {len(stale)} still name {p.target}")


def articles(workspace: Cerebro) -> list[str]:
    return sorted(a.identity.id for c in workspace.store.collections() for a in workspace.store.query(c))


def writers(workspace: Cerebro) -> list[str]:
    curators = {a.id for a in workspace.agents if a.role == "curator"}
    return [row.data["agent"] for row in workspace.store.rows("agent-runs") if row.data["agent"] not in curators]


def one(workspace: Cerebro, decline: int, reasons: list[str], label: str, empty: list[str],
        own: set[str], notes: dict[str, str] | None) -> Any:
    """One first run: discover, check that nothing was written, decide, then check what landed and what did not. It
    adds to `empty` each part it had nothing to check in, and gives the list it decided, or None."""
    seen: dict[str, Any] = {}

    def choose(pile: Any) -> Any:
        if pile.late:  # shown as the first list is, and approved as --yes approves it
            choices = whole(pile)
            print(f"decided   {label}: late list: {len(choices.accept)} ticked, as --yes decides")
            return choices
        print("\n".join(lines(pile)))
        seen["pile"] = pile
        seen["articles"], seen["writers"] = articles(workspace), writers(workspace)
        seen["waiting"] = [p.status for p in proposals(workspace.store) if p.kind == "article.admit"]
        curators = {row.data["run"] for row in workspace.store.rows("agent-runs")}
        seen["spent"] = {row.data["run"] for row in workspace.store.rows("spend") if row.data.get("usd")} - curators \
            - {PROMPT}  # the call that arranged the list
        where = placed(pile, notes, "ourco.example") if notes else ""
        if where:
            print(f"placed    {label}: {where}")
        ticked = [g for top in pile.groups for g in (top, *top.under) if g.tick]
        if not ticked:
            reasons.append(f"{label}: the first run's list ticked nothing" + (f": {where}" if where else ""))
        tops = [g for g in pile.groups if g.tick]
        seen["unticked"] = [g for top in tops[:decline] for g in (top, *top.under)]
        left = [g for g in pile.left() if not any(row.own for row in g.rows)]  # a caller may tick the own side too
        seen["left"] = left[0] if decline and left else None
        if decline and not left:
            empty.append(f"{label}: the list left no row out, so no left-out row was ticked")
        taken = [g for g in ticked if g not in seen["unticked"]] + ([seen["left"]] if seen["left"] else [])
        print(f"chosen    {label}: {len(taken)} ticked, {len(seen['unticked'])} unticked under {decline} lines")
        return Choices.of(pile, taken)

    out = run(workspace, choose=choose, say=print)
    for line in (ran_lines(out.result) if out is not None and out.result is not None else []):
        print(line)
    for key, why in (out.result.failed.items() if out is not None and out.result is not None else []):
        if key.startswith("sources/"):
            reasons.append(f"{label}: {key} failed: {why}")
    if out is None or "pile" not in seen:
        reasons.append(f"{label}: the workspace was not in its first run, or its list waited")
        return None
    if seen["articles"]:
        reasons.append(f"{label}: articles existed before the decision: {seen['articles']}")
    if seen["writers"]:
        reasons.append(f"{label}: a writer ran before the decision: {seen['writers']}")
    if any(status != "waiting" for status in seen["waiting"]):
        reasons.append(f"{label}: an admit landed before the decision: {seen['waiting']}")
    if seen["spent"]:
        reasons.append(f"{label}: something but a curator spent before the decision: {sorted(seen['spent'])}")
    after = {p.id: p for p in proposals(workspace.store)}
    now = articles(workspace)
    unticked = {row.id for g in seen["unticked"] for row in g.rows}
    for g in (g for top in seen["pile"].groups for g in (top, *top.under) if g.rows):
        for row in g.rows:
            p = after.get(row.id)
            if row.id in unticked and (p is None or p.status != "waiting") and not (p and p.target in now):
                reasons.append(f"{label}: {row.name}, unticked, is {p.status if p else 'gone'}, not waiting")
            elif g.tick and row.id not in unticked and (p is None or p.status not in ("accepted", "withdrawn")):
                reasons.append(f"{label}: {row.name}, ticked, is {p.status if p else 'gone'}")
            elif g.tick and row.id not in unticked and p.status == "accepted" and p.applied.get("article") not in now:
                reasons.append(f"{label}: {row.name}, ticked and accepted, has no article once the curators ran: "
                               f"applied {p.applied}")
    if (left := seen["left"]) is not None:
        filed = [p for p in after.values() if p.reason == f"left out: {left.lead.reason}" and p.status == "accepted"]
        if not filed or filed[0].target not in now:
            reasons.append(f"{label}: {left.name}, left out and ticked, was not admitted")
        else:
            print(f"admitted  {label}: {left.name}, left out by agents/{left.lead.agent}.md and ticked, as "
                  f"{filed[0].target}")
    if own:  # one line for each article someone on the workspace's own side holds, however it was admitted
        from live_gtm import own_side_articles

        own_side_articles(workspace, own, [f"{label}:"], reasons)
    written = out.result.compiled if out.result is not None else []
    if not written:
        reasons.append(f"{label}: nothing was written after the decision")
    print(f"decided   {label}: wrote {sorted(written)}")
    for line in [*out.lines, *out.after]:
        print(f"  {line}")
    print(f"spent     {label}: ${workspace.ledger.spent(workspace.clock()):.4f}")
    return seen["pile"]


def verdict(reasons: list[str], empty: list[str]) -> tuple[list[str], int]:
    """The lines the check ends with and its exit code: FAIL with every reason, exit 1; else NOTHING TO CHECK with
    each part that had nothing in it, exit 0, since nothing failed but those parts were not shown to hold; else PASS."""
    if reasons:
        return ["FAIL", *(f"- {reason}" for reason in reasons)], 1
    if empty:
        return [*(f"- {part}" for part in empty), "NOTHING TO CHECK"], 0
    return ["PASS"], 0


def own_people() -> set[str]:
    """The gtm workspace's own people: its notes' attendees at its own domain, as `live_gtm` reads them."""
    from live_gtm import NOTES
    from live_gtm import own_people as of

    return of(NOTES)


def notes() -> dict[str, str]:
    """The gtm workspace's notes, as `live_gtm` writes them."""
    from live_gtm import NOTES

    return NOTES


def main() -> int:
    fake = "--fake" in sys.argv
    if not fake and (not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY")):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY, or run "
              "with --fake on the stand-ins")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    reasons: list[str] = []
    empty: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        make = stand_in if fake else gtm
        for label, decline in (("approved whole", 0), ("two declined", DECLINED)):
            with make(Path(folder) / label.replace(" ", "-")) as workspace:
                workspace.agent_run = watching = Watching(openings := Openings(workspace.agent_run))
                pile = one(workspace, decline, reasons, label, empty, set() if fake else own_people(),
                           None if fake else notes())
                if pile is not None:
                    counts(workspace, pile, openings, label)
                for line in filter(None, map(paths, watching.runs)):  # a reply taken, nothing handed in, a floor met
                    print(f"{line.split(' ', 1)[0]:<9} {label}: {line.split(' ', 1)[1].strip()}")  # or a room refusal
    lines, code = verdict(reasons, empty)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    from spend import watch

    watch()
    sys.exit(main())
