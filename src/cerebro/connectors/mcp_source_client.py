"""One open session with an MCP server, called from sync code: the client every MCP-backed connector is handed.

A source file says where its server is: `url` over Streamable HTTP, or `command`, run from the workspace's folder with
no shell, over standard input and output. A server started by command gets the few variables a process needs and
the ones its file names under `env:`, by name alone, each valued from the environment Cerebro runs in or else from
the secrets store. Credentials are handed in as a function that returns headers or a bare token, asked again on
every HTTP request so a renewed login is picked up. Signing in is not done here.

Opening the client opens the session and lists the server's tools. `call(tool, arguments)` gives the reply as plain
data: the structured content when the server sends it, else its text read as JSON, else the text. A tool the server
does not offer, a reply the server marks as an error, a timeout and a server that cannot be reached each raise one
of the errors below, whose message is one line that names the source and what to do. No type of the MCP SDK
leaves this module. `reader(*tools)` gives a `call` that refuses every tool but those, so a connector that reads
can never write.

The SDK is async. The client runs it on an event loop of its own, in one thread, for as long as it is open, and
every call waits for its answer there, so it may be called from many threads at once. `close` ends the session
and stops a server it started.
"""

from __future__ import annotations

import functools
import ipaddress
import json
import shlex
import tempfile
from collections.abc import AsyncIterator, Callable, Generator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlsplit

from ..connector import one_line
from ..errors import CerebroError
from ..source import SourceFile

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal
    from mcp import Client

__all__ = ["Call", "Credentials", "McpClient", "McpError", "Reach", "Reply", "Tool", "ToolNotOffered", "ToolRefused",
           "reach"]

Credentials = Callable[[], Mapping[str, str] | str]
"""Gives the headers to send with each HTTP request, or a token sent as `Authorization: Bearer <token>`."""
Call = Callable[..., Any]
"""`call(tool, arguments=None)`: one tool's reply as plain data."""

TIMEOUT = 120.0
MAX_PAGES = 100
PASSED_ENV = ("PATH", "HOME", "LANG", "TMPDIR")
NOTE = "cerebro/note"  # the `_meta` key a Cerebro server marks its note of proposals waiting with

def _client_info() -> Any:
    """Who this client is, to a server. The MCP SDK, and the HTTP and async stacks under it, are imported when a
    session first opens, so importing cerebro does not pay for them."""
    from mcp.types import Implementation

    return Implementation(name="cerebro", version="0")


class McpError(CerebroError):
    """A server could not be reached, or did not answer as an MCP server does."""


class ToolRefused(McpError):
    """The server answered a call and marked its reply as an error."""

    def __init__(self, message: str, tool: str, text: str):
        super().__init__(message)
        self.tool = tool
        self.text = text


class ToolNotOffered(McpError):
    """A call named a tool the server does not offer, or one the caller may not call."""


@dataclass(frozen=True)
class Tool:
    """One tool a server offers. `read_only` and `destructive` are the server's own hints, not guarantees."""

    name: str
    description: str = ""
    read_only: bool = False
    destructive: bool = False


@dataclass(frozen=True)
class Reply:
    """One reply: `value` as plain data, and `text`, its text items joined by new lines."""

    value: Any
    text: str


@dataclass
class Reach:
    """How to reach one source's server: `url`, or `command` with the variables `env` gives it, run in `cwd`.
    `transport` stands in for the network and `server` serves an SDK server object in this process, both for
    tests. `name` is the source's name, for messages."""

    name: str
    url: str = ""
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    credentials: Credentials | None = None
    timeout: float = TIMEOUT
    transport: Any = None
    server: Any = None


def _line(text: object, limit: int = 300) -> str:
    return one_line(text, limit)


def _loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def reach(source: SourceFile, root: Path, *, credentials: Credentials | None = None,
          secret: Callable[[str], str | None] | None = None, environ: Mapping[str, str] | None = None) -> Reach:
    """How to reach the server a source file names. Each variable under `env:` takes its value from `environ`, else
    from `secret`, under the source's name, a dash and the variable's name. Nothing is read from the process's own
    environment unless the caller hands it in as `environ`, as the app does."""
    name, label = source.name, f"sources/{source.name}.yaml"
    environ = {} if environ is None else environ
    if source.auth and credentials is None:
        raise McpError(f"{name}: auth {source.auth} needs a login: run cerebro connect {name}, or use a server that "
                       f"needs no login")
    if source.url:
        if not source.url.startswith(("https://", "http://")):
            raise McpError(f"{label} url {source.url}: write an http or https address")
        if credentials is not None and source.url.startswith("http://") and not _loopback(source.url):
            raise McpError(f"{label} url {urlsplit(source.url).hostname}: a login is sent over https alone, or over "
                           f"http to this machine: write the server's https address")
        return Reach(name, url=source.url, credentials=credentials, cwd=root)
    if not source.command:
        raise McpError(f"{label} names no server: add url, or command with the command line that starts one")
    if credentials is not None:
        raise McpError(f"{name}: credentials reach a server over http alone: name the variables a command takes "
                       f"under env: instead")
    try:
        command = shlex.split(source.command)
    except ValueError as error:
        raise McpError(f"{label} command does not read ({error}): close its quotes") from None
    env = {key: environ[key] for key in PASSED_ENV if environ.get(key)}
    for variable in source.env:
        key = f"{name.replace('.', '_')}-{variable}"
        value = environ.get(variable) or (secret(key) if secret is not None else None)
        if not value:
            raise McpError(f"{name}: env {variable} has no value: set {variable} in the environment cerebro runs "
                           f"in, or keep it in the secrets store as {key}")
        env[variable] = value
    return Reach(name, command=command, env=env, cwd=root)


