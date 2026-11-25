"""The thin path, end to end on one machine, with no network and no model.

A workspace folder with a settings file, two files sources, and one agent file, on the real store,
files connector and lookup. One pass: the sources list their notes, a stand-in facts layer turns them into facts,
and the agent compiles its article through Pydantic AI on its function test model. The article lands in the store
and in the mirror folder, its cost in the spend ledger, and an MCP client finds, queries and reads it back. The
other tests here pin smaller rules of the path on stand-in sources.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.types import Implementation

from cerebro import Caller, Cerebro, Record
from cerebro.article import passages
from cerebro.fakes import FakeConnector, FakeExtraction
from cerebro.run import PydanticRun

SETTINGS = """\
models:
  extraction: { provider: fireworks, model: glm-5p3-flash, effort: low }
  decision: { provider: fireworks, model: rune }
  compilation: { provider: fireworks, model: glm-5p3, effort: high }
budget:
  daily_usd: 20
"""

# Modelled on the gtm template's agents/reports/pipeline.md: one fixed article, written on a schedule.
WEEKLY = """\
---
evidence:
  - query: { from: facts, source: notes }   # the facts of every note
heartbeat: [change]
---

# Instructions

Write this week's summary of the team's notes for someone with five minutes
on Monday morning. Every sentence cites a fact. Leave out anything you cannot
cite.

# Output

## Summary
"""

NOTES = [
    Record(id="notes:kickoff", source="notes", revision="1", at="2026-11-02T09:00:00Z", fields={"title": "Kickoff"},
           body="The launch review moved to March 3.\n\nVega leads the review."),
    Record(id="notes:budget", source="notes", revision="1", at="2026-11-03T09:00:00Z", fields={"title": "Budget"},
           body="The launch budget is set at 40 hours a week."),
]
PARAGRAPHS = {"The launch review moved to March 3.", "Vega leads the review.",
              "The launch budget is set at 40 hours a week."}
MAIL = [Record(id="mail:1", source="mail", revision="1", at="2026-11-04T09:00:00Z",
               body="A newsletter nobody asked for.")]


def summarize(prompt: str, leave_out: str = "") -> dict[str, str]:
    """What the scripted model writes: one sentence per fact it was given, each citing its fact, but for a fact
    whose text holds `leave_out`."""
    facts = re.findall(r"\[\^(fact_\w+)\] ([^(\n]+)", prompt)
    return {"Summary": " ".join(f"{text.strip()} [^{fid}]" for fid, text in facts
                                if not leave_out or leave_out not in text)}


def make_workspace(root: Path) -> Path:
    (root / "agents" / "reports").mkdir(parents=True)
    (root / "settings.yaml").write_text(SETTINGS)
    (root / "agents" / "reports" / "weekly.md").write_text(WEEKLY)
    return root


LOCAL = """\
kind: files
window: { behind: all }
select:
  - glob: "local/**/*.md"
    extract: facts
