"""Which text a run lands when its model writes in plain text: the model marks its article by writing the headings it
was given, and code only reads that mark. A candidate is a hand-in, or a reply that calls no tool and holds text
under a heading the agent lists; the run lands its latest, and with none it lands nothing and leaves the article as
it was."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from suites.agent_run import AGENT, EVIDENCE, citing_nothing

from cerebro.agent import Agent, Budget
from cerebro.run import REVIEW, UNMARKED, WROTE_NOTHING, PydanticRun, hand_in

ARTICLE = "## Summary\n\nThe review moved to March [^f_1].\n\n## Next\n\nWatch March [^f_1]."
REMARK = "The evidence holds nothing new about this article."


def _scripted(*replies: Any) -> FunctionModel:
    """A model whose n-th reply, 0 first, is `replies[n]` (the last one every turn after): a string is a reply in
    plain text, a list its parts, and an exception is raised in place of the reply."""

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        said = replies[min(sum(isinstance(m, ModelResponse) for m in messages), len(replies) - 1)]
        if isinstance(said, Exception):
            raise said
        return ModelResponse(parts=[TextPart(said)] if isinstance(said, str) else list(said))

    return FunctionModel(reply, model_name="scripted")


def _hand(text: str, agent: Agent = AGENT) -> list[Any]:
    return [ToolCallPart(hand_in(agent), {"text": text})]


def _run(*replies: Any, agent: Agent = AGENT, budget: Budget | None = None) -> Any:
    """A run on `replies`, its first hand-in shown to a stand-in editor that has something to say of one citing
    nothing."""
    reviewing = REVIEW.set(citing_nothing)
    try:
        return PydanticRun(_scripted(*replies)).run(agent, EVIDENCE, [lookup], budget or Budget())
    finally:
        REVIEW.reset(reviewing)


def lookup(id: str) -> str:
    """Look one thing up."""
    return f"looked up {id}"


def _text(run: Any) -> str | None:
    return run.result.text if run.result is not None else None


# -- a remark to the person is never the article and never replaces one


def test_a_remark_then_done_takes_nothing() -> None:
    run = _run(REMARK, "Done.")
    assert run.result is None and run.status == WROTE_NOTHING, (run.status, _text(run))


class _Hands:
    at = datetime(2026, 11, 17, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


def _workspace(tmp_path: Path, script: Any) -> tuple[Any, _Hands, list[int], Path, Any]:
    """One writer of two headings over one note, run through real passes on a scripted model. `script(n, fact,
    phase)` gives the parts of the n-th reply of a run, or raises."""
    from cerebro import Cerebro, Record
    from cerebro.article import Identity
    from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore

    calls: list[int] = []
    phase = [1]

    class Scripted(PydanticRun):
        def run(self, agent: Agent, evidence: Any, tools: Any, budget: Budget) -> Any:
            fact = next(c.id for e in evidence for c in e.citations if c.kind == "fact")
            calls.append(0)

            def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                calls[-1] += 1
                return ModelResponse(parts=script(sum(isinstance(m, ModelResponse) for m in messages), fact,
                                                  phase[0]))

            return PydanticRun(FunctionModel(reply, model_name="scripted")).run(agent, evidence, tools, budget)

    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: notes }\n---\n\nWrite about the thing.\n\n# Output\n\n"
        "## Summary\n\n## Next\n")
    notes = FakeConnector("notes", [Record(id="n0", source="notes", revision="1", at="2026-11-17T08:00:00Z",
                                           body="The first note says the launch moved.", keys={"topic": ["first"]})])
    hands = _Hands()
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands, connectors=[notes],
                      agent_run=Scripted("unused"), trusted=True)
    cerebro.store.create(Identity("things/first", "things", "First", {"topic": ["first"]}))
    return cerebro, hands, calls, tmp_path / "articles" / "things" / "first.md", (notes, phase)


def test_in_a_pass_a_remark_leaves_the_article_byte_for_byte(tmp_path: Path) -> None:
    from cerebro import Record

    def script(n: int, fact: str, phase: int) -> list[Any]:
        if phase == 1:
            return _hand(f"## Summary\n\nThe first note says the launch moved [^{fact}].\n")
        return [TextPart(REMARK if n == 0 else "Done.")]

    cerebro, hands, _calls, article, (notes, phase) = _workspace(tmp_path, script)
    cerebro.run_once()
    before = article.read_bytes()
    assert b"launch moved" in before
    phase[0] = 2
    notes.add(Record(id="n1", source="notes", revision="1", at="2026-11-17T10:00:00Z",
                     body="The second note says the team met.", keys={"topic": ["first"]}))
    hands.at += timedelta(hours=2)
    second = cerebro.run_once()
    assert article.read_bytes() == before, article.read_text()
    assert UNMARKED in second.ran.get("things/article on things/first", ""), second.ran  # counted ran, not written
    assert not second.compiled, second.compiled


# -- the answer to the ask does not cost the article written before it


def test_an_article_then_nothing_to_write_lands_the_article() -> None:
    run = _run(ARTICLE, [ToolCallPart("nothing_to_write", {"reason": "Nothing new since my last article."})])
    assert run.status == "done" and _text(run) == ARTICLE, (run.status, run.note)


def test_in_a_pass_the_article_lands_and_waits_for_an_input(tmp_path: Path) -> None:
    def script(n: int, fact: str, phase: int) -> list[Any]:
        if n == 0:
            return [TextPart(f"## Summary\n\nThe first note says the launch moved [^{fact}].\n")]
        return [ToolCallPart("nothing_to_write", {"reason": "Nothing new since my last article."})]

    cerebro, hands, calls, article, _ = _workspace(tmp_path, script)
    first = cerebro.run_once()
    assert first.compiled == ["things/first"] and "launch moved" in article.read_text(), first
    hands.at += timedelta(hours=2)
    cerebro.run_once()
    assert len(calls) == 1


# -- a line said on the way to the article gives way to the article


@pytest.mark.parametrize("first", ["done", "Let me look at the evidence first.",
                                   "I could not find anything new about this in the evidence."])
def test_a_line_then_the_article_lands_the_article(first: str) -> None:
    run = _run(first, ARTICLE)
    assert run.status == "done" and _text(run) == ARTICLE, (run.status, _text(run))


def test_a_preamble_a_lookup_then_the_article_lands_the_article() -> None:
    run = _run("Let me check the records first.", [ToolCallPart("lookup", {"id": "memo-1"})], ARTICLE)
    assert run.status == "done" and _text(run) == ARTICLE, (run.status, _text(run))


# -- a held hand-in gives way to a later candidate; a remark handed in never replaces one


def test_a_held_hand_in_then_the_article_lands_the_article() -> None:
    run = _run(_hand("The review moved to March."), ARTICLE)
    assert run.status == "done" and _text(run) == ARTICLE, (run.status, _text(run))


def test_the_article_then_a_line_handed_in_lands_the_article() -> None:
    run = _run(ARTICLE, _hand("Handed in above."))
    assert run.status == "done" and _text(run) == ARTICLE, (run.status, _text(run))


def test_a_hand_in_with_listed_headings_after_the_article_lands_as_the_latest() -> None:
    later = "## Summary\n\nThe review moved."
    run = _run(ARTICLE, _hand(later))
    assert _text(run) == later, (run.status, _text(run))


# -- an error on any request after a held hand-in fails the run


# -- a curator spent or cut with nothing decided ends with nothing new and waits


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
            def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                calls.append(agent.id)
                return make(sum(isinstance(m, ModelResponse) for m in messages))

            return PydanticRun(FunctionModel(reply, model_name="scripted")).run(agent, evidence, tools, budget_)

    hands = _Hands()
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands, connectors=[notes],
                      agent_run=Counting("unused"), trusted=True)
    return cerebro, calls, hands


def _per_pass(cerebro: Any, calls: list[str], hands: _Hands, passes: int = 5) -> list[int]:
    per = []
    for _ in range(passes):
        before = len(calls)
        cerebro.run_once()
        per.append(len(calls) - before)
        hands.at += timedelta(hours=2)
    return per


@pytest.mark.parametrize("parts", [[TextPart("I will admit Thi")], []], ids=["with-text", "empty"])
def test_a_curator_cut_with_nothing_decided_waits_for_an_input(tmp_path: Path, parts: list[Any]) -> None:
    cerebro, calls, hands = _curated(tmp_path, lambda n: ModelResponse(parts=list(parts), finish_reason="length"))
    per = _per_pass(cerebro, calls, hands)
    assert per[0] > 0 and per[2:] == [0, 0, 0], per  # its second pass reads the facts the first linked
    statuses = {row.data["status"] for row in cerebro.store.rows("agent-runs", {})}
    assert "failed" not in statuses, statuses


@pytest.mark.parametrize("last", [[TextPart("I ran out of room to decide.")], []], ids=["with-text", "empty"])
def test_a_curator_whose_calls_were_spent_with_nothing_decided_waits_for_an_input(
        tmp_path: Path, last: list[Any]) -> None:
    cerebro, calls, hands = _curated(
        tmp_path, lambda n: ModelResponse(parts=[ToolCallPart("query", {"source": "notes"})] if n == 0 else list(last)),
        budget="budget: { tool_calls: 1 }\n")
    per = _per_pass(cerebro, calls, hands)
    assert per[0] >= 2 and per[2:] == [0, 0, 0], per
    statuses = {row.data["status"] for row in cerebro.store.rows("agent-runs", {})}
    assert "failed" not in statuses, statuses


def test_a_curator_run_that_spent_its_call_with_nothing_decided_ends_with_nothing_new() -> None:
    curator = replace(AGENT, id="things/curator", role="curator", output=[], prompt="Keep the things.")
    for last in ([TextPart("I ran out of room to decide.")], []):
        run = PydanticRun(_scripted([ToolCallPart("lookup", {"id": "memo-1"})], last)).run(
            curator, EVIDENCE, [lookup], Budget(tool_calls=1))
        assert run.status in ("done", WROTE_NOTHING) and run.result is None, (run.status, run.note)


# -- the transcript keeps what the run told the model


def test_the_transcript_keeps_the_ask_and_what_the_editor_said_of_the_first_hand_in() -> None:
    from cerebro.run import unwritten

    run = _run(ARTICLE, _hand("The review moved to March."), _hand(ARTICLE))
    told = [e.text for e in run.transcript if e.role == "tool"]
    assert any(unwritten(AGENT).split(". ")[0] in text for text in told), told
    assert any(citing_nothing("").split(": ")[0] in text for text in told), told


# -- a hand-in that cites, under none of the listed headings, gives way to the article written under them


@pytest.mark.parametrize("late", ["Handed in above [^f_1].", "## Elsewhere\n\nThe review moved [^f_1]."],
                         ids=["no-heading", "unlisted-heading"])
def test_a_cited_hand_in_under_no_listed_heading_gives_way_to_the_article(late: str) -> None:
    run = _run(ARTICLE, _hand(late))
    assert run.status == "done" and _text(run) == ARTICLE, (run.status, _text(run))


def test_a_cited_hand_in_under_no_listed_heading_lands_when_there_is_no_article() -> None:
    late = "## Elsewhere\n\nThe review moved [^f_1]."
    run = _run(_hand(late))
    assert run.status == "done" and _text(run) == late, (run.status, _text(run))


def test_an_agent_that_names_no_heading_lands_its_cited_hand_in_after_a_reply() -> None:
    whole = replace(AGENT, output=[], prompt="Write the weekly report in your own words.")
    run = _run(ARTICLE, _hand("Handed in above [^f_1].", whole), agent=whole)
    assert run.status == "done" and _text(run) == "Handed in above [^f_1].", (run.status, _text(run))


def test_in_a_pass_a_cited_line_handed_in_after_the_article_leaves_the_article(tmp_path: Path) -> None:
    from cerebro import Record

    def script(n: int, fact: str, phase: int) -> list[Any]:
        if phase == 1:
            return _hand(f"## Summary\n\nThe first note says the launch moved [^{fact}].")
        if n == 0:
            return [TextPart(f"## Summary\n\nThe second note says the team met [^{fact}].")]
        return _hand(f"Handed in above [^{fact}].")

    cerebro, hands, _calls, article, (notes, phase) = _workspace(tmp_path, script)
    cerebro.run_once()
    phase[0] = 2
    notes.add(Record(id="n1", source="notes", revision="1", at="2026-11-17T10:00:00Z",
                     body="The second note says the team met.", keys={"topic": ["first"]}))
    hands.at += timedelta(hours=2)
    cerebro.run_once()
    assert "team met" in article.read_text(), article.read_text()


def test_a_curator_whose_reply_is_empty_says_so_in_a_curators_words() -> None:
    curator = replace(AGENT, id="things/curator", role="curator", output=[], prompt="Keep the things.")
    run = PydanticRun(_scripted("")).run(curator, EVIDENCE, [lookup], Budget())
    assert run.status == WROTE_NOTHING and run.result is None, (run.status, run.note)
    assert "hand" not in run.note and run.note == "it ended with an empty reply", run.note
