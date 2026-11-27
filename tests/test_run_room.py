"""What a run does with a model that never calls its hand-in, and with a curator that spends its room, each on a
stand-in model of the shape live runs showed: a model that writes its text in its
last reply in place of the hand-in has the parts its output names taken, and nothing written beside a tool call
earlier; a run that ends with nothing to take fails; calls made side by side each take only the room their reply uses; a
curator keeps no room to write, since it decides with its tools, its decisions run whatever room is left, and at its
floor its reads are refused with the line that says to decide now, told before that, ahead of a turn while it has its
tools. The run's own lines count against no budget."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from suites.agent_run import AGENT, EVIDENCE

from cerebro.agent import Budget
from cerebro.run import PydanticRun

CITED = "## Summary\n\nThe review moved to March [^f_1]."


def _model(turns: list[list[tuple[str, dict[str, Any]]]], then: str, seen: list[Any] | None = None,
           beside: dict[int, str] | None = None) -> object:
    """A function model that makes each turn's calls in order, with the text `beside` gives a turn written next to
    them, then answers in plain text, `then`, every turn after: never the hand-in tool. `seen` gets each request's
    messages and settings."""
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        n = sum(isinstance(m, ModelResponse) for m in messages)
        if seen is not None:
            seen.append((list(messages), dict(info.model_settings or {})))
        if n < len(turns):
            text = [TextPart((beside or {})[n])] if n in (beside or {}) else []
            return ModelResponse(parts=[*text, *(ToolCallPart(name, args) for name, args in turns[n])])
        return ModelResponse(parts=[TextPart(then)])

    return FunctionModel(reply, model_name="no hand-in")


def lookup(id: str) -> str:
    """Look one thing up."""
    return f"looked up {id}"


# -- 1. a model that never calls the hand-in tool


def test_a_model_that_writes_its_cited_text_in_its_reply_in_place_of_the_hand_in_has_that_text_land() -> None:
    turns = [[("lookup", {"id": f"n{i}"})] for i in range(4)]  # it reads first
    run = PydanticRun(_model(turns, CITED)).run(AGENT, EVIDENCE, [lookup], Budget())
    assert run.status == "done" and run.result is not None and run.result.text == CITED
    assert "took its text from its reply" in run.note


def test_the_last_reply_is_taken_whatever_it_cites_and_grounding_judges_it() -> None:
    """No citation picks the text: the parts of the last reply under the headings its output names are taken as they
    stand, and grounding, after the run, keeps what its evidence holds."""
    uncited = "## Summary\n\nThe review moved to March."
    run = PydanticRun(_model([], uncited)).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == "done" and run.result is not None and run.result.text == uncited


def test_text_beside_an_earlier_call_is_never_taken_and_a_run_whose_last_reply_is_empty_fails() -> None:
    turns = [[("lookup", {"id": "n1"})]]
    run = PydanticRun(_model(turns, "", beside={0: "Let me check [^f_1] next."})).run(
        AGENT, EVIDENCE, [lookup], Budget())
    assert run.status == "failed" and run.result is None
    assert run.note == "it handed in nothing, also after the turn that asked for it"


# -- 2. calls made side by side


def test_calls_made_side_by_side_each_take_only_the_room_their_reply_uses() -> None:
    """Five reads in one turn on the default budget, each slow enough that they overlap: none is refused for room,
    with a few hundred tokens of a hundred thousand used."""
    import time

    def slow(id: str) -> str:
        """Look one thing up."""
        time.sleep(0.05)
        return "word " * 50

    turns = [[("slow", {"id": f"n{i}"}) for i in range(5)]]
    run = PydanticRun(_model(turns, CITED)).run(AGENT, EVIDENCE, [slow], Budget())
    replies = [e.text for e in run.transcript if e.role == "tool" and e.tool == "slow"]
    assert len(replies) == 5 and not any("not run" in r for r in replies), replies


# -- 3. a curator that spends its room


CURATOR = replace(AGENT, id="things/curator", path=Path("agents/things/curator.md"), role="curator", target="things",
                  prompt="Keep the things.", output=[])


def _curator_tools(admitted: list[str], big: int) -> list[Any]:
    def read(id: str) -> str:
        """Read one record."""
        return "word " * (big // 5)

    def admit(name: str, reason: str = "") -> dict[str, str]:
        """Admit an article."""
        admitted.append(name)
        return {"admitted": f"things/{name.lower()}"}

    admit.decides = True  # type: ignore[attr-defined]
    return [read, admit]


def test_a_curators_decision_runs_though_its_reads_have_used_the_room() -> None:
    """Ten long reads on the default budget, then six admits in one turn: every admit runs."""
    admitted: list[str] = []
    turns = [[("read", {"id": f"r{i}"})] for i in range(10)]
    turns.append([("admit", {"name": f"T{i}", "reason": "Named."}) for i in range(6)])
    run = PydanticRun(_model(turns, "Left out 0.")).run(CURATOR, EVIDENCE, _curator_tools(admitted, 30_000), Budget())
    assert sorted(admitted) == [f"T{i}" for i in range(6)], [e.text[:120] for e in run.transcript if e.role == "tool"]
    assert not any("no room left" in e.text for e in run.transcript if e.tool == "admit")


def test_a_curators_decision_runs_past_its_tool_calls_and_a_read_at_its_floor_is_refused_with_the_line_to_decide()\
        -> None:
    admitted: list[str] = []
    budget = Budget(tokens_per_run=100_000, tool_calls=6)
    turns = [[("read", {"id": f"r{i}"}) for i in range(3)], [("read", {"id": "r9"})],
             [("admit", {"name": f"T{i}", "reason": "Named."}) for i in range(4)]]
    run = PydanticRun(_model(turns, "Left out 0.")).run(CURATOR, EVIDENCE, _curator_tools(admitted, 10), budget)
    replies = [e.text for e in run.transcript if e.role == "tool" and e.tool == "read"]
    assert "Decide now with what you have read" in replies[-1] and "admit" in replies[-1]
    assert sorted(admitted) == ["T0", "T1", "T2", "T3"]  # side by side, in any order


def test_a_curator_is_told_to_decide_ahead_of_a_turn_while_it_still_has_its_tools() -> None:
    """The note goes to the model before the turn, as the run's own words, never inside what a tool replied."""
    from pydantic_ai.messages import ToolReturnPart, UserPromptPart

    admitted: list[str] = []
    seen: list[Any] = []
    budget = Budget(tokens_per_run=40_000, tool_calls=40)
    turns = [[("read", {"id": "r1"})], [("read", {"id": "r2"})], [("admit", {"name": "Alder", "reason": "Named."})]]
    run = PydanticRun(_model(turns, "Left out 0.", seen)).run(CURATOR, EVIDENCE, _curator_tools(admitted, 20_000),
                                                              budget)
    told = [p for m in seen[-1][0] for p in getattr(m, "parts", []) if isinstance(p, (UserPromptPart, ToolReturnPart))
            if "This run's room is short" in str(p.content)]
    assert told and all(isinstance(p, UserPromptPart) for p in told) and admitted == ["Alder"]
    assert sum("This run's room is short" in e.text for e in run.transcript) == 1


