"""Any MCP server as a source, read only, through the tools its source file's `binding:` names.

A `kind: mcp` source file says where its server is, `url` or `command`, and binds two operations to its tools:
`list` gives the ids of the records that changed since a time, and `get` gives one record. Each names a `tool`,
its `args`, whose strings fill `{since}` and `{id}`, and JSONPaths into the reply: `list.id` for the ids, and
`get.revision`, `get.at`, `get.text`, `get.url` and each of `get.identity` and `get.fields`. A url, into, identity or field
that does not start with `$` is a template filled with `{id}`. `binding: contract` calls a server's own
`cerebro.list` and `cerebro.get`, whose replies carry `ids`, and `id`, `revision`, `url`, `at`, `fields`, `text`,
`into` and `identity`. The JSONPath subset is `$`, `.name`, `['name']`, `[n]` with a negative `n` from the end, and
`[*]` or `.*` for every element.

A record's id is what the list gives, its revision what `get.revision` reads, or a hash of the reply when the binding
names none, and its time what `get.at` reads, or none: a source that cannot say when a record happened gives no time.
Its body is what `get.text` reads, or the reply's whole text when the binding names no path.
Its keys are its identity, its extract the file's, else `records` when the record
lands in a collection and `facts` when not. A reply in which the revision path matches nothing holds no record, as a
query that found no row, and so does a reply the server marks as an error.

`{since}` is when the last listing began. A first listing, and the first after the binding changes, send the start
of the file's window, 90 days back by default or the epoch for `all`. A list whose args never name `{since}` reads
the whole source each time, so a `window:` line is refused, and so is a written `capabilities` line that claims
what the binding cannot do: `fetch_by_id` and `enumerate_history` come with every binding, and `incremental_since`
with a list that names `{since}`. A file that names `{since}` and leaves `incremental_since` out of its capabilities
is read whole each time, from the window's start. Only a listing that reads the whole window can tell a record is
gone, and then only one the get tool no longer finds is removed, never one that left the window: an incremental
listing lists none as removed. `select:` entries match a record's id by glob, a later entry first,
and override `extract` and `into`: with `select`, an id no entry matches is not read.

It calls only the binding's tools, and refuses one the server marks as destructive. It keeps no copy of a reply:
a listing gets each record to learn its revision and drops the body, and the cursor carries ids, short hashes of
revisions and the times to send.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import json
import os
import re
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from ..clock import system_clock
from ..connector import Connector, Listing, Record, RecordNotFound, gone, holds, one_line
from ..connector import CursorError as SharedCursorError
from ..errors import CerebroError
from ..secrets import SecretNotFound, env_key, login_name, mcp_token_names
from ..source import SourceError, SourceFile
from .mcp_source_client import Credentials, McpClient, Reply, ToolNotOffered, ToolRefused, reach

__all__ = ["CONTRACT_GET", "CONTRACT_LIST", "Binding", "CursorError", "JsonPath", "McpSource", "ReplyError",
           "read_binding"]

CONTRACT_LIST = "cerebro.list"
CONTRACT_GET = "cerebro.get"
CAPABILITIES = ("enumerate_history", "fetch_by_id", "incremental_since", "bulk_export")
SOURCE_KEYS = ("url", "command", "env", "auth", "extract", "window", "into", "select", "binding", "read_only",
               "capabilities", "freshness")
SELECT_KEYS = ("glob", "extract", "into")
PAGE_SIZE = 100
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CURSOR = "mcp1."
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_GET_KEYS = ("tool", "args", "revision", "at", "text", "url", "into", "identity", "fields")


class CursorError(SharedCursorError):
    """A cursor this connector did not give, or one that does not read."""


class ReplyError(CerebroError, ValueError):
    """A server's reply does not hold what the binding says it does."""


