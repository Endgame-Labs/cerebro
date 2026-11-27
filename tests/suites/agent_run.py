"""The `AgentRun` suite. Fixture: `make_run`, which takes `write(prompt)`, the text to hand in or its parts as
`{heading: text}`, and, optionally, `calls`, a list of (tool name, arguments). It gives a run that makes those calls,
then hands in that text, and calls no model."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest

from cerebro.agent import Agent, Budget
from cerebro.apps import ToolError
from cerebro.article import Citation
from cerebro.evidence import Evidence
from cerebro.run import ROLES, RUN_ID, AgentRun, Entry

AGENT = Agent(id="reports/weekly", path=Path("agents/reports/weekly.md"), role="writer", target="reports/weekly",
              prompt="Write the weekly report.\n\n# Output\n\n## Summary\n\n## Next", output=["Summary", "Next"])
EVIDENCE = [Evidence("Query from facts, source notes", ["- [^f_1] The review moved to March."],
                     [Citation("fact", "f_1", "memo-1", "The review moved to March.")])]


def citing_nothing(text: str) -> str:
    """A stand-in editor: it has something to say of a hand-in that cites nothing, and nothing of any other."""
    return "" if "[^" in text else "An editor scored your text: no sentence of it cites the evidence."


def lookup_tool(id: str) -> str:
    """Look one thing up."""
    return f"looked up {id} in the notes"


def test_a_run_gives_the_text_it_handed_in_with_a_transcript_and_usage(make_run: Callable[..., AgentRun]) -> None:
    run = make_run(lambda prompt: {"Summary": "It moved [^f_1].", "Next": "Watch March."}).run(
        AGENT, EVIDENCE, [lookup_tool], Budget())
    assert run.status == "done" and run.result is not None
    assert run.result.text == "## Summary\n\nIt moved [^f_1].\n\n## Next\n\nWatch March."
    assert run.agent == "reports/weekly" and run.target == "reports/weekly" and run.id
    assert run.usage.requests >= 1 and run.usage.model


def test_a_run_takes_the_id_set_for_it_before_it_starts(make_run: Callable[..., AgentRun]) -> None:
    minted = RUN_ID.set("run_minted")  # as compile sets it, so what grounding spends on the run is kept under it
    try:
        run = make_run(lambda prompt: "x [^f_1]").run(AGENT, EVIDENCE, [], Budget())
    finally:
        RUN_ID.reset(minted)
    assert run.id == "run_minted"


def test_a_transcript_is_entries_the_run_given_first_and_each_call_answered(make_run: Callable[..., AgentRun]) -> None:
    run = make_run(lambda prompt: "It moved [^f_1].", calls=[("lookup_tool", {"id": "memo-7"})]).run(
        AGENT, EVIDENCE, [lookup_tool], Budget())
    assert run.transcript and all(isinstance(e, Entry) and e.role in ROLES for e in run.transcript)
    assert run.transcript[0].role == "user" and "[^f_1] The review moved to March." in run.transcript[0].text
    calls = [e for e in run.transcript if e.role == "call"]
    assert [(e.tool, e.args) for e in calls] == [("lookup_tool", {"id": "memo-7"}),
                                                 ("write_article", {"text": "It moved [^f_1]."})]
    assert next(e.tool for e in run.transcript if e.role == "tool") == "lookup_tool"


def test_a_run_sees_its_evidence(make_run: Callable[..., AgentRun]) -> None:
    seen: list[str] = []
    make_run(lambda prompt: seen.append(prompt) or {"Summary": "x"}).run(AGENT, EVIDENCE, [], Budget())
    assert "[^f_1] The review moved to March." in seen[0]
    assert "Query from facts, source notes" in seen[0]


def test_a_run_keeps_any_heading_the_agent_writes_since_its_output_is_guidance(
        make_run: Callable[..., AgentRun]) -> None:
    run = make_run(lambda prompt: {"Summary": "x [^f_1]", "Elsewhere": "y"}).run(AGENT, EVIDENCE, [], Budget())
    assert run.result is not None and run.result.text == "## Summary\n\nx [^f_1]\n\n## Elsewhere\n\ny"


def test_a_subagent_returns_its_content_with_no_heading_of_its_own(make_run: Callable[..., AgentRun]) -> None:
    sub = Agent(id="reports/subagents/risks", path=Path("agents/reports/subagents/risks.md"), role="subagent",
                target="reports", prompt="Draft the risks.", output=["Risks"])
    run = make_run(lambda prompt: {"Risks": "It slipped [^f_1]."}).run(sub, EVIDENCE, [], Budget())
    assert run.result is not None and run.result.text == "It slipped [^f_1]."
    assert [e.tool for e in run.transcript if e.role == "call"] == ["return_content"]


def test_a_run_is_plain_data(make_run: Callable[..., AgentRun]) -> None:
    run = make_run(lambda prompt: "x [^f_1]").run(AGENT, EVIDENCE, [lookup_tool], Budget())
    assert json.loads(json.dumps(asdict(run)))["result"] == {"text": "x [^f_1]", "files": []}


@pytest.mark.parametrize("name", ["write_article", "return_content", "nothing_to_write"])
def test_a_tool_named_like_the_runs_own_hand_in_is_refused(make_run: Callable[..., AgentRun], name: str) -> None:
    def shadow(text: str) -> str:
        """Shadow the run's own hand-in."""
        return ""

    shadow.__name__ = name
    with pytest.raises(ToolError, match=f"a tool is named {name}, which is how a run hands in what it wrote"):
        make_run(lambda prompt: {}).run(AGENT, EVIDENCE, [shadow], Budget())


def test_a_run_hands_the_agent_its_tools_and_keeps_what_they_returned(make_run: Callable[..., AgentRun]) -> None:
    run = make_run(lambda prompt: "x [^f_1]", calls=[("lookup_tool", {"id": "memo-7"})]).run(
        AGENT, EVIDENCE, [lookup_tool], Budget())
    assert run.status == "done" and run.result is not None and run.result.text == "x [^f_1]"
    assert any(e.role == "tool" and e.tool == "lookup_tool" and "looked up memo-7 in the notes" in e.text
               for e in run.transcript)


def test_a_run_past_its_tool_calls_makes_no_more_and_still_hands_in(make_run: Callable[..., AgentRun]) -> None:
    run = make_run(lambda prompt: "x [^f_1]", calls=[("lookup_tool", {"id": "memo-7"})]).run(
        AGENT, EVIDENCE, [lookup_tool], Budget(tool_calls=0))
    assert run.status == "done" and run.result is not None and run.result.text == "x [^f_1]"
    assert not any("looked up memo-7" in e.text for e in run.transcript)
    assert any(e.role == "tool" and "has made every tool call its budget allows" in e.text for e in run.transcript)