def test_the_floor_at_five_calls_left_refuses_reads_spending_no_call_in_the_turn_that_reached_it_only() -> None:
    """Eight calls: the third read leaves five, so its reply says to decide now. The read made beside it in the same
    turn, before the model read the line, is refused and spends nothing; each read refused in a later turn spends its
    call, so a run that keeps reading still ends. Every refusal is in the run's refused list, and the decisions run."""
    admitted: list[str] = []
    budget = Budget(tokens_per_run=100_000, tool_calls=8)
    turns = [[("read", {"id": "r1"})], [("read", {"id": "r2"})], [("read", {"id": "r3"}), ("read", {"id": "r4"})],
             [("read", {"id": "r5"})], [("read", {"id": "r6"})],
             [("admit", {"name": f"T{i}", "reason": "Named."}) for i in range(4)]]
    run = PydanticRun(_model(turns, "Left out 0.")).run(CURATOR, EVIDENCE, _curator_tools(admitted, 10), budget)
    replies = [e.text for e in run.transcript if e.role == "tool" and e.tool == "read"]
    import re

    left = [int(m.group(1)) for r in replies if (m := re.search(r"spent: (\d+) tool call", r))]
    assert left == [5, 5, 4, 3], replies
    assert run.refused.count("read") == 3
    assert sorted(admitted) == ["T0", "T1", "T2", "T3"]


