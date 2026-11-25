"""Tokens for the HTTP server: a token names a caller, a request proves who it is with one, and the token itself is
never written anywhere. Each server here runs over real loopback HTTP, bound to port 0 on 127.0.0.1."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import uvicorn.server
from click.testing import CliRunner
from test_mcp import workspace_at

from cerebro import Caller, Cerebro, cli
from cerebro.cli import main
from cerebro.mcp import http_server
from cerebro.mcp import serve as serve_mcp
from cerebro.tokens import (
    BODY_CAP,
    HEALTH,
    LOOPBACK_NAMES,
    MASKED,
    ONCE,
    PREFIX,
    Gate,
    HostError,
    Token,
    TokenError,
    Tokens,
    allowed_hosts,
    is_loopback,
)

LOOPBACK = "127.0.0.1"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
           "MCP-Protocol-Version": "2025-06-18"}
HOST = (b"host", b"127.0.0.1:4747")
TOKEN_SHAPE = re.compile(rf"{PREFIX}[A-Za-z0-9_-]{{43}}")
AUTH = (b"authorization", b"Bearer " + PREFIX.encode() + b"0" * 43)


class Admit:
    """Tokens that take any token: for a test of the gate's other checks."""

    def check(self, authorization: str | None) -> Token | None:
        return Token("agent-a", "2026-11-18T00:00:00Z") if authorization else None


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback alone, in place of the suite's rule of no sockets at all: a socket may reach 127.0.0.1 and nothing
    else, and no name but 127.0.0.1 resolves."""
    connect, getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def to_loopback(self: socket.socket, address: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] != LOOPBACK:
            raise OSError("tests reach loopback alone")
        connect(self, address)

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != LOOPBACK:
            raise OSError("tests resolve loopback alone")
        return getaddrinfo(host, *args, **kwargs)

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("tests do not reach the network")

    monkeypatch.setattr(socket.socket, "connect", to_loopback)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    for name in ("connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, refuse)
    for name in ("gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, refuse)


class _Ticks:
    """`asyncio` as uvicorn's server module sees it while a test serves, with its 0.1 second polls, of the stop flag
    and of the connections a stop waits on, cut to 5 ms: a stop takes as long, and in what order, as uvicorn makes it,
    without two fixed waits a test would sit through."""

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    @staticmethod
    def sleep(delay: float, *args: Any) -> Any:
        return asyncio.sleep(min(delay, 0.005), *args)


@contextmanager
def serving(workspace: Cerebro, host: str = LOOPBACK, allowed: tuple[str, ...] = ()) -> Iterator[httpx.Client]:
    """The workspace's HTTP server as `serve` builds it, run on a socket bound to an ephemeral port of 127.0.0.1
    whatever `host` says, and a client of it. `host` sets only how the server treats requests, and `allowed` its
    Host names."""
    server = http_server(workspace, host, 0, allowed or None)
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)  # asyncio sets TCP_NODELAY
    # only on a socket that names TCP, as `serve`'s own does: without it each reply on a kept connection waits 40 ms
    bound.bind((LOOPBACK, 0))
    port = bound.getsockname()[1]
    assert port != 0
    uvicorn.server.asyncio = _Ticks()  # type: ignore[assignment]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [bound]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert server.started
    try:
        with httpx.Client(base_url=f"http://{LOOPBACK}:{port}", timeout=5) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(5)
        bound.close()
        uvicorn.server.asyncio = asyncio


def _messages(response: httpx.Response) -> list[dict[str, Any]]:
    if response.headers.get("content-type", "").startswith("application/json"):
        return [response.json()]
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


class Session:
    """An MCP session over HTTP, opened with `initialize` under a client name, each request carrying `token`."""

    def __init__(self, client: httpx.Client, token: str | None, name: str = "desk-agent",
                 extra: dict[str, str] | None = None):
        self.client, self.token, self.ids, self.extra = client, token, 0, extra or {}
        opened = self.post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": name, "version": "1"}})
        assert opened.status_code == 200, opened.text
        self.id = opened.headers["mcp-session-id"]
        self.post("notifications/initialized", None, notify=True)

    def headers(self) -> dict[str, str]:
        auth = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        session = {"Mcp-Session-Id": self.id} if hasattr(self, "id") else {}
        return HEADERS | auth | session | self.extra

    def post(self, method: str, params: dict[str, Any] | None, notify: bool = False) -> httpx.Response:
        self.ids += 1
        body = {"jsonrpc": "2.0", "method": method} | ({"params": params} if params is not None else {}) | (
            {} if notify else {"id": self.ids})
        return self.client.post("/mcp", json=body, headers=self.headers())

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        answered = self.post("tools/call", {"name": tool, "arguments": arguments})
        assert answered.status_code == 200, answered.text
        return _messages(answered)[-1]["result"]


def call_once(client: httpx.Client, token: str | None, tool: str = "read",
              arguments: dict[str, Any] | None = None) -> httpx.Response:
    """One tools/call with no session: a gate that refuses answers before the SDK would ask for one."""
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {"ids": ["projects/apollo"]}}}
    return client.post("/mcp", json=body, headers=HEADERS | auth)


class Counted:
    """Counts each call of a workspace's operations and each article its store reads."""

    def __init__(self, workspace: Cerebro, monkeypatch: pytest.MonkeyPatch):
        self.calls: list[str] = []
        for name in ("find", "query", "read", "tell", "decide", "retract"):
            monkeypatch.setattr(workspace, name, self._counting(name, getattr(workspace, name)))
        monkeypatch.setattr(workspace.store, "article", self._counting("article", workspace.store.article))

    def _counting(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return fn(*args, **kwargs)

        return counted


# -- making, listing and revoking ----------------------------------------------------------------------------------

def test_a_token_is_256_random_bits_behind_a_fixed_prefix_and_the_store_keeps_only_its_hash(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    made = workspace.make_token("agent-a")
    other = workspace.make_token("agent-b")
    assert TOKEN_SHAPE.fullmatch(made.token) and TOKEN_SHAPE.fullmatch(other.token) and made.token != other.token
    rows = [row.data for row in workspace.store.rows("serve-tokens")]
    assert [row["name"] for row in rows] == ["agent-a", "agent-b"]
    assert all(set(row) == {"name", "hash", "made", "id"} and re.fullmatch("[0-9a-f]{64}", row["hash"])
               for row in rows) and rows[0]["id"] != rows[1]["id"]
    assert made.token not in json.dumps(rows) and other.token not in json.dumps(rows)


def test_a_live_name_is_refused_again_and_a_revoked_one_is_made_anew_with_a_new_token(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    first = workspace.make_token("agent-a")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", first.record.made)
    assert first.record == Token("agent-a", first.record.made) == workspace.tokens()[0]
    with pytest.raises(TokenError, match="^a token named agent-a exists already: revoke it with cerebro tokens "
                                         "revoke agent-a, or pick another name$"):
        workspace.make_token("agent-a")
    keeper = Tokens(workspace.store, workspace.clock)
    assert keeper.check(f"Bearer {first.token}") is not None and workspace.tokens()[0].used != ""
    assert workspace.revoke_token("agent-a") == Token("agent-a", first.record.made) and workspace.tokens() == []
    with pytest.raises(TokenError, match="no live token is named agent-a: cerebro tokens list shows them"):
        workspace.revoke_token("agent-a")
    again = workspace.make_token("agent-a")
    assert workspace.tokens()[0].used == ""  # the name's last use was by the token it had before
    assert again.token != first.token and keeper.check(f"Bearer {first.token}") is None
    assert keeper.check(f"Bearer {again.token}").caller == Caller("agent-a", "person")  # type: ignore[union-attr]


@pytest.mark.parametrize(("name", "says"), [
    ("", ("is not one cerebro takes: use letters, digits, dot, dash or underscore, starting with a letter or digit, "
          "at most 64$")),
    ("has space", "is not one cerebro takes"), ("-dash", "is not one cerebro takes"),
    ("x" * 65, "is not one cerebro takes"),
    ("you", "the name Cerebro gives a caller with no token"), ("MCP", "the name Cerebro gives a caller with no token"),
])
def test_a_token_name_cerebro_does_not_take_is_refused_in_one_line(tmp_path: Path, name: str, says: str) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    with pytest.raises(TokenError, match=says):
        workspace.make_token(name)
    assert workspace.store.rows("serve-tokens") == []


def test_a_check_takes_only_a_bearer_token_of_the_prefix_and_stamps_when_it_was_last_used(tmp_path: Path) -> None:
    times = iter(["2026-11-17T09:00:00+00:00", "2026-11-17T10:00:00+00:00", "2026-11-17T10:00:00+00:00"])
    keeper = Tokens(workspace_at(tmp_path / "workspace").store, lambda: datetime.fromisoformat(next(times)))
    made = keeper.make("agent-a")
    assert keeper.live()[0].used == ""
    for wrong in ("", made.token, f"Basic {made.token}", "Bearer", f"Bearer {made.token[len(PREFIX):]}",
                  f"Bearer {made.token}x", f"Bearer {made.token[:-1]}"):
        assert keeper.check(wrong) is None, wrong
    assert keeper.live()[0].used == ""
    assert keeper.check(f"bearer   {made.token} ") == Token("agent-a", "2026-11-17T09:00:00Z", "2026-11-17T10:00:00Z")
    assert keeper.live() == [Token("agent-a", "2026-11-17T09:00:00Z", "2026-11-17T10:00:00Z")]


def test_tokens_new_prints_the_token_once_and_list_and_revoke_never_show_it(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    runner = CliRunner()
    made = runner.invoke(main, ["tokens", "new", "agent-a", "--workspace", str(root)])
    assert made.exit_code == 0, made.output
    token = made.stdout.splitlines()[0]
    assert TOKEN_SHAPE.fullmatch(token) and made.stdout.count(token) == 1 and token not in made.stderr
    assert made.stdout.splitlines()[1].startswith("made     agent-a, a caller, at ")
    person = runner.invoke(main, ["tokens", "new", "desk", "--workspace", str(root)])
    listed = runner.invoke(main, ["tokens", "list", "--workspace", str(root)])
    as_json = runner.invoke(main, ["tokens", "list", "--json", "--workspace", str(root)])
    assert listed.stdout.splitlines()[0].startswith("agent-a  made ") and listed.stdout.endswith(
        "last used never\n2 tokens\n")
    assert [(row["name"], row["used"]) for row in json.loads(as_json.stdout)] == [("agent-a", ""), ("desk", "")]
    revoked = runner.invoke(main, ["tokens", "revoke", "agent-a", "--workspace", str(root)])
    assert revoked.stdout == "revoked  agent-a: the next request that carries its token is refused\n"
    again = runner.invoke(main, ["tokens", "revoke", "agent-a", "--workspace", str(root)])
    assert again.exit_code == 1 and "no live token is named agent-a" in again.output
    second = person.stdout.splitlines()[0]
    hashes = [row.data.get("hash") for row in Cerebro(root).store.rows("serve-tokens")]
    for shown in (listed, as_json, revoked, again, person):
        assert token not in shown.output and all(h not in shown.output for h in hashes if h)
    for kept in root.rglob("*"):
        if kept.is_file():
            assert token.encode() not in kept.read_bytes() and second.encode() not in kept.read_bytes(), kept
    assert runner.invoke(main, ["tokens", "list", "--workspace", str(root)]).stdout == (
        listed.stdout.splitlines()[1] + "\n1 token\n")


# -- serving over HTTP ---------------------------------------------------------------------------------------------

def test_on_loopback_with_no_token_every_request_but_healthz_is_refused_whatever_the_store_says(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    counted = Counted(workspace, monkeypatch)
    with serving(workspace) as client:
        bare = call_once(client, None)
        proxied = client.post("/mcp", headers=HEADERS | {"X-Forwarded-For": "203.0.113.9"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "you", "version": "1"}}})
        health = client.get("/healthz")
    assert bare.status_code == proxied.status_code == 401 and health.status_code == 200
    assert bare.text == ("this workspace needs a token: send Authorization: Bearer <token>, made with cerebro tokens new "
                         "<name>\n")
    workspace.make_token("agent-a")
    workspace.revoke_token("agent-a")
    with serving(workspace) as client:
        assert call_once(client, None).status_code == 401
    fresh = workspace_at(tmp_path / "moved")  # a store that never saw a token, as a moved store.path leaves a workspace
    with serving(fresh) as client:
        assert call_once(client, None).status_code == 401
    assert counted.calls == [] and workspace.store.rows("mcp-requests") == fresh.store.rows("mcp-requests") == []


def test_serve_on_loopback_with_no_live_token_starts_and_says_every_http_caller_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    served = _serve_stub(monkeypatch)
    result = CliRunner().invoke(main, ["serve", "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[1:] == [
        "serving   MCP on localhost, a pass every 30 seconds. Ctrl-C stops.",
        "mcp       http://127.0.0.1:4747/mcp",  # where it listens, said at start
        "api       http://127.0.0.1:4747/api/v1, the JSON API",
        "health    http://127.0.0.1:4747/healthz, which answers with no token",
        ("tokens    this workspace has no live token, so every HTTP caller is refused until one is made with cerebro "
         "tokens new <name>"),
        "stopped   no request is open and no pass is under way"]
    assert served == [(None, "127.0.0.1", None)]


def test_once_a_token_exists_a_request_needs_it_and_the_caller_is_the_token_never_the_clients_name(
        tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    with serving(workspace) as client:
        bare = call_once(client, None)
        assert bare.status_code == 401
        assert bare.headers["www-authenticate"] == 'Bearer realm="cerebro"'
        assert bare.text == ("this workspace needs a token: send Authorization: Bearer <token>, made with cerebro tokens "
                             "new <name>\n")
        session = Session(client, agent.token, name="someone-else")
        told = session.call("tell", {"text": "The review moved.", "target": "projects/apollo", "as": "fact"})
        read = session.call("read", {"ids": ["projects/apollo"]})
    assert workspace.store.record(told["structuredContent"]["record"]).by == Caller("agent-a", "person")
    assert "# projects/apollo: Apollo" in read["content"][0]["text"]
    assert [row.data["caller"] for row in workspace.store.rows("mcp-requests")] == ["agent-a", "agent-a"]
    assert workspace.tokens()[0].used != ""


def test_a_tokens_command_over_http_is_accepted_as_sent_and_its_decide_decides(tmp_path: Path) -> None:
    from cerebro.records import Proposal, proposals, propose

    workspace = workspace_at(tmp_path / "workspace")
    (tmp_path / "workspace" / "agents" / "projects").mkdir(parents=True)
    (tmp_path / "workspace" / "agents" / "projects" / "curator.md").write_text("---\n---\n\nKeep projects.\n")
    propose(workspace.store, Proposal("p_waits", "article.admit", "projects/zeta", Caller("projects/curator", "agent"),
                                      "2026-11-17T09:00:00Z"))
    desk = workspace.make_token("desk")
    with serving(workspace) as client:
        session = Session(client, desk.token)
        archive = session.call("tell", {"text": "Apollo shut down.", "target": "projects/apollo", "as": "proposal",
                                        "kind": "article.archive"})["structuredContent"]
        decided = session.call("decide", {"accept": ["p_waits"]})["structuredContent"]
    [filed] = archive["proposals"]
    assert archive["notes"][-1] == f"Accepted {filed}, archive of projects/apollo: its curator carries it out on its " \
        "next run."
    assert decided["answers"] == {"p_waits": "decided"}
    rows = {p.id: p for p in proposals(workspace.store)}
    assert {(rows[id].status, rows[id].decided_by) for id in (filed, "p_waits")} == {("accepted", Caller("desk", "person"))}


def test_a_refused_request_runs_no_tool_reads_no_article_and_a_revoke_holds_from_the_next_request(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    counted = Counted(workspace, monkeypatch)
    with serving(workspace) as client:
        session = Session(client, agent.token)
        counted.calls.clear()  # opening a session reads the collections it names
        assert session.call("read", {"ids": ["projects/apollo"]})["structuredContent"]["articles"]
        assert counted.calls[0] == "read" and "article" in counted.calls  # the counter sees what a call does
        counted.calls.clear()
        fake = PREFIX + "0" * 43
        for token in (None, fake, "not-a-token"):
            refused = call_once(client, token)
            assert refused.status_code == 401 and "www-authenticate" in refused.headers
        workspace.revoke_token("agent-a")
        revoked = session.post("tools/call", {"name": "read", "arguments": {"ids": ["projects/apollo"]}})
        assert revoked.status_code == 401
        assert revoked.headers["www-authenticate"] == 'Bearer realm="cerebro", error="invalid_token"'
        assert revoked.text == ("this token is not accepted: it is unknown or was revoked: make a new one with "
                                "cerebro tokens new <name>\n")
        assert call_once(client, None).status_code == 401  # revoking the last token does not open loopback again
    assert counted.calls == [] and len(workspace.store.rows("mcp-requests")) == 1


def test_healthz_answers_with_no_token_a_fixed_body_and_headers_that_name_nothing(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    workspace.make_token("agent-a")
    with serving(workspace) as client:
        answer = client.get("/healthz")
        head = client.head("/healthz")
        posted = client.post("/healthz")
    assert (answer.status_code, answer.http_version, answer.content) == (200, "HTTP/1.1", HEALTH) == (
        200, "HTTP/1.1", b"ok\n")
    assert [name for name, _ in answer.headers.multi_items()] == ["date", "content-type", "content-length"]
    assert re.fullmatch(r"[A-Z][a-z]{2}, \d\d [A-Z][a-z]{2} \d{4} \d\d:\d\d:\d\d GMT", answer.headers["date"])
    assert answer.headers["content-type"] == "text/plain; charset=utf-8" and answer.headers["content-length"] == "3"
    assert (head.status_code, head.content, head.headers["content-length"]) == (200, b"", "3")
    assert (posted.status_code, posted.headers["allow"], posted.text) == (405, "GET, HEAD", "healthz answers GET\n")
    assert workspace.store.rows("mcp-requests") == []


def test_a_body_over_the_cap_is_refused_by_its_length_before_any_of_it_is_read_and_no_tool_runs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    counted = Counted(workspace, monkeypatch)
    with serving(workspace) as client:
        port = client.base_url.port
        with socket.create_connection((LOOPBACK, port), timeout=5) as raw:  # the head alone: no byte of the body
            raw.sendall((f"POST /mcp HTTP/1.1\r\nHost: {LOOPBACK}:{port}\r\nAuthorization: Bearer {agent.token}\r\n"
                         f"Content-Type: application/json\r\nContent-Length: {BODY_CAP + 1}\r\n\r\n").encode())
            head = b""
            while b"send a smaller one" not in head and (got := raw.recv(4096)):
                head += got
        head = head.decode()
        whole = '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read", "arguments": ' \
                '{"ids": ["projects/apollo"], "about": "' + "x" * BODY_CAP + '"}}}'
        chunked = client.post("/mcp", content=iter([whole[i:i + 65536].encode() for i in range(0, len(whole), 65536)]),
                              headers=HEADERS | {"Authorization": f"Bearer {agent.token}"})
    assert head.startswith("HTTP/1.1 413 ") and "larger than 1048576 bytes" in head
    assert chunked.request.headers["transfer-encoding"] == "chunked" and "content-length" not in chunked.request.headers
    assert chunked.status_code == 413
    assert counted.calls == [] and workspace.store.rows("mcp-requests") == []


def test_the_gate_stops_reading_a_body_with_no_length_as_soon_as_it_passes_the_cap() -> None:
    reached: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["path"])

    pulled, sent = 0, []

    async def receive() -> dict[str, Any]:
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": b"x" * 100, "more_body": True}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    gate = Gate(app, Admit(), cap=1000)  # type: ignore[arg-type]
    anyio.run(gate, {"type": "http", "path": "/mcp", "method": "POST", "headers": [HOST, AUTH]}, receive, send)
    assert pulled == 11 and reached == [] and sent[0]["status"] == 413
    wrong = []

    async def unread() -> dict[str, Any]:
        raise AssertionError("the body was read")

    async def keep(message: dict[str, Any]) -> None:
        wrong.append(message)

    anyio.run(gate, {"type": "http", "path": "/mcp", "method": "POST",
                     "headers": [HOST, AUTH, (b"content-length", b"1001")]}, unread, keep)
    anyio.run(gate, {"type": "http", "path": "/mcp", "method": "POST",
                     "headers": [HOST, AUTH, (b"content-length", b"-1")]}, unread, keep)
    assert [m["status"] for m in wrong if m["type"] == "http.response.start"] == [413, 400] and reached == []


def test_a_body_within_the_cap_reaches_the_app_whole_and_the_app_still_hears_the_client_after() -> None:
    heard: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        heard.extend([await receive(), await receive()])

    chunks = iter([{"type": "http.request", "body": b"ab", "more_body": True},
                   {"type": "http.request", "body": b"cd", "more_body": False},
                   {"type": "http.disconnect"}])

    async def receive() -> dict[str, Any]:
        return next(chunks)

    async def send(message: dict[str, Any]) -> None:
        raise AssertionError(message)

    anyio.run(Gate(app, Admit(), cap=4), {"type": "http", "path": "/mcp", "method": "POST",  # type: ignore[arg-type]
                                                             "headers": [HOST, AUTH, (b"content-length", b"4")]},
              receive, send)
    assert heard == [{"type": "http.request", "body": b"abcd", "more_body": False}, {"type": "http.disconnect"}]


# -- binding another address --------------------------------------------------------------------------------------

@pytest.fixture
def binds(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every address a socket is bound to from here on."""
    seen: list[Any] = []
    bind = socket.socket.bind

    def recorded(self: socket.socket, address: Any) -> None:
        seen.append(address)
        bind(self, address)

    monkeypatch.setattr(socket.socket, "bind", recorded)
    return seen


def test_serve_on_an_address_not_loopback_with_no_token_is_refused_before_anything_is_bound(
        tmp_path: Path, binds: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    passes: list[Any] = []
    monkeypatch.setattr(cli, "run_forever", lambda **kwargs: passes.append(kwargs))
    refused = CliRunner().invoke(main, ["serve", "--host", "0.0.0.0", "--workspace", str(root)])
    assert refused.exit_code == 1
    assert refused.output.splitlines()[-1] == (
        "Error: serve --host 0.0.0.0 is not loopback, and this workspace has no token: make one with cerebro tokens new "
        "<name> first, so a caller must prove who it is")
    workspace = workspace_at(tmp_path / "other")
    with pytest.raises(TokenError, match="cerebro tokens new"):
        serve_mcp(workspace, 0, "0.0.0.0")
    assert binds == [] and passes == []


def test_serve_on_another_address_with_a_token_says_it_does_not_end_tls(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    CliRunner().invoke(main, ["tokens", "new", "agent-a", "--workspace", str(root)])
    served: list[Any] = []
    monkeypatch.setattr(cli, "run_forever", lambda **kwargs: None)
    taken_port(monkeypatch)
    monkeypatch.setattr("cerebro.mcp.serve", lambda workspace, port, host, allowed, **k: served.append((port, host,
                                                                                                    allowed)))
    result = CliRunner().invoke(main, ["serve", "--host", "0.0.0.0", "--port", "5050", "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[1:] == [
        "serving   MCP on 0.0.0.0, a pass every 30 seconds. Ctrl-C stops.",
        "mcp       http://0.0.0.0:5050/mcp",
        "api       http://0.0.0.0:5050/api/v1, the JSON API",
        "health    http://0.0.0.0:5050/healthz, which answers with no token",
        "tokens    every request needs a token: cerebro tokens list shows them",
        "note      cerebro does not end TLS: serve this address only behind something that does",
        "note      only a loopback Host is accepted: list each name this workspace is reached by with --allowed-host <name>",
        "stopped   no request is open and no pass is under way"]
    assert served == [(5050, "0.0.0.0", None)]
    listed = CliRunner().invoke(main, ["serve", "--host", "0.0.0.0", "--allowed-host", "wiki.example",
                                       "--allowed-host", "Mirror.Example", "--workspace", str(root)])
    assert listed.stdout.splitlines()[6:] == [
        "hosts     a request must name one of these in its Host: wiki.example, mirror.example",
        "note      cerebro does not end TLS: serve this address only behind something that does",
        "stopped   no request is open and no pass is under way"]
    assert served[-1] == (None, "0.0.0.0", ("wiki.example", "Mirror.Example"))


def test_on_another_address_a_request_needs_a_token_even_after_the_last_is_revoked(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    with serving(workspace, host="0.0.0.0") as client:
        assert Session(client, agent.token).call("find", {"name": "Apollo"})["structuredContent"]["matches"]
        workspace.revoke_token("agent-a")
        assert call_once(client, None).status_code == 401
        assert call_once(client, agent.token).status_code == 401


@pytest.mark.parametrize(("host", "loopback"), [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True), ("127.0.0.2", True),
    ("0.0.0.0", False), ("::", False), ("10.0.0.5", False), ("wiki.example", False), ("", False)])
def test_loopback_is_localhost_and_the_loopback_addresses_alone(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


# -- the token never leaks ------------------------------------------------------------------------------------------

def test_a_token_planted_in_every_place_it_could_reach_is_absent_from_each(
        tmp_path: Path, capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    workspace = workspace_at(tmp_path / "workspace")
    made = workspace.make_token("agent-a")
    token = made.token
    seen = {"repr of the new token": repr(made), "str of the new token": str(made),
            "repr of its record": repr(made.record), "str of its record": str(made.record),
            "repr of its caller": repr(made.record.caller), "str of its caller": str(made.record.caller),
            "the app's list of tokens": repr(workspace.tokens())}

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the store went away mid-request")

    with serving(workspace) as client:
        session = Session(client, token)
        session.call("tell", {"text": "The review moved.", "target": "projects/apollo", "as": "fact"})
        responses = {"no token": call_once(client, None), "an unknown token": call_once(client, PREFIX + "1" * 43),
                     "a body over the cap": client.post("/mcp", content=b"x" * (BODY_CAP + 1),
                                                        headers=HEADERS | {"Authorization": f"Bearer {token}"})}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(workspace, "read", broken)
            responses["a tool that raises"] = session.post("tools/call", {"name": "read",
                                                                          "arguments": {"ids": ["x"]}})
            patch.setattr(workspace.store, "state", lambda key: None)
            patch.setattr(workspace.store, "set_state", broken)  # the check itself fails, the token in hand
            responses["a check that raises"] = session.post("tools/call", {"name": "find",
                                                                           "arguments": {"name": "A"}})
        workspace.revoke_token("agent-a")
        responses["a revoked token"] = session.post("tools/call", {"name": "find", "arguments": {"name": "A"}})
        responses["the health route"] = client.get("/healthz", headers={"Authorization": f"Bearer {token}"})
    assert [r.status_code for r in responses.values()] == [401, 401, 413, 200, 500, 401, 200]
    assert _messages(responses["a tool that raises"])[-1]["result"]["isError"]
    seen |= {f"the reply to {what}": f"{r.status_code} {r.headers} {r.text}" for what, r in responses.items()}
    seen |= {f"the {log} log": json.dumps([row.data for row in workspace.store.rows(log)])
             for log in ("mcp-requests", "serve-tokens", "proposals", "merges", "agent-runs", "spend")}
    seen["the state of when it was used"] = json.dumps(workspace.store.state("serve-tokens/used/agent-a"))
    seen["the records kept"] = repr(workspace.store.records("tell"))
    out, err = capfd.readouterr()
    seen |= {"standard output": out, "standard error": err, "every log line of the process": caplog.text}
    trace = err + caplog.text  # uvicorn writes its own log to standard error
    assert "Exception in ASGI application" in trace and "the store went away mid-request" in trace
    assert [row.data["caller"] for row in workspace.store.rows("mcp-requests")] == ["agent-a", "agent-a"]
    for place, text in seen.items():
        assert token not in text and token[len(PREFIX):] not in text, place


def test_a_token_a_client_pastes_into_a_tools_arguments_is_masked_in_the_request_log(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    token = workspace.make_token("agent-a").token
    with serving(workspace) as client:
        session = Session(client, token)
        session.call("find", {"name": token})
        session.call("tell", {"text": f"{'x' * 1000} my token is {token}, keep it", "target": "projects/apollo",
                              "as": "fact"})  # astride the 1 KiB cut of a logged string
        session.call("decide", {"accept": [{"id": token, "reason": f"see {token}"}], "reject": [token]})
        session.call("query", {"from": "projects", token: "open"})  # as a filter's key
    rows = workspace.store.rows("mcp-requests")
    assert [row.data["tool"] for row in rows] == ["find", "tell", "decide", "query"]
    assert all(row.data["caller"] == "agent-a" for row in rows)
    logged = json.dumps([row.data for row in rows])
    assert token not in logged and token[len(PREFIX):] not in logged
    assert rows[0].data["args"] == {"name": MASKED}
    told = f"{'x' * 1000} my token is {MASKED}, keep it"  # masked first, then cut at 1 KiB
    assert rows[1].data["args"]["text"] == f"{told[:1024]}… ({len(told)} bytes)"
    assert rows[2].data["args"]["accept"] == [{"id": MASKED, "reason": f"see {MASKED}"}]
    assert rows[2].data["args"]["reject"] == [MASKED] and MASKED in rows[3].data["args"]
    assert MASKED.startswith("[") and not re.search(r"[A-Za-z0-9_-]{43}", MASKED)


# -- the Host a request names --------------------------------------------------------------------------------------

def test_the_host_names_come_from_the_flags_else_the_settings_else_loopback_and_a_pattern_is_refused() -> None:
    settings = {"serve": {"allowed_hosts": ["settings.example"]}}
    assert allowed_hosts(("Wiki.Example", "[::1]", "wiki.example"), settings) == ("wiki.example", "::1")
    assert allowed_hosts((), settings) == ("settings.example",) == allowed_hosts(None, settings)
    assert allowed_hosts((), {}) == LOOPBACK_NAMES == allowed_hosts((), {"serve": {"interval": 5}})
    with pytest.raises(HostError, match=r"^--allowed-host '\*' is a pattern, and a host name is matched exactly: list "
                                        r"each name this workspace is reached by, such as wiki.example$"):
        allowed_hosts(("*",), {})
    with pytest.raises(HostError, match=r"^serve.allowed_hosts in settings.yaml '\*.example' is a pattern"):
        allowed_hosts((), {"serve": {"allowed_hosts": ["*.example"]}})
    for wrong in ("wiki.example:443", "https://wiki.example", "", "a b.example", "x@wiki.example", "::zz"):
        with pytest.raises(HostError, match="is not a host name: give the name alone"):
            allowed_hosts((wrong,), {})
    with pytest.raises(HostError, match="serve.allowed_hosts in settings.yaml is not a list of host names"):
        allowed_hosts((), {"serve": {"allowed_hosts": "wiki.example"}})


def test_a_listed_host_passes_and_its_origin_too_while_another_sites_origin_is_refused(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    with serving(workspace, allowed=("wiki.example",)) as client:
        named = Session(client, agent.token, extra={"Host": "Wiki.Example:8443", "Origin": "https://wiki.example"})
        found = named.call("find", {"name": "Apollo"})
        other = client.post("/mcp", headers=HEADERS | {"Host": "wiki.example", "Origin": "https://elsewhere.example",
                                                       "Authorization": f"Bearer {agent.token}"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        local = call_once(client, agent.token)  # the loopback names are no longer listed
    assert found["structuredContent"]["matches"] == ["projects/apollo"]
    assert other.status_code == 403 and local.status_code == 421


def test_a_client_that_names_the_ipv6_loopback_in_brackets_passes_the_gate_and_the_sdks_own_check(
        tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    with serving(workspace, "::1") as client:
        named = Session(client, agent.token, extra={"Host": "[::1]:4747", "Origin": "http://[::1]:4747"})
        found = named.call("find", {"name": "Apollo"})
    assert found["structuredContent"]["matches"] == ["projects/apollo"]


def test_serve_hands_the_host_names_and_origins_it_was_given_to_the_server_it_runs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    built: list[Any] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: built.append(self))
    serve_mcp(workspace, 0, LOOPBACK, ("wiki.example",), ("https://app.example",))

    async def listing(host: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=built[0].config.app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as client:
            return await client.get("/api/v1", headers={"Authorization": f"Bearer {agent.token}",
                                                        "Origin": "https://app.example"})

    listed = anyio.run(listing, "wiki.example")
    assert listed.status_code == 200 and listed.headers["access-control-allow-origin"] == "https://app.example"
    assert anyio.run(listing, "127.0.0.1").status_code == 421


def test_a_host_not_listed_is_refused_in_one_line_before_any_token_or_store_is_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    with serving(workspace) as client:
        reads: list[str] = []
        for name in ("latest", "rows", "state", "set_state", "article"):
            real = getattr(workspace.store, name)
            monkeypatch.setattr(workspace.store, name, lambda *a, _n=name, _r=real, **k: reads.append(_n) or _r(*a, **k))
        for name in ("check", "any"):
            monkeypatch.setattr(Tokens, name, lambda *a, _n=name, **k: reads.append(_n))
        refused = client.post("/mcp", headers=HEADERS | {"Host": "wiki.example:4747",
                                                         "Authorization": f"Bearer {agent.token}"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "read", "arguments": {"ids": ["projects/apollo"]}}})
        bare = client.post("/mcp", headers=HEADERS | {"Host": "wiki.example"}, json={"jsonrpc": "2.0", "id": 1,
                                                                                     "method": "ping"})
    assert refused.status_code == 421 and refused.text == (
        "this server is not reached by the Host 'wiki.example:4747': list each name it is reached by with serve "
        "--allowed-host <name>\n")
    assert bare.status_code == 421 and "www-authenticate" not in refused.headers
    assert reads == [] and agent.token not in refused.text


def test_serve_refuses_a_pattern_for_a_host_name_in_one_line_before_anything_is_bound(
        tmp_path: Path, binds: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    passes: list[Any] = []
    monkeypatch.setattr(cli, "run_forever", lambda **kwargs: passes.append(kwargs))
    refused = CliRunner().invoke(main, ["serve", "--allowed-host", "*", "--workspace", str(root)])
    assert refused.exit_code == 1 and refused.output.splitlines()[-1] == (
        "Error: --allowed-host '*' is a pattern, and a host name is matched exactly: list each name this workspace is "
        "reached by, such as wiki.example")
    assert binds == [] and passes == []


def test_revoking_the_last_live_token_says_the_server_now_refuses_every_request(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    runner = CliRunner()
    for name in ("agent-a", "agent-b"):
        runner.invoke(main, ["tokens", "new", name, "--workspace", str(root)])
    first = runner.invoke(main, ["tokens", "revoke", "agent-a", "--workspace", str(root)])
    last = runner.invoke(main, ["tokens", "revoke", "agent-b", "--workspace", str(root)])
    assert first.stdout == "revoked  agent-a: the next request that carries its token is refused\n"
    assert last.stdout.splitlines() == [
        "revoked  agent-b: the next request that carries its token is refused",
        ("note     that was the last live token: the HTTP server now refuses every request until a new one is made "
         "with cerebro tokens new <name>")]


@pytest.mark.parametrize(("host", "passes"), [
    (b"127.0.0.1:4747", True), (b"localhost", True), (b"LOCALHOST:80", True), (b"[::1]:4747", True), (b"[::1]", True),
    (None, False), (b"", False), (b"localhost:abc", False), (b"localhost:1:2", False), (b"[::1:4747", False),
    (b"::1", True),  # malformed without brackets, and loopback: the SDK's own check refuses it after the gate
    (b"wiki.example", False), (b"127.0.0.1.wiki.example", False)])
def test_the_gate_reads_the_host_name_without_its_port_and_an_ipv6_address_without_brackets(
        host: bytes | None, passes: bool) -> None:
    reached: list[Any] = []
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(dict(scope["headers"]).get(b"host"))

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [AUTH] if host is None else [(b"host", host), AUTH]
    anyio.run(Gate(app, Admit()), {"type": "http", "path": "/mcp", "method": "POST",  # type: ignore[arg-type]
                                                      "headers": headers}, receive, send)
    assert reached == ([host.lower()] if passes else []), host  # type: ignore[union-attr]
    assert passes or sent[0]["status"] == 421


# -- where serve binds, from the flags or settings.yaml -------------------------------------------------------------

class TakenPort:
    """`take_port`, standing in for a test that serves in its own process: it takes nothing and says the port asked
    for is taken, so no test binds a real port, 4747 or another address's."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port

    def getsockname(self) -> tuple[str, int]:
        return self.host, self.port

    def close(self) -> None:
        pass


def taken_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.mcp.take_port", TakenPort)


def _serve_stub(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    served: list[Any] = []
    monkeypatch.setattr(cli, "run_forever", lambda **kwargs: None)
    taken_port(monkeypatch)
    monkeypatch.setattr("cerebro.mcp.serve", lambda workspace, port, host="127.0.0.1", allowed=None, **k: served.append(
        (port, host, allowed)))
    return served


def test_settings_alone_set_where_serve_binds_and_its_refusal_and_tls_note_hold_the_same(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("port: 6000\nserve:\n  host: 0.0.0.0\n  port: 5151\n")
    served = _serve_stub(monkeypatch)
    refused = CliRunner().invoke(main, ["serve", "--workspace", str(root)])
    assert refused.exit_code == 1 and "serve --host 0.0.0.0 is not loopback, and this workspace has no token" in \
        refused.output and served == []
    CliRunner().invoke(main, ["tokens", "new", "agent-a", "--workspace", str(root)])
    result = CliRunner().invoke(main, ["serve", "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    assert served == [(5151, "0.0.0.0", None)]
    assert "serving   MCP on 0.0.0.0, a pass every 30 seconds. Ctrl-C stops." in result.stdout
    assert "note      cerebro does not end TLS: serve this address only behind something that does" in result.stdout


def test_a_flag_wins_over_settings_and_the_port_falls_back_to_the_top_level_then_4747(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("serve:\n  host: 0.0.0.0\n  port: 5151\n")
    served = _serve_stub(monkeypatch)
    result = CliRunner().invoke(main, ["serve", "--host", "127.0.0.1", "--port", "5050", "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    assert served == [(5050, "127.0.0.1", None)] and "cerebro does not end TLS" not in result.stdout
    (root / "settings.yaml").write_text("serve:\n  port: http\n")
    wrong = CliRunner().invoke(main, ["serve", "--workspace", str(root)])
    assert wrong.exit_code == 1 and wrong.output.splitlines()[-1] == (
        "Error: settings.yaml: line 2: serve.port is 'http': write a whole number from 1 to 65535")
    workspace = workspace_at(tmp_path / "other")
    for settings, port in (({"serve": {"port": 5252}, "port": 6000}, 5252), ({"port": 6000}, 6000), ({}, 4747)):
        workspace.settings = settings
        assert http_server(workspace).config.port == port, settings  # built, never started: nothing is bound
    assert http_server(workspace, LOOPBACK, 5353).config.port == 5353


def test_the_gate_refuses_a_websocket_and_any_other_kind_of_request_and_hands_on_lifespan_alone() -> None:
    reached: list[str] = []
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["type"])

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    gate = Gate(app, Admit())
    for kind in ("websocket", "webtransport", "lifespan"):
        anyio.run(gate, {"type": kind, "path": "/mcp", "headers": [HOST, AUTH]}, receive, send)
    assert reached == ["lifespan"] and sent == [{"type": "websocket.close", "code": 1008}]


def test_an_open_stream_ends_once_its_token_is_revoked_and_a_session_answers_only_the_token_that_opened_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.tokens.WATCH", 0.1)
    workspace = workspace_at(tmp_path / "workspace")
    agent, other = workspace.make_token("agent-a"), workspace.make_token("agent-b")
    with serving(workspace) as client:
        session = Session(client, agent.token)
        port = client.base_url.port
        with socket.create_connection((LOOPBACK, port), timeout=5) as raw:
            raw.sendall((f"GET /mcp HTTP/1.1\r\nHost: {LOOPBACK}\r\nAccept: text/event-stream\r\n"
                         f"Authorization: Bearer {agent.token}\r\nMcp-Session-Id: {session.id}\r\n"
                         "MCP-Protocol-Version: 2025-06-18\r\n\r\n").encode())
            head = raw.recv(4096)
            assert head.startswith(b"HTTP/1.1 200"), head
            borrowed = client.post("/mcp", headers=session.headers() | {"Authorization": f"Bearer {other.token}"},
                                   json={"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                         "params": {"name": "find", "arguments": {"name": "Apollo"}}})
            workspace.revoke_token("agent-a")
            started = time.monotonic()
            rest = b""
            while (got := raw.recv(4096)) and b"0\r\n\r\n" not in rest + got:
                rest += got
            ended = time.monotonic() - started
    assert ended < 5, ended
    assert borrowed.status_code == 403 and borrowed.text == (
        "this session was opened with another token: open a session of your own with initialize\n")


def test_a_half_sent_head_or_body_is_dropped_in_the_seconds_settings_give_and_the_bounds_are_checked(
        tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    workspace.settings = {"serve": {"head_timeout": 0.2, "body_timeout": 0.2, "concurrency": 50}}
    assert http_server(workspace).config.limit_concurrency == 50
    with serving(workspace) as client:
        port = client.base_url.port
        with socket.create_connection((LOOPBACK, port), timeout=5) as head, \
                socket.create_connection((LOOPBACK, port), timeout=5) as body:
            started = time.monotonic()
            head.sendall(f"POST /mcp HTTP/1.1\r\nHost: {LOOPBACK}\r\n".encode())
            body.sendall((f"POST /mcp HTTP/1.1\r\nHost: {LOOPBACK}\r\nAuthorization: Bearer {agent.token}\r\n"
                          "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{").encode())
            assert head.recv(4096) == b"" and time.monotonic() - started < 5  # the default would wait 10
            answer = b""
            while got := body.recv(4096):
                answer += got
        assert answer.startswith(b"HTTP/1.1 408 ") and b"did not arrive whole in time" in answer
        assert client.get("/healthz").status_code == 200
    root = tmp_path / "wrong"
    root.mkdir()
    (root / "settings.yaml").write_text("serve:\n  head_timeout: 0\n")
    refused = CliRunner().invoke(main, ["serve", "--workspace", str(root)])
    assert refused.exit_code == 1 and "serve.head_timeout is 0" in refused.output


@pytest.mark.parametrize("name", sorted(n.decode() for n in ONCE))
@pytest.mark.parametrize("path", ["/mcp", "/api/v1/find"])
def test_a_header_the_gate_or_the_library_decides_on_sent_twice_is_refused_before_anything_else(
        name: str, path: str) -> None:
    reached: list[Any] = []
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    class Watched(Admit):
        def check(self, authorization: str | None) -> Token | None:
            raise AssertionError("the token was read")

    values = {"host": b"127.0.0.1", "authorization": AUTH[1], "content-length": b"0"}
    first, second = values.get(name, b"one"), values.get(name, b"two")
    headers = [(k, v) for k, v in (HOST, AUTH) if k != name.encode()] + [(name.encode(), first),
                                                                          (name.title().encode(), second)]
    anyio.run(Gate(app, Watched()), {"type": "http", "path": path, "method": "POST",  # type: ignore[arg-type]
                                     "headers": headers}, receive, send)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert reached == [] and sent[0]["status"] == 400 and f"carries {name} more than once".encode() in body



def test_a_token_in_a_json_rpc_id_or_method_is_refused_before_the_sdk_could_send_it_back(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    agent = workspace.make_token("agent-a")
    pasted = workspace.make_token("agent-b").token
    with serving(workspace) as client:
        session = Session(client, agent.token)
        answers = [client.post("/mcp", headers=session.headers(), json=body) for body in (
            {"jsonrpc": "2.0", "id": 5, "method": pasted},
            {"jsonrpc": "2.0", "id": f"x{pasted}", "method": "tools/list"},
            [{"jsonrpc": "2.0", "id": 6, "method": "tools/list"}, {"jsonrpc": "2.0", "id": pasted, "method": "ping"}])]
        listed = session.post("tools/list", None)
    assert [a.status_code for a in answers] == [400, 400, 400] and listed.status_code == 200
    assert all(pasted not in a.text and pasted[4:] not in a.text for a in answers)