"""
MAIL_SOURCE = LOCAL.replace("local/**/*.md", "mail/*.md")
REAL_SETTINGS = SETTINGS.replace("budget:\n  daily_usd: 20", "budget:\n  compilation_daily_usd: 20\nstore:\n  path: data/cerebro.sqlite"
                                 "\nschedule:\n  articles: 0   # a changed article is rewritten on the next pass")
# Modelled on the gtm template's agents/accounts/subagents/overview.md: a query of facts by source in a window.
REAL_WEEKLY = WEEKLY.replace("  - query: { from: facts, source: notes }   # the facts of every note",
                             "  - query: { from: facts, source: local, window: last_30_days, limit: 30 }")


NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)  # the one time the real pieces read, so no date in a note drifts


def note(root: Path, path: str, days_ago: int, text: str) -> None:
    day = (NOW - timedelta(days=days_ago)).date().isoformat()
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(f"Date: {day}\n\n{text}\n")


def real_workspace(root: Path) -> Path:
    (root / "agents" / "reports").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "settings.yaml").write_text(REAL_SETTINGS)
    (root / "agents" / "reports" / "weekly.md").write_text(REAL_WEEKLY)
    (root / "sources" / "local.yaml").write_text(LOCAL)
    (root / "sources" / "mail.yaml").write_text(MAIL_SOURCE)
    note(root, "local/kickoff.md", 2, "The launch review moved to March 3.\n\nVega leads the review.")
    note(root, "local/budget.md", 1, "The launch budget is set at 40 hours a week.")
    note(root, "mail/newsletter.md", 1, "A newsletter nobody asked for.")
    return root


async def over_mcp(workspace: Cerebro) -> list[Any]:
    async with Client(workspace.mcp_server(), client_info=Implementation(name="desk-agent", version="1")) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        return [names, await client.call_tool("find", {"name": "Weekly"}),
                await client.call_tool("query", {"from": "facts", "window": "last_30_days"}),
                await client.call_tool("read", {"ids": ["reports/weekly", "reports/none"]}),
                await client.call_tool("read", {"ids": ["local/kickoff.md"], "about": "vega"}),
                await client.call_tool("query", {"from": "reports"})]


def test_one_pass_on_the_real_pieces_compiles_an_article_an_outside_agent_reads_over_mcp(tmp_path: Path) -> None:
    root = real_workspace(tmp_path / "team-workspace")
    writer = PydanticRun.scripted(lambda prompt: summarize(prompt, leave_out="Vega"),
                                  calls=[("query", {"source": "facts", "filters": {"source": "local"}})])
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), agent_run=writer, clock=lambda: NOW) as workspace:
        result = workspace.run_once()
        assert result.failed == {} and result.refused == []
        assert result.listed == {"local": 2, "mail": 1} and result.extracted == 4
        assert result.compiled == ["reports/weekly"]
        run = result.runs[0]
        # it queries, then writes its one heading, and the run ends there, with no turn to say it is done
        assert run.status == "done" and run.usage.requests == 2 and run.usage.tool_calls == 2
        assert [e.role for e in run.transcript] == ["user", "call", "tool", "call", "tool"]
        assert run.transcript[2].tool == "query" and "Vega leads the review." in run.transcript[2].text
        assert (root / "data" / "cerebro.sqlite").is_file()

        stored = workspace.read(Caller("you", "person"), ["reports/weekly"]).articles[0]
        summary = passages(stored)[0]
        cited = {c.quote: c for c in summary.citations}
        assert summary.heading == "Summary" and summary.by == "reports/weekly" and summary.digest and summary.started
        assert stored.compilations[0].whole and stored.compilations[0].heading == "reports/weekly"
        assert set(cited) == PARAGRAPHS - {"Vega leads the review."} and {c.kind for c in summary.citations} == {"fact"}
        assert {c.source for c in summary.citations} == {"local/kickoff.md", "local/budget.md"}
        assert summary.text == " ".join(f"{c.quote} [^{c.id}]" for c in summary.citations)
        copy = (root / "articles" / "reports" / "weekly.md").read_text()
        assert copy.startswith("---\nid: reports/weekly\n") and "## Summary" in copy
        for citation in summary.citations:
            assert f'[^{citation.id}]: {citation.source} "{citation.quote}"' in copy

        spent = [row.data for row in workspace.store.rows("spend")]
        assert [(r["layer"], r["run"], r["model"]) for r in spent if r["layer"] == "compilation"] == [
            ("compilation", run.id, "glm-5p3")]
        assert sorted(r["run"] for r in spent if r["layer"] == "extraction") == [
            "local/budget.md", "local/kickoff.md", "mail/newsletter.md"]
        assert [(r.data["agent"], r.data["model"]) for r in workspace.store.rows("agent-runs")] == [
            ("reports/weekly", "glm-5p3")]
        assert workspace.query(Caller("you", "person"), "spend", {"layer": "compilation"}).count == 1

        names, find, window, read, about, query = anyio.run(over_mcp, workspace)
        assert sorted(names) == ["decide", "find", "query", "read", "retract", "tell"]
        assert find.structured_content["matches"][0] == "reports/weekly"
        assert window.structured_content["count"] == 4
        served = read.structured_content
        assert served["missing"] == ["reports/none"] and served["articles"][0]["id"] == "reports/weekly"
        assert all(f"[^{c.id}]" in served["articles"][0]["text"] for c in summary.citations)
        assert served["articles"][0]["compiled_at"] and "facts_since_written" not in served["articles"][0]
        assert [f["text"] for f in about.structured_content["facts"]] == ["Vega leads the review."]
        assert [a["id"] for a in query.structured_content["items"]] == ["reports/weekly"]
        logged = [(row.data["tool"], row.data["result"], row.data["caller"]) for row in workspace.store.rows("mcp-requests")]
        assert logged == [("find", "hit", "mcp"), ("query", "hit", "mcp"), ("read", "hit", "mcp"),
                          ("read", "hit", "mcp"), ("query", "hit", "mcp")]

        again = workspace.run_once()
        assert again.compiled == [] and again.listed == {"local": 0, "mail": 0}
        assert again.skipped["reports/weekly"].startswith("nothing it reads has moved")

        note(root, "local/budget.md", 0, "The launch budget is now 30 hours a week.")
        third = workspace.run_once()
        assert third.compiled == ["reports/weekly"] and third.listed["local"] == 1
        fresh = workspace.read(Caller("you", "person"), "reports/weekly").articles[0].compilations[0]
        assert "now 30 hours" in fresh.text and "40 hours" not in fresh.text and fresh.digest != summary.digest
        assert len(workspace.store.rows("spend", {"layer": "compilation"})) == 2


def test_a_tell_over_mcp_gives_a_record_kept_under_the_local_caller(tmp_path: Path) -> None:
    async def tell(workspace: Cerebro) -> Any:
        async with Client(workspace.mcp_server()) as client:
            return await client.call_tool("tell", {"text": "The review moved to April.", "as": "fact"})

    with Cerebro(make_workspace(tmp_path / "workspace"), trusted=True, agent_run=PydanticRun.scripted(summarize)) as workspace:
        reply = anyio.run(tell, workspace)
        assert not reply.is_error
        record = workspace.store.record(json.loads(reply.content[0].text)["record"])
        assert record is not None and record.body == "The review moved to April." and record.by == Caller("mcp", "person")


def test_an_article_whose_copy_cannot_be_written_still_compiles_with_a_warning(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / "workspace")
    (root / "articles").mkdir()
    (root / "articles" / "reports").write_text("a file where the folder goes")
    with Cerebro(root, trusted=True, connectors=[FakeConnector("notes", NOTES)], extraction=FakeExtraction(),
                 agent_run=PydanticRun.scripted(summarize)) as workspace:
        result = workspace.run_once()
        assert result.compiled == ["reports/weekly"] and result.failed == {}
        assert result.warnings and "copy articles/reports/weekly.md was not written" in result.warnings[0]
        assert workspace.read(Caller("you", "person"), "reports/weekly").articles
        again = workspace.run_once()  # nothing moved, and the path is still wrong: it says so again, with no model call
        assert again.compiled == [] and "was not written" in again.warnings[0]
        (root / "articles" / "reports").unlink()  # the owner fixes the path
        fixed = workspace.run_once()
        assert fixed.compiled == [] and fixed.runs == [] and fixed.warnings == []  # not due, and no model is called
        assert "## Summary" in (root / "articles" / "reports" / "weekly.md").read_text()
        assert workspace.store.state("cerebro:unmirrored") == []


def test_two_agents_with_one_id_are_refused_when_the_workspace_loads(tmp_path: Path) -> None:
    from cerebro import App
    from cerebro.project import WorkspaceError

    root = make_workspace(tmp_path / "workspace")
    other = tmp_path / "app-agents" / "reports"
    other.mkdir(parents=True)
    (other / "weekly.md").write_text(WEEKLY)
    with pytest.raises(WorkspaceError, match="both have the id reports/weekly"):
        Cerebro(root, trusted=True, apps=[App(name="extra", agents=other.parent)])


def test_extensions_may_hold_a_dataclass_and_two_workspaces_keep_their_own(tmp_path: Path) -> None:
    code = ('''from __future__ import annotations\nfrom dataclasses import dataclass\nfrom cerebro import evidence\n\n'''
            '''@dataclass\nclass Row:\n    text: str\n\n@evidence("rows")\ndef rows() -> list[dict[str, str]]:\n'''
            '''    """Rows."""\n    return [{"id": "r1", "text": Row(TEXT).text}]\n\nTEXT = "{text}"\n''')
    workspaces = []
    for name in ("one", "two"):
        root = make_workspace(tmp_path / name)
        (root / "extensions.py").write_text(code.replace("{text}", name))
        workspaces.append(Cerebro(root, trusted=True, agent_run=PydanticRun.scripted(summarize)))
    assert [b.exposed.evidence["rows"]()[0]["text"] for b in workspaces] == ["one", "two"]
    for workspace in workspaces:
        workspace.close()


def test_an_outside_agent_queries_a_collection_over_mcp(tmp_path: Path) -> None:
    async def query(workspace: Cerebro) -> Any:
        async with Client(workspace.mcp_server()) as client:
            every = await client.call_tool("query", {"from": "reports", "limit": 5})
            none = await client.call_tool("query", {"from": "reports", "owner": "west"})
            return every, none

    with Cerebro(make_workspace(tmp_path / "workspace"), trusted=True, connectors=[FakeConnector("notes", NOTES)],
                 extraction=FakeExtraction(), agent_run=PydanticRun.scripted(summarize)) as workspace:
        workspace.run_once()
        reply, filtered = anyio.run(query, workspace)
    assert not reply.is_error and filtered.structured_content["count"] == 0
    assert reply.structured_content["count"] == 1
    assert [item["id"] for item in reply.structured_content["items"]] == ["reports/weekly"]


def test_an_mcp_tool_in_extensions_is_served_beside_the_six(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / "workspace")
    (root / "extensions.py").write_text(
        "from cerebro import mcp_tool\n\n"
        "@mcp_tool\ndef note(text: str) -> dict[str, str]:\n"
        "    \"\"\"Write a note.\"\"\"\n    return {\"kept\": text}\n")

    async def call(workspace: Cerebro) -> Any:
        async with Client(workspace.mcp_server()) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            return names, await client.call_tool("note", {"text": "the review moved"})

    with Cerebro(root, trusted=True, agent_run=PydanticRun.scripted(summarize)) as workspace:
        names, reply = anyio.run(call, workspace)
    assert "note" in names and len(names) == 7
    assert reply.structured_content == {"kept": "the review moved"}


def test_an_evidence_function_gets_the_target_id_for_this_and_its_caller(tmp_path: Path) -> None:
    root = make_workspace(tmp_path / "workspace")
    agent = root / "agents" / "reports" / "weekly.md"
    agent.write_text(agent.read_text().replace("  - query: { from: facts, source: notes }", "  - topic: { article: this }"))
    (root / "extensions.py").write_text(
        "from cerebro import Caller, evidence\n\n"
        "@evidence('topic')\ndef topic(article: str, caller: Caller) -> list[dict[str, str]]:\n"
        "    \"\"\"The topic.\"\"\"\n    return [{'id': 'r1', 'text': f'{article} read by {caller.name}', 'source': 'topic'}]\n")
    seen: list[str] = []
    with Cerebro(root, trusted=True, agent_run=PydanticRun.scripted(lambda p: seen.append(p) or {"Summary": "It [^r1]."})) as workspace:
        assert workspace.run_once().compiled == ["reports/weekly"]
        cited = workspace.read(Caller("you", "person"), "reports/weekly").articles[0].compilations[0].citations
    assert "reports/weekly read by reports/weekly" in seen[0]
    assert [(c.kind, c.id, c.source) for c in cited] == [("record", "r1", "topic")]


def test_tell_takes_as_over_mcp_and_the_caller_is_the_one_local_caller(tmp_path: Path) -> None:
    from mcp.types import Implementation

    from cerebro.lookup import StoreLookup, TellResult

    told: list[tuple[Caller, str, str | None, str | None]] = []

    class Recording(StoreLookup):
        def tell(self, caller: Caller, text: str, target: str | None = None, as_: str | None = None,
                 kind: str | None = None) -> TellResult:
            told.append((caller, text, as_, kind))
            return TellResult(record="r_1")

    async def tell(workspace: Cerebro) -> Any:
        async with Client(workspace.mcp_server(), client_info=Implementation(name="desk-agent", version="1")) as client:
            schemas = {t.name: t.input_schema for t in (await client.list_tools()).tools}
            reply = await client.call_tool("tell", {"text": "Moved.", "as": "proposal", "kind": "article.instruct"})
            return schemas, reply

    with Cerebro(make_workspace(tmp_path / "workspace"), trusted=True, agent_run=PydanticRun.scripted(summarize)) as workspace:
        workspace.lookup = Recording(workspace.store)
        schemas, reply = anyio.run(tell, workspace)
    assert not reply.is_error and reply.structured_content["record"] == "r_1"
    assert told == [(Caller("mcp", "person"), "Moved.", "proposal", "article.instruct")]
    assert "as" in schemas["tell"]["properties"]
    assert all("ctx" not in schema.get("properties", {}) for schema in schemas.values())
