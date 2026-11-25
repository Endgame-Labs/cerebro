"""Signing in to a hosted MCP server through the browser, against a stand-in authorization server on loopback and a
fake browser that records the address it is handed and plays the person. No real server, browser or keychain is
reached, every port is one the system picks, and every token is synthetic."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from cerebro.connectors.mcp_oauth import (
    LiveToken,
    Login,
    LoginExpired,
    SignInError,
    authorize,
    discover,
    is_login,
    safe_address,
)
from cerebro.fakes import FakeSecrets
from cerebro.secrets import FileSecrets

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# The socket calls as they are before the suite's guard replaces them, so a test may reach loopback and nothing else.
_REAL = {"connect": socket.socket.connect, "create_connection": socket.create_connection,
         "getaddrinfo": socket.getaddrinfo}


@pytest.fixture
def loopback_only(no_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lets a test reach a stand-in on this machine's loopback address, and still refuses every other address."""

    def host_of(address: Any) -> str:
        return str(address[0]) if isinstance(address, tuple) and address else ""

    def refuse(where: str) -> OSError:
        return OSError(f"tests reach loopback alone, not {where}")

    def connect(self: socket.socket, address: Any) -> None:
        if host_of(address) not in LOOPBACK:
            raise refuse(host_of(address))
        return _REAL["connect"](self, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if host_of(address) not in LOOPBACK:
            raise refuse(host_of(address))
        return _REAL["create_connection"](address, *args, **kwargs)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host) not in LOOPBACK:
            raise refuse(str(host))
        return _REAL["getaddrinfo"](host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


class Clock:
    """A clock a test moves by hand."""

    def __init__(self, at: datetime = NOW):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class AuthStandIn:
    """An authorization server and the front of an MCP server, on loopback, on a port the system picks: the 401 that
    points at the protected-resource metadata, both metadata documents, dynamic client registration, the consent
    screen, which sends the browser back with a code, and the token endpoint, which checks PKCE. Tokens it hands out
    are numbered, `tok-test-0001` on; `live` holds the access tokens it still takes."""

    def __init__(self, *, register: bool = True, expires_in: int = 3600, secret: str = "", rotate: bool = True,
                 back: Callable[[str, str], str] | None = None):
        self.register, self.expires_in, self.back = register, expires_in, back
        self.secret, self.rotate = secret, rotate  # a client secret registration hands out; new refresh tokens
        self.lock = threading.Lock()
        self.issued = 0
        self.clients: dict[str, list[str]] = {}
        self.codes: dict[str, tuple[str, str, str]] = {}  # code: client, redirect, challenge
        self.refresh: dict[str, str] = {}  # refresh token: client
        self.live: set[str] = set()
        self.seen: list[str] = []  # each request's method and path, without its query
        self.forms: list[dict[str, str]] = []
        self.refuse_refresh = False
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.server = self.base + "/mcp"
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def _number(self) -> int:
        with self.lock:
            self.issued += 1
            return self.issued

    def tokens(self, client: str, keep: str = "") -> dict[str, Any]:
        """New tokens: a new refresh token too, unless `keep` names the one that stays good."""
        n = self._number()
        access = f"tok-test-{n:04d}"
        self.live.add(access)
        out: dict[str, Any] = {"access_token": access, "token_type": "Bearer"}
        if keep:
            self.refresh[keep] = client
        else:
            out["refresh_token"] = f"refresh-test-{n:04d}"
            self.refresh[out["refresh_token"]] = client
        if self.expires_in:
            out["expires_in"] = self.expires_in
        return out

    def answer(self, method: str, path: str, query: dict[str, str], body: bytes,
               headers: Any) -> tuple[int, dict[str, str], Any]:
        self.seen.append(f"{method} {path}")
        if method == "GET" and path == "/mcp":
            return 401, {"WWW-Authenticate": f'Bearer resource_metadata="{self.base}/.well-known/oauth-protected-'
                                             f'resource/mcp"'}, {"error": "unauthorized"}
        if path == "/.well-known/oauth-protected-resource/mcp":
            return 200, {}, {"resource": self.server, "authorization_servers": [self.base]}
        if path == "/.well-known/oauth-authorization-server":
            found = {"issuer": self.base, "authorization_endpoint": self.base + "/authorize", "token_endpoint":
                     self.base + "/token", "code_challenge_methods_supported": ["S256"]}
            if self.register:
                found["registration_endpoint"] = self.base + "/register"
            return 200, {}, found
        if method == "POST" and path == "/register":
            asked = json.loads(body)
            client = f"client-test-{self._number():04d}"
            self.clients[client] = asked["redirect_uris"]
            assert asked["token_endpoint_auth_method"] == "none" and "refresh_token" in asked["grant_types"]
            return 201, {}, {"client_id": client, **({"client_secret": self.secret} if self.secret else {})}
        if method == "GET" and path == "/authorize":
            assert query["response_type"] == "code" and query["code_challenge_method"] == "S256"
            assert query["redirect_uri"] in self.clients[query["client_id"]] and query["resource"] == self.server
            if self.back is not None:
                return 302, {"Location": self.back(query["redirect_uri"], query["state"])}, {}
            code = f"code-test-{self._number():04d}"
            self.codes[code] = (query["client_id"], query["redirect_uri"], query["code_challenge"])
            return 302, {"Location": query["redirect_uri"] + "?" + urlencode({"code": code, "state":
                                                                              query["state"]})}, {}
        if method == "POST" and path == "/token":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            self.forms.append(form)
            if self.secret and form.get("client_secret") != self.secret:
                return 401, {}, {"error": "invalid_client"}
            if form.get("grant_type") == "authorization_code":
                client, redirect, challenge = self.codes.pop(form.get("code", ""), ("", "", ""))
                verifier = form.get("code_verifier", "")
                pkce = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if client != form.get("client_id") or redirect != form.get("redirect_uri") or pkce != challenge:
                    return 400, {}, {"error": "invalid_grant"}
                return 200, {}, self.tokens(client)
            if form.get("grant_type") == "refresh_token":
                client = self.refresh.pop(form.get("refresh_token", ""), "")
                if self.refuse_refresh or not client or client != form.get("client_id"):
                    return 400, {}, {"error": "invalid_grant"}
                return 200, {}, self.tokens(client, keep="" if self.rotate else form["refresh_token"])
            return 400, {}, {"error": "unsupported_grant_type"}
        return 404, {}, {"error": "not_found"}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        standin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                parts = urlsplit(self.path)
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size) if size else b""
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                try:
                    status, headers, payload = standin.answer(self.command, parts.path, query, body, self.headers)
                except AssertionError as error:  # a request the stand-in does not take shows as a 400
                    status, headers, payload = 400, {}, {"error": "bad_request", "why": str(error)}
                data = json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

        return Handler


