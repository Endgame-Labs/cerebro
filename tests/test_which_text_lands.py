"""Which text lands at the end of a run: each test runs the case on a scripted model. Fakes only."""

from __future__ import annotations

from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from suites.agent_run import AGENT, EVIDENCE

from cerebro.agent import Budget
from cerebro.run import PydanticRun, hand_in

ARTICLE = "## Summary\n\nThe review moved to March [^f_1].\n\n## Next\n\nWatch March [^f_1]."


def lookup(id: str) -> str:
    """Look one thing up."""
    return f"looked up {id}"


def _run(*replies: list[Any], budget: Budget | None = None) -> Any:
    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=replies[min(sum(isinstance(m, ModelResponse) for m in messages), len(replies) - 1)])

    return PydanticRun(FunctionModel(reply, model_name="scripted")).run(AGENT, EVIDENCE, [lookup], budget or Budget())


def test_text_beside_a_tool_call_in_the_turn_a_spent_run_ends_on_is_taken() -> None:
    look = ToolCallPart("lookup", {"id": "memo-1"})
    run = _run([look], [TextPart(ARTICLE), ToolCallPart("lookup", {"id": "memo-2"})], [TextPart("Done.")],
               budget=Budget(tool_calls=1))
    assert run.result is not None and run.result.text == ARTICLE, (run.status, run.note)


def test_a_later_hand_in_replaces_the_whole_article() -> None:
    later = "## Next\n\nWatch March closely [^f_1]."
    run = _run([TextPart(ARTICLE)], [ToolCallPart(hand_in(AGENT), {"text": later})])
    assert run.result is not None and run.result.text == later, (run.status, run.note)
