"""The `App` suite. Fixtures: `app`, an app's declaration; `workspace`, a `Workspace` handle that knows the app's kinds;
and `keep_and_read`, described on the last test."""

from __future__ import annotations

import inspect
from typing import Any

from cerebro.apps import App, bind, check_app, check_tool, collect, marks
from cerebro.caller import Caller
from cerebro.workspace import Workspace


def test_an_app_declaration_loads(app: App) -> None:
    check_app(app)
    assert app.name and all(kind == kind.lower() for kind in app.kinds)


def test_every_function_an_app_lists_is_a_marked_plain_typed_function(app: App) -> None:
    for fn in app.functions:
        assert inspect.isfunction(fn)
        found = marks(fn)
        assert found.surfaces or found.evidence
        check_tool(fn)


def test_each_function_reaches_only_the_surfaces_it_is_marked_for(app: App) -> None:
    exposed = collect(app.functions)
    for fn in app.functions:
        found = marks(fn)
        assert (exposed.agent_tools.get(fn.__name__) is fn) == ("agent" in found.surfaces)
        assert (exposed.mcp_tools.get(fn.__name__) is fn) == ("mcp" in found.surfaces)
        assert (found.evidence is not None) == (exposed.evidence.get(found.evidence or "") is fn)


def test_an_app_keeps_a_record_through_a_tool_and_reads_it_back_through_evidence(
        app: App, workspace: Workspace, keep_and_read: dict[str, Any]) -> None:
    """`keep_and_read` names a tool and its arguments, an evidence function and its arguments, the kind the tool
    keeps, and the text the evidence must give back, which names the kept record by its id or as its source. Its
    `between`, if given, runs on the workspace and the kept records before the read, as a pass would."""
    exposed = collect(app.functions)
    you = Caller("you", "person")
    name = keep_and_read["tool"]
    tool = bind(exposed.mcp_tools.get(name) or exposed.agent_tools[name], you, workspace)
    assert "caller" not in inspect.signature(tool).parameters and "workspace" not in inspect.signature(tool).parameters
    tool(**keep_and_read["args"])
    kept = workspace.records(keep_and_read["kind"])
    assert len(kept) == 1 and kept[0].by == you
    if "between" in keep_and_read:
        keep_and_read["between"](workspace, kept)
    read_back = bind(exposed.evidence[keep_and_read["evidence"]], Caller("notes/weekly", "agent"), workspace)
    items = read_back(**keep_and_read["evidence_args"])
    assert [item["text"] for item in items] == [keep_and_read["text"]]
    assert kept[0].id in (items[0]["id"], items[0].get("source"))
