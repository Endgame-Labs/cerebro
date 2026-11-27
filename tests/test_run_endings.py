"""How a run's ending decides what it hands in, what is recorded, and when it runs again. A run that says it has
nothing to write ran and wrote nothing: its inputs are recorded, so it costs no further model call until one moves, and
it is no failure. A reply is taken for its parts under the headings the agent's `# Output` names, or for a subagent
the text under its heading; the turn a spent or cut run ends on is read so: an agent of one heading, or a subagent,
takes the text under its first heading or all of it, and one that names none takes all of it. With nothing handed in
and nothing to take, at a normal end, after the run's room or calls were spent, or at a turn cut at its output limit,
the run fails and runs again, but a subagent's normal end wrote nothing, so its part stands. A composer that adds
nothing leaves its subagents' content standing, whatever the ending."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from suites.agent_run import AGENT, EVIDENCE

from cerebro.agent import Agent, Budget
from cerebro.run import (
    CUT_NOTHING,
    HANDED_NOTHING,
    SPENT_NOTHING,
    STANDS,
    TOOK_REPLY,
    UNMARKED,
    WROTE_NOTHING,
    PydanticRun,
)


def _model(turns: list[list[tuple[str, dict[str, Any]]]], then: str, cut: bool = False) -> object:
    """A function model that makes each turn's calls in order, then answers in plain text, `then`, every turn after,
    each such reply cut at its output limit when `cut`."""
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        n = sum(isinstance(m, ModelResponse) for m in messages)
        if n < len(turns):
            return ModelResponse(parts=[ToolCallPart(name, args) for name, args in turns[n]])
        return ModelResponse(parts=[TextPart(then)] if then else [], finish_reason="length" if cut else "stop")

    return FunctionModel(reply, model_name="no hand-in")


def lookup(id: str) -> str:
    """Look one thing up."""
    return f"looked up {id}"


SUB = replace(AGENT, id="things/subagents/risks", path=Path("agents/things/subagents/risks.md"), role="subagent",
              target="things", prompt="Name the risks.", output=["Risks"])
COMPOSER = replace(AGENT, id="things/article", path=Path("agents/things/article.md"), role="composer",
                   target="things", subagents=["risks"], output=[])
SPENT = Budget(tool_calls=1)  # one call, then the run offers only the hand-in: its last turn, without tools
DECLINES = [[("nothing_to_write", {"reason": "Nothing new."})]]
DECLINED = "it said there is nothing to write: Nothing new."


# -- a normal end


def test_a_normal_end_with_no_part_under_a_named_heading_takes_nothing_and_says_so() -> None:
    said = "I looked and found the review moved [^f_1]."
    run = PydanticRun(_model([], said)).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == "failed" and run.result is None and run.note == f"{HANDED_NOTHING}, and {UNMARKED}"


def test_a_normal_end_with_an_empty_reply_fails() -> None:
    run = PydanticRun(_model([], "")).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == "failed" and run.result is None and run.note == HANDED_NOTHING


def test_a_normal_end_takes_only_the_parts_under_the_headings_its_output_names() -> None:
    said = ("Here is the report.\n\n## Summary\n\nThe review moved [^f_1].\n\n## Aside\n\nA thought.\n\n"
            "## Next\n\nWatch March.\n")
    run = PydanticRun(_model([], said)).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == "done" and run.note == TOOK_REPLY and run.result is not None
    assert run.result.text == "## Summary\n\nThe review moved [^f_1].\n\n## Next\n\nWatch March."


def test_a_normal_end_of_an_agent_whose_output_names_no_heading_takes_nothing_from_its_reply(
        ) -> None:
    whole = replace(AGENT, output=[])  # a whole-body agent takes nothing from a reply on a normal end
    said = "## Summary\n\nThe review moved [^f_1]."
    run = PydanticRun(_model([], said)).run(whole, EVIDENCE, [], Budget())
    assert run.status == "failed" and run.result is None


def test_a_subagent_on_a_normal_end_returns_the_text_under_its_heading_and_nothing_with_no_heading() -> None:
    run = PydanticRun(_model([], "Done.\n\n## Risks\n\nThe review moved [^f_1].\n")).run(SUB, EVIDENCE, [], Budget())
    assert run.status == "done" and run.result is not None and run.result.text == "The review moved [^f_1]."
    bare = PydanticRun(_model([], "The review moved [^f_1].")).run(SUB, EVIDENCE, [], Budget())
    assert bare.status == WROTE_NOTHING and bare.result is None  # nothing under a named heading


def test_a_composer_that_ends_with_a_reply_and_no_hand_in_leaves_its_subagents_content_standing() -> None:
    run = PydanticRun(_model([], "## Risks\n\nThey read well [^f_1].")).run(COMPOSER, EVIDENCE, [], Budget())
    assert run.status == WROTE_NOTHING and run.result is None
    assert run.note.startswith("it added nothing, so its subagents' content stands")


# -- a spent budget, a cut turn


def test_after_its_calls_are_spent_a_last_reply_under_no_heading_is_taken_only_by_one_of_one_heading_or_none() -> None:
    said = "Here is the report: the review moved [^f_1]."
    run = PydanticRun(_model([[("lookup", {"id": "n1"})]], said)).run(AGENT, EVIDENCE, [lookup], SPENT)
    assert run.status == "failed" and run.result is None and run.note == SPENT_NOTHING  # two headings
    for agent in (replace(AGENT, output=["Summary"]), replace(AGENT, output=[])):  # the last turn's text
        one = PydanticRun(_model([[("lookup", {"id": "n1"})]], said)).run(agent, EVIDENCE, [lookup], SPENT)
        assert one.status == "done" and one.result is not None and one.result.text == said and one.note == TOOK_REPLY


def test_after_its_calls_are_spent_a_run_with_nothing_to_take_fails_so_it_runs_again() -> None:
    run = PydanticRun(_model([[("lookup", {"id": "n1"})]], "")).run(AGENT, EVIDENCE, [lookup], SPENT)
    assert run.status == "failed" and run.result is None and run.note == SPENT_NOTHING


def test_a_composer_whose_calls_are_spent_and_hands_in_nothing_leaves_its_subagents_content_standing() -> None:
    run = PydanticRun(_model([[("lookup", {"id": "n1"})]], "")).run(COMPOSER, EVIDENCE, [lookup], SPENT)
    assert run.status == WROTE_NOTHING and run.note.startswith("it added nothing, so its subagents' content stands")


def test_a_turn_cut_at_its_output_limit_with_nothing_to_take_fails_and_says_it_was_cut() -> None:
    from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:  # its hand-in, cut mid-arguments
        return ModelResponse(parts=[ToolCallPart("write_article", '{"text": "The review mo')], finish_reason="length")

    run = PydanticRun(FunctionModel(reply, model_name="cut")).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == "failed" and run.result is None and run.note == CUT_NOTHING


# -- what a pass records of it


class _Hands:
    at = datetime(2026, 11, 17, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


def _workspace(tmp_path: Path, said: list[str | None]) -> Any:
    """One writer of one article whose model replies `said[0]` in plain text, or says it has nothing to write when
    that is None."""
    from cerebro import Cerebro, Record
    from cerebro.article import Identity
    from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore

    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: notes }\n---\n\n# Output\n\n## Summary\n")
    notes = FakeConnector("notes", [Record(id="n0", source="notes", revision="1", at="2026-11-17T08:00:00Z",
                                           body="The first note.", keys={"topic": ["first"]})])
    calls: list[str] = []

    class Counting(PydanticRun):
        def run(self, agent: Agent, evidence: Any, tools: Any, budget: Budget) -> Any:
            calls.append(agent.id)
            return PydanticRun(_model([] if said[0] is not None else DECLINES, said[0] or "")).run(
                agent, evidence, tools, budget)

    hands = _Hands()
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands, connectors=[notes],
                      agent_run=Counting("unused"))
    cerebro.store.create(Identity("things/first", "things", "First", {"topic": ["first"]}))
    return cerebro, hands, calls, notes


def test_a_pass_fails_a_run_that_ignored_the_hand_in_and_runs_one_that_declined_no_more_on_the_same_inputs(
        tmp_path: Path) -> None:
    from cerebro import Record

    said: list[str | None] = [""]
    cerebro, hands, calls, notes = _workspace(tmp_path, said)
    first = cerebro.run_once()
    assert first.compiled == [] and first.ran == {}
    assert first.failed["things/article on things/first"].endswith(HANDED_NOTHING), first.failed
    said[0] = None
    hands.at += timedelta(minutes=1)  # past the wait after one failed run
    declined = cerebro.run_once()
    assert len(calls) == 2 and declined.failed == {} and declined.ran == {"things/article on things/first": DECLINED}
    hands.at += timedelta(hours=2)
    again = cerebro.run_once()
    assert len(calls) == 2 and again.failed == {} and again.ran == {}  # the same inputs: no model call
    notes.add(Record(id="n1", source="notes", revision="1", at="2026-11-17T10:00:00Z",
                                body="The second note.", keys={"topic": ["first"]}))
    facts = cerebro.extraction.facts  # type: ignore[union-attr]
    hands.at += timedelta(minutes=1)
    said[0] = f"## Summary\n\nThe first note [^{facts()[0].id}]."
    moved = cerebro.run_once()
    assert len(calls) == 3 and moved.compiled == ["things/first"]  # never written: at once, not after a cooldown


def test_a_run_that_ran_and_wrote_nothing_moves_the_articles_clock(tmp_path: Path) -> None:
    cerebro, _, _, _ = _workspace(tmp_path, [None])
    cerebro.run_once()
    beat = cerebro.store.state("heartbeat:things/article")
    assert (beat.get("articles") or {}).get("things/first", {}).get("ran") == "2026-11-17T09:00:00Z"


def test_a_compile_that_ran_and_wrote_nothing_ends_its_step_as_ran_never_failed() -> None:
    from types import SimpleNamespace

    from cerebro.runner import _compiled_step

    run = SimpleNamespace(status=WROTE_NOTHING, note=DECLINED)
    step = _compiled_step(AGENT, SimpleNamespace(skipped=False, article=None, run=run, runs=[run]))
    assert step == {"mark": "ran", "text": f"ran and wrote nothing: {DECLINED}"}
    failed = SimpleNamespace(status="failed", note="a model error", agent=AGENT.id)
    assert _compiled_step(AGENT, SimpleNamespace(skipped=False, article=None, run=failed, runs=[failed]))["mark"] == "failed"


def test_the_progress_screen_shows_a_run_that_wrote_nothing_quiet_never_as_done_or_failed() -> None:
    from cerebro.output import Mark
    from cerebro.progress import MARKS

    assert MARKS["ran"] == Mark.QUIET


def test_what_a_pass_says_of_a_run_that_ran_and_wrote_nothing_comes_masked(tmp_path: Path) -> None:
    cerebro, _, _, _ = _workspace(tmp_path, [None])
    cerebro.mask = lambda text: text.replace("Nothing new", "[masked]")
    assert cerebro.run_once().ran == {"things/article on things/first": DECLINED.replace("Nothing new", "[masked]")}


def test_a_reply_the_provider_refused_fails_never_ran_and_wrote_nothing() -> None:
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[], finish_reason="content_filter")

    run = PydanticRun(FunctionModel(reply, model_name="refusing")).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == "failed" and run.result is None and run.note.startswith("ContentFilterError")


# -- a reply the loop refuses is no normal end


def _each(make: Any) -> object:
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return make(sum(isinstance(m, ModelResponse) for m in messages))

    return FunctionModel(reply, model_name="calls")


def test_a_run_the_loop_ends_on_a_call_it_refuses_twice_fails_with_the_error_and_never_ran_and_wrote_nothing() -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    cases = {
        "an unknown tool": lambda n: ModelResponse(parts=[ToolCallPart("search_web", {"q": "x"})]),
        "a tool's arguments": lambda n: ModelResponse(parts=[ToolCallPart("lookup", {"wrong": 1})]),
        "a malformed hand-in": lambda n: ModelResponse(parts=[ToolCallPart("write_article", {"body": "It moved."})]),
        "one call id twice": lambda n: ModelResponse(parts=[ToolCallPart("lookup", {"id": "a"}, tool_call_id="x"),
                                                           ToolCallPart("lookup", {"id": "b"}, tool_call_id="x")]),
    }
    for name, make in cases.items():
        run = PydanticRun(_each(make)).run(AGENT, EVIDENCE, [lookup], Budget())
        assert run.status == "failed" and run.result is None, (name, run.status, run.note)
        assert run.note.startswith("UnexpectedModelBehavior: "), (name, run.note)


def test_an_uncited_hand_in_then_plain_text_lands_the_held_text() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

    run = PydanticRun(_each(lambda n: ModelResponse(parts=[ToolCallPart("write_article", {"text": "no cite"})])
                            if n == 0 else ModelResponse(parts=[TextPart("ok")]))).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == "done" and run.result is not None and run.result.text == "no cite"


# -- which turn was the last without tools


def test_a_final_reply_made_while_tools_were_offered_is_a_normal_end_whatever_its_own_tokens_close() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.usage import RequestUsage

    big = int(Budget().tokens_per_run * 0.85)  # the reply's own tokens close the room after it
    run = PydanticRun(_each(lambda n: ModelResponse(parts=[], usage=RequestUsage(output_tokens=big)))).run(
        AGENT, EVIDENCE, [lookup], Budget())
    assert run.status == "failed" and run.result is None and run.note == HANDED_NOTHING, run.note
    said = "## Summary\n\nStill no article, just prose."
    late = PydanticRun(_each(lambda n: ModelResponse(parts=[], usage=RequestUsage(output_tokens=big)) if n == 0
                             else ModelResponse(parts=[TextPart(said)]))).run(AGENT, EVIDENCE, [lookup], Budget())
    assert late.status == "done" and late.result is not None and late.result.text == said


# -- a cut turn ends the run


def test_a_turn_cut_at_its_output_limit_has_its_text_under_its_headings_taken_and_ends_the_run_there() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart

    said = "## Summary\n\nHere is the report: the review moved [^f_1] to Mar"
    run = PydanticRun(_each(lambda n: ModelResponse(parts=[TextPart(said)], finish_reason="length") if n == 0
                            else ModelResponse(parts=[TextPart("Sorry, I'll keep it short.")]))).run(
        AGENT, EVIDENCE, [lookup], Budget())
    assert run.status == "done" and run.result is not None and run.result.text == said and run.note == TOOK_REPLY
    assert run.usage.requests == 1 and [e.text for e in run.transcript if e.role == "model"] == [said]


# -- what a composer and a subagent take from their last turn


def test_a_composer_takes_nothing_from_its_last_turn_and_its_subagents_content_stands() -> None:
    said = "I ran out of room; in short the review moved [^f_1]."
    run = PydanticRun(_model([[("lookup", {"id": "n1"})]], said)).run(COMPOSER, EVIDENCE, [lookup], SPENT)
    assert run.status == WROTE_NOTHING and run.result is None and run.note == STANDS
    cut = PydanticRun(_model([], said, cut=True)).run(COMPOSER, EVIDENCE, [], Budget())
    assert cut.status == WROTE_NOTHING and cut.result is None and cut.note == STANDS


def test_a_subagent_in_its_last_turn_takes_the_text_under_its_own_heading_first() -> None:
    said = "## Notes\n\nscratch [^f_1]\n\n## Risks\n\nThe review moved [^f_1]."
    run = PydanticRun(_model([[("lookup", {"id": "n1"})]], said)).run(SUB, EVIDENCE, [lookup], SPENT)
    assert run.status == "done" and run.result is not None and run.result.text == "The review moved [^f_1]."
    first = PydanticRun(_model([[("lookup", {"id": "n1"})]], "## Notes\n\nscratch [^f_1]\n\n## Other\n\nx")).run(
        SUB, EVIDENCE, [lookup], SPENT)
    assert first.result is not None and first.result.text == "scratch [^f_1]"


# -- a composer's pass where one of its runs failed


def test_a_composers_compile_where_a_subagent_failed_is_a_failure_though_its_own_run_wrote_nothing(
        tmp_path: Path) -> None:
    from cerebro import Cerebro, Record
    from cerebro.agent import Agent as AgentFile
    from cerebro.article import Identity
    from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore
    from cerebro.run import AgentRun, Run

    agents = tmp_path / "agents" / "things"
    (agents / "subagents").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (agents / "article.md").write_text("---\nsubagents: [risks]\n---\n\nA brief.\n\n# Composing\n\nWrite a verdict.\n")
    (agents / "subagents" / "risks.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: notes }\n---\n\n# Instructions\n\nDraft it.\n\n"
        "# Output\n\n## Risks\n")
    notes = FakeConnector("notes", [Record(id="n0", source="notes", revision="1", at="2026-11-17T08:00:00Z",
                                           body="The first note.", keys={"topic": ["first"]})])

    class Runs(AgentRun):
        def run(self, agent: AgentFile, evidence: Any, tools: Any, budget: Budget) -> Run:
            if agent.role == "subagent":
                return Run("run_sub", agent.id, agent.target, "failed", note="a model error")
            return Run("run_own", agent.id, agent.target, WROTE_NOTHING, note=STANDS)

    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=_Hands(), connectors=[notes],
                      agent_run=Runs())
    cerebro.store.create(Identity("things/first", "things", "First", {"topic": ["first"]}))
    done = cerebro.run_once()
    assert done.ran == {} and "things/article on things/first" in done.failed, (done.ran, done.failed)
    assert "run_sub" in done.failed["things/article on things/first"]


def test_the_progress_screen_ends_a_row_that_ran_and_wrote_nothing_quiet_with_its_words() -> None:
    import io

    from cerebro.output import Printer
    from cerebro.progress import LiveProgress
    from cerebro.runner import Progress

    out = io.StringIO()
    screen = LiveProgress(Printer(out, io.StringIO(), terminal=lambda stream: False, width=lambda: 200),
                          now=lambda: 1.0, ticking=False)
    screen.begin()
    screen(Progress("compile", "open", total=1))
    screen(Progress("compile", "start", "things/first"))
    screen(Progress("compile", "end", "things/first", mark="ran", text=f"ran and wrote nothing: {DECLINED}"))
    screen.finish()
    row = [line for line in out.getvalue().splitlines() if "things/first" in line][-1]  # the frame drawn last
    assert row.lstrip().startswith("[-] first") and f"ran and wrote nothing: {DECLINED}" in row, row
