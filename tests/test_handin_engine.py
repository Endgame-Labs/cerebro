"""What a run takes when the extra ask to hand in brings no hand-in: its latest reply that holds text under a heading
the agent was given, so the ask never loses an article written before it. A request of the ask that errors fails the
run. Whether an ending is normal or spent is read from the reply before the ask. A heading written twice keeps the text
under it that is not blank. A curator that ends, normally or spent or cut, with nothing decided is not asked again
until an input moves."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from suites.agent_run import AGENT, EVIDENCE

from cerebro.agent import Agent, Budget
from cerebro.run import (
    DECIDED_CUT,
    DECIDED_SPENT,
    HANDED_NOTHING,
    SPENT_NOTHING,
    STANDS,
    TOOK_REPLY,
    WROTE_NOTHING,
    PydanticRun,
)

ARTICLE = "## Summary\n\nThe review moved to March [^f_1]."
SHORT = "I wrote it in my reply above."
SUB = replace(AGENT, id="things/subagents/risks", path=Path("agents/things/subagents/risks.md"), role="subagent",
              target="things", prompt="Name the risks.", output=["Risks"])
COMPOSER = replace(AGENT, id="things/article", path=Path("agents/things/article.md"), role="composer",
                   target="things", subagents=["risks"], output=[])
SPENT = Budget(tool_calls=1)


def _each(make: Any) -> object:
    """A function model whose reply to its n-th request, 0 first, is `make(n)`."""
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return make(sum(isinstance(m, ModelResponse) for m in messages))

    return FunctionModel(reply, model_name="scripted")


def _said(*turns: str, cut_last: bool = False) -> object:
    """A model that answers each turn in plain text, `turns` in order, the last one every turn after."""
    from pydantic_ai.messages import ModelResponse, TextPart

    def make(n: int) -> ModelResponse:
        said = turns[min(n, len(turns) - 1)]
        cut = cut_last and n >= len(turns) - 1
        return ModelResponse(parts=[TextPart(said)] if said else [], finish_reason="length" if cut else "stop")

    return _each(make)


def lookup(id: str) -> str:
    """Look one thing up."""
    return f"looked up {id}"


def _taken(run: Any) -> str | None:
    return run.result.text if run.result else None


# -- the reply before the ask is taken


def test_text_then_a_short_line_takes_the_text_before_the_ask() -> None:
    run = PydanticRun(_said(ARTICLE, SHORT)).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == "done" and _taken(run) == ARTICLE and run.note == TOOK_REPLY


def test_text_then_an_empty_answer_or_a_cut_one_takes_the_text_before_the_ask() -> None:
    for model in (_said(ARTICLE, ""), _said(ARTICLE, "Sorry, I was cu", cut_last=True)):
        run = PydanticRun(model).run(AGENT, EVIDENCE, [], Budget())
        assert run.status == "done" and _taken(run) == ARTICLE, (run.status, run.note)


def test_unheaded_text_before_the_ask_is_not_taken_for_a_writer_or_a_subagent() -> None:
    said = "The review moved to March [^f_1]."
    run = PydanticRun(_said(said, "Done.")).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == WROTE_NOTHING and run.result is None
    sub = PydanticRun(_said(said, "Done.")).run(SUB, EVIDENCE, [], Budget())
    assert sub.status == WROTE_NOTHING and sub.result is None
    headed = PydanticRun(_said("## Other\n\n" + said, "Done.")).run(SUB, EVIDENCE, [], Budget())
    assert headed.result is None
    own = PydanticRun(_said("## Risks\n\n" + said, "Done.")).run(SUB, EVIDENCE, [], Budget())
    assert _taken(own) == said


def test_a_composer_takes_nothing_from_its_reply_before_the_ask_and_its_subagents_content_stands() -> None:
    run = PydanticRun(_said(ARTICLE, "Done.")).run(COMPOSER, EVIDENCE, [], Budget())
    assert run.status == WROTE_NOTHING and run.result is None and run.note == STANDS


def test_calls_spent_then_the_article_then_a_short_line_takes_the_article() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

    run = PydanticRun(_each(lambda n: ModelResponse(parts=[ToolCallPart("lookup", {"id": "n1"})]) if n == 0 else
                            ModelResponse(parts=[TextPart(ARTICLE if n == 1 else SHORT)]))).run(
        AGENT, EVIDENCE, [lookup], SPENT)
    assert run.status == "done" and _taken(run) == ARTICLE


def test_empty_then_empty_ran_and_wrote_nothing() -> None:
    run = PydanticRun(_said("")).run(AGENT, EVIDENCE, [], Budget())
    assert run.status == WROTE_NOTHING and run.result is None and run.note == HANDED_NOTHING


# -- a request of the ask that errors fails the run


def test_a_model_error_or_a_transport_error_on_the_ask_fails_the_run() -> None:
    import httpx
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.messages import ModelResponse, TextPart

    for error in (UnexpectedModelBehavior("Invalid response from the provider"), httpx.ConnectError("reset")):
        def make(n: int, error: Exception = error) -> ModelResponse:
            if n == 0:
                return ModelResponse(parts=[TextPart("Let me think about the review first.")])
            raise error

        run = PydanticRun(_each(make)).run(AGENT, EVIDENCE, [], Budget())
        assert run.status == "failed" and run.result is None and run.note.startswith(type(error).__name__), run.note


# -- the ask never turns a normal end into a spent one


def test_a_long_first_reply_that_uses_the_room_is_a_normal_end_and_is_taken() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.usage import RequestUsage

    big = int(Budget().tokens_per_run * 0.85)
    first = "## Summary\n\nThinking aloud about the review [^f_1], at length."
    for second in ("", "Nothing new: the review moved to March [^f_1]."):
        run = PydanticRun(_each(lambda n, second=second: ModelResponse(parts=[TextPart(first)],
                                                                      usage=RequestUsage(output_tokens=big)) if n == 0
                                else ModelResponse(parts=[TextPart(second)] if second else []))).run(
            AGENT, EVIDENCE, [lookup], Budget())
        assert run.status == "done" and _taken(run) == first, (second, run.status, run.note)


def test_a_first_reply_that_holds_the_whole_budget_is_taken_though_the_budget_stops_the_ask() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.usage import RequestUsage

    whole = Budget().tokens_per_run
    run = PydanticRun(_each(lambda n: ModelResponse(parts=[TextPart(ARTICLE)], usage=RequestUsage(output_tokens=whole))
                            )).run(AGENT, EVIDENCE, [lookup], Budget())
    assert run.status == "done" and _taken(run) == ARTICLE and run.usage.requests == 1, (run.status, run.note)


def test_a_spent_ending_with_nothing_in_the_reply_before_the_ask_still_fails() -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    run = PydanticRun(_each(lambda n: ModelResponse(parts=[ToolCallPart("lookup", {"id": "n1"})]) if n == 0 else
                            ModelResponse(parts=[]))).run(AGENT, EVIDENCE, [lookup], SPENT)
    assert run.status == "failed" and run.note == SPENT_NOTHING


# -- a heading written twice


def test_a_heading_written_twice_keeps_the_text_that_is_not_blank_and_the_last_of_two() -> None:
    from cerebro.run import _headed

    assert _headed(AGENT, "## Summary\n\nThe review moved [^f_1].\n\n## Summary\n") == \
        "## Summary\n\nThe review moved [^f_1]."
    assert _headed(AGENT, "## Summary\n\nfirst\n\n## Next\n\nsoon\n\n## Summary\n\nsecond\n") == \
        "## Summary\n\nsecond\n\n## Next\n\nsoon"


# -- a curator


CURATOR = replace(AGENT, id="things/curator", path=Path("agents/things/curator.md"), role="curator", target="",
                  output=[])


def test_a_curators_empty_reply_cut_at_its_output_limit_ends_with_what_it_decided_and_never_claims_an_ask() -> None:
    from pydantic_ai.messages import ModelResponse

    run = PydanticRun(_each(lambda n: ModelResponse(parts=[], finish_reason="length"))).run(
        CURATOR, EVIDENCE, [], Budget())
    assert run.status == "done" and run.note == DECIDED_CUT  # nothing new


class _Hands:
    at = datetime(2026, 11, 17, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


def _curated(tmp_path: Path, make: Any, budget: str = "") -> tuple[Any, list[str], _Hands]:
    from cerebro import Cerebro, Record
    from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore

    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things" / "curator.md").write_text(
        f"---\nevidence:\n  - query: {{ from: facts, source: notes }}\n{budget}---\n\n"
        "Keep the things worth an article.\n")
    notes = FakeConnector("notes", [Record(id="n0", source="notes", revision="1", at="2026-11-17T08:00:00Z",
                                           body="The first note.", keys={"topic": ["first"]})])
    calls: list[str] = []

    class Counting(PydanticRun):
        def run(self, agent: Agent, evidence: Any, tools: Any, budget_: Budget) -> Any:
            def counted(n: int) -> Any:
                calls.append(agent.id)
                return make(n)

            return PydanticRun(_each(counted)).run(agent, evidence, tools, budget_)

    hands = _Hands()
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands, connectors=[notes],
                      agent_run=Counting("unused"))
    return cerebro, calls, hands


def test_a_curator_that_ends_normally_with_an_empty_reply_is_not_asked_again_until_an_input_moves(
        tmp_path: Path) -> None:
    from pydantic_ai.messages import ModelResponse, TextPart

    for said in ("Nothing to decide.", ""):
        cerebro, calls, hands = _curated(tmp_path / str(len(said)),
                                         lambda n, said=said: ModelResponse(parts=[TextPart(said)] if said else []))
        per = []
        for _ in range(5):
            before = len(calls)
            cerebro.run_once()
            per.append(len(calls) - before)
            hands.at += timedelta(hours=2)
        assert per[0] > 0 and per[2:] == [0, 0, 0], (said, per)  # its second pass reads the facts the first linked


def test_a_curator_whose_calls_were_spent_with_nothing_decided_waits_until_an_input_moves(tmp_path: Path) -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    cerebro, calls, hands = _curated(
        tmp_path, lambda n: ModelResponse(parts=[ToolCallPart("query", {"source": "notes"})] if n == 0 else []),
        budget="budget: { tool_calls: 1 }\n")
    per = []
    for _ in range(4):
        before = len(calls)
        cerebro.run_once()
        per.append(len(calls) - before)
        hands.at += timedelta(hours=2)
    runs = [row.data for row in cerebro.store.rows("agent-runs", {})]
    # its one call ran and spent the budget, so its last turn offered no tools: it ended spent, with nothing decided
    assert runs and all(r["status"] == "done" and r["note"] == DECIDED_SPENT for r in runs), runs
    assert per[0] >= 2 and per[2:] == [0, 0], per  # its second pass reads the facts the first linked


def test_a_curator_whose_room_was_spent_with_nothing_decided_waits_until_an_input_moves(tmp_path: Path) -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.usage import RequestUsage

    whole = Budget().tokens_per_run
    cerebro, calls, hands = _curated(tmp_path, lambda n: ModelResponse(
        parts=[ToolCallPart("query", {"source": "notes"})], usage=RequestUsage(output_tokens=whole)))
    per = []
    for _ in range(4):
        before = len(calls)
        cerebro.run_once()
        per.append(len(calls) - before)
        hands.at += timedelta(hours=2)
    runs = [row.data for row in cerebro.store.rows("agent-runs", {})]
    assert runs and all(r["status"] == "budget spent" for r in runs), runs
    assert per[0] >= 1 and per[2:] == [0, 0], per


# -- in a pass


def test_a_pass_lands_the_text_before_the_ask_and_fails_a_run_whose_ask_errored(tmp_path: Path) -> None:
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.messages import ModelResponse, TextPart

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
    errs = [True]

    class Counting(PydanticRun):
        def run(self, agent: Agent, evidence: Any, tools: Any, budget: Budget) -> Any:
            calls.append(agent.id)
            fact = next(c.id for e in evidence for c in e.citations if c.kind == "fact")

            def make(n: int) -> ModelResponse:
                if n == 0:
                    return ModelResponse(parts=[TextPart(f"## Summary\n\nThe first note [^{fact}].\n")])
                if errs[0]:
                    raise UnexpectedModelBehavior("Invalid response from the provider")
                return ModelResponse(parts=[TextPart(SHORT)])

            return PydanticRun(_each(make)).run(agent, evidence, tools, budget)

    hands = _Hands()
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands, connectors=[notes],
                      agent_run=Counting("unused"))
    cerebro.store.create(Identity("things/first", "things", "First", {"topic": ["first"]}))
    first = cerebro.run_once()
    assert "things/article on things/first" in first.failed and first.ran == {}
    errs[0] = False
    hands.at += timedelta(hours=2)  # past the wait after one failed run
    again = cerebro.run_once()
    assert len(calls) == 2 and again.compiled == ["things/first"] and again.failed == {}


# -- a budget spent before anything was written


def test_a_budget_spent_on_replies_that_held_no_text_is_budget_spent_and_never_wrote_nothing() -> None:
    from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart
    from pydantic_ai.usage import RequestUsage

    whole = Budget().tokens_per_run
    thought = _each(lambda n: ModelResponse(parts=[ThinkingPart("thinking long")], usage=RequestUsage(output_tokens=whole)))
    called = _each(lambda n: ModelResponse(parts=[ToolCallPart("lookup", {"id": "n1"})]) if n == 0 else
                   ModelResponse(parts=[], usage=RequestUsage(output_tokens=whole)))
    for model in (thought, called):
        run = PydanticRun(model).run(AGENT, EVIDENCE, [lookup], Budget())
        assert run.status == "budget spent" and run.result is None, (run.status, run.note)


# -- text beside a tool call in a turn that offered no tools


def test_a_line_beside_a_refused_call_gives_way_to_the_article_written_in_a_later_reply() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

    def make(n: int) -> ModelResponse:
        if n == 0:
            return ModelResponse(parts=[ToolCallPart("lookup", {"id": "n1"})])
        if n == 1:
            return ModelResponse(parts=[TextPart("Let me check one more thing."), ToolCallPart("lookup", {"id": "n2"})])
        return ModelResponse(parts=[TextPart(ARTICLE if n == 2 else "Done.")])

    run = PydanticRun(_each(make)).run(AGENT, EVIDENCE, [lookup], SPENT)
    assert run.status == "done" and _taken(run) == ARTICLE, (run.status, _taken(run))
