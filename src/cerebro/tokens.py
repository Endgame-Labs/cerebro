"""Tokens: how a caller of the workspace's HTTP server proves who it is.

A token names a caller by a name. Every token is one kind of caller.
`cerebro tokens new <name>` makes one and prints it once. Cerebro keeps only its SHA-256, in the store's
`serve-tokens` log, with the name, when it was made and a random id of its own; the latest row of a name is
its state, and a row with no hash is a revoked token. When each was last used is kept as state under the name, with
the id, so a name made again does not take the last use of the token it had before. The plain token is never
written anywhere: not to the store, a log, an error, a `repr` or the request log.

A token is `cbt_` and then 43 characters of URL-safe base64: 256 bits from the `secrets` module, so guessing one is
hopeless, and the prefix makes one easy to spot in a leak. A plain SHA-256 is enough to keep: with 256 random bits
there is nothing to guess, so a keyed or slow hash would add a key to keep and nothing else.

Over HTTP a request carries `Authorization: Bearer <token>`. The gate hashes it and compares it with every live
token's hash by `hmac.compare_digest`, and a good token gives the request its caller, by the token's name,
which the MCP server reads in place of any name the client gives itself. A missing, unknown or revoked token gets
401 with a `WWW-Authenticate` header and one plain line, and reaches nothing in the workspace. A token is checked on
each request against the store, so a revoke takes effect on the next request, and a request still running, such as an
open stream, is ended within `WATCH` seconds and a few more. A session belongs to the token that opened it. A request
that carries a header the gate or the library decides on more than once, such as two Authorization or two
Mcp-Session-Id headers, is refused before anything else, so the gate and the library never read different values. A
token in a JSON-RPC id or method, which the SDK would send back, is refused.

Every request needs a live token, on every address, loopback too: a proxy or tunnel in front of a loopback server
makes every caller look local, so where the server binds says nothing of where a request comes from. With no live
token the server on loopback still starts and refuses every request but `/healthz`; binding another address with
no live token is refused before anything is bound. The tokenless way to reach a workspace on one machine is standard
input and output, `cerebro mcp`. Cerebro does not end TLS: a workspace on a host sits behind something that does.

Before the token, the gate checks the request's Host header against the names the workspace is reached by: those given
with `serve --allowed-host`, else those under `serve: allowed_hosts:` in settings.yaml, else the loopback names alone.
A name is matched exactly, ignoring case and the port, and `*` is refused. A request whose Host is not listed gets 421
and one line naming the Host it sent, before its token is read.

`GET /healthz` answers 200 with a fixed body and needs no token and no listed Host. A request body larger than `BODY_CAP` is refused
with 413: by its Content-Length before any of it is read, and without one, as soon as the chunks read pass the cap.

A token in a request's URL, its path or its query string, is refused with 400 after the Host check and before anything
else: a URL is kept by proxies and browsers, so a token belongs in the `Authorization` header alone.

The JSON API under `/api/v1` (see `api`) passes the same gate in the same order, with three more rules. Every call
needs a live token, on loopback too, since a page in a browser on this machine may call it. A browser's `Origin` must
be one listed with `serve --allowed-origin`, else under `serve: allowed_origins:`, matched as an exact string: one not
listed is refused with 403 before the token is read, and with none listed no CORS header is ever sent. A preflight is
answered here, with no token, and says nothing about the workspace. Cookies are never read or set, so a request with a
cookie and no bearer token gets 401. A refusal on these routes is `{"error": {"code": ..., "message": ...}}`.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote_plus

import anyio.to_thread

from .caller import Caller
from .clock import Clock, stamp
from .errors import CerebroError
from .store import Store, StoreBusy, StoreUnwritable, brief_wait

__all__ = ["API", "BODY_CAP", "CALLER_KEY", "HEALTH", "HIDDEN", "LOG", "LOOPBACK_NAMES", "MASKED", "ONCE", "PREFIX",
           "SETTINGS_KEYS",
           "Gate", "HostError", "NewToken", "OriginError", "Token", "TokenError", "Tokens", "allowed_hosts",
           "allowed_origins", "error_reply", "hide_tokens", "is_api", "is_loopback", "masked"]

PREFIX = "cbt_"
BYTES = 32  # 256 bits
LOG = "serve-tokens"
USED = "serve-tokens/used/"  # the state key of when a name's token was last used, followed by the name
RESERVED = ("you", "mcp")  # the names Cerebro gives the local caller at a terminal and over MCP with no token
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHAPE = re.compile(re.escape(PREFIX) + r"[A-Za-z0-9_-]{43}")  # a token wherever it sits in a string
MASKED = "[a token, masked]"  # holds no part of any token
HIDDEN = ("a token was masked in what you sent, before anything was kept: if it was a real token, revoke it with "
          "cerebro tokens revoke <name>")
BODY_CAP = 1024 * 1024  # bytes in one request body: far more than any tool's arguments need
CALLER_KEY = "cerebro.caller"  # where the gate puts a request's caller in its ASGI scope
HEALTH = b"ok\n"
GONE = b"\0gone"  # a marker, compared by identity, for a body whose client went away
SLOW = b"\0slow"  # a marker, compared by identity, for a body not whole in time
REALM = 'Bearer realm="cerebro"'
NEEDS = b"this workspace needs a token: send Authorization: Bearer <token>, made with cerebro tokens new <name>\n"
REFUSED = (b"this token is not accepted: it is unknown or was revoked: make a new one with cerebro tokens new "
           b"<name>\n")
TOO_LARGE = f"this request's body is larger than {BODY_CAP} bytes: send a smaller one\n".encode()
ECHOED = b"this request's id or method holds a token, which a reply would send back: send them without it\n"
TOO_DEEP = b"this request's body nests too deeply to read: send it flatter\n"
NO_TEXT = b"this request's body holds a lone surrogate escape, which is no character: send UTF-8 text\n"
DEPTH = 64  # brackets a body may nest: far more than any tool's arguments need
# The headers the gate or the library behind it decides on: each must come once, so both read the one same value.
ONCE = frozenset({b"host", b"authorization", b"mcp-session-id", b"origin", b"content-length", b"content-type",
                  b"transfer-encoding", b"mcp-protocol-version", b"accept", b"last-event-id"})
TOO_SLOW = b"this request's body did not arrive whole in time: send it again at once\n"
NOT_A_LENGTH = b"this request's Content-Length is not a number of bytes: send a valid one\n"
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")  # the Host names a server accepts when none are listed
OTHERS = b"this session was opened with another token: open a session of your own with initialize\n"
WATCH = 2.0  # seconds between the checks that a running request's token is live: a revoke ends a stream within 5
SESSIONS = 10_000  # the sessions whose token the gate remembers, newest kept
API = "/api"  # the JSON API's routes sit under this path, and its refusals are JSON
IN_URL = b"a token was sent in this request's URL: send it only as Authorization: Bearer <token>, and make a new one " \
         b"with cerebro tokens new, since a URL is kept where a header is not\n"
NO_ORIGIN = b"this origin may not call this server: list it with serve --allowed-origin <origin>\n"
ORIGIN = re.compile(r"(https?)://([a-z0-9]([a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:.]+\])(:([1-9][0-9]{0,4}))?")
DEFAULT_PORTS = {"http": "80", "https": "443"}
PREFLIGHT = [(b"access-control-allow-methods", b"GET, POST"),
             (b"access-control-allow-headers", b"authorization, content-type"), (b"access-control-max-age", b"600")]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


class TokenError(CerebroError, ValueError):
    """A token could not be made, revoked, or served with as asked."""


class HostError(CerebroError, ValueError):
    """A host name the server is told to accept is not one it takes."""


class OriginError(CerebroError, ValueError):
    """An origin the server is told to let call its JSON API is not one it takes."""


@dataclass(frozen=True)
class Token:
    """A token as Cerebro shows it: its name, when it was made and when last used. Never the token or its hash."""

    name: str
    made: str
    used: str = ""

    @property
    def caller(self) -> Caller:
        return Caller(self.name, "person")


@dataclass(frozen=True)
class NewToken:
    """A token just made: the plain token, to print once, and its record. The token is left out of `repr`."""

    token: str = field(repr=False)
    record: Token


def is_loopback(host: str) -> bool:
    """Whether `host` is this machine alone: `localhost`, or an address in 127.0.0.0/8 or ::1."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def _host_name(name: str) -> str:
    """A host as it is compared: lower case, without its port, and an IPv6 address without its brackets."""
    name = name.strip().lower()
    if name.startswith("["):
        end = name.find("]")
        return name[1:end] if end > 0 else name
    bare, colon, port = name.rpartition(":")
    return bare if colon and port.isdigit() and ":" not in bare else name