class Browser:
    """The person and their browser: records each address it is handed, goes to the consent screen, and follows it
    back to the loopback address, after a return of its own first when `first` gives one."""

    def __init__(self, first: Callable[[str, str], str] | None = None):
        self.opened: list[str] = []
        self.first = first
        self.answers: list[int] = []

    def __call__(self, url: str) -> None:
        self.opened.append(url)
        with httpx.Client(follow_redirects=False) as client:
            back = client.get(url).headers["Location"]
            if self.first is not None:
                query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
                self.answers.append(client.get(self.first(query["redirect_uri"], query["state"])).status_code)
            self.answers.append(client.get(back).status_code)


@pytest.fixture
def standin(loopback_only: None) -> Iterator[AuthStandIn]:
    serving = AuthStandIn()
    yield serving
    serving.stop()


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=5.0) as client:
        yield client


def sign_in(standin: AuthStandIn, http: httpx.Client, browser: Browser, said: list[str] | None = None,
            clock: Clock | None = None, wait: float = 5.0) -> Login:
    return authorize(standin.server, http=http, open_browser=browser, say=(said.append if said is not None else None),
                     now=clock or Clock(), wait=wait)


# -- the sign-in


def test_a_sign_in_finds_the_authorization_server_registers_and_trades_the_code_with_pkce(
        standin: AuthStandIn, http: httpx.Client) -> None:
    browser = Browser()
    login = sign_in(standin, http, browser)
    assert login.server == standin.server and login.token_endpoint == standin.base + "/token"
    assert login.client_id == "client-test-0001" and login.access_token == "tok-test-0003"
    assert login.refresh_token == "refresh-test-0003" and login.expiry == NOW + timedelta(hours=1)
    [url] = browser.opened
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"][0].startswith("http://127.0.0.1:")
    [exchange] = standin.forms
    assert exchange["code_verifier"] not in url and exchange["resource"] == standin.server
    assert standin.seen[:4] == ["GET /mcp", "GET /.well-known/oauth-protected-resource/mcp",
                                "GET /.well-known/oauth-authorization-server", "POST /register"]


