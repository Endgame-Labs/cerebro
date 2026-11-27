"""Which reply a run takes, as a table: for each sequence of replies from a scripted model, what `PydanticRun` lands
and how the run ends, for a writer of two headings, a whole-body agent, a subagent, a composer and a curator. Each
row is one break the run's endings could make: a reply taken that should not be, or one lost that should land.

Forms: `H` text under the listed headings, cited; `U` text under no heading; `X` under a heading the file does not
list; `S` `done`; `R` "I could not find anything new about this in the evidence."; `A` a remark grounding would keep
("The evidence holds nothing new about this article."); `P` a line on the way ("Let me check the records first.");
`E` an empty reply; `C` a reply cut for length, its text under Summary; `CU` cut, under no heading; `CE` cut and
empty; `T` a tool call; `RT` an unknown tool beside text; `HI` a hand-in that cites; `HG` a hand-in that cites nothing,
under no heading, of which the stand-in editor has something to say; `HGH` one that cites nothing, under Summary; `N`
nothing to write; an error by its name, `Refused` one that refuses the key. The last
form repeats on every turn after. Limits: `calls` one tool call, spent by the first `T`; `room@1` the opening alone
closes the room for tools; `room@2` the first reply's tokens close it; `budget@2` the first reply's tokens spend the
whole budget."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from suites.agent_run import AGENT, EVIDENCE, citing_nothing

from cerebro.agent import Agent, Budget
from cerebro.budget import RoundCut
from cerebro.run import REVIEW, WROTE_NOTHING, PydanticRun, hand_in, instructions, opening, tokens

SUB = replace(AGENT, id="things/subagents/risks", path=Path("agents/things/subagents/risks.md"), role="subagent",
              target="things", prompt="Name the risks.", output=["Risks"])
COMPOSER = replace(AGENT, id="things/article", path=Path("agents/things/article.md"), role="composer",
                   target="things", subagents=["risks"], output=[])
CURATOR = replace(AGENT, id="things/curator", path=Path("agents/things/curator.md"), role="curator", target="",
                  output=[], prompt="Keep the things worth an article.")
WHOLE = replace(AGENT, output=[], prompt="Write the weekly report in your own words.")
AGENTS = {"writer": AGENT, "subagent": SUB, "composer": COMPOSER, "curator": CURATOR, "whole": WHOLE}

ART = "## Summary\n\nThe review moved to March [^f_1].\n\n## Next\n\nWatch March [^f_1]."
RISKS = "## Risks\n\nThe review moved to March [^f_1]."
U = "The review moved to March [^f_1]."
CUT = "## Summary\n\nThe review moved to March [^f_1]. The te"
HG = "The review moved to March."
HGH = "## Summary\n\nHanded in above."
NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)


class Refused(Exception):
    status_code = 401  # a provider's answer that refuses the key


def lookup(id: str) -> str:
    """Look one thing up."""
    return f"looked up {id}"


def _parts(form: str, agent: Agent) -> tuple[list[Any], str]:
    """The parts of a reply of `form` for `agent`, and its finish reason, or raise its error."""
    text = {"H": RISKS if agent.role == "subagent" else ART, "U": U, "X": "## Elsewhere\n\n" + U, "S": "done",
            "R": "I could not find anything new about this in the evidence.",
            "A": "The evidence holds nothing new about this article.", "P": "Let me check the records first."}
    if form in text:
        return [TextPart(text[form])], "stop"
    cited = U if agent.role == "subagent" else "## Summary\n\n" + U
    given = {"E": ([], "stop"), "C": ([TextPart(CUT)], "length"), "CU": ([TextPart("I will admit Thi")], "length"),
             "CE": ([], "length"), "T": ([ToolCallPart("lookup", {"id": "memo-1"})], "stop"),
             "RT": ([TextPart("Let me look."), ToolCallPart("no_such_tool", {})], "stop"),
             "HI": ([ToolCallPart(hand_in(agent), {"text": cited})], "stop"),
             "HG": ([ToolCallPart(hand_in(agent), {"text": HG})], "stop"),
             "HGH": ([ToolCallPart(hand_in(agent), {"text": HGH})], "stop"),
             "N": ([ToolCallPart("nothing_to_write", {"reason": "Nothing new since my last article."})], "stop")}
    if form in given:
        return given[form]
    raise {"UnexpectedModelBehavior": UnexpectedModelBehavior("Invalid response from the provider"),
           "ContentFilterError": ContentFilterError("filtered", body="{}"),
           "ConnectError": httpx.ConnectError("connection reset"), "RoundCut": RoundCut("the round's cap was reached"),
           "Refused": Refused("the provider refused the key")}[form]


def run(role: str, replies: str, limit: str = "none") -> Any:
    """Run `role`'s agent on the replies, `,` between them, under `limit`."""
    agent, forms = AGENTS[role], replies.split(",")
    budget = Budget(tool_calls=1) if limit == "calls" else Budget()
    if limit == "room@1":  # a budget the opening alone closes to tools, though it is not spent
        held = tokens(instructions(agent) + opening(agent, EVIDENCE, NOW))
        budget = Budget(tokens_per_run=held + 10)
    first = {"room@2": int(budget.tokens_per_run * 0.85), "budget@2": budget.tokens_per_run}.get(limit, 0)

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        n = sum(isinstance(m, ModelResponse) for m in messages)
        parts, finish = _parts(forms[min(n, len(forms) - 1)], agent)
        return ModelResponse(parts=parts, finish_reason=finish,  # type: ignore[arg-type]
                             usage=RequestUsage(output_tokens=first if n == 0 else 0))

    reviewing = REVIEW.set(citing_nothing)
    try:
        return PydanticRun(FunctionModel(reply, model_name="scripted"), clock=lambda: NOW).run(
            agent, EVIDENCE, [lookup], budget)
    finally:
        REVIEW.reset(reviewing)