def _anyio() -> Any:
    import anyio

    return anyio


@functools.cache
def _headers_class() -> type:
    import httpx2

    class _Headers(httpx2.Auth):
        """Asks the credentials function on every request and sends what it gives. When the server answers 401 and
        the credentials can `refresh`, as a browser login's live token can, they are renewed once and the request
        sent again."""

        def __init__(self, credentials: Credentials, refused: list[int] | None = None):
            self.credentials = credentials
            self.refused = refused if refused is not None else []  # each 401 or 403 that stood, since the SDK drops it

        def _sign(self, request: httpx2.Request) -> None:
            bound = getattr(self.credentials, "server", None)  # a browser login, bound to the server it was made for
            if isinstance(bound, str) and bound:
                made, sent = urlsplit(bound), urlsplit(str(request.url))
                if (made.scheme, made.hostname, made.port) != (sent.scheme, sent.hostname, sent.port):
                    raise McpError(f"the browser login for {made.hostname} is not sent to {sent.hostname}: sign in "
                                   f"to that server with cerebro connect")
            given = self.credentials()
            request.headers.update({"Authorization": f"Bearer {given}"} if isinstance(given, str) else dict(given))

        def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
            self._sign(request)
            response = yield request
            refresh = getattr(self.credentials, "refresh", None)
            if response.status_code == 401 and callable(refresh):
                refresh()
                self._sign(request)
                response = yield request
            if response.status_code in (401, 403):
                self.refused.append(response.status_code)

    return _Headers


