"""Kept text: a case for each way a body or a part is kept, and for a fact a person or a source takes back.

A part is kept as it stands, less each line that cites only facts withdrawn since."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_compile import AGENT, BRIEF, JOINED, ByAgent, briefs, setup

from cerebro import Cerebro
from cerebro.agent import Budget
from cerebro.article import Citation, Identity
from cerebro.caller import Caller
from cerebro.compile import KEPT, _Grounding, compile_article
from cerebro.connector import Record
from cerebro.facts import Fact
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.run import AgentRun, Run
from cerebro.runner import _compiled_line

WROTE = "The pump in hall two failed on Monday [^r0]."


def _texts(store: Any, article: str) -> list[tuple[str, str]]:
    return [(s.heading, s.text) for s in store.article(article).compilations]


def _body(store: Any, article: str) -> str:
    return "\n\n".join(f"## {s.heading}\n\n{s.text}" for s in store.article(article).compilations)


# -- a kept body -----------------------------------------------------------------------------------------------------


def test_a_fact_counts_as_taken_back_only_when_its_record_holds_no_fact_now(tmp_path: Path) -> None:
    def fact(id: str, status: str, record: str) -> Fact:
        return Fact(id=id, text="x", subject="the pump", kind="event", at="2026-11-10", source="notes", record=record,
                    status=status)

    held = {f.id: f for f in (fact("f_gone", "retracted", "n0"), fact("f_one", "retracted", "n1"),
                              fact("f_stands", "current", "n1"), fact("f_old", "superseded", "n2"))}

    def facts(ids: list[str] | None = None, source: str = "", record: str = "", latest: bool = True) -> list[Fact]:
        return [held[id] for id in ids if id in held] if ids is not None else [
            f for f in held.values() if f.record == record and f.status != "retracted"]

    grounding = _Grounding("things/alder", lookup=SimpleNamespace(extraction=SimpleNamespace(facts=facts)),  # type: ignore[arg-type]
                           root=tmp_path, ledger=None, clock=lambda: datetime.now(UTC), decider=None, decision=None,
                           zone=UTC, floor=0.6)
    cites = [Citation("fact", id) for id in ("f_gone", "f_one", "f_old", "f_unknown")]
    assert grounding.withdrawn(cites) == {"f_gone"}  # an earlier save, or a fact its standing record left, stays


@pytest.mark.parametrize(("by", "facts", "kind", "target"), [
    (Caller("you", "person"), 2, "fact.retract", "f_0"), (Caller("things/curator", "agent"), 1, "fact.retract", "f_0"),
    (Caller("you", "person"), 1, "record.retract", "n0")])
def test_a_take_back_counts_when_a_caller_made_it_and_not_when_an_agent_did(tmp_path: Path, by: Caller, facts: int,
                                                                              kind: str, target: str) -> None:
    from cerebro.lookup import StoreLookup
    from cerebro.records import ACCEPTED, Proposal, propose

    store, extraction = MemoryStore(), FakeExtraction()
    extraction.by_record["n0"] = [Fact(id=f"f_{n}", text="x", subject="the pump", kind="event", at="2026-11-10",
                                       source="notes", record="n0") for n in range(facts)]
    extraction.retract("f_0")  # the caller's leaves its record a fact, the agent's none, as a source's removal does
    propose(store, Proposal("p_1", kind, target, by, "2026-11-12T09:00:00Z", status=ACCEPTED, decided_by=by))
    grounding = _Grounding("things/alder", lookup=StoreLookup(store, extraction), root=tmp_path, ledger=None,
                           clock=lambda: datetime.now(UTC), decider=None, decision=None, zone=UTC, floor=0.6)
    assert grounding.withdrawn([Citation("fact", "f_0")]) == ({"f_0"} if by.kind == "person" else set())


def test_a_kept_body_drops_a_line_citing_only_a_record_taken_back(tmp_path: Path) -> None:
    from cerebro.lookup import StoreLookup
    from cerebro.records import retract, tell

    store, extraction, you = MemoryStore(), FakeExtraction(), Caller("you", "person")
    told = tell(store, extraction, you, "The pump in hall two failed on Monday.", as_="fact").record
    kept_record = tell(store, extraction, you, "The valve in hall one was mended.", as_="fact").record
    retract(store, extraction, you, told, "the wrong pump")
    revised = Record(id="n1", source="note", revision="1", at="2026-11-10T09:00:00Z", body="The fan stopped.", by=you)
    store.keep(revised)
    retract(store, extraction, you, revised.id, "reworded", clock=lambda: datetime(2026, 11, 11, 9, tzinfo=UTC))
    store.keep(replace(revised, revision="2", at="2026-11-12T09:00:00Z"))  # its maker's word after the retract
    grounding = _Grounding("things/alder", lookup=StoreLookup(store, extraction), root=tmp_path,
                           ledger=None, clock=lambda: datetime.now(UTC), decider=None, decision=None, zone=UTC,
                           floor=0.6)
    gone = grounding.withdrawn([Citation("record", told), Citation("record", kept_record), Citation("record", "n1")])
    assert told in gone and kept_record not in gone and "n1" not in gone


def test_compile_converges_a_run_that_returns_nothing_for_an_agent_that_never_wrote(tmp_path: Path) -> None:
    store, _, _, common = setup(tmp_path)
    compiled = compile_article(AGENT, agent_run=ScriptedRun(lambda p: ""), **common)
    assert compiled.article is None and store.article("reports/weekly") is None  # nothing appeared


def test_a_writer_that_returns_nothing_keeps_its_body_as_it_stands_and_settles(tmp_path: Path) -> None:
    store, items, _, common = setup(tmp_path)
    items[:] = ["The pump in hall two failed on Monday."]
    article = compile_article(AGENT, agent_run=ScriptedRun(lambda p: WROTE), **common).article.identity.id
    before = _texts(store, article)
    items.clear()  # nothing to cite now
    again = compile_article(AGENT, agent_run=ScriptedRun(lambda p: ""), **common)  # run, told it has nothing to cite
    assert again.article is None and again.notes == [KEPT] and again.dropped == [] and _texts(store, article) == before
    line = _compiled_line(SimpleNamespace(settings=SimpleNamespace(), ledger=None), AGENT.id, again)
    assert "wrote nothing" in line and "wrote articles" not in line
    assert compile_article(AGENT, agent_run=ScriptedRun(lambda p: "never asked"), **common).skipped


# -- keep_part --------------------------------------------------------------------------------------------------------


def _joined(tmp_path: Path) -> tuple[Any, Any, list[str], dict[str, Any]]:
    agents, store, items, common = briefs(tmp_path, JOINED)
    items[:] = ["The pump in hall two failed on Monday."]
    return agents["briefs/article"], store, items, common


def _first(composer: Any, common: dict[str, Any]) -> None:
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "Risks: " + WROTE),
                   "briefs/subagents/people": ScriptedRun(lambda p: "People: " + WROTE)})
    compile_article(composer, agent_run=run, this="briefs/alder", **common)


def test_compile_converges_a_run_that_leaves_out_a_part_it_wrote_before(tmp_path: Path) -> None:
    composer, store, items, common = _joined(tmp_path)
    _first(composer, common)
    items.append("The valve in hall one was mended.")
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: ""),
                   "briefs/subagents/people": ScriptedRun(lambda p: "The valve in hall one was mended [^r1].")})
    compiled = compile_article(composer, agent_run=run, this="briefs/alder", **common)
    assert "Risks: empty, so it kept what it wrote last" in compiled.notes
    risks, people = (dict(_texts(store, "briefs/alder"))[h] for h in ("Risks", "People"))
    assert "[^r0]" in risks and "[^r1]" not in risks and "[^r1]" in people
    assert compile_article(composer, agent_run=run, this="briefs/alder", **common).skipped  # converges


@pytest.mark.parametrize("body", ["", "  \n\n\t\n", "<!-- written by briefs/subagents/risks -->\n"],
                         ids=["empty", "white space", "only a provenance line"])
def test_empty_output_keeps_text(tmp_path: Path, body: str) -> None:
    composer, store, items, common = _joined(tmp_path)
    _first(composer, common)
    items.append("The valve in hall one was mended.")  # its evidence moved: it runs again
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: body),
                   "briefs/subagents/people": ScriptedRun(lambda p: "People: " + WROTE)})
    compiled = compile_article(composer, agent_run=run, this="briefs/alder", **common)
    assert "Risks: empty, so it kept what it wrote last" in compiled.notes
    assert "Risks: The pump in hall two failed on Monday" in dict(_texts(store, "briefs/alder"))["Risks"]
    assert compile_article(composer, agent_run=run, this="briefs/alder", **common).skipped  # converges


# -- kept text, and facts a person or a source takes back ------------------------------------------------------------


EVIDENCE = "---\nevidence:\n  - query: { from: facts, source: notes, window: last_365_days }\nheartbeat: [change]\n---\n\n"
PUMP = "The pump in hall two failed on Monday."


class Citing(AgentRun):
    """Each agent cites the first current fact its evidence shows, a composer as its verdict, or, once `quiet`, hands
    in nothing."""

    def __init__(self, extraction: FakeExtraction):
        self.extraction, self.quiet = extraction, False

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Budget) -> Run:
        def write(prompt: str) -> str:
            ids = list(dict.fromkeys(re.findall(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])))
            if self.quiet or not ids:
                return ""
            lead = "Verdict: " if agent.role == "composer" else ""
            return f"{lead}{PUMP[:-1]} [^{ids[0]}]."

        return ScriptedRun(write).run(agent, evidence, tools, budget)


class Facts:
    """A workspace with one notes source, its facts on the suite's extraction, a writer or a composed article that
    cites the pump fact, and the passes over it."""

    def __init__(self, root: Path, files: dict[str, str], article: str, run: type[Citing] = Citing):
        for rel, text in files.items():
            (root / "agents" / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / "agents" / rel).write_text(text)
        (root / "settings.yaml").write_text("schedule: { articles: 0 }\n")
        self.page, self.store, self.extraction = article, MemoryStore(), FakeExtraction()
        self.notes = FakeConnector("notes", [Record(id="n0", source="notes", revision="1",
                                                    at="2026-11-10T09:00:00Z", body=PUMP)])
        self.run = run(self.extraction)
        self.workspace = Cerebro(root, store=self.store, extraction=self.extraction, connectors=[self.notes],
                             clock=lambda: datetime(2026, 11, 16, 9, tzinfo=UTC), agent_run=self.run, trusted=True)
        if article.startswith("briefs/"):
            self.store.create(Identity(article, "briefs", "Alder"))
        self.workspace.run_once()
        assert PUMP[:-1] in self.text()

    def text(self) -> str:
        found = self.store.article(self.page)
        return "\n".join(s.text for s in found.compilations) if found is not None else ""

    def take_back(self, how: str) -> None:
        self.run.quiet = True
        if how == "retracted":
            fact = next(f for fs in self.extraction.by_record.values() for f in fs if f.text == PUMP)
            self.workspace.retract(Caller("you", "person"), fact.id, "the wrong pump")
        else:
            self.notes.remove("n0")


WRITER = EVIDENCE + "# Instructions\n\nWrite it.\n\n# Output\n\n## Summary\n"
RISKS = EVIDENCE + "# Instructions\n\nDraft it.\n\n# Output\n\n## Risks\n"


@pytest.mark.parametrize("how", ["retracted", "removed"])
@pytest.mark.parametrize("role", ["writer", "subagent"])
def test_a_writers_only_cited_fact_taken_back_leaves_the_article(tmp_path: Path, role: str, how: str) -> None:
    files, article = ({"reports/weekly.md": WRITER}, "reports/weekly") if role == "writer" else (
        {"briefs/article.md": JOINED.replace("[risks, people]", "[risks]"), "briefs/subagents/risks.md": RISKS},
        "briefs/alder")
    space = Facts(tmp_path, files, article)
    space.take_back(how)
    result = space.workspace.run_once()
    assert PUMP[:-1] not in space.text(), result.warnings


@pytest.mark.parametrize("how", ["retracted", "removed"])
def test_a_composers_verdict_that_cites_a_fact_taken_back_goes(tmp_path: Path, how: str) -> None:
    files = {"briefs/article.md": BRIEF.replace("[risks, people]", "[risks]"), "briefs/subagents/risks.md": RISKS}
    space = Facts(tmp_path, files, "briefs/alder")
    assert "Verdict:" in space.text()
    space.take_back(how)
    result = space.workspace.run_once()
    assert PUMP[:-1] not in space.text(), result.warnings


VALVE = "The valve in hall three leaked on Tuesday."


class Drafts(Citing):
    """A subagent cites every current fact its evidence shows, one sentence each, then says `again` when it has
    something to say; a composer cites the first as its verdict, or, once `quiet`, hands in nothing."""

    again = ""

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Budget) -> Run:
        def write(prompt: str) -> str:
            ids = list(dict.fromkeys(re.findall(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])))
            said = {f.id: f.text for fs in self.extraction.by_record.values() for f in fs}
            if agent.role == "composer":
                return "" if self.quiet or not ids else f"Verdict: {said[ids[0]][:-1]} [^{ids[0]}]."
            return " ".join(f"{said[i][:-1]} [^{i}]." for i in ids if i in said) + (self.again if ids else "")

        return ScriptedRun(write).run(agent, evidence, tools, budget)


def test_a_composer_that_adds_nothing_lands_its_subagents_new_draft_in_place_of_what_it_composed(
        tmp_path: Path) -> None:
    files = {"briefs/article.md": BRIEF.replace("[risks, people]", "[risks]"), "briefs/subagents/risks.md": RISKS}
    space = Facts(tmp_path, files, "briefs/alder", run=Drafts)
    assert "Verdict:" in space.text()
    space.run.quiet = True
    space.notes.add(Record(id="n1", source="notes", revision="1", at="2026-11-15T09:00:00Z", body=VALVE))
    result = space.workspace.run_once()
    assert VALVE[:-1] in space.text() and PUMP[:-1] in space.text(), result.warnings
    assert "Verdict:" not in space.text()  # its last whole body gives way to the drafts
    again = space.workspace.run_once()
    assert VALVE[:-1] in space.text(), again.warnings


def test_a_writers_text_is_never_cut_as_it_lands_even_a_line_that_cites_a_fact_taken_back(tmp_path: Path) -> None:
    space = Facts(tmp_path, {"reports/weekly.md": WRITER}, "reports/weekly", run=Drafts)
    pump = next(f.id for fs in space.extraction.by_record.values() for f in fs if f.text == PUMP)
    space.run.again = f"\n{PUMP[:-1]} [^{pump}]."
    space.notes.remove("n0")
    space.notes.add(Record(id="n1", source="notes", revision="1", at="2026-11-15T09:00:00Z", body=VALVE))
    result = space.workspace.run_once()
    assert VALVE[:-1] in space.text() and PUMP[:-1] in space.text(), result.warnings  # its editor scores it


def test_a_draft_that_stood_while_another_lands_loses_a_line_whose_fact_its_source_took_back(tmp_path: Path) -> None:
    pump_only = EVIDENCE.replace("window: last_365_days", "record: n0")  # it has nothing new to say once n0 goes
    files = {"briefs/article.md": BRIEF, "briefs/subagents/risks.md": pump_only + "# Instructions\n\nDraft it.\n\n"
             "# Output\n\n## Risks\n", "briefs/subagents/people.md": RISKS.replace("## Risks", "## People")}
    space = Facts(tmp_path, files, "briefs/alder", run=Drafts)
    pump = next(f.id for fs in space.extraction.by_record.values() for f in fs if f.text == PUMP)
    space.run.quiet, space.run.again = True, f"\n{PUMP[:-1]} [^{pump}]."  # the composer hands in nothing
    space.notes.remove("n0")
    space.notes.add(Record(id="n1", source="notes", revision="1", at="2026-11-15T09:00:00Z", body=VALVE))
    result = space.workspace.run_once()
    parts = dict(_texts(space.store, "briefs/alder"))
    assert "Risks" not in parts and VALVE[:-1] in parts["People"], result.warnings
    assert PUMP[:-1] in parts["People"]  # what a writer just handed in is never cut: its editor scores it
