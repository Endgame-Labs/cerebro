"""The JSON API: the six operations over HTTP and JSON, beside MCP on the same server, behind the same gate. Each
server here runs over real loopback HTTP, bound to port 0 on 127.0.0.1, with the token tests' loopback-only rule."""

from __future__ import annotations

import json
import logging
import socket
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from click.testing import CliRunner
from test_mcp import workspace_at
from test_tokens import (
    HEADERS,
    LOOPBACK,
    Counted,
    Session,
    _messages,
    call_once,
    no_network,  # noqa: F401  autouse: sockets reach 127.0.0.1 alone
    serving,
    taken_port,
)

from cerebro import Caller, Cerebro, cli
from cerebro.api import CODES, INTERNAL, Api
from cerebro.cli import main
from cerebro.mcp import SIX
from cerebro.records import Proposal, propose
from cerebro.tokens import BODY_CAP, MASKED, PREFIX, OriginError, Tokens, allowed_origins

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
APP = "https://app.example"
WAITING = "p_7c41e09b2d15"


def call(client: httpx.Client, operation: str, body: Any, token: str | None,
         headers: dict[str, str] | None = None) -> httpx.Response:
    """One POST to the JSON API, the token in the Authorization header when given."""
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"/api/v1/{operation}", json=body, headers=auth | (headers or {}))


def refusal(response: httpx.Response, status: int, code: str) -> str:
    """The message of a refusal in the fixed shape, after checking its shape, status and code."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    shape = response.json()
    assert list(shape) == ["error"] and list(shape["error"]) == ["code", "message"], shape
    assert shape["error"]["code"] == code and CODES[code] == status
    message = shape["error"]["message"]
    assert message and "\n" not in message
    return message


def cors(response: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.startswith("access-control-")}


def raw(port: int, head: str, until: bytes = b'"}}') -> str:
    """The reply to a request whose head alone is sent, no byte of its body, read until `until` arrives."""
    with socket.create_connection((LOOPBACK, port), timeout=5) as conn:
        conn.sendall(head.encode())
        got = b""
        while until not in got and (chunk := conn.recv(4096)):
            got += chunk
    return got.decode()


def with_a_proposal(workspace: Cerebro) -> Cerebro:
    """`workspace` with a curator's proposal awaiting review by a caller."""
    propose(workspace.store, Proposal(WAITING, "article.archive", "projects/vega", Caller("projects/curator", "agent"),
                                  "2026-11-03T09:00:00Z", reason="Vega closed in October."))
    return workspace


def server_log(caplog: pytest.LogCaptureFixture) -> str:
    """Every line the process logged but the test's own HTTP client, whose log names each URL it asked for."""
    return "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records
                     if not r.name.startswith(("httpx", "httpcore")))


def ops(counted: Counted) -> list[str]:
    """The operations counted, without the store's reads."""
    return [name for name in counted.calls if name in SIX]