def _line(text: object, limit: int = 200) -> str:
    return one_line(text, limit)


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class JsonPath:
    """A JSONPath of the subset a binding may use, parsed. Each step is a member name, an index, or `*`."""

    text: str
    steps: tuple[str | int, ...]

    @classmethod
    def parse(cls, text: str) -> JsonPath:
        if not text.startswith("$"):
            raise ValueError(f"{text!r} must start with $")
        steps: list[str | int] = []
        rest = text[1:]
        while rest:
            if rest.startswith(".."):
                raise ValueError(f"{text!r}: recursive descent (..) is not supported")
            if rest.startswith(".*"):
                steps.append("*")
                rest = rest[2:]
            elif rest[0] == ".":
                name = re.match(r"[^.\[]*", rest[1:]).group(0)
                if not name:
                    raise ValueError(f"{text!r}: a member name is empty")
                steps.append(name)
                rest = rest[1 + len(name):]
            elif rest[0] == "[":
                quoted = re.match(r"\[(['\"])(.*?)\1\]", rest)
                if quoted:
                    steps.append(quoted.group(2))
                    rest = rest[quoted.end():]
                    continue
                end = rest.find("]")
                if end < 0:
                    raise ValueError(f"{text!r}: a [ is not closed")
                inner = rest[1:end].strip()
                if inner == "*":
                    steps.append("*")
                elif re.fullmatch(r"-?[0-9]+", inner):
                    steps.append(int(inner))
                else:
                    raise ValueError(f"{text!r}: [{inner}] is not an index, a quoted name or *: filters and slices "
                                     f"are not supported")
                rest = rest[end + 1:]
            else:
                raise ValueError(f"{text!r}: {rest!r} does not read")
        return cls(text, tuple(steps))

    def many(self) -> bool:
        return "*" in self.steps

    def eval(self, doc: Any) -> tuple[list[Any], bool]:
        """Every value the path matches, in order, and whether it resolved: a path that matched nothing only
        because a wildcard ran over an empty list or mapping resolves, and one with a member or index missing
        does not."""
        current = [doc]
        for step in self.steps:
            found: list[Any] = []
            empty = True
            for value in current:
                if step == "*" and isinstance(value, dict):
                    found += [value[key] for key in sorted(value)]
                    empty = empty and not value
                elif step == "*" and isinstance(value, list):
                    found += value
                    empty = empty and not value
                else:
                    empty = False
                    if isinstance(step, int) and not isinstance(step, bool) and isinstance(value, list):
                        if -len(value) <= step < len(value):
                            found.append(value[step])
                    elif isinstance(step, str) and isinstance(value, dict) and step in value:
                        found.append(value[step])
            if current and not found:
                return [], step == "*" and empty
            current = found
        return current, True

    def value(self, doc: Any) -> tuple[Any, bool]:
        """What a field gets: the one match, or every match for a path with a wildcard, and whether it matched."""
        got, resolved = self.eval(doc)
        if not resolved:
            return None, False
        if self.many():
            return got, True
        return (got[0], True) if got else (None, False)


@dataclass
class Binding:
    """A source file's `binding:`, read. Each value of `revision`, `at`, `url`, `identity` and `fields` is a
    `JsonPath` or a template."""

    contract: bool = False
    list_tool: str = ""
    list_args: dict[str, Any] = field(default_factory=dict)
    list_id: JsonPath | None = None
    get_tool: str = ""
    get_args: dict[str, Any] = field(default_factory=dict)
    revision: JsonPath | None = None
    at: JsonPath | None = None
    text: JsonPath | None = None
    url: JsonPath | str = ""
    into: str = ""
    identity: dict[str, JsonPath | str] = field(default_factory=dict)
    fields: dict[str, JsonPath | str] = field(default_factory=dict)
    vouched: frozenset[str] = frozenset()  # tools the file's read_only: list says only read

    def tools(self) -> tuple[str, ...]:
        return (CONTRACT_LIST, CONTRACT_GET) if self.contract else tuple(dict.fromkeys((self.list_tool,
                                                                                        self.get_tool)))

    def names_since(self) -> bool:
        return self.contract or "{since}" in json.dumps(self.list_args)


class _Refuse(Exception):
    """One problem with the binding, before the file's name is put in front of it."""


def _placeholders(value: Any, allowed: tuple[str, ...], key: str) -> None:
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            _placeholders(item, allowed, key)
        return
    for name in _PLACEHOLDER.findall(value if isinstance(value, str) else ""):
        if name not in allowed:
            raise _Refuse(f"{key}: {{{name}}} is not a template it fills: use "
                          f"{' or '.join('{' + a + '}' for a in allowed)}")


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _Refuse(f"{key}: write a mapping, as {key}: {{ name: value }}")
    return {str(k): v for k, v in value.items()}