def test_the_runs_own_lines_count_against_no_budget() -> None:
    """The line that says to decide now is the run's own words: the room it holds is what the tools replied."""
    from cerebro.run import _bounded, _Room

    def read(id: str) -> str:
        """Read one record."""
        return "word"

    room = _Room(100_000, "", 6, deciding=True, decisions=["admit"])
    reading = _bounded(read, room)
    reading(id="r1")
    before = room.held
    told = reading(id="r2")  # five calls left: its reply carries the line
    assert "Decide now" in told and room.held - before < 10
    before = room.held
    assert "Decide now" in reading(id="r3") and room.held == before


def test_a_decision_the_model_makes_in_a_turn_offered_no_tools_still_runs() -> None:
    """Once its calls are spent the run offers no tools; a decision the model calls there anyway runs, since a
    curator's decisions run whatever is left, so it is never lost. A read beside it is refused: no call is left."""
    admitted: list[str] = []
    turns = [[("read", {"id": f"r{i}"}) for i in range(3)],
             [("admit", {"name": "Late", "reason": "Named."}), ("read", {"id": "r9"})]]
    run = PydanticRun(_model(turns, "Left out 0.")).run(CURATOR, EVIDENCE, _curator_tools(admitted, 10),
                                                        Budget(tool_calls=3))
    late = [e.text for e in run.transcript if e.role == "tool" and e.tool == "read"][-1]
    assert "not run" in late or "Decide now" in late
    assert admitted == ["Late"] and run.status == "done"


def test_only_a_curators_decisions_run_past_its_budget() -> None:
    from cerebro.clock import system_clock
    from cerebro.curate import Curated, _Curation
    from cerebro.fakes import MemoryStore

    tools = _Curation(CURATOR, MemoryStore(), system_clock, Curated(), None).tools()
    decides = sorted(n for n, t in tools.items() if getattr(t, "decides", False))
    assert decides == ["admit", "archive", "instruct", "leave_out", "link", "merge", "reject", "split"]


def test_a_curator_out_of_room_or_calls_is_never_told_to_hand_in_text() -> None:
    from cerebro.run import _bounded, _Room

    def read(id: str) -> str:
        """Read one record."""
        return "word"

    room = _Room(100_000, "", 0, deciding=True, decisions=["admit"])
    said = _bounded(read, room)(id="r1")
    assert "Hand in" not in said and "keeps to write" not in said, said


def test_the_live_checks_print_a_line_for_each_run_that_took_one_of_these_paths(monkeypatch: Any) -> None:
    """So a live run shows whether they fire: the floor met and its refusals, a call refused for room, a reply taken,
    and nothing for a run that took none."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "checks"))
    from _paths import paths

    admitted: list[str] = []
    turns = [[("read", {"id": f"r{i}"})] for i in range(4)] + [[("admit", {"name": "Alder", "reason": "Named."})]]
    run = PydanticRun(_model(turns, "Left out 0.")).run(CURATOR, EVIDENCE, _curator_tools(admitted, 10),
                                                        Budget(tool_calls=7))
    assert "met its floor, 2 calls refused there" in paths(run), paths(run)
    taken = PydanticRun(_model([], CITED)).run(AGENT, EVIDENCE, [], Budget())
    assert paths(taken).endswith("took its reply as its text")
    plain = PydanticRun.scripted(lambda p: {"Summary": "Done [^f_1]."}).run(AGENT, EVIDENCE, [], Budget())
    assert paths(plain) == ""
