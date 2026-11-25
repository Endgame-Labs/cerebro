"""Signing in to a hosted MCP server through the browser, and keeping that login live.

`authorize` finds the server's authorization server from its protected-resource metadata, registers this client
with it on the spot, sends the person to consent with PKCE and a `state` it checks, and trades the code that comes
back to a loopback address for tokens. `LiveToken` is the `credentials` function the MCP client is handed: it reads
the login from the secrets store, renews it with its refresh token when it is about to expire, keeps the renewed
login, and always gives a live access token. A `Login` is kept in the store as one JSON item.

Everything that touches the outside is handed in: the HTTP client, the opener for a browser, the loopback listener,
the clock and the line a person reads. Nothing here opens a browser by itself.

The rules. A token, a code, a verifier or a client secret goes over https alone, or over
http to this machine. The redirect address is loopback alone, and a return whose `state` this sign-in did not start
is ignored. No token, code or verifier appears in an error, a log line, a `repr` or a printed URL: the one URL
printed, the consent page's, carries the challenge, never the verifier. A login is sent only to the server it was
made for, so a server address a source file names never decides where another server's login goes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import queue
import re
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from secrets import token_urlsafe
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

from ..clock import seconds
from ..errors import CerebroError
from ..secrets import SecretNotFound, Secrets

__all__ = ["AuthServer", "LiveToken", "Login", "LoginExpired", "SignInError", "authorize", "discover",
           "is_login", "listen_on_loopback", "safe_address"]

CLIENT_NAME = "cerebro"
WAIT = 600.0  # seconds a sign-in waits for the browser to come back: ten minutes
HANDLER_WAIT = 10.0  # seconds the loopback listener waits on one connection that sends nothing
STOP_WAIT = 5.0  # seconds the listener's thread is given to stop
RENEW_BEFORE = timedelta(seconds=30)  # a token that expires within this is renewed first
MAX_BODY = 1 << 20
PROBE_TIMEOUT = 10.0
DEADLINE = 30.0  # seconds one exchange with a sign-in endpoint may take in all, however its reply trickles in
_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # an OAuth error code, which may be said; anything else is not


class SignInError(CerebroError):
    """A browser sign-in did not finish, or a kept login cannot be used. The message names the server, never a
    token, code or verifier."""


class LoginExpired(SignInError):
    """A login can no longer be renewed: the server refused its refresh token, or there never was one. The person
    signs in again."""


# -- addresses


def _loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def safe_address(url: str) -> bool:
    """Whether a token may be sent to `url`: https, or http to this machine, with a host, and no user in it."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - a port that does not read raises here
    except ValueError:
        return False
    host = parts.hostname or ""
    if not host or parts.username is not None or parts.password is not None:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and _loopback_host(host))


def _host(url: str) -> str:
    """A URL's host, for a message: never its path or query, which may carry a value."""
    try:
        return urlsplit(url).hostname or "an address that does not read"
    except ValueError:
        return "an address that does not read"


def _require_safe(url: str, what: str, server: str) -> str:
    if not safe_address(url):
        raise SignInError(f"{_host(server)}: {what} at {_host(url)} is not https, so nothing is sent there: ask the "
                          f"server's owner for an https address")
    return url