class Callers:
    """Each operation a workspace runs, with the caller it was handed."""

    def __init__(self, workspace: Cerebro, monkeypatch: pytest.MonkeyPatch):
        self.seen: list[tuple[str, Caller]] = []
        for name in SIX:
            monkeypatch.setattr(workspace, name, self._seeing(name, getattr(workspace, name)))

    def _seeing(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def seen(caller: Caller, *args: Any, **kwargs: Any) -> Any:
            self.seen.append((name, caller))
            return fn(caller, *args, **kwargs)

        return seen


class Through:
    """A call of an operation over MCP, by a session per token, or through the JSON API, giving its result."""

    def __init__(self, client: httpx.Client, surface: str, tokens: tuple[str, ...]):
        self.client, self.surface = client, surface
        self.sessions = {token: Session(client, token) for token in tokens} if surface == "mcp" else {}

    def __call__(self, token: str, op: str, args: dict[str, Any]) -> Any:
        if self.surface == "mcp":
            got = self.sessions[token].call(op, args)
            if got.get("isError"):  # a refusal, as one line on either surface
                return {"refused": got["content"][0]["text"].removeprefix(f"Error executing tool {op}: ")}
            return got["structuredContent"]
        answered = call(self.client, op, args, token)
        if answered.status_code != 200 and "error" in answered.json():
            return {"refused": answered.json()["error"]["message"]}
        assert answered.status_code == 200, answered.text
        assert answered.headers["cache-control"] == "no-store" and "set-cookie" not in answered.headers
        return answered.json()


# -- the six operations, one code path ------------------------------------------------------------------------------

def test_each_operation_gives_through_the_api_the_result_mcp_gives_and_the_tokens_caller_reaches_it_the_same_way(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.app.system_clock", lambda: NOW)
    results: dict[str, list[Any]] = {}
    callers: dict[str, list[tuple[str, Caller]]] = {}
    for surface in ("mcp", "api"):
        ids = iter(range(1, 1000))
        monkeypatch.setattr("cerebro.records.uuid.uuid4", lambda _ids=ids: uuid.UUID(int=next(_ids)))
        workspace = with_a_proposal(workspace_at(tmp_path / surface))
        agent, desk = workspace.make_token("agent-a").token, workspace.make_token("desk").token
        seen = Callers(workspace, monkeypatch)
        with serving(workspace) as client:
            run = Through(client, surface, (agent, desk))

            got = [run(agent, "find", {"name": "Apollo"}),
                   run(agent, "query", {"from": "projects", "stage": "open"}),
                   run(agent, "query", {"from": "projects", "limit": 1, "offset": 1}),
                   run(agent, "read", {"ids": ["projects/apollo"], "about": "review"}),
                   run(agent, "tell", {"text": "Apollo should be called Apollo Two.", "target": "projects/apollo",
                                       "as": "proposal"})]
            got.append(run(desk, "decide", {"accept": [{"id": WAITING, "reason": "it closed"}]}))
            got.append(run(agent, "tell", {"text": "The review moved.", "target": "projects/apollo", "as": "fact"}))
            got.append(run(agent, "retract", {"id": got[-1]["record"], "reason": "not so"}))
        results[surface], callers[surface] = got, seen.seen
        logged = [(row.data["tool"], row.data["caller"]) for row in workspace.store.rows("mcp-requests")]
        assert logged == [(op, who.name) for op, who in seen.seen]
        assert {row.data["surface"] for row in workspace.store.rows("mcp-requests")} == {surface}
    assert results["api"] == results["mcp"]
    assert results["api"][0]["matches"] == ["projects/apollo"] and results["api"][3]["articles"]
    assert results["api"][5]["answers"] == {WAITING: "decided"} and results["api"][7]["withdrew"]
    agent_a, desk_ = Caller("agent-a", "person"), Caller("desk", "person")
    assert callers["api"] == callers["mcp"] == [
        ("find", agent_a), ("query", agent_a), ("query", agent_a), ("read", agent_a), ("tell", agent_a),
        ("decide", desk_), ("tell", agent_a), ("retract", agent_a)]


def test_the_listing_gives_each_operation_with_the_schema_mcp_tools_list_gives_read_when_asked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    token = workspace.make_token("agent-a").token
    with serving(workspace) as client:
        listed = client.get("/api/v1", headers={"Authorization": f"Bearer {token}"})
        tools = Session(client, token).post("tools/list", {})
        wire = {tool["name"]: tool for tool in _messages(tools)[-1]["result"]["tools"]}
        real = __import__("cerebro.mcp", fromlist=["MCPServer"]).MCPServer.list_tools

        async def changed(self: Any) -> Any:
            found = await real(self)
            found[0].input_schema = {**found[0].input_schema, "title": "changed just now"}
            return found

        monkeypatch.setattr("cerebro.mcp.MCPServer.list_tools", changed)
        again = client.get("/api/v1/", headers={"Authorization": f"Bearer {token}"}).json()
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    operations = listed.json()["operations"]
    assert [op["name"] for op in operations] == list(SIX)
    for op in operations:
        assert op["method"] == "POST" and op["path"] == f"/api/v1/{op['name']}"
        assert op["input_schema"] == wire[op["name"]]["inputSchema"], op["name"]
        assert op["description"] == wire[op["name"]].get("description", "")
    assert "from" in operations[1]["input_schema"]["properties"] and "as" in operations[3]["input_schema"]["properties"]
    assert again["operations"][0]["input_schema"]["title"] == "changed just now"  # read on each request, not copied


# -- the gate, in order, on the new routes --------------------------------------------------------------------------

def test_a_bad_host_is_refused_before_the_token_is_read_and_runs_nothing(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    token = workspace.make_token("agent-a").token
    counted = Counted(workspace, monkeypatch)
    with serving(workspace) as client:
        reads: list[str] = []
        for name in ("check", "any"):
            monkeypatch.setattr(Tokens, name, lambda *a, _n=name, **k: reads.append(_n))
        refused = call(client, "find", {"name": "Apollo"}, token, {"Host": "wiki.example"})
        listing = client.get("/api/v1", headers={"Host": "wiki.example", "Authorization": f"Bearer {token}"})
    message = refusal(refused, 421, "host_refused")
    assert message == ("this server is not reached by the Host 'wiki.example': list each name it is reached by with "
                       "serve --allowed-host <name>")
    refusal(listing, 421, "host_refused")
    assert reads == [] and counted.calls == [] and workspace.store.rows("mcp-requests") == []


def test_a_bad_token_is_refused_before_any_of_the_body_is_read(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    workspace.make_token("agent-a")
    counted = Counted(workspace, monkeypatch)
    with serving(workspace) as client:
        port = client.base_url.port
        assert port is not None
        heads = [raw(port, f"POST /api/v1/find HTTP/1.1\r\nHost: {LOOPBACK}:{port}\r\n{auth}Content-Type: "
                           "application/json\r\nContent-Length: 40\r\n\r\n")  # the body never comes
                 for auth in ("", f"Authorization: Bearer {PREFIX}{'0' * 43}\r\n", "Authorization: Basic eDp5\r\n")]
    for head, says in zip(heads, ("this workspace needs a token", "this token is not accepted",
                                  "this token is not accepted"), strict=True):
        assert head.startswith("HTTP/1.1 401 ") and '"code": "unauthorized"' in head and says in head, head
        assert 'www-authenticate: bearer realm="cerebro"' in head.lower()
    assert counted.calls == [] and workspace.store.rows("mcp-requests") == []


def test_a_body_over_the_cap_is_refused_before_any_of_it_is_read_by_length_or_as_it_streams(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    token = workspace.make_token("agent-a").token
    counted = Counted(workspace, monkeypatch)
    with serving(workspace) as client:
        port = client.base_url.port
        assert port is not None
        head = raw(port, f"POST /api/v1/tell HTTP/1.1\r\nHost: {LOOPBACK}:{port}\r\nAuthorization: Bearer {token}\r\n"
                         f"Content-Type: application/json\r\nContent-Length: {BODY_CAP + 1}\r\n\r\n")
        whole = json.dumps({"text": "x" * BODY_CAP, "target": "projects/apollo"}).encode()
        streamed = client.post("/api/v1/tell", content=iter([whole[i:i + 65536] for i in range(0, len(whole), 65536)]),
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert head.startswith("HTTP/1.1 413 ") and '"code": "too_large"' in head and token not in head
    assert "larger than 1048576 bytes" in refusal(streamed, 413, "too_large")
    assert streamed.request.headers["transfer-encoding"] == "chunked"
    assert counted.calls == [] and workspace.store.rows("mcp-requests") == []


def test_a_token_in_the_url_is_refused_with_400_on_every_route_and_reaches_no_log_or_reply(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    workspace = workspace_at(tmp_path / "workspace")
    token = workspace.make_token("agent-a").token
    counted = Counted(workspace, monkeypatch)
    auth = {"Authorization": f"Bearer {token}"}
    escaped = token.replace("_", "%5F")
    with serving(workspace) as client:
        replies = {
            "query string": call(client, f"find?token={token}", {"name": "Apollo"}, token),
            "query string, no header": call(client, f"find?token={token}", {"name": "Apollo"}, None),
            "query string, escaped": client.get(f"/api/v1?t={escaped}", headers=auth),
            "access_token": client.get("/api/v1?access_token=anything", headers=auth),
            "path": client.post(f"/api/v1/{token}", json={}, headers=auth),
            "path, escaped": client.post(f"/api/v1/find/{escaped}", json={}, headers=auth),
            "listing": client.get(f"/api/v1?key={token}"),
        }
        mcp = client.post(f"/mcp?token={token}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          headers=HEADERS | auth)
    for what, reply in replies.items():
        assert refusal(reply, 400, "token_in_url").startswith("a token was sent in this request's URL"), what
    assert mcp.status_code == 400 and mcp.text.startswith("a token was sent in this request's URL")
    assert counted.calls == [] and workspace.store.rows("mcp-requests") == []
    out, err = capfd.readouterr()
    for place in [*(f"{r.status_code} {r.headers} {r.text}" for r in [*replies.values(), mcp]), out, err,
                  server_log(caplog), json.dumps(workspace.store.state("serve-tokens/used/agent-a"))]:
        assert token not in place and token[len(PREFIX):] not in place and escaped not in place


def test_a_cookie_without_a_bearer_token_gets_401_and_no_reply_sets_one(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    counted = Counted(workspace, monkeypatch)
    with serving(workspace) as client:  # no token was ever made: neither MCP nor the JSON API is open on loopback
        closed_mcp = call_once(client, None, "find", {"name": "Apollo"})
        bare = call(client, "find", {"name": "Apollo"}, None)
        token = workspace.make_token("agent-a").token
        cookies = [call(client, "tell", {"text": "Ship it."}, None, {"Cookie": cookie})
                   for cookie in ("session=abc123", f"token={token}", f"Authorization=Bearer {token}")]
        good = call(client, "find", {"name": "Apollo"}, token, {"Cookie": "session=abc123"})
    assert closed_mcp.status_code == 401
    assert refusal(bare, 401, "unauthorized").startswith("this workspace needs a token")
    for refused in cookies:
        assert refusal(refused, 401, "unauthorized").startswith("this workspace needs a token")
        assert refused.headers["www-authenticate"] == 'Bearer realm="cerebro"' and token not in refused.text
    assert good.status_code == 200
    assert all("set-cookie" not in r.headers for r in [bare, *cookies, good])
    assert ops(counted) == ["find"]  # the API's one call with a bearer token


def test_a_revoked_token_is_refused_from_the_next_request_and_the_check_is_never_cached(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    token = workspace.make_token("agent-a").token
    with serving(workspace) as client:
        first = call(client, "find", {"name": "Apollo"}, token)
        workspace.revoke_token("agent-a")
        second = call(client, "find", {"name": "Apollo"}, token)
    assert first.status_code == 200
    assert refusal(second, 401, "unauthorized").startswith("this token is not accepted")
    assert second.headers["www-authenticate"] == 'Bearer realm="cerebro", error="invalid_token"'


# -- browsers and origins -------------------------------------------------------------------------------------------

def test_with_no_origin_listed_no_cors_header_is_ever_sent_and_a_page_from_any_origin_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "quasar-workspace")
    token = workspace.make_token("agent-a").token
    counted = Counted(workspace, monkeypatch)
    with serving(workspace) as client:
        preflight = client.options("/api/v1/find", headers={"Origin": APP, "Access-Control-Request-Method": "POST",
                                                            "Access-Control-Request-Headers": "authorization"})
        page = call(client, "find", {"name": "Apollo"}, token, {"Origin": APP})
        script = call(client, "find", {"name": "Apollo"}, token)
        listing = client.get("/api/v1", headers={"Authorization": f"Bearer {token}"})
        bare = call(client, "find", {"name": "Apollo"}, None, {"Origin": APP})
    refusal(preflight, 403, "origin_refused")
    refusal(page, 403, "origin_refused")
    refusal(bare, 403, "origin_refused")  # the origin before the token: a page learns nothing of the token rule
    assert script.status_code == 200 and listing.status_code == 200
    for reply in (preflight, page, script, listing, bare):
        assert cors(reply) == {} and "vary" not in reply.headers
    for text in (preflight.text, str(preflight.headers), page.text):
        assert "quasar" not in text and "apollo" not in text.lower() and "cerebro-" not in text
    assert ops(counted) == ["find"]  # the script's one call


def test_a_listed_origin_passes_its_preflight_with_no_token_and_its_calls_carry_cors_headers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = workspace_at(tmp_path / "quasar-workspace")
    workspace.settings = {**workspace.settings, "serve": {"allowed_origins": [APP, "http://127.0.0.1:5173"]}}
    token = workspace.make_token("agent-a").token
    counted = Counted(workspace, monkeypatch)
    with serving(workspace) as client:
        preflight = client.options("/api/v1/find", headers={"Origin": APP, "Access-Control-Request-Method": "POST",
                                                            "Access-Control-Request-Headers": "authorization"})
        found = call(client, "find", {"name": "Apollo"}, token, {"Origin": APP})
        unauthorized = call(client, "find", {"name": "Apollo"}, None, {"Origin": "http://127.0.0.1:5173"})
        near = [call(client, "find", {"name": "Apollo"}, token, {"Origin": origin})
                for origin in ("https://app.example:443", "https://APP.example", "https://app.example/",
                               "http://app.example", "https://app.example.other", "null")]
        near_preflight = client.options("/api/v1/find", headers={"Origin": "http://app.example",
                                                                 "Access-Control-Request-Method": "POST"})
    assert (preflight.status_code, preflight.content) == (204, b"")
    assert cors(preflight) == {"access-control-allow-origin": APP, "access-control-allow-methods": "GET, POST",
                               "access-control-allow-headers": "authorization, content-type",
                               "access-control-max-age": "600"}
    assert preflight.headers["vary"] == "Origin" and "content-type" not in preflight.headers
    assert found.status_code == 200 and found.json()["matches"] == ["projects/apollo"]
    assert cors(found) == {"access-control-allow-origin": APP} and found.headers["vary"] == "Origin"
    refusal(unauthorized, 401, "unauthorized")
    assert cors(unauthorized) == {"access-control-allow-origin": "http://127.0.0.1:5173"}  # a page may read why
    for reply in [*near, near_preflight]:
        refusal(reply, 403, "origin_refused")
        assert cors(reply) == {}
    assert ops(counted) == ["find"]


def test_origins_come_from_the_flags_else_the_settings_and_a_pattern_or_a_loose_form_is_refused_in_one_line() -> None:
    settings = {"serve": {"allowed_origins": ["https://settings.example"]}}
    assert allowed_origins((APP, "http://127.0.0.1:5173", APP), settings) == (APP, "http://127.0.0.1:5173")
    assert allowed_origins((), settings) == ("https://settings.example",) == allowed_origins(None, settings)
    assert allowed_origins((), {}) == () == allowed_origins((), {"serve": {"interval": 5}})
    assert allowed_origins(("http://[::1]:8080", "https://a-b.example:8443"), {})
    with pytest.raises(OriginError, match=r"^--allowed-origin '\*' is a pattern, and an origin is matched exactly: "
                                          r"list each origin a page calls from, such as https://app.example$"):
        allowed_origins(("*",), {})
    with pytest.raises(OriginError, match=r"^serve.allowed_origins in settings.yaml 'https://\*.example' is a pattern"):
        allowed_origins((), {"serve": {"allowed_origins": ["https://*.example"]}})
    for wrong in ("app.example", "https://app.example/", "https://App.example", "https://app.example:443",
                  "http://app.example:80", "ftp://app.example", "null", "", "https://app.example:99999",
                  "https://app.example:0", "https://-app.example"):
        with pytest.raises(OriginError, match="is not an origin as a browser sends it: give the scheme"):
            allowed_origins((wrong,), {})
    with pytest.raises(OriginError, match="serve.allowed_origins in settings.yaml is not a list of origins"):
        allowed_origins((), {"serve": {"allowed_origins": APP}})


def _workspace_folder(tmp_path: Path, settings: str = "{}\n") -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text(settings)
    return root


def test_serve_refuses_a_pattern_for_an_origin_in_one_line_from_the_flag_or_the_settings_before_anything_is_bound(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace_folder(tmp_path)
    passes: list[Any] = []
    served: list[Any] = []
    monkeypatch.setattr(cli, "run_forever", lambda **kwargs: passes.append(kwargs))
    monkeypatch.setattr("cerebro.mcp.serve", lambda *a, **k: served.append(a))
    taken_port(monkeypatch)
    refused = CliRunner().invoke(main, ["serve", "--allowed-origin", "*", "--workspace", str(root)])
    assert refused.exit_code == 1 and refused.output.splitlines()[-1] == (
        "Error: --allowed-origin '*' is a pattern, and an origin is matched exactly: list each origin a page calls "
        "from, such as https://app.example")
    (root / "settings.yaml").write_text("serve:\n  allowed_origins: ['*']\n")
    kept = CliRunner().invoke(main, ["serve", "--workspace", str(root)])
    assert kept.exit_code == 1 and "settings.yaml: line 2: serve.allowed_origins '*' is a pattern" in kept.output
    assert passes == [] and served == []


def test_serve_hands_the_listed_origins_to_the_server_and_says_which(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace_folder(tmp_path)
    served: list[Any] = []
    monkeypatch.setattr(cli, "run_forever", lambda **kwargs: None)
    monkeypatch.setattr("cerebro.mcp.serve", lambda *a, **k: served.append(a[1:]))
    taken_port(monkeypatch)
    flagged = CliRunner().invoke(main, ["serve", "--allowed-origin", APP, "--allowed-origin", "http://127.0.0.1:5173",
                                        "--workspace", str(root)])
    assert flagged.exit_code == 0, flagged.output
    assert "origins   a page from one of these may call /api/v1: https://app.example, http://127.0.0.1:5173" in \
        flagged.stdout.splitlines()
    assert served == [(None, "127.0.0.1", None, (APP, "http://127.0.0.1:5173"))]
    (root / "settings.yaml").write_text(f"serve:\n  allowed_origins: [{APP}]\n")
    kept = CliRunner().invoke(main, ["serve", "--workspace", str(root)])
    assert kept.exit_code == 0 and f"origins   a page from one of these may call /api/v1: {APP}" in kept.stdout
    assert served[-1] == (None, "127.0.0.1", None)  # settings alone: the server reads them itself


# -- errors ---------------------------------------------------------------------------------------------------------

def test_every_error_is_the_fixed_shape_with_its_code_and_status(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    token = workspace.make_token("agent-a").token
    auth = {"Authorization": f"Bearer {token}"}
    with serving(workspace) as client:
        replies = {
            "an unknown operation": (call(client, "delete", {}, token), 404, "not_found"),
            "an app path past the version": (call(client, "find/more", {}, token), 404, "not_found"),
            "another version": (client.get("/api/v2", headers=auth), 404, "not_found"),
            "the api's own root": (client.get("/api", headers=auth), 404, "not_found"),
            "a get of an operation": (client.get("/api/v1/find", headers=auth), 405, "method_not_allowed"),
            "a post to the listing": (client.post("/api/v1", json={}, headers=auth), 405, "method_not_allowed"),
            "a body that is not json": (client.post("/api/v1/find", content=b"{name", headers=auth | {
                "Content-Type": "application/json"}), 400, "bad_request"),
            "a json list": (call(client, "find", ["Apollo"], token), 400, "bad_request"),
            "no body": (client.post("/api/v1/find", headers=auth | {"Content-Type": "application/json"}), 400,
                        "bad_request"),
            "a form": (client.post("/api/v1/find", data={"name": "Apollo"}, headers=auth), 400, "bad_request"),
            "no content type": (client.post("/api/v1/find", content=b'{"name": "Apollo"}', headers=auth), 400,
                                "bad_request"),
            "plain text": (client.post("/api/v1/find", content=b'{"name": "Apollo"}', headers=auth | {
                "Content-Type": "text/plain"}), 400, "bad_request"),
            "a missing argument": (call(client, "find", {}, token), 400, "bad_request"),
            "a wrong type": (call(client, "retract", {"id": ["r_1"]}, token), 400, "bad_request"),
            "an operation's refusal": (call(client, "retract", {"id": "r_nothing"}, token), 400, "refused"),
            "an empty read": (call(client, "read", {"ids": []}, token), 400, "refused"),
            "no token": (call(client, "find", {"name": "Apollo"}, None), 401, "unauthorized"),
        }
        charset = client.post("/api/v1/find", content=b'{"name": "Apollo"}', headers=auth | {
            "Content-Type": "Application/JSON; charset=utf-8"})  # as a browser's fetch may send it
    assert charset.status_code == 200 and charset.json()["matches"] == ["projects/apollo"]
    said = {what: refusal(reply, status, code) for what, (reply, status, code) in replies.items()}
    assert said["a missing argument"] == "find: name is missing: send the arguments GET /api/v1 lists for find"
    assert said["a wrong type"].startswith("retract: id input should be a valid string")
    assert said["an operation's refusal"] == ("retract: nothing has the id 'r_nothing': pass a fact's fact_ id, a "
                                              "record's r_ id as tell gave it, or an accepted proposal's p_ id")
    assert said["an empty read"] == "read: ids is empty: pass at least one article id, such as notes/weekly"
    assert replies["a get of an operation"][0].headers["allow"] == "POST"
    assert replies["a post to the listing"][0].headers["allow"] == "GET"
    assert said["a json list"].startswith("the body of find is not a JSON object")


def test_an_unexpected_failure_answers_500_with_a_fixed_line_naming_no_message_path_or_internal_name(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    token = workspace.make_token("agent-a").token
    planted = f"the table _shadow_rows at {tmp_path / 'workspace' / '.cerebro' / 'cerebro.sqlite'} is locked by sqlite3"

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(planted)

    replies = []
    with serving(workspace) as client:
        for name, args in (("find", {"name": "Apollo"}), ("query", {"from": "projects"}),
                           ("read", {"ids": ["projects/apollo"]}), ("tell", {"text": "Ship it."}),
                           ("decide", {"accept": ["p_1"]}), ("retract", {"id": "r_1"})):
            with monkeypatch.context() as patch:
                patch.setattr(workspace, name, broken)
                replies.append(call(client, name, args, token))
        with monkeypatch.context() as patch:
            patch.setattr("cerebro.mcp.MCPServer.list_tools", broken)
            replies.append(client.get("/api/v1", headers={"Authorization": f"Bearer {token}"}))
    for reply in replies:
        assert refusal(reply, 500, "internal") == INTERNAL
        whole = f"{reply.headers} {reply.text}"
        for secret in (planted, "_shadow_rows", str(tmp_path), "cerebro.sqlite", "RuntimeError", "Traceback",
                       "cerebro.", "mcp.py", "/Users/", "Error executing tool"):
            assert secret not in whole, secret
    assert planted in caplog.text  # the trace stays in the server's own output
    assert [row.data["result"] for row in workspace.store.rows("mcp-requests")] == ["miss"] * 6


def test_the_api_itself_refuses_a_request_the_gate_named_no_caller_for_and_runs_nothing() -> None:
    class Nothing:
        names = SIX

        async def definitions(self) -> list[dict[str, Any]]:
            raise AssertionError("listed")

        async def call(self, name: str, arguments: dict[str, Any], scope: Any) -> Any:
            raise AssertionError("called")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b'{"name": "Apollo"}', "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    for path, method in (("/api/v1/find", "POST"), ("/api/v1", "GET")):
        sent.clear()
        anyio.run(Api(Nothing()), {"type": "http", "path": path, "method": method,  # type: ignore[arg-type]
                                   "headers": [(b"content-type", b"application/json")]}, receive, send)
        assert sent[0]["status"] == 401 and json.loads(sent[1]["body"]) == {"error": {
            "code": "unauthorized", "message": "this API needs a token: send Authorization: Bearer <token>, made with "
                                               "cerebro tokens new"}}


# -- the token never leaks ------------------------------------------------------------------------------------------

def test_a_token_planted_on_every_route_in_the_url_the_query_and_a_body_field_is_absent_from_the_log_and_replies(
        tmp_path: Path, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    workspace = workspace_at(tmp_path / "workspace")
    made = workspace.make_token("agent-a")
    token = made.token
    person = workspace.make_token("desk").token
    with serving(workspace) as client:
        replies = [
            client.get("/api/v1", headers={"Authorization": f"Bearer {token}"}),
            call(client, "find", {"name": token}, token),
            call(client, "query", {"from": "projects", "stage": token, token: "open"}, token),
            call(client, "query", {"matching": f"review {token}"}, token),
            call(client, "read", {"ids": [token, "projects/apollo"], "about": token}, token),
            call(client, "tell", {"text": f"my token is {token}", "target": "projects/apollo", "as": "fact"}, token),
            call(client, "tell", {"text": "Rename it.", "target": token, "as": "proposal", "kind": token}, token),
            call(client, "decide", {"accept": [{"id": token, "reason": token}], "reject": [token]}, person),
            call(client, "retract", {"id": token, "reason": token}, person),
            call(client, "find", {"name": "Apollo", "extra": token}, token),
            call(client, "find", {"name": "Apollo"}, token, {"X-Trace": token}),
            call(client, "find", {"name": "Apollo"}, token, {"Cookie": f"t={token}"}),
            call(client, f"find?token={token}", {"name": "Apollo"}, token),
            client.get(f"/api/v1?q={token}", headers={"Authorization": f"Bearer {token}"}),
            client.post(f"/api/v1/{token}", json={}, headers={"Authorization": f"Bearer {token}"}),
            call(client, "find", {"name": "Apollo"}, PREFIX + "1" * 43),
            call(client, "find", {"name": "Apollo"}, token, {"Host": f"{token}.example"}),
            call(client, "find", {"name": "Apollo"}, token, {"Origin": f"https://{token}.example"}),
            client.options("/api/v1/find", headers={"Origin": f"https://{token}.example",
                                                    "Access-Control-Request-Method": "POST"}),
            client.post("/api/v1/tell", content=json.dumps({"text": token + "x" * BODY_CAP}).encode(),
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}),
        ]
        workspace.revoke_token("agent-a")
        replies.append(call(client, "find", {"name": "Apollo"}, token))
    statuses = [r.status_code for r in replies]
    assert statuses[:3] == [200, 200, 200] and statuses[-1] == 401 and statuses[12:15] == [400, 400, 400]
    assert MASKED in replies[1].text  # find echoes the name it was given, masked
    seen = {f"reply {n}": f"{r.status_code} {r.headers} {r.text}" for n, r in enumerate(replies)}
    seen |= {f"the {log} log": json.dumps([row.data for row in workspace.store.rows(log)])
             for log in ("mcp-requests", "serve-tokens", "proposals", "spend")}
    seen["repr of the new token"] = repr(made)
    out, err = capfd.readouterr()
    seen |= {"standard output": out, "standard error": err, "every log line of the server": server_log(caplog)}
    for place, text in seen.items():
        assert token not in text and token[len(PREFIX):] not in text, place
    assert len(workspace.store.rows("mcp-requests")) >= 9


def test_a_body_nested_too_deep_or_holding_a_lone_surrogate_is_refused_on_both_surfaces_and_runs_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    token = workspace.make_token("agent-a").token
    counted = Counted(workspace, monkeypatch)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    deep = b"[" * 5000 + b"]" * 5000
    lone = b'{"name": "Apollo \\ud800"}'
    flat = b'{"name": "' + b"[" * 5000 + b' \\ud83d\\ude00"}'  # brackets in a string, and a pair of surrogates
    with serving(workspace) as client:
        api = [client.post("/api/v1/find", content=body, headers=headers) for body in (deep, lone, flat)]
        session = Session(client, token)
        mcp = [client.post("/mcp", content=body, headers=session.headers()) for body in (
            b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ' + deep + b"}",
            b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "find", "arguments": ' + lone
            + b"}}")]
    assert [r.status_code for r in api] == [400, 400, 200] and [r.status_code for r in mcp] == [400, 400]
    assert refusal(api[0], 400, "bad_request") == "this request's body nests too deeply to read: send it flatter"
    assert "lone surrogate" in refusal(api[1], 400, "bad_request") and "lone surrogate" in mcp[1].text
    assert ops(counted) == ["find"] and "Traceback" not in caplog.text
