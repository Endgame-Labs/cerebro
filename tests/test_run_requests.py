"""A run's requests to a provider, over real loopback HTTP to a stand-in for Fireworks' chat API: each reply is
streamed, so it takes as long as it keeps coming, and a request that stalls, meets a rate limit or a server error is
tried again up to its model's retries."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from suites.agent_run import AGENT, EVIDENCE
from test_tokens import LOOPBACK, no_network  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro.agent import Budget
from cerebro.gateway import Gateway, Route
from cerebro.models import read_model
from cerebro.run import PydanticRun

TEXT = "## Summary\n\nIt moved [^f_1].\n\n## Next\n\nWatch March."
ARGS = json.dumps({"text": TEXT})
CALL = {"id": "call_1", "type": "function", "function": {"name": "write_article", "arguments": ARGS}}
USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


class Provider(ThreadingHTTPServer):
    """Answers each request with the next of `script`, and the last once it runs out: a status, `steady` (the hand-in
    in pieces, `GAP` seconds apart, or whole after all of them when the request asks for no stream), or `stall`."""

    GAP, PIECES = 0.15, 6

    def __init__(self, script: list[Any]):
        super().__init__((LOOPBACK, 0), _Answer)
        self.script, self.asked = script, 0


class _Answer(BaseHTTPRequestHandler):
    server: Provider

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        streamed = json.loads(self.rfile.read(int(self.headers["Content-Length"]))).get("stream")
        step = self.server.script[min(self.server.asked, len(self.server.script) - 1)]
        self.server.asked += 1
        if isinstance(step, int):
            self._send(step, {"error": {"message": "scripted", "code": step}}, {"Retry-After": "0"})
        elif not streamed:
            time.sleep(self.server.GAP * self.server.PIECES)
            self._send(200, {"id": "c", "object": "chat.completion", "created": 0, "model": "m", "usage": USAGE,
                             "choices": [{"index": 0, "finish_reason": "tool_calls",
                                          "message": {"role": "assistant", "content": None, "tool_calls": [CALL]}}]})
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            size = -(-len(ARGS) // self.server.PIECES)
            pieces = [ARGS[i:i + size] for i in range(0, len(ARGS), size)]
            self._event({"role": "assistant", "tool_calls": [{**CALL, "function": {"name": "write_article",
                                                                                  "arguments": ""}, "index": 0}]})
            for n, piece in enumerate(pieces):
                if step == "stall" and n:
                    time.sleep(10 * self.server.GAP)
                    return
                time.sleep(self.server.GAP)
                self._event({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
            self._event({}, "tool_calls")
            self._line({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": [],
                        "usage": USAGE})
            self.wfile.write(b"data: [DONE]\n\n")

    def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        for name, value in {"Content-Type": "application/json", "Content-Length": str(len(data)),
                            **(headers or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _event(self, delta: dict[str, Any], finish: str | None = None) -> None:
        self._line({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    def _line(self, chunk: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.flush()


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Starts a stand-in provider for a script and gives a run on it: the compilation model settings.yaml names as
    `{ provider: fireworks, model: glm-5p3 }` and `more`, on the stand-in's host, with the stall limit cut to three
    of its gaps."""
    monkeypatch.setattr("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True)
    monkeypatch.setattr("cerebro.run.STALL", 3 * Provider.GAP)
    started: list[Provider] = []

    def start(script: list[Any], **more: Any) -> tuple[Provider, PydanticRun]:
        server = Provider(script)
        started.append(server)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        route = Route("fireworks", f"http://{LOOPBACK}:{server.server_port}")
        gateway = Gateway({"fireworks": route}, lambda: datetime(2026, 11, 27, tzinfo=UTC), UTC)
        spec = read_model({"provider": "fireworks", "model": "glm-5p3", **more}, "settings.yaml models.compilation")
        return server, PydanticRun.from_spec(spec, key=lambda provider: "stand-in", gateway=gateway)

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def test_a_reply_that_keeps_coming_lands_past_the_stall_limit_and_a_stalled_one_is_tried_again(provider: Any) -> None:
    server, run = provider(["stall", "steady"])
    done = run.run(AGENT, EVIDENCE, [], Budget())
    assert done.status == "done" and done.result is not None and done.result.text == TEXT
    assert server.asked == 2 and done.usage.output_tokens == USAGE["completion_tokens"]


@pytest.mark.parametrize(("job", "own", "script", "asked", "status"), [
    ({}, None, [429], 4, "failed"),
    ({"retries": 1}, None, [503], 2, "failed"),
    ({"retries": 1}, 0, [429], 1, "failed"),
    ({}, None, [401], 1, "refused"),
])
def test_a_rate_limit_or_a_server_error_is_tried_again_up_to_the_agents_retries_then_its_jobs_and_a_refused_key_is_not(
        provider: Any, job: dict[str, int], own: int | None, script: list[Any], asked: int, status: str) -> None:
    server, run = provider(script, **job)
    agent = AGENT if own is None else replace(AGENT, model={"provider": "fireworks", "model": "glm-5p3",
                                                            "retries": own})
    assert run.run(agent, EVIDENCE, [], Budget()).status == status
    assert server.asked == asked