def _path_or_template(value: Any, key: str, *, path_only: bool = False) -> JsonPath | str:
    text = _scalar(value)
    if not text.startswith("$"):
        if path_only:
            raise _Refuse(f"{key} {text}: write a JSONPath that starts with $, as $.updated_at")
        _placeholders(text, ("id",), key)
        return text
    try:
        return JsonPath.parse(text)
    except ValueError as error:
        raise _Refuse(f"{key}: JSONPath {error}") from None


def _binding(value: Any) -> Binding:
    if value == "contract":
        return Binding(contract=True)
    if value is None:
        raise _Refuse("has no binding: add binding: contract, or a binding with list and get")
    block = _mapping(value, "binding")
    for key in block:
        if key not in ("list", "get"):
            raise _Refuse(f"binding.{key} is not a key a binding reads: delete it, or write list and get")
    listing, get = _mapping(block.get("list"), "binding.list"), _mapping(block.get("get"), "binding.get")
    for key in listing:
        if key not in ("tool", "args", "id"):
            raise _Refuse(f"binding.list.{key} is not a key it reads: delete it (tool, args, id)")
    for key in get:
        if key not in _GET_KEYS:
            raise _Refuse(f"binding.get.{key} is not a key it reads: delete it ({', '.join(_GET_KEYS)})")
    if not listing.get("tool") or not listing.get("id"):
        raise _Refuse("binding.list needs tool and id, as { tool: list_rows, id: \"$.rows[*].id\" }")
    if not get.get("tool"):
        raise _Refuse("binding.get needs tool, as { tool: get_row, args: { id: \"{id}\" } }")
    found = Binding(list_tool=_scalar(listing["tool"]), get_tool=_scalar(get["tool"]),
                    list_args=_mapping(listing.get("args"), "binding.list.args"),
                    get_args=_mapping(get.get("args"), "binding.get.args"), into=_scalar(get.get("into")))
    _placeholders(found.list_args, ("since",), "binding.list.args")
    _placeholders(found.get_args, ("id",), "binding.get.args")
    _placeholders(found.into, ("id",), "binding.get.into")
    found.list_id = _path_or_template(listing["id"], "binding.list.id", path_only=True)
    for key in ("revision", "at", "text"):
        if get.get(key) is not None:
            setattr(found, key, _path_or_template(get[key], f"binding.get.{key}", path_only=True))
    if get.get("url") is not None:
        found.url = _path_or_template(get["url"], "binding.get.url")
    for key in ("identity", "fields"):
        setattr(found, key, {k: _path_or_template(v, f"binding.get.{key}.{k}")
                             for k, v in _mapping(get.get(key), f"binding.get.{key}").items()})
    return found


def _capabilities(source: SourceFile, binding: Binding) -> bool:
    """Whether a listing sends `{since}` from the last one, as the binding and the file's capabilities allow."""
    for word in source.capabilities:
        if word not in CAPABILITIES:
            raise _Refuse(f"capabilities {word}: write {', '.join(CAPABILITIES[:-1])} or {CAPABILITIES[-1]}")
        if word == "bulk_export":
            raise _Refuse("capabilities bulk_export is not read by an mcp source: delete it")
        if word == "incremental_since" and not binding.names_since():
            raise _Refuse("capabilities incremental_since: binding.list.args never name {since}: name it, such as "
                          "updated_at > '{since}', or delete incremental_since")
    if source.window.behind and not binding.names_since():
        raise _Refuse("window: binding.list.args never name {since}, so this source reads everything the server "
                      "lists: delete window:, or name {since} in binding.list.args, such as updated_at > '{since}'")
    return binding.names_since() and (not source.capabilities or "incremental_since" in source.capabilities)


def _vouched(source: SourceFile, binding: Binding) -> frozenset[str]:
    """The tools the file's `read_only:` list vouches for: the person says in writing that each only reads, though
    the server does not mark it so. Each must be a tool the binding calls."""
    raw = source.settings.get("read_only")
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not all(isinstance(t, str) and t for t in raw):
        raise _Refuse("read_only: write a list of the binding's tools that only read, as read_only: [execute_sql]")
    for tool in raw:
        if tool not in binding.tools():
            raise _Refuse(f"read_only names {tool}, which the binding does not call: name a tool of the binding")
    return frozenset(raw)