SETTINGS_KEYS = {"serve": {"allowed_hosts": None, "allowed_origins": None}}


def allowed_hosts(given: Iterable[str] | None, settings: Mapping[str, Any]) -> tuple[str, ...]:
    """The Host names the server accepts: those `given`, as by `serve --allowed-host`, else settings.yaml's
    `serve: allowed_hosts:`, else the loopback names. Each is matched exactly; a pattern such as `*` is refused."""
    names: Any = list(given or ())
    where = "--allowed-host"
    if not names:
        names = (settings.get("serve") or {}).get("allowed_hosts") or []
        where = "serve.allowed_hosts in settings.yaml"
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise HostError(f"{where} is not a list of host names: write it as allowed_hosts: [wiki.example]")
    found = []
    for name in names:
        if "*" in name:
            raise HostError(f"{where} {name[:70]!r} is a pattern, and a host name is matched exactly: list each name "
                            "this workspace is reached by, such as wiki.example")
        bare = _host_name(name)
        plain = name.strip().lower().removeprefix("[").removesuffix("]")
        if not bare or bare != plain or any(c in bare for c in "/@ \t") or (":" in bare and not _is_ip(bare)):
            raise HostError(f"{where} {name[:70]!r} is not a host name: give the name alone, such as wiki.example, "
                            "with no scheme, path or port")
        found.append(bare)
    return tuple(dict.fromkeys(found)) or LOOPBACK_NAMES


