"""The JSON API: the wiki's six operations over HTTP and JSON, for a browser app or a plain script.

`POST /api/v1/<operation>` takes a JSON object of the arguments the MCP tool of that name takes, and answers with the
structured result that tool gives. `GET /api/v1` lists the operations, each with its input schema, read from the MCP
server's own tool definitions on each request, so the two cannot drift. A route calls the MCP tool itself, through
the operations `mcp` hands it, so an operation runs one way whichever surface asks: the same argument checks, the same
caller, and a row of the same `mcp-requests` log.

It sits beside MCP on the same server and port, behind `tokens.Gate`, which checks the Host, the token and the size
of the body, in that order, before a route is reached. Every call needs a token, on loopback too, and the caller is
the one the token names. The API reads no cookie and sets none. A page in a browser may call it only from an origin
listed with `serve --allowed-origin`.

Every refusal is `{"error": {"code": ..., "message": ...}}` with one of `CODES` and its status. An operation's own
refusal keeps its one line, as over MCP. A failure Cerebro did not expect answers 500 with a fixed line, so no
stack trace, file path or internal name reaches a reply. Every token in a reply is masked.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from .caller import Caller
from .errors import CerebroError
from .tokens import API, CALLER_KEY, App, Receive, Scope, Send, error_reply, is_api, masked

__all__ = ["CODES", "ROOT", "Api", "ArgumentsError", "JsonApiError", "OperationRefused", "Operations", "beside"]

ROOT = API + "/v1"
CODES = {  # every code a refusal on the JSON API's routes carries, gate and routes alike, and its status
    "bad_request": 400, "token_in_url": 400, "refused": 400, "unauthorized": 401, "origin_refused": 403,
    "forbidden": 403, "not_found": 404, "method_not_allowed": 405, "too_slow": 408, "too_large": 413,
    "host_refused": 421, "internal": 500}
INTERNAL = "Cerebro could not answer this request: try it again, and if it fails again, read the serve output"
JSON = b"application/json"

logger = logging.getLogger(__name__)


class JsonApiError(CerebroError):
    """A request the JSON API refuses, with the code and status it answers with."""

    code = "bad_request"


class ArgumentsError(JsonApiError, ValueError):
    """An operation's arguments do not match its input schema."""


class OperationRefused(JsonApiError):
    """The operation refused the call, in its own one line, as it would over MCP."""

    code = "refused"


class Operations(Protocol):
    """What the API needs of the MCP server: the operations' names, their tool definitions, and a call of one."""

    names: Sequence[str]

    async def definitions(self) -> list[dict[str, Any]]: ...

    async def call(self, name: str, arguments: dict[str, Any], scope: Scope) -> Any: ...


async def _reply(send: Send, status: int, data: Any) -> None:
    body = json.dumps(masked(data), ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", JSON), (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


async def _refuse(send: Send, code: str, message: str, headers: Sequence[tuple[bytes, bytes]] = ()) -> None:
    await error_reply(send, CODES[code], code, message, headers)


async def _body(receive: Receive) -> bytes:
    """The request's body, which the gate has read whole, within its cap, and hands on as one message."""
    message = await receive()
    return message.get("body", b"") if message["type"] == "http.request" else b""


class Api:
    """The JSON API's routes as an ASGI app, over the MCP server's `operations`. It runs behind `tokens.Gate`."""

    def __init__(self, operations: Operations):
        self.operations = operations

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not isinstance(scope.get(CALLER_KEY), Caller):  # the gate names a caller for every request it lets by
            await _refuse(send, "unauthorized", "this API needs a token: send Authorization: Bearer <token>, made "
                                                "with cerebro tokens new")
            return
        path, method = scope["path"].rstrip("/"), scope["method"]
        if path == ROOT:
            if method != "GET":
                await _refuse(send, "method_not_allowed", f"{ROOT} answers GET: POST to {ROOT}/<operation> to call "
                                                          "one", [(b"allow", b"GET")])
                return
            await self._answer(send, "the listing", self._listed)
            return
        name = path.removeprefix(ROOT + "/") if path.startswith(ROOT + "/") else ""
        if name not in self.operations.names:
            await _refuse(send, "not_found", f"there is no route at this path: GET {ROOT} lists the operations, each "
                                             f"called as POST {ROOT}/<operation>")
            return
        if method != "POST":
            await _refuse(send, "method_not_allowed", f"{ROOT}/{name} answers POST, with its arguments as a JSON "
                                                      "object", [(b"allow", b"POST")])
            return
        kind = dict(scope.get("headers", [])).get(b"content-type", b"").split(b";")[0].strip().lower()
        if kind != JSON:
            await _refuse(send, "bad_request", "send the body as JSON, with Content-Type: application/json")
            return
        try:
            arguments = json.loads(await _body(receive) or b"null")
        except (ValueError, RecursionError):
            arguments = None
        if not isinstance(arguments, dict):
            await _refuse(send, "bad_request", f"the body of {name} is not a JSON object: send its arguments as one, "
                                               f"such as {{}}, as GET {ROOT} lists them")
            return
        await self._answer(send, name, lambda: self.operations.call(name, arguments, scope))

    async def _answer(self, send: Send, what: str, get: Callable[[], Awaitable[Any]]) -> None:
        """The data `get` gives, or its refusal in its own line, or 500 with a fixed line for anything else."""
        try:
            result = await get()
        except JsonApiError as error:
            await _refuse(send, error.code, str(error))
            return
        except Exception:
            logger.exception("the JSON API's %s failed", what)  # the trace stays in the server's own output
            await _refuse(send, "internal", INTERNAL)
            return
        await _reply(send, 200, result)

    async def _listed(self) -> dict[str, Any]:
        """Each operation as `GET /api/v1` gives it, its schema read from its MCP tool definition just now."""
        tools = {tool["name"]: tool for tool in await self.operations.definitions()}
        return {"operations": [{"name": name, "method": "POST", "path": f"{ROOT}/{name}",
                 "description": tools[name].get("description", ""), "input_schema": tools[name]["inputSchema"]}
                for name in self.operations.names if name in tools]}


def beside(other: App, api: App) -> App:
    """One ASGI app that sends a request under the JSON API's path to `api`, and everything else, lifespan included,
    to `other`."""

    async def routed(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_api(scope["path"]):
            await api(scope, receive, send)
        else:
            await other(scope, receive, send)

    return routed