def read_binding(source: SourceFile) -> tuple[Binding, bool]:
    """The binding a `kind: mcp` source file holds, and whether its listings are incremental. Raises
    `SourceError` with one line that names the file, the key and the fix."""
    try:
        if source.kind != "mcp":
            raise _Refuse(f"is kind {source.kind}: an mcp connector reads kind mcp alone")
        binding = _binding(source.settings.get("binding"))
        binding.vouched = _vouched(source, binding)
        incremental = _capabilities(source, binding)
    except _Refuse as error:
        raise SourceError(f"sources/{source.name}.yaml {error}") from None
    return binding, incremental


def _fill(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _fill_url(template: str, values: dict[str, str]) -> str:
    """A url template filled, each value escaped for where it sits, unless it fills the whole template."""
    if _PLACEHOLDER.fullmatch(template):
        return _fill(template, values)
    query = template.find("?")

    def escape(match: re.Match[str]) -> str:
        value = values.get(match.group(1), match.group(0))
        return quote_plus(value) if 0 <= query < match.start() else quote(value, safe="")
    return _PLACEHOLDER.sub(escape, template)


def _args(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _fill(value, values)
    if isinstance(value, list):
        return [_args(v, values) for v in value]
    if isinstance(value, dict):
        return {k: _args(v, values) for k, v in value.items()}
    return value


def _when(text: str) -> str:
    """A time as an ISO stamp in its own zone, `Z` for UTC, or empty when it does not read."""
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        return ""
    when = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return when.isoformat().replace("+00:00", "Z")


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(revision: str) -> str:
    return hashlib.sha256(revision.encode()).hexdigest()[:16]


class McpSource(Connector):
    """A `kind: mcp` source. The session opens on the first listing or fetch, from `client`, a factory that gives
    an open `McpClient`, or from the source file with `credentials` and `secret`, and `close` ends it. `now` is
    the clock a window and `{since}` are read by."""

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> McpSource:
        """Any MCP server, through its file's binding. A token kept under `auth: { keychain: <item> }` goes to a
        server over http; each variable under `env:` comes from the environment Cerebro runs in, else the store."""
        credentials: Credentials | None = None
        own, token_name = mcp_token_names(source)
        if source.auth.startswith("keychain:"):
            item = login_name(source) or ""
            if item not in (own, token_name):
                raise SourceError(f"sources/{source.name}.yaml auth keychain: {item}: an mcp source reads only a "
                                  f"secret named for itself: keep its token with cerebro secrets set {token_name} and "
                                  f"write auth: {{ keychain: {token_name} }}")
            credentials = app.token(source)
        elif source.auth == "oauth":
            if not source.url:
                raise SourceError(f"sources/{source.name}.yaml auth oauth: a browser sign-in reaches a server over "
                                  f"http: add url")
            credentials = app.signed_in(source, source.url)
        elif source.auth:
            raise app.bad_auth(source, f"{own}-token")

        variables = {env_key(source.name, variable) for variable in source.env}

        def secret(name: str) -> str | None:
            if app.secrets is None or name not in variables:
                return None
            try:
                value = app.secrets.get(name)
            except SecretNotFound:
                return None
            app.mask.add(value)
            return value

        def open_client() -> McpClient:
            where = reach(source, root, credentials=credentials, secret=secret, environ=os.environ)
            app.mask.add(*(where.env[v] for v in source.env if v in where.env))
            return app.mcp_open(where)

        return cls(source, root, client=open_client, now=app.clock)

    def __init__(self, source: SourceFile, root: Path, *, client: Callable[[], McpClient] | None = None,
                 credentials: Credentials | None = None, secret: Callable[[str], str | None] | None = None,
                 now: Callable[[], datetime] | None = None, page_size: int = PAGE_SIZE):
        self.binding, self.incremental = read_binding(source)
        self.name = source.name
        self.source = source
        self.root = Path(root)
        self.page_size = max(1, page_size)
        self.now = now or system_clock
        self.notices: list[str] = []
        self._open = client or (lambda: McpClient(reach(source, self.root, credentials=credentials, secret=secret)))
        self._client: McpClient | None = None
        config = json.dumps([self.source.settings.get("binding"), self.source.extract, self.source.into,
                             asdict(self.source.window), self.source.capabilities,
                             [asdict(entry) for entry in self.source.select]], sort_keys=True, default=str)
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]

    # -- the session
    def client(self) -> McpClient:
        """The open session, opened now if it is not, with the binding's tools checked against the server's."""
        if self._client is None:
            client = self._open()
            tools = {tool.name: tool for tool in client.tools}
            for name in self.binding.tools():
                problem = ""
                if name not in tools:
                    offered = ", ".join(sorted(tools)[:10]) or "none"
                    problem = f"the server offers no tool {name} (it offers {offered}): fix the binding"
                elif tools[name].destructive:
                    problem = f"the server marks {name} as one that may destroy data: bind a tool that only reads"
                elif not tools[name].read_only and name not in self.binding.vouched:
                    problem = (f"the server does not mark {name} as read only, and a tool it does not mark may "
                               f"write: if it only reads, vouch for it as read_only: [{name}]")
                if problem:
                    client.close()
                    raise ToolNotOffered(f"sources/{self.name}.yaml binding: {problem}")
            self._client = client
        return self._client

    def close(self) -> None:
        """End the session. A later listing opens a new one."""
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def _reply(self, tool: str, arguments: dict[str, Any]) -> Reply:
        if tool not in self.binding.tools():
            raise ToolNotOffered(f"{self.name}: {tool} is not a tool this source reads with "
                                 f"({', '.join(self.binding.tools())}): a source reads and never writes")
        return self.client().reply(tool, arguments)

    # -- listing ids
    def _floor(self) -> str:
        behind = self.source.behind()
        return _stamp(EPOCH if behind is None else self.now() - behind)

    def _ids(self, since: str) -> list[str]:
        if self.binding.contract:
            tool = CONTRACT_LIST
            reply = self._reply(tool, {"since": since})
            if not isinstance(reply.value, dict) or not isinstance(reply.value.get("ids"), list):
                raise ReplyError(f"{self.name}: {tool} gave no ids list ({_line(reply.text)}): reply with "
                                 f"{{\"ids\": [...]}}")
            found = reply.value["ids"]
        else:
            tool, path = self.binding.list_tool, self.binding.list_id
            reply = self._reply(tool, _args(self.binding.list_args, {"since": since}))
            found, resolved = path.eval(reply.value)
            if not resolved:
                raise ReplyError(f"{self.name}: binding.list.id {path.text} matched nothing in the reply of {tool}, "
                                 f"so the listing stops here: {_line(reply.text)}")
        ids = [text for text in dict.fromkeys(_scalar(v) for v in found) if text]
        return [id for id in ids if self._entry(id) is not None]

    def _entry(self, id: str) -> tuple[str, str] | None:
        """The extract and collection a record goes by, or None when `select` reads no record by that id."""
        extract, into = self.source.extract, self.source.into
        if not self.source.select:
            return extract, into
        for entry in reversed(self.source.select):
            if fnmatch.fnmatchcase(id, entry.glob):
                return entry.extract or extract, entry.into or into
        return None

    # -- getting one record
    def _text(self, path: JsonPath, doc: Any, id: str, key: str) -> str | None:
        value, matched = path.value(doc)
        if not matched:
            self.notices.append(f"{id}: binding.get.{key} {path.text} matched nothing in the reply, so it is left out")
            return None
        if isinstance(value, list):
            value = value[0] if value else ""
        return _scalar(value)

    def _from_binding(self, id: str, reply: Reply) -> Record:
        binding, doc, values = self.binding, reply.value, {"id": id}
        revision = ""
        if binding.revision is not None:
            got, matched = binding.revision.value(doc)
            if not matched:
                raise RecordNotFound(f"{self.name} has no record {id}: the reply of {binding.get_tool} holds no "
                                     f"{binding.revision.text}")
            revision = _scalar(got[0] if isinstance(got, list) and got else got)
        record = Record(id=id, source=self.name, revision=revision, body=reply.text,
                        into=_fill(binding.into, values))
        if binding.text is not None:
            record.body = self._text(binding.text, doc, id, "text") or ""
        if binding.at is not None:
            record.at = _when(self._text(binding.at, doc, id, "at") or "")
        if isinstance(binding.url, JsonPath):
            record.url = self._text(binding.url, doc, id, "url") or ""
        else:
            record.url = _fill_url(binding.url, values)
        for key, given in binding.identity.items():
            value = self._text(given, doc, id, f"identity.{key}") if isinstance(given, JsonPath) \
                else _fill(given, values)
            if value:
                record.keys[key] = [value]
        for key, given in binding.fields.items():
            if isinstance(given, JsonPath):
                value, matched = given.value(doc)
                if not matched:
                    self.notices.append(f"{id}: binding.get.fields.{key} {given.text} matched nothing in the "
                                        f"reply, so it is left out")
                    continue
                record.fields[key] = value
            else:
                record.fields[key] = _fill(given, values)
        return record

    def _from_contract(self, id: str, reply: Reply) -> Record:
        given = reply.value
        if not isinstance(given, dict):
            raise ReplyError(f"{self.name}: {CONTRACT_GET} gave no record for {id} ({_line(reply.text)}): reply with "
                             f"a JSON object of id, revision, url, fields and text")
        identity = given.get("identity") if isinstance(given.get("identity"), dict) else {}
        fields = given.get("fields") if isinstance(given.get("fields"), dict) else {}
        return Record(id=_scalar(given.get("id")) or id, source=self.name, revision=_scalar(given.get("revision")),
                      at=_when(_scalar(given.get("at"))), url=_scalar(given.get("url")),
                      keys={k: [_scalar(v)] for k, v in identity.items() if _scalar(v)}, fields=dict(fields),
                      body=_scalar(given.get("text")), into=_scalar(given.get("into")),
                      extract=_scalar(given.get("extract")))

    def _get(self, id: str) -> Record:
        entry = self._entry(id)
        if entry is None or not id:
            raise RecordNotFound(f"{self.name} has no record {id!r}: list the source for the ids it holds")
        tool = CONTRACT_GET if self.binding.contract else self.binding.get_tool
        arguments = {"id": id} if self.binding.contract else _args(self.binding.get_args, {"id": id})
        try:
            reply = self._reply(tool, arguments)
        except ToolRefused as error:
            raise RecordNotFound(f"{self.name} has no record {id}: {tool} refused it ({_line(error.text, 120)})") \
                from None
        record = self._from_contract(id, reply) if self.binding.contract else self._from_binding(id, reply)
        if not record.revision:
            record.revision = "sha256:" + hashlib.sha256(reply.text.encode()).hexdigest()
        extract, into = entry
        record.into = into or record.into
        record.extract = extract or record.extract or ("records" if record.into else "facts")
        return record

    # -- the cursor
    def _cursor(self, state: dict[str, Any]) -> str:
        text = json.dumps({"config": self._config, **state}, sort_keys=True, separators=(",", ":"))
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(text.encode())).decode()

    def _state(self, cursor: str | None) -> tuple[dict[str, Any], bool]:
        """What a cursor carries, and whether the source file read the same when it was given."""
        if cursor is None:
            return {"seen": {}, "since": None, "pass": None}, True
        try:
            if not cursor.startswith(_CURSOR):
                raise ValueError
            state = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
            seen = state.get("seen")
        except (ValueError, TypeError, AttributeError, zlib.error, binascii.Error):
            seen = None
        if not isinstance(seen, dict):
            raise CursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with no "
                              f"cursor to start over")
        return state, state.get("config") == self._config

    # -- the contract
    def list(self, since: str | None = None) -> Listing:
        self.notices = []
        state, same = self._state(since)
        seen: dict[str, str] = state["seen"]
        current = state.get("pass") if same else None
        if current is None:
            self.close()  # each pass opens its own session
            begin = state.get("since") if same and self.incremental else None
            current = {"since": begin or self._floor(), "began": _stamp(self.now()), "whole": begin is None}
        ids = self._ids(current["since"])
        listed = set(ids)
        absent = [id for id in seen if id not in listed] if current["whole"] else []
        removed = gone(absent, holds(self._get))  # one that only left the window still exists, and is forgotten
        seen = {id: rev for id, rev in seen.items() if id not in absent} if same else {}
        records: list[Record] = []
        more = False
        for index, id in enumerate(ids):
            try:
                record = self._get(id)
            except RecordNotFound:
                self.notices.append(f"{id}: listed, then gone before it was fetched")
                continue
            if seen.get(id) == _short(record.revision):
                continue
            seen[id] = _short(record.revision)
            record.body = None  # a listing carries no body, and the text is not kept
            records.append(record)
            if len(records) == self.page_size:
                more = index < len(ids) - 1
                break
        after = {"since": current["began"] if self.incremental else None, "pass": None}
        if more:
            after = {"since": state.get("since") if same else None, "pass": current}
        return Listing(records, self._cursor({"seen": seen, **after}), removed, more, list(self.notices))

    def get(self, id: str) -> Record:
        return self._get(id)