def allowed_origins(given: Iterable[str] | None, settings: Mapping[str, Any]) -> tuple[str, ...]:
    """The origins whose pages may call the JSON API from a browser: those `given`, as by `serve --allowed-origin`,
    else settings.yaml's `serve: allowed_origins:`, else none. Each is written as a browser sends it, scheme, host in
    lower case and a port only when not the default, and matched as an exact string; a pattern such as `*` is
    refused."""
    origins: Any = list(given or ())
    where = "--allowed-origin"
    if not origins:
        origins = (settings.get("serve") or {}).get("allowed_origins") or []
        where = "serve.allowed_origins in settings.yaml"
        if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
            raise OriginError(f"{where} is not a list of origins: write it as allowed_origins: [https://app.example]")
    for origin in origins:
        if "*" in origin:
            raise OriginError(f"{where} {origin[:70]!r} is a pattern, and an origin is matched exactly: list each "
                              "origin a page calls from, such as https://app.example")
        shape = ORIGIN.fullmatch(origin)
        port = shape[5] if shape else None
        if shape is None or port == DEFAULT_PORTS[shape[1]] or (port and int(port) > 65535):
            raise OriginError(f"{where} {origin[:70]!r} is not an origin as a browser sends it: give the scheme, the "
                              "host in lower case, and a port only when it is not the default, such as "
                              "https://app.example or http://127.0.0.1:5173")
    return tuple(dict.fromkeys(origins))


def is_api(path: str) -> bool:
    """Whether `path` is one of the JSON API's routes, whose refusals are JSON."""
    return path == API or path.startswith(API + "/")


def _in_url(scope: Scope) -> bool:
    """Whether a token, or an `access_token` parameter, sits in the request's path or query string, as sent or
    decoded."""
    query = scope.get("query_string", b"").decode("latin-1")
    raw = scope.get("raw_path", b"").decode("latin-1")
    seen = (scope.get("path", ""), raw, unquote_plus(raw), query, unquote_plus(query))
    names = [part.partition("=")[0].lower() for part in unquote_plus(query).split("&")]
    return any(SHAPE.search(text) for text in seen) or "access_token" in names