def test_a_return_with_a_state_this_sign_in_did_not_start_is_ignored_and_the_sign_in_goes_on(
        standin: AuthStandIn, http: httpx.Client) -> None:
    browser = Browser(first=lambda back, state: back + "?code=code-test-forged&state=forged")
    said: list[str] = []
    login = sign_in(standin, http, browser, said)
    assert login.access_token.startswith("tok-test-") and browser.answers == [400, 200]
    assert any("state this sign-in did not start was ignored" in line for line in said)


@pytest.mark.parametrize(("register", "back", "line"), [
    (True, lambda back, state: f"{back}?error=access_denied&state={state}", ("the server refused the sign-in "
     "(access_denied)")),
    (True, lambda back, state: f"{back}?state={state}", "the browser came back without a code"),
    (True, lambda back, state: f"{back}?code=code-test-0009&state=forged", "the sign-in was not finished"),
    (False, None, "offers no dynamic client registration"),
])
def test_a_sign_in_that_does_not_finish_says_why_in_one_line_and_gives_no_login(
        loopback_only: None, http: httpx.Client, register: bool, back: Callable[[str, str], str] | None, line: str) -> None:
    serving = AuthStandIn(register=register, back=back)
    try:
        with pytest.raises(SignInError) as failed:
            sign_in(serving, http, Browser(), wait=0.1)
    finally:
        serving.stop()
    assert line in str(failed.value) and "\n" not in str(failed.value)
    assert not any(path == "POST /token" for path in serving.seen)


def test_a_sign_in_whose_browser_never_comes_back_ends_within_its_wait_even_with_a_silent_connection_open(
        standin: AuthStandIn, http: httpx.Client, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from cerebro.connectors import mcp_oauth

    monkeypatch.setattr(mcp_oauth._Handler, "timeout", 0.05)
    held: list[socket.socket] = []

    def closed_tab(url: str) -> None:  # the person closes the tab; something else opens a connection and says nothing
        back = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0])
        held.append(socket.create_connection((back.hostname, back.port)))

    started = time.monotonic()
    try:
        with pytest.raises(SignInError) as unfinished:
            authorize(standin.server, http=http, open_browser=closed_tab, now=Clock(), wait=0.1)
    finally:
        for sock in held:
            sock.close()
    assert time.monotonic() - started < 3.0 and held
    assert "the sign-in was not finished" in str(unfinished.value) and "run cerebro connect again" in str(
        unfinished.value)


def test_no_token_code_or_verifier_appears_in_a_line_said_an_error_a_log_or_a_repr(
        standin: AuthStandIn, http: httpx.Client, capfd: pytest.CaptureFixture[str]) -> None:
    said: list[str] = []
    login = sign_in(standin, http, Browser(), said)
    [exchange] = standin.forms
    hidden = [login.access_token, login.refresh_token, exchange["code"], exchange["code_verifier"]]
    out, err = capfd.readouterr()
    shown = "\n".join([*said, out, err, repr(login), repr(LiveToken(FakeSecrets(), "linear", standin.server,
                                                                    http=http, now=Clock(), login=login))])
    assert not [value for value in hidden if value in shown]
    assert said[0].endswith("go to:") and said[1].startswith("http://127.0.0.1:")  # the page, on its own line