def _same_server(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


# -- records


@dataclass(frozen=True)
class AuthServer:
    """What an authorization server's metadata says."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ""


@dataclass(frozen=True)
class Login:
    """A person's login with one MCP server, as the browser sign-in made it: everything renewing it needs and
    nothing else. Its `repr` shows the server and the client alone."""

    server: str
    token_endpoint: str
    client_id: str
    access_token: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    authorization_endpoint: str = ""
    expiry: datetime | None = None

    def secrets(self) -> list[str]:
        """Every value of the login that must never be printed, for a mask."""
        return [v for v in (self.access_token, self.refresh_token, self.client_secret) if v]

    def to_json(self) -> str:
        """The login as the store keeps it."""
        out: dict[str, Any] = {"server": self.server}
        if self.authorization_endpoint:
            out["authorization_endpoint"] = self.authorization_endpoint
        out |= {"token_endpoint": self.token_endpoint, "client_id": self.client_id}
        if self.client_secret:
            out["client_secret"] = self.client_secret
        out["access_token"] = self.access_token
        if self.refresh_token:
            out["refresh_token"] = self.refresh_token
        if self.expiry is not None:
            out["expiry"] = self.expiry.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return json.dumps(out)

    @classmethod
    def from_json(cls, text: str, name: str) -> Login:
        """A login the store keeps under `name`. One that does not read is refused without its text."""
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise SignInError(f"the secret {name} holds no browser login: run cerebro connect again to sign in")
        text_of = {k: data.get(k) for k in ("server", "token_endpoint", "client_id", "access_token")}
        if not all(isinstance(v, str) and v for v in text_of.values()):
            raise SignInError(f"the browser login kept as {name} lacks its server, token endpoint, client or token: "
                              f"run cerebro connect again to sign in")
        expiry = None
        if data.get("expiry"):
            try:
                expiry = datetime.fromisoformat(str(data["expiry"]))
            except ValueError:
                raise SignInError(f"the browser login kept as {name} has an expiry that does not read: run cerebro "
                                  f"connect again to sign in") from None
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)

        def opt(key: str) -> str:
            value = data.get(key)
            return value if isinstance(value, str) else ""

        return cls(server=data["server"], token_endpoint=data["token_endpoint"], client_id=data["client_id"],
                   access_token=data["access_token"], refresh_token=opt("refresh_token"),
                   client_secret=opt("client_secret"), authorization_endpoint=opt("authorization_endpoint"),
                   expiry=expiry)


def is_login(value: str) -> bool:
    """Whether a kept value is a browser login, which is JSON, rather than a token, which never is."""
    return value.lstrip().startswith("{")


# -- HTTP, with nothing followed and nothing large read


@dataclass(frozen=True)
class _Reply:
    status: int
    headers: httpx.Headers
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def _send(http: httpx.Client, method: str, url: str, server: str, what: str, *, deadline: float | None = None,
          ticks: Callable[[], float] = seconds, **options: Any) -> _Reply:
    """One request to an address a token may go to, given `deadline` seconds in all, by default the client's own read
    timeout, or `DEADLINE` without one, so a reply that trickles in cannot hold it past that. Its failure says the server, what was asked and the error's type, never the error's
    text, which can quote the request."""
    _require_safe(url, what, server)
    headers = {"Accept": "application/json", **options.pop("headers", {})}
    if deadline is None:
        given = options.get("timeout", http.timeout)
        read = given.read if isinstance(given, httpx.Timeout) else given
        deadline = min(float(read), DEADLINE) if isinstance(read, int | float) and read > 0 else DEADLINE
    ends = ticks() + deadline
    late = SignInError(f"{_host(server)}: {what} at {_host(url)} did not finish answering within {deadline:g} "
                       "seconds: check the network, then try again")
    try:
        with http.stream(method, url, headers=headers, follow_redirects=False, **options) as response:
            body = b""
            for chunk in response.iter_bytes():
                if ticks() > ends:
                    raise late
                body += chunk
                if len(body) > MAX_BODY:
                    raise SignInError(f"{_host(server)}: {what} answered more than {MAX_BODY} bytes: ask the "
                                      f"server's owner to fix it")
            if ticks() > ends:
                raise late
            return _Reply(response.status_code, response.headers, body)
    except httpx.HTTPError as error:
        raise SignInError(f"{_host(server)}: {what} at {_host(url)} did not answer ({type(error).__name__}): check "
                          f"the network, then try again") from None


def _get_json(http: httpx.Client, url: str, server: str, what: str) -> Any:
    reply = _send(http, "GET", url, server, what)
    return reply.json() if 200 <= reply.status < 300 else None


# -- discovery


def _auth_param(header: str, name: str) -> str:
    """One quoted parameter of a `WWW-Authenticate` header."""
    _, found, rest = header.partition(f'{name}="')
    return rest.partition('"')[0] if found else ""


def discover(http: httpx.Client, server: str) -> AuthServer:
    """The authorization server of an MCP server: the protected-resource metadata the server points to in its 401,
    or at its well-known address, then that authorization server's own metadata. Every address it names must be
    one a token may go to."""
    _require_safe(server, "the mcp server", server)
    parts = urlsplit(server)
    origin = f"{parts.scheme}://{parts.netloc}"
    metadata: list[str] = []
    try:
        probe = _send(http, "GET", server, server, "the mcp server", timeout=PROBE_TIMEOUT)
        pointed = _auth_param(probe.headers.get("WWW-Authenticate", ""), "resource_metadata")
        if pointed and safe_address(pointed):
            metadata.append(pointed)
    except SignInError:
        pass  # a server that does not answer the probe may still publish its metadata
    path = parts.path.rstrip("/")
    metadata += [origin + "/.well-known/oauth-protected-resource" + path, origin + "/.well-known/oauth-protected-resource"]
    issuer = origin
    for url in dict.fromkeys(metadata):
        found = _get_json(http, url, server, "the protected-resource metadata")
        servers = found.get("authorization_servers") if isinstance(found, dict) else None
        if isinstance(servers, list) and servers and isinstance(servers[0], str):
            named = found.get("resource")
            at_origin = url == origin + "/.well-known/oauth-protected-resource"
            if not isinstance(named, str) or not (_same_server(named, server)
                                                  or (at_origin and _same_server(named, origin))):
                raise SignInError(f"{_host(server)}: its protected-resource metadata names another server, "
                                  f"{_host(str(named))}, not this one, so nothing is sent: check the server's "
                                  "address, or ask its owner to fix its metadata")
            issuer = _require_safe(servers[0].rstrip("/"), "the authorization server", server)
            break
    for suffix in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        found = _get_json(http, issuer + suffix, server, "the authorization server's metadata")
        if not isinstance(found, dict):
            continue
        ends = {k: found.get(k) for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint")}
        if not (isinstance(ends["authorization_endpoint"], str) and ends["authorization_endpoint"]
                and isinstance(ends["token_endpoint"], str) and ends["token_endpoint"]):
            continue
        named = found.get("issuer")
        if not isinstance(named, str) or not _same_server(named, issuer):
            raise SignInError(f"{_host(server)}: the authorization server's metadata names its issuer as "
                              f"{_host(str(named))}, not {_host(issuer)}, so nothing is sent: ask the server's owner "
                              "to fix its metadata")
        registration = ends["registration_endpoint"] if isinstance(ends["registration_endpoint"], str) else ""
        _require_safe(ends["authorization_endpoint"], "the authorization endpoint", server)
        _require_safe(ends["token_endpoint"], "the token endpoint", server)
        if registration:
            _require_safe(registration, "the registration endpoint", server)
        return AuthServer(named, ends["authorization_endpoint"], ends["token_endpoint"],
                          registration)
    raise SignInError(f"{_host(server)}: could not find the server's authorization server: its metadata names no "
                      f"authorization or token endpoint: check the server's address")


# -- the loopback return


def listen_on_loopback() -> socket.socket:
    """A listening socket on this machine's loopback address, on a port the system picks."""
    return socket.create_server(("127.0.0.1", 0))


@dataclass(frozen=True)
class _Return:
    code: str = field(default="", repr=False)
    error: str = ""


class _Handler(BaseHTTPRequestHandler):
    """Answers the browser's return. It writes no log line: the request line carries the code."""

    server: _Loopback
    timeout = HANDLER_WAIT  # a connection that opens and sends nothing does not hold the listener

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _answer(self, status: int, text: str) -> None:
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != "/callback":
            self._answer(404, "Not found.\n")
            return
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        if not hmac.compare_digest(query.get("state", "").encode(), self.server.state.encode()):
            # A return this sign-in did not start, from a stray tab or another program, is not this sign-in's.
            self.server.say("A sign-in return with a state this sign-in did not start was ignored. Still waiting "
                            "for the browser.")
            self._answer(400, "This return is not for the sign-in in progress. Return to the terminal.\n")
            return
        error, code = query.get("error", ""), query.get("code", "")
        if error:
            got = _Return(error=error if _CODE.match(error) else "an error")
        elif not code:
            got = _Return(error="")
        else:
            got = _Return(code=code)
        if got.code:
            self._answer(200, "Signed in. You can close this tab and return to the terminal.\n")
        else:
            self._answer(400, "The sign-in did not complete. Return to the terminal.\n")
        self.server.offer(got)


class _Loopback(HTTPServer):
    """An HTTP server on a socket handed in, which takes the first return whose state is its own."""

    def __init__(self, sock: socket.socket, state: str, say: Callable[[str], None]):
        super().__init__(sock.getsockname()[:2], _Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.state, self.say = state, say
        self.got: queue.Queue[_Return] = queue.Queue(maxsize=1)

    def offer(self, got: _Return) -> None:
        try:
            self.got.put_nowait(got)
        except queue.Full:
            pass


def _redirect_of(sock: socket.socket) -> str:
    host, port = sock.getsockname()[:2]
    if not _loopback_host(str(host)):
        raise SignInError(f"the sign-in's return address {host} is not this machine's loopback: hand in a listener "
                          f"on 127.0.0.1")
    shown = f"[{host}]" if ":" in str(host) else str(host)
    return f"http://{shown}:{port}/callback"


# -- the sign-in


def _register(http: httpx.Client, server: str, endpoint: str, client_name: str, redirect: str) -> tuple[str, str]:
    body = {"client_name": client_name, "redirect_uris": [redirect], "grant_types": ["authorization_code",
            "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}
    reply = _send(http, "POST", endpoint, server, "the registration endpoint", json=body)
    if not 200 <= reply.status < 300:
        raise SignInError(f"{_host(server)}: the registration endpoint answered HTTP {reply.status}: try again "
                          f"later")
    found = reply.json()
    client_id = found.get("client_id") if isinstance(found, dict) else None
    if not isinstance(client_id, str) or not client_id:
        raise SignInError(f"{_host(server)}: the registration endpoint sent no client id: try again later")
    secret = found.get("client_secret")
    return client_id, secret if isinstance(secret, str) else ""


def _exchange(http: httpx.Client, login: Login, form: dict[str, str], now: datetime) -> Login:
    """Post a token request and take the tokens it answers."""
    form = dict(form, client_id=login.client_id)
    if login.client_secret:
        form["client_secret"] = login.client_secret
    reply = _send(http, "POST", login.token_endpoint, login.server, "the token endpoint", data=form,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    found = reply.json()
    found = found if isinstance(found, dict) else {}
    access = found.get("access_token")
    if not 200 <= reply.status < 300 or not isinstance(access, str) or not access:
        code = str(found.get("error") or "")
        if code == "invalid_grant" or reply.status == 401:
            raise LoginExpired(f"{_host(login.server)}: the server refused the grant, so the login has expired: run "
                               f"cerebro connect again to sign in")
        why = code if _CODE.match(code) else f"HTTP {reply.status}"
        raise SignInError(f"{_host(login.server)}: the token endpoint refused ({why}): run cerebro connect again "
                          f"to sign in")
    refresh = found.get("refresh_token")
    expires = found.get("expires_in")
    expiry = None
    if isinstance(expires, int | float) and not isinstance(expires, bool) and expires > 0:
        expiry = now + timedelta(seconds=int(expires))
    return replace(login, access_token=access, expiry=expiry,
                   refresh_token=refresh if isinstance(refresh, str) and refresh else login.refresh_token)


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _with_query(endpoint: str, query: dict[str, str]) -> str:
    parts = urlsplit(endpoint)
    kept = {k: v[0] for k, v in parse_qs(parts.query).items()}
    return urlunsplit(parts._replace(query=urlencode(kept | query)))


def authorize(server: str, *, http: httpx.Client, open_browser: Callable[[str], Any] | None = None,
              listen: Callable[[], socket.socket] = listen_on_loopback, say: Callable[[str], None] | None = None,
              now: Callable[[], datetime], wait: float = WAIT, client_name: str = CLIENT_NAME) -> Login:
    """Sign a person in to the MCP server at `server` through their browser, and give the login. `open_browser`
    is handed the consent page's address, and `say` the line a person reads with it, for when no browser opens.
    `listen` gives the socket the browser comes back to, which must be on loopback. Nothing is registered anywhere
    ahead of time, and nothing is kept here: the caller keeps the login."""
    say = say or (lambda line: None)
    found = discover(http, server)
    if not found.registration_endpoint:
        raise SignInError(f"{_host(server)}: the server's authorization server offers no dynamic client "
                          f"registration, so cerebro cannot sign in to it: keep a token instead, with auth: keychain "
                          f"in the source's file and cerebro connect")
    sock = listen()
    try:
        redirect = _redirect_of(sock)
        client_id, client_secret = _register(http, server, found.registration_endpoint, client_name, redirect)
        verifier, state = token_urlsafe(48), token_urlsafe(24)
        page = _with_query(found.authorization_endpoint, {
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
            "code_challenge": _challenge(verifier), "code_challenge_method": "S256", "state": state,
            "resource": server})
        loopback = _Loopback(sock, state, say)
        serving = threading.Thread(target=loopback.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        serving.start()
        try:
            say(f"Opening your browser to sign in to {_host(server)}. If it does not open, go to:")
            say(page)
            if open_browser is not None:
                try:
                    open_browser(page)
                except Exception as error:  # noqa: BLE001 - a browser that does not open is said, and the wait goes on
                    say(f"Could not open the browser ({type(error).__name__}). Open the address above yourself.")
            try:
                got = loopback.got.get(timeout=wait)
            except queue.Empty:
                raise SignInError(f"{_host(server)}: the sign-in was not finished: no answer came back from the "
                                  f"browser within {wait:g}s, so nothing was kept: run cerebro connect again and "
                                  f"finish signing in in the browser") from None
        finally:
            loopback.shutdown()
            serving.join(timeout=STOP_WAIT)
    finally:
        sock.close()
    if got.error:
        raise SignInError(f"{_host(server)}: the server refused the sign-in ({got.error}): run cerebro connect again")
    if not got.code:
        raise SignInError(f"{_host(server)}: the browser came back without a code: run cerebro connect again")
    login = Login(server=server, token_endpoint=found.token_endpoint, client_id=client_id, access_token="",
                  client_secret=client_secret, authorization_endpoint=found.authorization_endpoint)
    return _exchange(http, login, {"grant_type": "authorization_code", "code": got.code, "redirect_uri": redirect,
                                   "code_verifier": verifier, "resource": server}, now())


# -- a login in use


class LiveToken:
    """The `credentials` function the MCP client is handed for a browser login: each call gives a live access
    token. The login is read from the store under `name` on the first call, sent only to `server`, renewed with
    its refresh token when it expires within half a minute, and the renewed login is kept in the store, so the
    next process starts from it. `seen` is handed every token value read or renewed, for the app's mask. `source`
    names the source in messages."""

    def __init__(self, store: Secrets, name: str, server: str, *, http: httpx.Client, now: Callable[[], datetime],
                 source: str = "", seen: Callable[..., None] | None = None, login: Login | None = None):
        self.store, self.name, self.server, self.http, self.now = store, name, server, http, now
        self.source = source or name
        self.seen = seen
        self._login = login
        self._lock = threading.Lock()
        if login is not None:
            self._check(login)

    def __repr__(self) -> str:
        return f"<LiveToken {self.name} for {_host(self.server)}>"

    def _check(self, login: Login) -> Login:
        if not _same_server(login.server, self.server):
            raise SignInError(f"{self.source}: the browser login kept as {self.name} is for {_host(login.server)}, "
                              f"not {_host(self.server)}, so it is not sent there: run cerebro connect "
                              f"{self.source} to sign in to this server")
        _require_safe(self.server, "the mcp server", self.server)
        if self.seen is not None:
            self.seen(*login.secrets())
        return login

    def _load(self) -> Login:
        if self._login is None:
            try:
                kept = self.store.get(self.name)
            except SecretNotFound:
                raise SignInError(f"{self.source}: no browser login is kept as {self.name}: run cerebro connect "
                                  f"{self.source} to sign in") from None
            if not is_login(kept):
                if self.seen is not None:
                    self.seen(kept)
                raise SignInError(f"{self.source}: the secret {self.name} holds a token, not a browser login: run "
                                  f"cerebro connect {self.source} to sign in, or write auth: {{ keychain: "
                                  f"{self.name} }} to use the token")
            self._login = self._check(Login.from_json(kept, self.name))
        return self._login

    def login(self) -> Login:
        """The login as it is now."""
        with self._lock:
            return self._load()

    def __call__(self) -> str:
        with self._lock:
            login = self._load()
            if login.expiry is not None and self.now() + RENEW_BEFORE >= login.expiry:
                login = self._renew(login)
            return login.access_token

    def refresh(self) -> None:
        """Renew the access token now, as when the server refused it."""
        with self._lock:
            self._renew(self._load())

    def _renew(self, login: Login) -> Login:
        if not login.refresh_token:
            raise LoginExpired(f"{self.source}: the browser login has expired and has no refresh token: run cerebro "
                               f"connect {self.source} to sign in again")
        try:
            renewed = _exchange(self.http, login, {"grant_type": "refresh_token", "refresh_token":
                                                   login.refresh_token, "resource": login.server}, self.now())
        except LoginExpired:
            raise LoginExpired(f"{self.source}: the browser login has expired and the server would not renew it: "
                               f"run cerebro connect {self.source} to sign in again") from None
        self._login = self._check(renewed)
        self.store.set(self.name, renewed.to_json())
        return renewed