async def error_reply(send: Send, status: int, code: str, message: str,
                      headers: Iterable[tuple[bytes, bytes]] = ()) -> None:
    """A refusal on the JSON API's routes: `{"error": {"code", "message"}}`, the message one line with every token in
    it masked, and never cached."""
    body = json.dumps({"error": {"code": code, "message": masked(" ".join(message.split()))}}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store"), *headers]})
    await send({"type": "http.response.body", "body": body})


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def hide_tokens(text: str) -> str:
    """`text` with every token in it masked: the one place the token shape is matched. The request log, the text a
    caller gives `tell`, `decide` and `retract`, and Cerebro's mask of secrets all pass through it."""
    return SHAPE.sub(MASKED, text)


def masked(value: Any) -> Any:
    """A value with every token in it masked: in each string, a map's keys included, however deeply nested. The
    request log keeps a tool's arguments through this, so a token a client pastes into one is never kept."""
    if isinstance(value, str):
        return hide_tokens(value)
    if isinstance(value, dict):
        return {masked(k): masked(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [masked(v) for v in value]
    return value


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Tokens:
    """A workspace's tokens, kept in its store, with the clock that stamps them."""

    def __init__(self, store: Store, clock: Clock):
        self.store, self.clock = store, clock

    def _live(self) -> list[dict[str, Any]]:
        return [row.data for row in self.store.latest(LOG, key="name") if row.data.get("hash")]

    def make(self, name: str) -> NewToken:
        """A new token for `name`. A name with a live token is refused; a revoked one may be made again."""
        if not NAME.fullmatch(name):
            raise TokenError(f"the token name {name[:70]!r} is not one cerebro takes: use letters, digits, dot, dash "
                             "or underscore, starting with a letter or digit, at most 64")
        if name.lower() in RESERVED:
            raise TokenError(f"the token name {name} is the name Cerebro gives a caller with no token: pick another")
        token = PREFIX + secrets.token_urlsafe(BYTES)
        made = stamp(self.clock())
        with self.store.exclusive():
            if any(row["name"] == name for row in self._live()):
                raise TokenError(f"a token named {name} exists already: revoke it with cerebro tokens revoke {name}, "
                                 "or pick another name")
            self.store.append(LOG, {"name": name, "hash": _hash(token), "made": made,
                                    "id": secrets.token_hex(8)})
        return NewToken(token, Token(name, made))

    def live(self) -> list[Token]:
        """Every live token, oldest first, with when each was last used."""
        found = []
        for row in self._live():
            used = self.store.state(USED + row["name"]) or {}
            found.append(Token(row["name"], row["made"],
                               used.get("at", "") if used.get("id") == row["id"] else ""))
        return found

    def revoke(self, name: str) -> Token:
        """End the token of `name`: the next request that carries it is refused."""
        with self.store.exclusive():
            live = next((row for row in self._live() if row["name"] == name), None)
            if live is None:
                raise TokenError(f"no live token is named {name[:70]}: cerebro tokens list shows them")
            self.store.append(LOG, {"name": name, "revoked": stamp(self.clock())})
        return Token(live["name"], live["made"])

    def any(self) -> bool:
        """Whether a live token exists."""
        return bool(self._live())

    def holds(self, hashed: str) -> bool:
        """Whether a live token has the hash `hashed`: what an open stream asks while it runs. Stamps nothing."""
        return any(hmac.compare_digest(hashed, str(row["hash"])) for row in self._live())

    def check(self, authorization: str | None) -> Token | None:
        """The live token an `Authorization: Bearer` header carries, or None. Every live hash is compared, in
        constant time, so how long a check takes says nothing of how near a guess came. A match stamps its use."""
        scheme, _, presented = (authorization or "").strip().partition(" ")
        presented = presented.strip()
        if scheme.lower() != "bearer" or not presented.startswith(PREFIX):
            return None
        given = _hash(presented)
        found = None
        for row in self._live():
            if hmac.compare_digest(given, str(row["hash"])):
                found = row
        if found is None:
            return None
        at = stamp(self.clock())
        key, used = USED + found["name"], {"id": found["id"], "at": at}
        if self.store.state(key) != used:
            with contextlib.suppress(StoreBusy, StoreUnwritable), brief_wait(self.store, 0):  # never held: a later request stamps it
                self.store.set_state(key, used)
        return Token(found["name"], found["made"], at)

    def may_serve(self, host: str) -> bool:
        """Whether the server may bind `host`: loopback always, another address only once a live token exists. Says
        whether a live token exists, so that a caller can get in at all."""
        if is_loopback(host):
            return self.any()
        if not self.any():
            raise TokenError(f"serve --host {host[:70]} is not loopback, and this workspace has no token: make one with "
                             "cerebro tokens new <name> first, so a caller must prove who it is")
        return True


async def _plain(send: Send, status: int, body: bytes, headers: Iterable[tuple[bytes, bytes]] = ()) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()), *headers]})
    await send({"type": "http.response.body", "body": body})