def test_a_token_goes_over_https_or_to_this_machine_alone(standin: AuthStandIn, http: httpx.Client) -> None:
    assert safe_address("https://mcp.example/mcp") and safe_address("http://127.0.0.1:9/token")
    assert safe_address("http://localhost:9/token") and safe_address("http://[::1]:9/token")
    assert not safe_address("http://mcp.example/mcp") and not safe_address("https://user@mcp.example/token")
    assert not safe_address("ftp://mcp.example/") and not safe_address("https:///nohost")
    with pytest.raises(SignInError, match="the mcp server at mcp.example is not https, so nothing is sent there"):
        discover(http, "http://mcp.example/mcp")
    original = standin.answer

    def plain_token_endpoint(method: str, path: str, *rest: Any) -> tuple[int, dict[str, str], Any]:
        status, headers, payload = original(method, path, *rest)
        if path == "/.well-known/oauth-authorization-server":
            payload = dict(payload, token_endpoint="http://tokens.example/token")
        return status, headers, payload

    standin.answer = plain_token_endpoint  # type: ignore[method-assign]
    with pytest.raises(SignInError, match="the token endpoint at tokens.example is not https"):
        discover(http, standin.server)


def test_the_return_address_is_loopback_alone(standin: AuthStandIn, http: httpx.Client) -> None:
    class Elsewhere:
        """A listener that says it is on an address of the network, which no test binds."""

        closed = False

        def getsockname(self) -> tuple[str, int]:
            return ("192.0.2.7", 40000)

        def close(self) -> None:
            self.closed = True

    held = Elsewhere()
    browser = Browser()
    with pytest.raises(SignInError, match="return address 192.0.2.7 is not this machine's loopback"):
        authorize(standin.server, http=http, open_browser=browser, listen=lambda: held,  # type: ignore[arg-type]
                  now=Clock(), wait=0.2)
    assert held.closed and browser.opened == [] and "POST /register" not in standin.seen


# -- a login in use


def test_a_live_token_renews_an_expiring_login_without_a_person_and_keeps_the_new_one(
        standin: AuthStandIn, http: httpx.Client, tmp_path: Path) -> None:
    clock = Clock()
    login = sign_in(standin, http, Browser(), clock=clock)
    store = FileSecrets(tmp_path, "workspace-test")
    store.set("linear", login.to_json())
    seen: list[str] = []
    token = LiveToken(store, "linear", standin.server, http=http, now=clock, seen=lambda *v: seen.extend(v))
    assert token() == login.access_token and token() == login.access_token  # a fresh token is not renewed
    clock.at += timedelta(minutes=59, seconds=40)
    renewed = token()
    assert renewed == "tok-test-0004" and renewed in seen
    kept = Login.from_json(store.get("linear"), "linear")
    assert kept.access_token == renewed and kept.refresh_token == "refresh-test-0004"
    assert kept.expiry == clock.at + timedelta(hours=1)
    assert standin.forms[-1]["grant_type"] == "refresh_token" and standin.forms[-1]["resource"] == standin.server
    assert token() == renewed and len(standin.forms) == 2


def test_a_login_the_server_will_not_renew_says_to_sign_in_again(standin: AuthStandIn, http: httpx.Client) -> None:
    clock = Clock()
    login = sign_in(standin, http, Browser(), clock=clock)
    standin.refuse_refresh = True
    clock.at += timedelta(hours=2)
    token = LiveToken(FakeSecrets({"linear": login.to_json()}), "linear", standin.server, http=http, now=clock,
                      source="work")
    with pytest.raises(LoginExpired, match="work: the browser login has expired and the server would not renew it: "
                                           "run cerebro connect work to sign in again"):
        token()
    bare = Login(standin.server, standin.base + "/token", "client-test-0001", "tok-test-0001", expiry=NOW)
    with pytest.raises(LoginExpired, match="has no refresh token"):
        LiveToken(FakeSecrets(), "linear", standin.server, http=http, now=clock, login=bare)()


