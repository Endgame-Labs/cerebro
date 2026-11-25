"""No field of profile.md, and no collection's name, of any size stops a session from opening, over standard input
and output or over HTTP."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_mcp import told
from test_tokens import (
    no_network,  # noqa: F401  autouse: sockets reach 127.0.0.1 alone
    serving,
)

from cerebro import Cerebro
from cerebro.article import Identity, Property
from cerebro.caller import Caller
from cerebro.fakes import MemoryStore

BIG = "x" * (1024 * 1024)
FIELDS = {
    "name": f"---\nidentity:\n  name: Robin {BIG}\n---\nBrief me.\n",
    "role": f"---\nidentity:\n  name: Robin Vale\nrole: Lead {BIG}\n---\nBrief me.\n",
    "alias": f"---\nidentity:\n  name: Robin Vale\n  aliases: [Robin, {BIG}]\n---\nBrief me.\n",
    "identity value": f"---\nidentity:\n  name: Robin Vale\n  badge_id: {BIG}\n---\nBrief me.\n",
    "collection name": "---\nidentity:\n  name: Robin Vale\n---\nBrief me.\n",
}


async def _opened(url: str, token: str) -> str | None:
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                                                   timeout=httpx2.Timeout(60.0)))
        client = await stack.enter_async_context(Client(streamable_http_client(url, http_client=http), mode="legacy"))
        found = await client.call_tool("find", {"name": "Robin Vale"})
        assert not found.is_error
        return client.instructions


@pytest.mark.parametrize("field", FIELDS)
def test_a_field_of_a_mebibyte_opens_every_session_over_http_and_standard_input_and_output(
        tmp_path: Path, field: str) -> None:
    root = tmp_path / "desk"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    (root / "profile.md").write_text(FIELDS[field])
    with Cerebro(root, store=MemoryStore()) as workspace:
        if field == "collection name":
            workspace.store.create(Identity(f"{BIG}/a", BIG, "A"))
            workspace.store.set_field(f"{BIG}/a", "stage", Property("open", Caller("you", "person"), "2026-11-01T09:00:00Z"))
        token = workspace.make_token("desk").token
        assert told(workspace)[0] is not None
        with serving(workspace) as client:
            said = anyio.run(_opened, str(client.base_url.join("/mcp")), token)
        assert said is not None and "The person you are helping is Robin" in said


def _initialize(client: Any, token: str, accept: str | None) -> Any:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if accept is not None:
        headers["Accept"] = accept
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "desk", "version": "1"}}})


def test_each_reply_takes_the_form_the_clients_accept_header_picks(tmp_path: Path) -> None:
    root = tmp_path / "desk"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    (root / "profile.md").write_text("---\nidentity:\n  name: Robin Vale\n---\nBrief me.\n")
    with Cerebro(root, store=MemoryStore()) as workspace:
        token = workspace.make_token("desk").token
        with serving(workspace) as client:
            kinds = {accept: _initialize(client, token, accept) for accept in (
                "application/json, text/event-stream", "text/event-stream, application/json", "application/json",
                "text/event-stream", "application/*", "*/*", None, "text/html")}
    framed = {accept: (r.status_code, r.headers["content-type"].split(";")[0]) for accept, r in kinds.items()}
    assert framed == {"application/json, text/event-stream": (200, "application/json"),
                      "text/event-stream, application/json": (200, "application/json"),
                      "application/json": (200, "application/json"), "text/event-stream": (200, "text/event-stream"),
                      "application/*": (200, "application/json"), "*/*": (200, "application/json"),
                      None: (200, "application/json"), "text/html": (406, "text/plain")}
    streamed = kinds["text/event-stream"].text
    assert streamed.startswith("event: message\ndata: {") and streamed.endswith("}\n\n")
    assert json.loads(streamed.split("data: ", 1)[1])["result"]["serverInfo"]
    assert kinds[None].json()["result"]["serverInfo"]
    assert kinds["text/html"].text == "accept application/json or text/event-stream\n"


@pytest.mark.parametrize("accept", ["application/json, text/event-stream", "text/event-stream"])
def test_a_reply_still_under_way_keeps_the_connection_alive_until_it_comes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accept: str) -> None:
    import time

    from cerebro import mcp

    monkeypatch.setattr(mcp, "KEEP_ALIVE", 0.2)
    root = tmp_path / "desk"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    with Cerebro(root, store=MemoryStore()) as workspace:
        token = workspace.make_token("desk").token
        found = workspace.find
        monkeypatch.setattr(workspace, "find", lambda caller, name: (time.sleep(1.2), found(caller, name))[1])
        with serving(workspace) as client:
            opened = _initialize(client, token, accept)
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}", "Accept": accept,
                       "Mcp-Session-Id": opened.headers["mcp-session-id"], "MCP-Protocol-Version": "2025-06-18"}
            call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "find", "arguments": {"name": "Robin"}}}
            chunks = []
            with client.stream("POST", "/mcp", headers=headers, json=call, timeout=0.6) as reply:
                assert reply.status_code == 200
                chunks = list(reply.iter_bytes())
    body = b"".join(chunks)
    assert len(chunks) > 2
    if accept.startswith("application/json"):
        assert reply.headers["content-type"] == "application/json" and body.startswith(b" ")
        assert json.loads(body)["result"]["structuredContent"]["name"] == "Robin"
    else:
        assert reply.headers["content-type"] == "text/event-stream" and body.startswith(b": ping\n\n")
        assert json.loads(body.split(b"data: ", 1)[1])["result"]["structuredContent"]["name"] == "Robin"