class Gate:
    """The HTTP server's check of each request, in front of the MCP app: the health route, the token, and the bound on
    the body. `hosts` are the Host names accepted, checked before the token, and `origins` those a page may call
    the JSON API from. A request runs only while its token stays live: every `watch` seconds the gate asks again,
    and a stream whose token was revoked is ended. A session belongs to the token that opened it, and a request that
    names it with another token is refused."""

    def __init__(self, app: App, tokens: Tokens, *, hosts: Iterable[str] = LOOPBACK_NAMES, cap: int = BODY_CAP,
                 origins: Iterable[str] = (), watch: float | None = None, body_timeout: float = 30.0):
        self.app, self.tokens, self.cap, self.watch = app, tokens, cap, watch or WATCH
        self.body_timeout = body_timeout  # seconds to read a request's whole body
        self.hosts = frozenset(_host_name(name) for name in hosts)
        self.origins = frozenset(origins)
        self.sessions: dict[str, str] = {}  # a session id, and the hash of the token that opened it

    async def _refuse(self, send: Send, api: bool, status: int, code: str, line: bytes,
                      headers: Iterable[tuple[bytes, bytes]] = ()) -> None:
        """A refusal as the route speaks: JSON with its code on the JSON API's routes, else one plain line."""
        if api:
            await error_reply(send, status, code, line.decode(), headers)
        else:
            await _plain(send, status, line, headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # a websocket or any other kind of request: nothing here serves one
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        if scope["path"] == "/healthz":
            if scope["method"] in ("GET", "HEAD"):
                await _plain(send, 200, HEALTH)
            else:
                await _plain(send, 405, b"healthz answers GET\n", [(b"allow", b"GET, HEAD")])
            return
        api = is_api(scope["path"])
        names = [k.lower() for k, _ in scope.get("headers", [])]
        twice = sorted({n.decode("latin-1") for n in names if n in ONCE and names.count(n) > 1})
        if twice:
            await self._refuse(send, api, 400, "bad_request", (
                f"this request carries {twice[0]} more than once: send each such header once\n").encode())
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        seen = headers.get(b"host", b"").decode("latin-1")
        if _host_name(seen) not in self.hosts:
            shown = "".join(c for c in seen[:100] if c.isprintable())
            await self._refuse(send, api, 421, "host_refused", (
                f"this server is not reached by the Host {shown!r}: list each name it is reached by with serve "
                "--allowed-host <name>\n").encode())
            return
        if _in_url(scope):
            await self._refuse(send, api, 400, "token_in_url", IN_URL)
            return
        if api:
            origin = headers.get(b"origin")
            listed = origin is not None and origin.decode("latin-1") in self.origins
            cors = [(b"access-control-allow-origin", origin), (b"vary", b"Origin")] if listed else []
            if scope["method"] == "OPTIONS":  # a preflight: answered here, with no token, naming nothing
                if listed:
                    await send({"type": "http.response.start", "status": 204, "headers": [*cors, *PREFLIGHT]})
                    await send({"type": "http.response.body", "body": b""})
                else:
                    await error_reply(send, 403, "origin_refused", NO_ORIGIN.decode())
                return
            if origin is not None and not listed:
                await error_reply(send, 403, "origin_refused", NO_ORIGIN.decode())
                return
            send = _adding(send, cors) if cors else send
        # The SDK checks Host again, case and all: hand it the name in the lower case it was matched in.
        scope = {**scope, "headers": [(k, v.lower() if k.lower() == b"host" else v) for k, v in scope["headers"]]}
        given = headers.get(b"authorization", b"").decode("latin-1")
        token = await anyio.to_thread.run_sync(self.tokens.check, given) if given else None
        if token is None:
            await self._refuse(send, api, 401, "unauthorized", REFUSED if given else NEEDS,
                               [(b"www-authenticate", (REALM + (', error="invalid_token"' if given else "")).encode())])
            return
        scope = {**scope, CALLER_KEY: token.caller}
        hashed = _hash(given.strip().partition(" ")[2].strip())
        opened = self.sessions.get(headers.get(b"mcp-session-id", b"").decode("latin-1"))
        if opened is not None and not hmac.compare_digest(opened, hashed):
            await self._refuse(send, api, 403, "forbidden", OTHERS)
            return
        length = headers.get(b"content-length")
        if length is not None:
            if not length.strip().isdigit():
                await self._refuse(send, api, 400, "bad_request", NOT_A_LENGTH)
                return
            if int(length) > self.cap:
                await self._refuse(send, api, 413, "too_large", TOO_LARGE)
                return
        body: bytes | None = SLOW
        with anyio.move_on_after(self.body_timeout):
            body = await self._bounded(receive)
        if body is SLOW:
            await self._refuse(send, api, 408, "too_slow", TOO_SLOW, [(b"connection", b"close")])
            return
        if body is GONE:
            return
        if body is None:
            await self._refuse(send, api, 413, "too_large", TOO_LARGE)
            return
        if _too_deep(body):
            await self._refuse(send, api, 400, "bad_request", TOO_DEEP)
            return
        if _no_text(body):
            await self._refuse(send, api, 400, "bad_request", NO_TEXT)
            return
        if not api and _echoed(body):
            await _plain(send, 400, ECHOED)
            return
        await self._watched(scope, _replay(body, receive), send, hashed, api)

    async def _watched(self, scope: Scope, receive: Receive, send: Send, hashed: str, api: bool) -> None:
        """Run the app on a request while its token stays live, and end the response once it is revoked."""
        state = {"started": False, "done": False, "revoked": False}

        async def tracked(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["started"] = True
                found = dict(message.get("headers") or []).get(b"mcp-session-id")
                if found and message.get("status") == 200 and scope["method"] == "POST":
                    self.sessions[found.decode("latin-1")] = hashed
                    while len(self.sessions) > SESSIONS:
                        self.sessions.pop(next(iter(self.sessions)))
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                state["done"] = True
            await send(message)

        async with anyio.create_task_group() as group:
            async def watch() -> None:
                while True:
                    await anyio.sleep(self.watch)
                    if not await anyio.to_thread.run_sync(self.tokens.holds, hashed):
                        state["revoked"] = True
                        group.cancel_scope.cancel()
                        return

            group.start_soon(watch)
            await self.app(scope, receive, tracked)
            group.cancel_scope.cancel()
        if state["revoked"]:
            if not state["started"]:
                await self._refuse(send, api, 401, "unauthorized", REFUSED,
                                   [(b"www-authenticate", (REALM + ', error="invalid_token"').encode())])
            elif not state["done"]:
                await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _bounded(self, receive: Receive) -> bytes | None:
        """The whole body, read chunk by chunk, or None as soon as it passes the cap, or `GONE` when the client
        went away before sending it all."""
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return GONE
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.cap:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)


def _too_deep(body: bytes) -> bool:
    """Whether a JSON body nests more than `DEPTH` brackets deep, outside its strings, past which a parser runs out of
    stack."""
    if body.count(b"[") + body.count(b"{") <= DEPTH:
        return False
    depth, quoted, escaped = 0, False, False
    for byte in body:
        if quoted:
            escaped, quoted = (False, True) if escaped else (byte == 92, byte != 34)
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > DEPTH:
                return True
        elif byte in (93, 125):
            depth -= 1
    return False


def _no_text(body: bytes) -> bool:
    """Whether a JSON body holds a lone surrogate, as an escape: no character, which no store or log can keep."""
    if b"\\ud" not in body.lower():
        return False
    try:
        json.dumps(json.loads(body), ensure_ascii=False).encode()
    except UnicodeEncodeError:
        return True
    except (ValueError, RecursionError):
        return False
    return False


def _echoed(body: bytes) -> bool:
    """Whether a JSON-RPC message's id or method holds a token: the SDK sends both back as they came, past any mask."""
    if not SHAPE.search(body.decode("utf-8", "replace")):
        return False
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return False
    return any(isinstance(m, dict) and any(isinstance(m.get(k), str) and SHAPE.search(m[k]) for k in ("id", "method"))
               for m in (data if isinstance(data, list) else [data]))


def _adding(send: Send, headers: list[tuple[bytes, bytes]]) -> Send:
    """A send that adds `headers` to the reply's start, as the CORS headers of a listed origin."""

    async def sending(message: Message) -> None:
        if message["type"] == "http.response.start":
            message = {**message, "headers": [*message.get("headers", []), *headers]}
        await send(message)

    return sending


def _replay(body: bytes, receive: Receive) -> Receive:
    """A receive that gives the body read already, then hands on to the client's own."""
    given = False

    async def replayed() -> Message:
        nonlocal given
        if not given:
            given = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replayed