@asynccontextmanager
async def _session(where: Reach, refused: list[int] | None = None) -> AsyncIterator[Client]:
    import httpx2
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    client_info = _client_info()
    async with AsyncExitStack() as stack:
        if where.server is not None:
            target: Any = where.server
        elif where.url:
            auth = _headers_class()(where.credentials, refused) if where.credentials is not None else None
            http = await stack.enter_async_context(httpx2.AsyncClient(
                transport=where.transport, auth=auth, timeout=httpx2.Timeout(30.0, read=where.timeout)))
            target = streamable_http_client(where.url, http_client=http)
        else:
            # The server's own error output goes to a file, never the terminal, where no mask reaches it. When the
            # session does not open, its last line joins the error, which the app masks.
            errlog = stack.enter_context(tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace"))  # noqa: SIM115
            target = stdio_client(StdioServerParameters(command=where.command[0], args=where.command[1:],
                                                        env=where.env, cwd=where.cwd), errlog=errlog)
            try:
                client = await stack.enter_async_context(Client(target, client_info=client_info, cache=None,
                                                                read_timeout_seconds=where.timeout, mode="legacy"))
            except Exception as error:
                errlog.seek(0)
                said = [line for line in errlog.read().splitlines() if line.strip()]
                if said:
                    raise McpError(f"{where.name}: the command {where.command[0]} did not open a session: it said "
                                   f"{one_line(said[-1], 200)}: check its command line") from error
                raise
            yield client
            return
        yield await stack.enter_async_context(Client(target, client_info=client_info, cache=None,
                                                     read_timeout_seconds=where.timeout, mode="legacy"))


def _leaf(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _value(structured: Any, text: str) -> Any:
    if structured is not None:
        return structured
    try:
        return json.loads(text)
    except ValueError:
        return text


class McpClient:
    """An open session with one server. Use it in a `with` block, or call `close`."""

    def __init__(self, where: Reach):
        self.reach = where
        self.name = where.name
        self._portal: BlockingPortal | None = None
        self._closers: list[Any] = []
        self._refusals: list[int] = []  # each 401 or 403 the server answered, which the SDK reports only as an error
        from anyio.from_thread import start_blocking_portal

        try:
            portal_cm = start_blocking_portal()
            self._portal = portal_cm.__enter__()
            self._closers.append(portal_cm)
            session_cm = self._portal.wrap_async_context_manager(_session(where, self._refusals))
            self._client = session_cm.__enter__()
            self._closers.append(session_cm)
            self.tools = self._portal.call(self._list_tools)
        except Exception as error:  # noqa: BLE001 - any failure to open is one line that names the server
            self.close()
            raise self._unreachable(_leaf(error)) from None
        self._offered = {tool.name for tool in self.tools}

    @classmethod
    def open(cls, source: SourceFile, root: Path, **options: Any) -> McpClient:
        """Open a session with the server a source file names. `options` are `reach`'s."""
        return cls(reach(source, root, **options))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _where(self) -> str:
        if self.reach.server is not None:
            return "the server"
        return f"the server at {self.reach.url}" if self.reach.url else f"the command {self.reach.command[0]}"

    def _refused(self, error: BaseException) -> McpError | None:
        """One line for a server that refused the login, from the SDK's error on a 401 or a 403, else None."""
        import httpx2

        status = error.response.status_code if isinstance(error, httpx2.HTTPStatusError) else 0
        if status not in (401, 403) and self._refusals:
            status = self._refusals[-1]
        if status in (401, 403):
            return McpError(f"{self.name}: {self._where()} refused the login ({status}): run "
                            f"cerebro connect {self.name} to log in again, then try again")
        return None

    def _unreachable(self, error: BaseException) -> CerebroError:
        if isinstance(error, CerebroError):
            return error
        if (refused := self._refused(error)) is not None:
            return refused
        fix = "check its command line" if self.reach.command else "check the address, and that the server runs"
        return McpError(f"{self.name}: {self._where()} did not open a session ({type(error).__name__}: "
                        f"{_line(error, 200)}): {fix}")

    async def _list_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            with _anyio().fail_after(self.reach.timeout):
                page = await self._client.list_tools(cursor=cursor)
            for tool in page.tools:
                hints = tool.annotations
                read_only = bool(hints is not None and hints.read_only_hint)
                tools.append(Tool(tool.name, tool.description or "", read_only,
                                  bool(hints is not None and hints.destructive_hint and not read_only)))
            cursor = page.next_cursor
            if not cursor:
                return tools
            if cursor in seen:
                raise McpError(f"{self.name}: the server repeated a page of its tools: ask its owner to fix it")
            seen.add(cursor)
        raise McpError(f"{self.name}: the server listed more than {MAX_PAGES} pages of tools: ask its owner to fix it")

    async def _call(self, tool: str, arguments: dict[str, Any]) -> tuple[Any, str, bool]:
        with _anyio().fail_after(self.reach.timeout):
            result = await self._client.call_tool(tool, arguments)
        text = "\n".join(block.text for block in result.content if getattr(block, "type", "") == "text"
                         and NOTE not in (getattr(block, "meta", None) or {}))  # a Cerebro server's note, not the reply
        return result.structured_content, text, result.is_error

    def reply(self, tool: str, arguments: Mapping[str, Any] | None = None) -> Reply:
        """One tool's reply, as data and as text."""
        if self._portal is None:
            raise McpError(f"{self.name}: the session is closed: open a new client")
        if tool not in self._offered:
            offered = ", ".join(sorted(self._offered)[:10]) or "none"
            raise ToolNotOffered(f"{self.name}: the server offers no tool {tool} (it offers {offered}): fix the "
                                 f"tool's name")
        self._refusals.clear()  # only what this call met counts
        from mcp.shared.exceptions import MCPError

        try:
            structured, text, refused = self._portal.call(self._call, tool, dict(arguments or {}))
        except TimeoutError:
            raise McpError(f"{self.name}: {tool} did not answer within {self.reach.timeout:g}s: try again, or ask "
                           f"less of it") from None
        except MCPError as error:
            raise McpError(f"{self.name}: {tool} failed: {_line(error.message, 200)}: check its arguments") from None
        except Exception as error:  # noqa: BLE001 - whatever the SDK raises becomes one line
            leaf = _leaf(error)
            if isinstance(leaf, CerebroError):
                raise leaf from None
            if (refused := self._refused(leaf)) is not None:
                raise refused from None
            raise McpError(f"{self.name}: {tool} failed ({type(leaf).__name__}: {_line(leaf, 200)}): check that "
                           f"the server still runs") from None
        if refused:
            raise ToolRefused(f"{self.name}: {tool} returned an error: {_line(text) or 'no text'}", tool, text)
        return Reply(_value(structured, text), text)

    def call(self, tool: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """One tool's reply as plain data."""
        return self.reply(tool, arguments).value

    def reader(self, *tools: str) -> Call:
        """A `call` that calls only `tools`, and refuses every other before it reaches the server."""
        allowed = frozenset(tools)

        def call(tool: str, arguments: Mapping[str, Any] | None = None) -> Any:
            if tool not in allowed:
                raise ToolNotOffered(f"{self.name}: {tool} is not a tool this source reads with "
                                     f"({', '.join(sorted(allowed))}): a source reads and never writes")
            return self.call(tool, arguments)
        return call

    def __del__(self) -> None:
        # A client dropped unclosed ends its session the orderly way: left to the portal, the session would be
        # cancelled, which can hang the thread that drops it.
        self.close()

    def close(self) -> None:
        """End the session, stop a server it started, and stop its event loop. Closing twice does nothing."""
        closers, self._closers = self._closers, []
        for closer in reversed(closers):
            try:
                closer.__exit__(None, None, None)
            except Exception:  # noqa: BLE001, S110 - a session that failed to open, or a server gone: nothing to end
                pass
        self._portal = None
