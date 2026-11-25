"""A call no JSON reader here can read, a lone surrogate escape, a number of more than 4300 digits or nesting deeper
than the HTTP gate takes, gets a reply on every surface, in the line the HTTP gate refuses its body with, and runs
nothing."""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import mcp.server.mcpserver.server as sdk_server
import pytest
from mcp.server.stdio import stdio_server
from test_cli import run, stand_ins  # noqa: F401  stand_ins: a fixture
from test_mcp import workspace_at

import cerebro.mcp
from cerebro import Cerebro
from cerebro.mcp import serve_stdio
from cerebro.records import TOLD
from cerebro.tokens import NO_TEXT, TOO_DEEP

LONE = NO_TEXT.decode().strip()
DEEP = TOO_DEEP.decode().strip()
LONG = "this request's body holds a number of more than 4300 digits, too long to read: send a smaller one"
NINES = "9" * 5000


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


class Pipe:
    """Standard input and output as the SDK's stdio transport reads and writes them, fed and read by the test."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.replies: queue.Queue[dict[str, Any]] = queue.Queue()

    def __aiter__(self) -> Pipe:
        return self

    async def __anext__(self) -> str:
        line = await anyio.to_thread.run_sync(self.lines.get)
        if line is None:
            raise StopAsyncIteration
        return line

    async def write(self, text: str) -> None:
        for line in text.splitlines():
            self.replies.put(json.loads(line))

    async def flush(self) -> None:
        pass


def over_stdio(monkeypatch: pytest.MonkeyPatch, workspace: Cerebro, lines: list[str], ids: set[Any]) -> dict[Any, Any]:
    """The replies `serve_stdio` sends to `lines`, after a handshake, by id, waiting at most 10 s for those of `ids`."""
    pipe = Pipe()
    piped = lambda *_: stdio_server(pipe, pipe)  # the SDK's transport, on the test's streams
    monkeypatch.setattr(sdk_server, "stdio_server", piped)
    monkeypatch.setattr(cerebro.mcp, "stdio_server", piped, raising=False)
    served = threading.Thread(target=serve_stdio, args=(workspace,), daemon=True)
    served.start()
    pipe.lines.put('{"jsonrpc":"2.0","id":"hello","method":"initialize","params":{"protocolVersion":"2025-06-18",'
                   '"capabilities":{},"clientInfo":{"name":"pipe","version":"1"}}}\n')
    pipe.lines.put('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    for line in lines:
        pipe.lines.put(line + "\n")
    replies: dict[Any, Any] = {}
    deadline = time.monotonic() + 10
    while not ids <= replies.keys() and time.monotonic() < deadline:
        try:
            reply = pipe.replies.get(timeout=max(deadline - time.monotonic(), 0.01))
        except queue.Empty:
            break
        replies[reply.get("id")] = reply
    pipe.lines.put(None)
    served.join(10)
    return replies


def call(id: int, tool: str, arguments: str) -> str:
    return f'{{"jsonrpc":"2.0","id":{id},"method":"tools/call","params":{{"name":"{tool}","arguments":{arguments}}}}}'


def test_over_stdio_a_call_the_sdk_cannot_read_gets_the_http_gates_line_as_its_reply_and_runs_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    calls = {2: ("find", '{"name":"\\ud800x"}', LONE), 3: ("find", '{"name":"Apollo","\\ud800":1}', LONE),
             4: ("tell", '{"text":"Apollo moved \\ud83d"}', LONE),  # an emoji cut in half
             5: ("query", '{"from":"projects","limit":' + NINES + "}", LONG),
             6: ("find", '{"name":"Apollo","bogus":' + nested(200) + "}", DEEP),
             7: ("find", '{"name":"Apollo","bogus":' + nested(70) + "}", DEEP)}  # one the SDK reads, as HTTP refuses
    lines = [call(id, tool, arguments) for id, (tool, arguments, _) in calls.items()]
    lines += ['{"jsonrpc":"2.0","id":8,"method":"resources/read","params":{"uri":"\\ud800"}}',
              '{"jsonrpc":"2.0","id":"\\ud800","method":"tools/call","params":{"name":"find","arguments":{}}}',
              call(9, "find", '{"name":"Apollo"}')]
    replies = over_stdio(monkeypatch, workspace, lines, {*calls, 8, None, 9})
    for id, (tool, arguments, line) in calls.items():
        assert id in replies, f"no reply to {tool} {arguments[:40]}"
        assert replies[id]["result"]["isError"] and [b["text"] for b in replies[id]["result"]["content"]] == [line]
    assert replies[8]["error"] == {"code": -32700, "message": LONE}
    assert replies[None]["error"] == {"code": -32700, "message": LONE}  # an id no reply can carry back
    assert not replies[9]["result"]["isError"] and replies[9]["result"]["structuredContent"]["matches"] == [
        "projects/apollo"]
    assert workspace.store.records(TOLD) == []


def test_over_stdio_a_message_nested_past_what_python_reads_gets_the_line_with_no_id(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    replies = over_stdio(monkeypatch, workspace, [call(2, "find", '{"name":"Apollo","bogus":' + nested(100_000) + "}"),
                                              call(3, "find", '{"name":"Apollo"}')], {None, 3})
    assert replies[None]["error"] == {"code": -32700, "message": DEEP}
    assert replies[3]["result"]["structuredContent"]["matches"] == ["projects/apollo"]


def test_cerebro_tools_refuses_what_no_surface_reads_in_one_line_as_the_http_gate_does_and_keeps_nothing(
        tmp_path: Path, stand_ins: None) -> None:  # noqa: F811
    run("init", str(tmp_path), "--daily-usd", "20")
    cases = [("find", '{"name":"\\ud800x"}', LONE), ("tell", '{"text":"Apollo moved \\ud83d","as":"fact"}', LONE),
             ("read", '{"ids":["\\ud800"]}', LONE), ("query", '{"from":"projects","limit":' + NINES + "}", LONG),
             ("query", '{"from":"projects","limit":' + NINES + ".5}", LONG),
             ("find", '{"name":"Apollo","bogus":' + nested(200) + "}", DEEP),
             ("find", '{"name":"Apollo","bogus":' + nested(100_000) + "}", DEEP),
             ("find", '{"name":"\\ud800","bogus":' + nested(70) + "}", DEEP)]  # the gate's order: depth first
    for tool, arguments, line in cases:
        done = run("tools", tool, "--json", arguments, "--workspace", str(tmp_path))
        assert isinstance(done.exception, SystemExit), (tool, arguments[:40], done.exc_info)
        assert (done.exit_code, done.output) == (1, f"Error: {line}\n"), (tool, arguments[:40])
    with Cerebro(tmp_path) as workspace:
        assert workspace.store.records(TOLD) == []


def test_over_stdio_a_line_no_json_reader_takes_or_a_request_of_a_form_the_sdk_refuses_is_answered(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    find = '"method":"tools/call","params":{"name":"find","arguments":{"name":"Apollo"}}'
    lines = ['{"id":11,' + find + "}", '{"jsonrpc":"1.0","id":12,' + find + "}",
             '{"jsonrpc":"2.0","id":13,"method":"tools/call","params":[1]}',
             '[{"jsonrpc":"2.0","id":14,' + find + "}]", '{"jsonrpc":"2.0","id":15,' + find + ",}",
             call(16, "find", '{"name":"Apollo","bogus":' + nested(62) + "}"),  # 65 deep in the message
             '{"jsonrpc":"2.0","method":"notifications/bogus","params":[1]}', call(17, "find", '{"name":"Apollo"}')]
    replies = over_stdio(monkeypatch, workspace, lines, {11, 12, 13, None, 16, 17})
    invalid = {"code": -32600, "message": "Invalid Request: send one JSON-RPC 2.0 request a line"}
    assert [replies[id]["error"] for id in (11, 12, 13)] == [invalid] * 3
    assert replies[None]["error"]["code"] in (-32600, -32700)  # the batch, and the line with a trailing comma
    assert [b["text"] for b in replies[16]["result"]["content"]] == [DEEP]
    assert replies[17]["result"]["structuredContent"]["matches"] == ["projects/apollo"]
    assert workspace.store.records(TOLD) == []
