"""What grounding shows the judge and a repair: an item of a data block told in neutral words and with no writer's
file, a fact that carries the people its record marks as the workspace's own side, and how a lead is read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cerebro.agent import Agent
from cerebro.article import Citation
from cerebro.connector import Record
from cerebro.errors import CerebroError
from cerebro.facts import Fact, Quote
from cerebro.fakes import FakeDecider, FakeRewriter
from cerebro.ground import DATA_ITEM, ground, own_sides
from cerebro.models import Answer, Decision

FILE = "Name each part's holder.\n\n- steers: gives the go or no-go for the work.\n- helps: does the work beside them.\n"
AGENT = Agent(id="things/parts", path=Path("agents/things/parts.md"), role="writer", target="things/halden",
              prompt=FILE)
FACT = Fact("f_1", "Ines Calder said the trial can start on 1 December if review signs off.", "Halden Works",
            "note", "2026-11-03T10:00:00Z", "files", "local/halden.md",
            [Quote("We can start on the first if review signs off.", "Ines Calder", "2026-11-03T10:00:00Z")])
CITABLE = {"f_1": Citation("fact", "f_1", "local/halden.md", FACT.text)}
BLOCK = "Ines Calder steers the trial [^f_1].\n\n```yaml\n- holder: Ines Calder\n  kind: steers\n  fact: \"[^f_1]\"\n```\n"


def _states(decider: Any) -> list[dict[str, Any]]:
    return [state for state, _ in decider.asked]


def _item_request(decider: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The request and questions that asked about the data block's item."""
    for state, questions in decider.asked:
        if any(s.get("sentence", "").startswith("holder:") for s in state["sentences"]):
            return state, questions
    raise AssertionError("no request asked about the item")


# -- a data block's item, read as its writer defines its values

def test_the_judge_is_not_shown_the_writers_file() -> None:
    decider = FakeDecider()
    ground(AGENT, {"Part": BLOCK}, CITABLE, decider=decider, facts={"f_1": FACT}, today="2026-11-10")
    state, questions = _item_request(decider)
    assert "writer_file" not in state
    assert all("writer_file" not in q.wire()["instructions"] for q in questions.values())


def test_what_the_judge_is_told_of_a_data_item_holds_no_word_of_one_use() -> None:
    told = DATA_ITEM.lower()
    for word in ("role", "decides", "decision", "person", "people/", "jane", "writer_file"):
        assert word not in told, word


# -- the workspace's own side, as a fact's record marks it

def test_a_fact_carries_the_people_its_record_marks_as_the_workspaces_own_side() -> None:
    decider = FakeDecider()
    ground(AGENT, {"Part": "Ines Calder, of the workspace's own side, steers the trial [^f_1]."}, CITABLE,
           decider=decider, facts={"f_1": FACT}, sides={"local/halden.md": ["Ines Calder"]})
    assert decider.asked[0][0]["facts"]["f_1"]["own_side"] == ["Ines Calder"]
    assert "own_side" in decider.asked[0][0]["contract"]


def test_a_repair_is_shown_the_side_its_facts_record() -> None:
    class Once(FakeDecider):
        """Unsupported the first time, grounded after."""

        def decide(self, state: Any, questions: Any) -> Decision:
            first = not self.asked
            decision = super().decide(state, questions)
            if first:
                decision.answers["s1"] = Answer(choice="unsupported", confidence=1.0, probabilities={
                    "grounded": 0.0, "nonfactual": 0.0, "unsupported": 1.0, "contradicted": 0.0})
            return decision

    rewriter = FakeRewriter(lambda request: json.dumps({"claim": "Ines Calder, of the workspace's own side, steers the "
                                                                 "trial", "fact_ids": ["f_1"]}))
    ground(AGENT, {"Part": "Ines Calder, of the workspace's own side, steers the whole trial [^f_1]."}, CITABLE,
           decider=Once(), facts={"f_1": FACT}, sides={"local/halden.md": ["Ines Calder"]}, rewriter=rewriter)
    request = rewriter.asked[0]
    assert request["facts"][0]["own_side"] == ["Ines Calder"]


def test_own_sides_reads_each_record_once_and_passes_over_one_it_cannot_read() -> None:
    read: list[str] = []
    other = Fact("f_2", "Tobin Marr helps.", "Halden Works", "note", "", "files", "local/broken.md")
    bare = Fact("f_3", "The trial starts.", "Halden Works", "note", "", "files", "local/bare.md")

    def record(id: str) -> Record | None:
        read.append(id)
        if id == "local/broken.md":
            raise CerebroError("the store is shut")
        if id == "local/bare.md":
            return Record(id=id, source="files", revision="1")
        return Record(id=id, source="files", revision="1", fields={"ours": ["Ines Calder", " ", "Ines Calder"]})

    assert own_sides(record, [FACT, FACT, other, bare]) == {"local/halden.md": ["Ines Calder"]}
    assert read == ["local/halden.md", "local/broken.md", "local/bare.md"]
    assert own_sides(lambda id: None, [FACT]) == {}