def test_a_login_is_sent_only_to_the_server_it_was_made_for(standin: AuthStandIn, http: httpx.Client) -> None:
    login = Login(standin.server, standin.base + "/token", "client-test-0001", "tok-test-0001")
    store = FakeSecrets({"linear": login.to_json(), "work-token": "tok-test-plain"})
    elsewhere = LiveToken(store, "linear", "https://mcp.elsewhere.example/mcp", http=http, now=Clock(), source="work")
    with pytest.raises(SignInError, match=r"work: the browser login kept as linear is for 127\.0\.0\.1, not "
                                          r"mcp\.elsewhere\.example, so it is not sent there"):
        elsewhere()
    with pytest.raises(SignInError, match="the secret work-token holds a token, not a browser login"):
        LiveToken(store, "work-token", standin.server, http=http, now=Clock())()
    with pytest.raises(SignInError, match="no browser login is kept as granola"):
        LiveToken(store, "granola", standin.server, http=http, now=Clock())()


def test_a_login_reads_back_as_it_was_kept() -> None:
    kept = ('{"server":"https://mcp.example/mcp","authorization_endpoint":"https://auth.example/authorize",'
            '"token_endpoint":"https://auth.example/token","client_id":"client-test-0001",'
            '"access_token":"tok-test-0001","refresh_token":"refresh-test-0001","expiry":"2026-11-17T13:00:00.5Z"}')
    login = Login.from_json(kept, "linear")
    assert login.expiry == datetime(2026, 11, 17, 13, 0, 0, 500000, tzinfo=UTC) and is_login(kept)
    again = json.loads(login.to_json())
    assert again == dict(json.loads(kept), expiry="2026-11-17T13:00:00Z") and not is_login("tok-test-0001")
    with pytest.raises(SignInError, match="lacks its server, token endpoint, client or token") as refused:
        Login.from_json('{"server": "https://mcp.example/mcp", "access_token": "tok-test-0002"}', "linear")
    assert "tok-test-0002" not in str(refused.value)


def test_a_listener_is_on_loopback_on_a_port_the_system_picks() -> None:
    from cerebro.connectors.mcp_oauth import listen_on_loopback

    with listen_on_loopback() as sock:
        host, port = sock.getsockname()[:2]
    assert host == "127.0.0.1" and port > 0 and sock.type == socket.SOCK_STREAM


def test_a_client_secret_registration_hands_out_is_sent_with_each_token_request_and_never_shown(
        loopback_only: None, http: httpx.Client) -> None:
    serving = AuthStandIn(secret="secret-test-0001", rotate=False)
    try:
        clock = Clock()
        login = sign_in(serving, http, Browser(), clock=clock)
        assert login.client_secret == "secret-test-0001" and "secret-test-0001" in login.secrets()
        assert "secret-test-0001" not in repr(login)
        kept = FakeSecrets({"linear": login.to_json()})
        assert Login.from_json(kept.get("linear"), "linear").client_secret == "secret-test-0001"
        clock.at += timedelta(hours=2)
        token = LiveToken(kept, "linear", serving.server, http=http, now=clock)
        assert token() == "tok-test-0004"
    finally:
        serving.stop()
    assert [form.get("client_secret") for form in serving.forms] == ["secret-test-0001", "secret-test-0001"]
    renewed = Login.from_json(kept.get("linear"), "linear")  # a reply with no new refresh token keeps the old one
    assert renewed.refresh_token == login.refresh_token and renewed.access_token == "tok-test-0004"


