"""The `App` suite against the notes app fake, and the rules for what may be a tool."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from mcp.server.mcpserver import Context
from suites.app import *

from cerebro.apps import (
    App,
    AppError,
    ToolError,
    agent_tool,
    apps_in,
    check_app,
    check_apps,
    check_tool,
    collect,
    evidence,
    load_app,
    mcp_tool,
)
from cerebro.fakes import MemoryStore, notes_app
from cerebro.lookup import StoreLookup
from cerebro.workspace import Workspace


@pytest.fixture
def app() -> App:
    return notes_app()


@pytest.fixture
def workspace() -> Workspace:
    store = MemoryStore()
    return Workspace(StoreLookup(store, None, ["note"]), store, ["note"])


@pytest.fixture
def keep_and_read() -> dict[str, Any]:
    return {"tool": "note", "args": {"text": "The review moved."}, "kind": "note", "evidence": "notes",
            "evidence_args": {}, "text": "The review moved."}


def test_marking_a_function_puts_it_on_no_surface_by_itself() -> None:
    @agent_tool
    @mcp_tool
    def lookup_ticket(number: int) -> dict[str, Any]:
        """One ticket by its number."""
        return {"number": number}

    assert lookup_ticket(7) == {"number": 7}
    assert collect([]).agent_tools == {}
    exposed = collect([lookup_ticket])
    assert exposed.agent_tools == {"lookup_ticket": lookup_ticket} == exposed.mcp_tools


def test_check_tool_refuses_a_function_that_is_not_a_plain_typed_function_and_names_it() -> None:
    def no_doc(x: int) -> int:
        return x

    def no_type(x):
        """Untyped."""
        return x

    def with_context(text: str, ctx: Context) -> str:
        """Takes the MCP server's context."""
        return text

    def returns_context(text: str) -> list[Context]:
        """Returns the MCP server's context."""
        return []

    def plain(ticket: int, tags: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """A plain typed tool."""
        return {}

    for bad in (no_doc, no_type, with_context, returns_context):
        with pytest.raises(ToolError) as caught:
            check_tool(bad)
        assert bad.__name__ in str(caught.value)
    check_tool(plain)


def test_an_app_that_lists_an_unmarked_function_is_refused() -> None:
    def helper(x: int) -> int:
        """A helper."""
        return x

    with pytest.raises(AppError) as caught:
        check_app(App(name="tickets", functions=[helper]))
    assert "helper" in str(caught.value)
    with pytest.raises(ToolError):
        evidence("Not A Word")


APP = """from cerebro import App, evidence


@evidence("rows")
def rows() -> list[dict[str, str]]:
    \"\"\"Rows.\"\"\"
    return []


app = App(name="rows", kinds=["row"], functions=[rows], agents={agents})
"""


def app_folder(root: Path, agents: str = "None", text: str = APP) -> Path:
    root.mkdir(parents=True)
    (root / "app.py").write_text(text.replace("{agents}", agents))
    return root


def test_an_app_folder_loads_once_with_its_agents_folder_read_from_the_folder(tmp_path: Path) -> None:
    folder = app_folder(tmp_path / "rows", agents='"agents"')
    (folder / "agents").mkdir()
    loaded = load_app(folder)
    assert loaded.agents == folder.resolve() / "agents" and loaded.kinds == ["row"]
    assert load_app(str(folder)) is loaded
    assert load_app(app_folder(tmp_path / "plain")).agents is None


def test_an_app_folder_that_does_not_declare_an_app_is_refused_with_one_line_naming_it(tmp_path: Path) -> None:
    cases = [(tmp_path / "empty", "holds no app.py"),
             (app_folder(tmp_path / "broken", text="import nowhere_at_all\n"), "does not import"),
             (app_folder(tmp_path / "bare", text="name = 'rows'\n"), "declares no app"),
             (app_folder(tmp_path / "lost", agents='"agents"'), "not a folder")]
    (tmp_path / "empty").mkdir()
    for folder, problem in cases:
        with pytest.raises(AppError) as caught:
            load_app(folder)
        assert problem in str(caught.value) and folder.name in str(caught.value)


def test_a_module_s_apps_are_found_each_once_and_two_that_clash_are_refused() -> None:
    one, two = App(name="one", kinds=["thing"]), App(name="two", kinds=["other"])
    module = ModuleType("extensions")
    module.one, module.again, module.two, module.text = one, one, two, "not an app"
    assert apps_in(module) == [one, two]
    with pytest.raises(AppError, match="two apps are named one"):
        check_apps([one, App(name="one")])
    with pytest.raises(AppError, match="apps one and three both declare kind thing"):
        check_apps([one, App(name="three", kinds=["thing"])])


def test_an_app_kind_that_query_already_reads_or_that_repeats_is_refused() -> None:
    for kind in ("facts", "mcp-requests", "collections"):
        with pytest.raises(AppError, match=f"kind {kind}, which query already reads"):
            check_app(App(name="tickets", kinds=[kind]))
    with pytest.raises(AppError, match="kind ticket twice"):
        check_app(App(name="tickets", kinds=["ticket", "ticket"]))


def test_an_app_name_or_kind_that_is_not_a_word_is_refused_with_a_line_that_names_it() -> None:
    for app, culprit in ((App(name="Tickets"), "'Tickets'"), (App(name="tickets", kinds=["Big Ticket"]), "'Big Ticket'"),
                         (App(name="tickets", kinds=[7]), "7")):  # type: ignore[list-item]
        with pytest.raises(AppError) as caught:
            check_app(app)
        assert culprit in str(caught.value) and "not a word" in str(caught.value)


def test_a_tool_reads_the_time_from_the_workspaces_own_clock() -> None:
    from datetime import UTC, datetime

    from cerebro.fakes import MemoryStore
    from cerebro.lookup import StoreLookup
    from cerebro.workspace import Workspace

    at = datetime(2026, 11, 17, 9, tzinfo=UTC)
    store = MemoryStore()
    assert Workspace(StoreLookup(store), store, [], clock=lambda: at).now() == at


def test_keep_with_an_id_revises_that_record_in_place_and_refuses_one_of_another_kind() -> None:
    from cerebro.caller import Caller
    from cerebro.fakes import MemoryStore
    from cerebro.lookup import OperationError, StoreLookup
    from cerebro.workspace import Workspace

    store, you = MemoryStore(), Caller("you", "person")
    workspace = Workspace(StoreLookup(store), store, ["note", "task"])
    first = workspace.keep(you, "note", "Draft one.")
    again = workspace.keep(you, "note", "Draft two.", id=first.id)
    assert (again.id, again.revision) == (first.id, "2") and [r.body for r in workspace.records("note")] == ["Draft two."]
    with pytest.raises(OperationError, match="is a record of note, not task"):
        workspace.keep(you, "task", "Not a task.", id=first.id)
    with pytest.raises(OperationError, match="there is no record task_none"):
        workspace.keep(you, "task", "Lost.", id="task_none")


def test_a_kinds_text_reaches_a_context_agent_only_when_its_app_declares_it_for_agents() -> None:
    from cerebro.caller import Caller

    store, you, agent = MemoryStore(), Caller("you", "person"), Caller("reports/weekly", "agent")
    lookup = StoreLookup(store, None, ["note", "task"], agent_text=["task"])
    workspace = Workspace(lookup, store, ["note", "task"])
    workspace.keep(you, "note", "The note's words.", {"title": "Plan", "owner": "Ada"})
    workspace.keep(you, "task", "The task's words.", {"title": "Call"})
    note, task = lookup.query(agent, "note"), lookup.query(agent, "task")
    assert note.items[0].body is None and note.items[0].fields == {"title": "Plan"} and note.warnings
    assert task.items[0].body == "The task's words." and not task.warnings
    assert lookup.query(you, "note").items[0].body == "The note's words."
    check_app(App(name="tasks", kinds=["task"], text_for_agents=["task"]))
    with pytest.raises(AppError, match="gives agents the text of kind 'note', which it does not declare"):
        check_app(App(name="tasks", kinds=["task"], text_for_agents=["note"]))
