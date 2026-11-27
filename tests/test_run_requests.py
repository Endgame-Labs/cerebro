"""A run's requests to a provider, over real loopback HTTP to a stand-in for Fireworks' chat API: each reply is
streamed, so it takes as long as it keeps coming, and a try that stalls, is cut short, times out, meets a rate limit
or a server error is tried again up to its model's retries, checked against the cap first, and counted as spent."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from suites.agent_run import AGENT, EVIDENCE
from test_tokens import LOOPBACK, no_network  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro.agent import Budget
from cerebro.budget import METER, Meter, MeterTurn
from cerebro.gateway import Gateway, Route
from cerebro.models import cost, read_model
from cerebro.run import PydanticRun

TEXT = "## Summary\n\nIt moved [^f_1].\n\n## Next\n\nWatch March."
ARGS = json.dumps({"text": TEXT})
CALL = {"id": "call_1", "type": "function", "function": {"name": "write_article", "arguments": ARGS}}
USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


class Provider(ThreadingHTTPServer):
    """Answers each request with the next of `script`, and the last once it runs out: a status, `steady` (the hand-in
    in pieces, `GAP` seconds apart, or whole after all of them when the request asks for no stream), `stall`, `late`
    (steady after a wait of five gaps for its first piece), or `cut` (every piece, then closed with no finish)."""

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
            time.sleep(5 * self.server.GAP if step == "late" else 0)
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
            if step == "cut":
                return
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
    of its gaps, and the wait for a reply to start to ten."""
    monkeypatch.setattr("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True)
    monkeypatch.setattr("cerebro.run.STALL", 3 * Provider.GAP)
    monkeypatch.setattr("cerebro.run.FIRST_BYTE", 10 * Provider.GAP)
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


@contextmanager
def metered(meter: Meter) -> Iterator[MeterTurn]:
    held = METER.set(turn := meter.turn("a run"))
    try:
        yield turn
    finally:
        METER.reset(held)


def test_a_reply_that_keeps_coming_lands_past_the_stall_limit_and_a_stalled_try_is_tried_again_and_counted(
        provider: Any) -> None:
    server, run = provider(["stall", "steady"])
    with metered(Meter(100.0, lambda: 0.0)) as turn:
        done = run.run(AGENT, EVIDENCE, [], Budget())
    assert done.status == "done" and done.result is not None and done.result.text == TEXT
    assert server.asked == done.usage.requests == 2 and done.usage.output_tokens == USAGE["completion_tokens"]
    assert done.usage.input_tokens > USAGE["prompt_tokens"]  # the stalled try read what the run held
    assert turn.usd == pytest.approx(cost("fireworks", "glm-5p3", done.usage))


def test_a_reply_that_starts_late_after_a_silence_longer_than_the_stall_limit_lands_on_its_first_try(
        provider: Any) -> None:
    server, run = provider(["late"])
    assert run.run(AGENT, EVIDENCE, [], Budget()).status == "done" and server.asked == 1


def test_each_try_again_of_a_model_call_is_checked_against_the_spending_cap_before_it_starts(provider: Any) -> None:
    server, run = provider([503, "steady"])
    spent = iter([0.0])  # under the cap for the first try, and past it by the second
    with metered(Meter(1.0, lambda: next(spent, 5.0))):
        assert run.run(AGENT, EVIDENCE, [], Budget()).status == "cut"
    assert server.asked == 1


@pytest.mark.parametrize(("job", "own", "script", "asked", "status"), [
    ({}, None, [429, 408, 503], 4, "failed"),
    ({"retries": 1}, {}, [503], 2, "failed"),
    ({"retries": 1}, {"retries": 0}, [429], 1, "failed"),
    ({}, None, ["cut", "steady"], 2, "done"),
    ({}, None, [401], 1, "refused"),
])
def test_a_failed_try_is_tried_again_up_to_the_agents_retries_then_its_jobs_unless_its_key_is_refused(
        provider: Any, job: dict[str, int], own: dict[str, int] | None, script: list[Any], asked: int,
        status: str) -> None:
    server, run = provider(script, **job)
    agent = AGENT if own is None else replace(AGENT, model={"provider": "fireworks", "model": "glm-5p3", **own})
    done = run.run(agent, EVIDENCE, [], Budget())
    assert done.status == status and server.asked == done.usage.requests == asked
