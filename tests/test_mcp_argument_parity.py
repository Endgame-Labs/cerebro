"""What a call's arguments take and what they refuse by name, on MCP, the JSON API and `cerebro tools` alike, each
refusal one line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
from mcp import Client
from test_api import call, refusal
from test_cli import run, stand_ins  # noqa: F401  stand_ins: a fixture
from test_mcp import NOW, workspace_at
from test_tokens import (
    Session,
    no_network,  # noqa: F401  autouse: sockets reach 127.0.0.1 alone
    serving,
)

from cerebro import Caller, Cerebro
from cerebro.article import Identity
from cerebro.mcp import server
from cerebro.records import WAITING, Proposal, proposals, propose

ONE_VALUE = "takes one value: a literal or a window, with no operators"


def waiting(workspace: Cerebro, *ids: str) -> Cerebro:
    for id in ids:  # one article each: accepting an archive settles the others waiting on the same article
        workspace.store.create(Identity(f"projects/{id}", "projects", id))
        propose(workspace.store, Proposal(id, "article.archive", f"projects/{id}", Caller("projects/curator", "agent"),
                                      "2026-11-03T09:00:00Z", reason="It closed in October."))
    return workspace


def local(workspace: Cerebro, *calls: tuple[str, dict[str, Any]]) -> list[Any]:
    async def run() -> list[Any]:
        async with Client(server(workspace, clock=lambda: NOW)) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return anyio.run(run)


def test_a_decide_entry_with_no_id_answers_by_itself_and_the_call_lands_none(
        tmp_path: Path) -> None:
    workspace = waiting(workspace_at(tmp_path / "workspace"), "p_a1", "p_a2", "p_a3")
    token = workspace.make_token("desk").token
    with serving(workspace) as client:
        api = call(client, "decide", {"accept": ["p_a1", {"reason": "the second, with its id forgotten"}]}, token)
        mcp = Session(client, token).call("decide", {"reject": [{"reason": "no id"}, "p_a2"]})
    assert api.status_code == 200, api.text
    assert api.json()["answers"] == {"p_a1": "not decided", "": "unknown"}
    assert not mcp["isError"] and mcp["structuredContent"]["answers"] == {"": "unknown", "p_a2": "not decided"}
    assert [p.id for p in proposals(workspace.store, WAITING)] == ["p_a1", "p_a2", "p_a3"]


def test_a_key_that_differs_from_an_argument_only_in_case_is_that_argument(
        tmp_path: Path) -> None:
    workspace = waiting(workspace_at(tmp_path / "workspace"), "p_a1", "p_a2")
    found = local(workspace, ("find", {"NAME": "Apollo"}), ("find", {"name": "Apollo", "Name": "Vega"}),
                  ("retract", {"ID": "r_none"}))
    assert [r.is_error for r in found] == [False, False, True]
    assert found[0].structured_content["matches"] == ["projects/apollo"]
    assert found[1].structured_content["matches"] == ["projects/vega"]  # the later key wins
    assert "unknown field" not in found[2].content[0].text
    token = workspace.make_token("desk").token
    with serving(workspace) as client:
        upper = call(client, "decide", {"Accept": ["p_a1"]}, token)
        entry = call(client, "decide", {"accept": [{"Id": "p_a2", "Reason": "Vega closed."}]}, token)
    assert upper.json()["answers"] == {"p_a1": "decided"} and entry.json()["answers"] == {"p_a2": "decided"}
    upper, lower = local(workspace, ("query", {"From": "projects"}), ("query", {"from": "projects"}))
    assert upper.content[0].text != lower.content[0].text  # query takes its own keys as written


def test_every_refusal_of_a_calls_arguments_over_mcp_is_one_line_that_names_the_tool(tmp_path: Path) -> None:
    refused = local(workspace_at(tmp_path / "workspace"), ("find", {"name": 5}), ("tell", {}), ("decide", {"accept": [5]}),
                    ("retract", {}), ("read", {"ids": {"a": 1}}), ("query", {"limit": "many"}))
    for reply, tool in zip(refused, ("find", "tell", "decide", "retract", "read", "query"), strict=True):
        text = reply.content[0].text
        assert reply.is_error and "\n" not in text and text.startswith(f"{tool}: "), text
        assert "pydantic" not in text and "Error executing tool" not in text, text


def test_a_query_filter_value_that_is_a_map_a_list_or_null_is_refused_by_name(
        tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    values: list[Any] = [{"x": 1}, ["open"], None]
    replies = local(workspace, *[("query", {"from": "projects", "stage": v}) for v in values],
                    ("query", {"from": "projects", "_meta": {"progressToken": 7}}),
                    ("query", {"from": "projects", "stage": "open", "count": 2, "flag": True}))
    assert [(r.is_error, r.content[0].text) for r in replies[:4]] == [
        (True, f'query: filter "stage": {ONE_VALUE}')] * 3 + [(True, f'query: filter "_meta": {ONE_VALUE}')]
    assert not replies[4].is_error
    token = workspace.make_token("desk").token
    with serving(workspace) as client:
        api = call(client, "query", {"from": "projects", "stage": ["open"]}, token)
    assert refusal(api, 400, "bad_request") == f'query: filter "stage": {ONE_VALUE}'


def test_cerebro_tools_refuses_each_call_an_outside_agents_call_is_refused_with_the_same_line(
        tmp_path: Path, stand_ins: None) -> None:  # noqa: F811
    run("init", str(tmp_path), "--daily-usd", "20")
    workspace = ["--workspace", str(tmp_path)]
    cases = {
        "tell": [({"text": "Apollo moved.", "section": "Overview"}, 'tell: unknown field "section"')],
        "decide": [({"accept": [{"id": "p_nosuch", "when": "now"}]}, 'decide: unknown field "when"'),
                   ({"accept": [{"id": "p_nosuch", "meta": {"a": 1}}]}, 'decide: unknown field "meta"')],
        "find": [({"name": "Apollo", "bogus": 1}, 'find: unknown field "bogus"')],
        "query": [({"from": "projects", "stage": {"$ne": "x"}}, f'query: filter "stage": {ONE_VALUE}')],
    }
    for tool, calls in cases.items():
        for args, line in calls:
            done = run("tools", tool, "--json", json.dumps(args), *workspace)
            assert (done.exit_code, done.output.strip()) == (1, f"Error: {line}"), (tool, args)
    entry = run("tools", "decide", "--json", json.dumps({"accept": [5]}), *workspace)
    assert entry.exit_code == 1 and entry.output.startswith("Error: decide: ") and len(entry.output.splitlines()) == 1
    found = run("tools", "find", "--json", json.dumps({"Name": "nobody"}), *workspace)
    assert found.exit_code == 0 and json.loads(found.output)["name"] == "nobody"


def test_reads_ids_and_decides_entries_sent_as_json_text_as_the_list_each_holds_as_a_desktop_client_sends_them(
        tmp_path: Path) -> None:
    workspace = waiting(workspace_at(tmp_path / "workspace"), "p_a1")
    token = workspace.make_token("desk").token
    with serving(workspace) as client:
        session = Session(client, token)
        answer = session.call("decide", {"Reject": '[{"ID": "p_a1", "Reason": "Vega is open."}]'})
        read = session.call("read", {"IDS": '["projects/apollo"]'})
    assert not answer["isError"] and answer["structuredContent"]["answers"] == {"p_a1": "decided"}
    assert proposals(workspace.store, WAITING) == []
    assert not read["isError"] and [a["id"] for a in read["structuredContent"]["articles"]] == ["projects/apollo"]


# Each call, and whether it is refused for its arguments (True) or runs, a refusal by the operation included. A whole
# number sent as its digits or as 5.0, and decide's entries sent as JSON text, are read, since clients send them so.
ARGUMENT_CASES = [("query", {"from": "projects", "limit": "5"}, False), ("query", {"from": "projects", "limit": 5.0}, False),
         ("query", {"from": "projects", "offset": "0"}, False), ("query", {"from": "projects", "limit": True}, True),
         ("query", {"from": "projects", "limit": 5.5}, True), ("query", {"from": "projects", "limit": "five"}, True),
         ("query", {"from": "projects", "offset": None}, False), ("query", {"from": "projects", "limit": None}, False),
         ("retract", {"id": "r_nosuch", "reason": None}, False),
         ("decide", {"accept": [{"id": "p_nosuch", "reason": None}]}, False), ("decide", {"accept": None}, False),
         ("decide", {"accept": [None, "p_nosuch"]}, False), ("tell", {"text": "   ", "target": None}, False),
         ("read", {"ids": "projects/apollo"}, False), ("read", {"ids": '["projects/apollo"]'}, False),
         ("decide", {"accept": '["p_nosuch"]'}, False), ("read", {"ids": ["projects/apollo"], "about": None}, False),
         ("find", {"name": None}, False), ("retract", {"id": None}, False), ("tell", {"text": None}, False),
         ("read", {"ids": None}, False)]


def test_each_call_is_refused_for_its_arguments_or_runs(
        tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    replies = local(workspace, *[(name, args) for name, args, _ in ARGUMENT_CASES])
    refused = [reply.is_error and not reply.content[0].text.startswith("Error executing tool") for reply in replies]
    assert [(name, args) for (name, args, want), got in zip(ARGUMENT_CASES, refused, strict=True) if want != got] == []