@dataclass
class Row:
    role: str
    replies: str
    limit: str
    status: str  # how the run ends
    takes: str | None  # the text it lands, or None


def _sub(text: str) -> str:
    return text.split("\n\n", 1)[1]


ROWS = [
    # a writer of two headings, a normal end
    Row("writer", "H", "none", "done", ART),
    Row("writer", "U", "none", "failed", None),
    Row("writer", "X", "none", "failed", None),
    Row("writer", "S", "none", "failed", None),
    Row("writer", "R", "none", "failed", None),
    Row("writer", "A", "none", "failed", None),
    Row("writer", "A,S", "none", "failed", None),
    Row("writer", "E", "none", "failed", None),
    Row("writer", "C", "none", "done", CUT),
    Row("writer", "HI", "none", "done", "## Summary\n\n" + U),
    Row("writer", "HG", "none", "done", HG),
    Row("writer", "N", "none", WROTE_NOTHING, None),
    Row("writer", "RT", "none", "failed", None),
    Row("writer", "T,H", "none", "done", ART),
    Row("writer", "H,S", "none", "done", ART),
    Row("writer", "H,E", "none", "done", ART),
    Row("writer", "H,C", "none", "done", ART),
    Row("writer", "H,N", "none", "done", ART),
    Row("writer", "H,HG", "none", "done", ART),
    Row("writer", "H,HGH", "none", "done", HGH),
    Row("writer", "S,H", "none", "done", ART),
    Row("writer", "R,H", "none", "done", ART),
    Row("writer", "P,T,H,H", "none", "done", ART),
    Row("writer", "E,H", "none", "done", ART),
    Row("writer", "HG,H", "none", "done", ART),
    Row("writer", "HG,HG", "none", "done", HG),
    Row("writer", "U,S", "none", "failed", None),
    Row("writer", "X,S", "none", "failed", None),
    # a writer, spent or cut
    Row("writer", "E", "room@1", "failed", None),
    Row("writer", "U", "room@1", "failed", None),
    Row("writer", "T,U", "room@2", "failed", None),
    Row("writer", "T,S", "room@2", "failed", None),
    Row("writer", "T,H", "room@2", "done", ART),
    Row("writer", "T,U", "calls", "failed", None),
    Row("writer", "T,H", "calls", "done", ART),
    Row("writer", "T,E", "calls", "failed", None),
    Row("writer", "T,H", "budget@2", "budget spent", None),
    # errors
    Row("writer", "UnexpectedModelBehavior", "none", "failed", None),
    Row("writer", "HG,UnexpectedModelBehavior", "none", "done", HG),
    Row("writer", "HG,ConnectError", "none", "done", HG),
    Row("writer", "HG,ContentFilterError", "none", "done", HG),
    Row("writer", "HG,RoundCut", "none", "done", HG),
    Row("writer", "HG,Refused", "none", "done", HG),
    Row("writer", "HG,H,ConnectError", "none", "done", ART),
    Row("writer", "ContentFilterError", "none", "failed", None),
    # an agent whose file names no heading: a whole-body agent
    Row("whole", "U", "none", "failed", None),
    Row("whole", "H", "none", "failed", None),
    Row("whole", "S,U", "none", "failed", None),
    Row("whole", "HI", "none", "done", "## Summary\n\n" + U),
    Row("whole", "T,U", "calls", "done", U),
    Row("whole", "T,S", "calls", "done", "done"),
    # a subagent: its heading is its one
    Row("subagent", "H", "none", "done", _sub(RISKS)),
    Row("subagent", "U", "none", "failed", None),
    Row("subagent", "X", "none", "failed", None),
    Row("subagent", "S", "none", "failed", None),
    Row("subagent", "H,N", "none", "done", _sub(RISKS)),
    Row("subagent", "S,H", "none", "done", _sub(RISKS)),
    Row("subagent", "T,U", "calls", "done", U),
    Row("subagent", "HI", "none", "done", U),
    # a composer, its subagents' content in its evidence
    Row("composer", "H", "none", WROTE_NOTHING, None),
    Row("composer", "U", "none", WROTE_NOTHING, None),
    Row("composer", "E", "none", WROTE_NOTHING, None),
    Row("composer", "HI", "none", "done", "## Summary\n\n" + U),
    Row("composer", "HG", "none", "done", HG),
    Row("composer", "RT", "none", "failed", None),
    # a curator
    Row("curator", "S", "none", "done", None),
    Row("curator", "E", "none", WROTE_NOTHING, None),
    Row("curator", "CU", "none", "done", None),
    Row("curator", "CE", "none", "done", None),
    Row("curator", "T,E", "calls", "done", None),
    Row("curator", "T,S", "calls", "done", None),
    Row("curator", "T", "budget@2", "budget spent", None),
]


@pytest.mark.parametrize("row", ROWS, ids=[f"{r.role}:{r.replies}:{r.limit}" for r in ROWS])
def test_the_reply_table(row: Row) -> None:
    logging.disable(logging.CRITICAL)
    try:
        got = run(row.role, row.replies, row.limit)
    finally:
        logging.disable(logging.NOTSET)
    taken = got.result.text if got.result is not None else None
    assert (got.status, taken) == (row.status, row.takes), (got.status, taken, got.note)