# -- a lead read across its two holding answers

def _torn(lead: Answer) -> FakeDecider:
    class Torn(FakeDecider):
        def decide(self, state: Any, questions: Any) -> Decision:
            decision = super().decide(state, questions)
            for name in questions:
                if name.startswith("l"):
                    decision.answers[name] = lead
            return decision

    return Torn()


TORN = Answer(choice="names", confidence=0.45, probabilities={"names": 0.45, "states_held": 0.45,
                                                               "states_unheld": 0.10})
LEANS_OUT = Answer(choice="states_unheld", confidence=0.40, probabilities={"names": 0.35, "states_held": 0.25,
                                                                           "states_unheld": 0.40})
LINE = "**Trial plan:** Ines Calder said the trial can start on 1 December if review signs off [^f_1]."


FIRM = Answer(choice="names", confidence=0.6, probabilities={"names": 0.6, "states_held": 0.3, "states_unheld": 0.1})


def test_a_lead_stands_on_one_firm_holding_answer() -> None:
    out = ground(AGENT, {"Part": LINE}, CITABLE, decider=_torn(FIRM), facts={"f_1": FACT})
    assert out.parts["Part"].startswith("**Trial plan:**"), out.dropped


def test_a_lead_torn_between_its_two_holding_answers_goes() -> None:
    out = ground(AGENT, {"Part": LINE}, CITABLE, decider=_torn(TORN), facts={"f_1": FACT})
    assert not out.parts["Part"].startswith("**Trial plan:**")


def test_a_lead_the_judge_leans_to_unheld_still_goes() -> None:
    out = ground(AGENT, {"Part": LINE}, CITABLE, decider=_torn(LEANS_OUT), facts={"f_1": FACT})
    assert not out.parts["Part"].startswith("**Trial plan:**")
    assert any('removed the lead "Trial plan:"' in line for line in out.dropped)


def test_a_lead_with_a_figure_its_facts_lack_does_not_stand_on_naming() -> None:
    line = "**Trial plan at $4,000:** Ines Calder said the trial can start on 1 December if review signs off [^f_1]."
    out = ground(AGENT, {"Part": line}, CITABLE, decider=_torn(TORN), facts={"f_1": FACT})
    assert not out.parts["Part"].startswith("**Trial plan")


# -- compile hands grounding the side each cited fact's record marks

def _compile(store: Any, decider: FakeDecider, root: Path) -> None:
    from cerebro.agent import EvidenceLine
    from cerebro.article import Identity
    from cerebro.compile import compile_article
    from cerebro.fakes import FakeExtraction, ScriptedRun
    from cerebro.lookup import StoreLookup

    facts = FakeExtraction()
    facts.by_record["local/halden.md"] = [FACT]
    store.create(Identity("things/halden", "things", "Halden"))
    store.link("f_1", ["things/halden"])
    agent = Agent(id="things/halden", path=Path("agents/things/halden.md"), role="writer", target="things/halden",
                  prompt="Write it.\n\n# Output\n\n## Summary", output=["Summary"],
                  evidence=[EvidenceLine("items", {})])
    compile_article(agent, lookup=StoreLookup(store, facts), store=store, root=root, agent_tools={},
                    functions={"items": lambda: [{"id": "f_1", "text": FACT.text}]}, decider=decider,
                    agent_run=ScriptedRun(lambda p: {"Summary": "Ines Calder steers the trial [^f_1]."}))


def test_compile_shows_the_judge_the_side_a_cited_facts_record_marks(tmp_path: Path) -> None:
    from cerebro.fakes import MemoryStore

    store = MemoryStore()
    store.keep(Record(id="local/halden.md", source="files", revision="1", fields={"ours": ["Ines Calder"]}))
    decider = FakeDecider()
    _compile(store, decider, tmp_path)
    state = next(state for state in _states(decider) if state.get("facts"))
    assert state["facts"]["f_1"]["own_side"] == ["Ines Calder"]


def test_compile_grounds_with_no_side_when_a_record_cannot_be_read(tmp_path: Path) -> None:
    from cerebro.fakes import MemoryStore

    class Shut(MemoryStore):
        def record(self, id: str) -> Record | None:
            if id == "local/halden.md":
                raise CerebroError("the store is shut")
            return super().record(id)

    store = Shut()
    store.keep(Record(id="local/halden.md", source="files", revision="1", fields={"ours": ["Ines Calder"]}))
    decider = FakeDecider()
    _compile(store, decider, tmp_path)
    state = next(state for state in _states(decider) if state.get("facts"))
    assert "own_side" not in state["facts"]["f_1"] and state["facts"]["f_1"]["text"] == FACT.text