def test_a_token_with_no_expiry_is_never_renewed_and_a_refusal_with_401_means_sign_in_again(
        loopback_only: None, http: httpx.Client) -> None:
    serving = AuthStandIn(expires_in=0)
    try:
        clock = Clock()
        login = sign_in(serving, http, Browser(), clock=clock)
        assert login.expiry is None and Login.from_json(login.to_json(), "linear").expiry is None
        clock.at += timedelta(days=30)
        token = LiveToken(FakeSecrets({"linear": login.to_json()}), "linear", serving.server, http=http, now=clock)
        assert token() == login.access_token and len(serving.forms) == 1
        original = serving.answer

        def unauthorized(method: str, path: str, *rest: Any) -> tuple[int, dict[str, str], Any]:
            return (401, {}, {"error": "invalid_client"}) if path == "/token" else original(method, path, *rest)

        serving.answer = unauthorized  # type: ignore[method-assign]
        with pytest.raises(LoginExpired, match="would not renew it"):
            token.refresh()
    finally:
        serving.stop()


def test_a_token_endpoint_that_redirects_is_not_followed(standin: AuthStandIn, http: httpx.Client) -> None:
    original = standin.answer

    def elsewhere(method: str, path: str, *rest: Any) -> tuple[int, dict[str, str], Any]:
        if path == "/token":
            return 307, {"Location": standin.base + "/stolen"}, {}
        return original(method, path, *rest)

    standin.answer = elsewhere  # type: ignore[method-assign]
    with pytest.raises(SignInError, match=r"the token endpoint refused \(HTTP 307\)"):
        sign_in(standin, http, Browser())
    assert "POST /stolen" not in standin.seen and "GET /stolen" not in standin.seen


def test_metadata_a_401_points_to_off_https_is_not_read_and_the_well_known_address_serves(
        standin: AuthStandIn, http: httpx.Client) -> None:
    original = standin.answer

    def pointing(method: str, path: str, *rest: Any) -> tuple[int, dict[str, str], Any]:
        if path == "/mcp":
            return 401, {"WWW-Authenticate": 'Bearer resource_metadata="http://metadata.example/prm"'}, {}
        return original(method, path, *rest)

    standin.answer = pointing  # type: ignore[method-assign]
    assert discover(http, standin.server).token_endpoint == standin.base + "/token"
    assert standin.seen[0] == "GET /.well-known/oauth-protected-resource/mcp"  # the probe is answered above


def test_an_address_whose_port_does_not_read_is_not_safe_and_a_trailing_slash_names_the_same_server(
        standin: AuthStandIn, http: httpx.Client) -> None:
    assert not safe_address("https://mcp.example:99999/mcp") and not safe_address("https://mcp.example:x/mcp")
    login = Login(standin.server + "/", standin.base + "/token", "client-test-0001", "tok-test-0001")
    assert LiveToken(FakeSecrets(), "linear", standin.server, http=http, now=Clock(), login=login)() == "tok-test-0001"


def test_sign_in_refuses_metadata_that_names_another_server_or_issuer_and_a_reply_that_trickles() -> None:
    from cerebro.connectors.mcp_oauth import _send, discover

    def answer(resource: str, issuer: str) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "oauth-protected-resource" in url:
                return httpx.Response(200, json={"resource": resource, "authorization_servers": [
                    "https://auth.wiki.example"]})
            if url.startswith("https://auth.wiki.example/.well-known/"):
                return httpx.Response(200, json={"issuer": issuer, "authorization_endpoint": "https://auth.wiki."
                                                 "example/a", "token_endpoint": "https://auth.wiki.example/t"})
            return httpx.Response(404)

        return httpx.Client(transport=httpx.MockTransport(handle))

    server = "https://mcp.wiki.example/mcp"
    with pytest.raises(SignInError, match="its protected-resource metadata names another server, mcp.other.example"):
        discover(answer("https://mcp.other.example/mcp", "https://auth.wiki.example"), server)
    with pytest.raises(SignInError, match="names its issuer as auth.other.example, not auth.wiki.example"):
        discover(answer(server, "https://auth.other.example"), server)
    assert discover(answer(server, "https://auth.wiki.example"), server).token_endpoint == "https://auth.wiki.example/t"

    trickle = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=iter([b" "] * 50))))
    clock = iter(range(1000))
    with pytest.raises(SignInError, match="did not finish answering within 5 seconds"):
        _send(trickle, "POST", "https://auth.wiki.example/t", server, "the token endpoint", deadline=5,
              ticks=lambda: float(next(clock)))
