"""The MCP source: the `Connector` suite over a small SQL server and a contract server, both served in this process,
then the client other connectors are handed, the binding's rules, and that it reads and never writes."""

from __future__ import annotations

import functools
import hashlib
import json
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
import anyio.to_thread
import httpx2
import pytest
from anyio.from_thread import start_blocking_portal
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from suites.connector import *
from suites.connector import every_page

from cerebro.connector import Record, RecordNotFound
from cerebro.connectors.mcp_source import CursorError, JsonPath, McpSource, ReplyError
from cerebro.connectors.mcp_source_client import McpClient, McpError, Reach, ToolNotOffered, ToolRefused, reach
from cerebro.source import Select, SourceError, SourceFile, Window, read_source

SOURCES = Path(__file__).parent / "data" / "mcp_source" / "sources"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
ROWS = [
    {"id": "acct-1", "revision": "r1", "at": "2026-11-01T09:00:00Z", "email": "ada@acme.example", "title": "Acme",
     "notes": "Acme renewed for two years."},
    {"id": "acct-2", "revision": "r1", "title": "Globex", "notes": "Globex asked for a demo."},
    {"id": "acct 3/x", "revision": "7", "at": "2026-11-10T15:30:00+01:00", "notes": "A row whose id needs escaping."},
]


def stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


class Warehouse:
    """A small database behind an MCP server: a SQL tool, the contract's two tools, and two that write, which a
    source must never call. Each row keeps when it was last updated, by a clock that ticks a second a reading. One
    server, built once, serves the warehouse made last, since building a server's tools costs more than a test."""

    def __init__(self) -> None:
        self.time = NOW
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ghosts: list[str] = []  # ids a listing gives that a fetch no longer finds
        for row in ROWS:
            self.put(dict(row))
        SERVED[:] = [self]
        self.server = served()

    def now(self) -> datetime:
        self.time += timedelta(seconds=1)
        return self.time

    def put(self, row: dict[str, Any]) -> None:
        self.rows[row["id"]] = {**row, "updated": stamp(self.now())}

    def reply(self, value: Any) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=as_text(value))], structured_content=value)


SERVED: list[Warehouse] = []


@functools.cache
def served() -> MCPServer:
    """The one server, over whichever warehouse `SERVED` holds."""
    server = MCPServer("warehouse")

    @server.tool()
    def execute_sql(query: str) -> CallToolResult:
        self = SERVED[0]
        self.calls.append(("execute_sql", {"query": query}))
        if match := re.fullmatch(r"select \* from rows where id = '(.*)'", query):
            return self.reply({"rows": [self.rows[match.group(1)]] if match.group(1) in self.rows else []})
        since = re.fullmatch(r"select id from rows(?: where updated >= '(.*)')?", query)
        if since is None:
            raise ToolError(f"syntax error in {query}")
        return self.reply({"rows": [{"id": id} for id in self.ghosts] + [
            {"id": id} for id, row in sorted(self.rows.items())
            if since.group(1) is None or row["updated"] >= since.group(1)]})

    @server.tool(name="cerebro.list", annotations=ToolAnnotations(read_only_hint=True))
    def contract_list(since: str) -> CallToolResult:
        self = SERVED[0]
        self.calls.append(("cerebro.list", {"since": since}))
        if since == "broken":
            return self.reply({"results": None})
        return self.reply({"ids": [id for id, row in sorted(self.rows.items()) if row["updated"] >= since]})

    @server.tool(name="cerebro.get", annotations=ToolAnnotations(read_only_hint=True))
    def contract_get(id: str) -> CallToolResult:
        self = SERVED[0]
        self.calls.append(("cerebro.get", {"id": id}))
        if id not in self.rows:
            raise ToolError(f"no note {id}")
        row = self.rows[id]
        given = {"id": id, "revision": row["revision"], "at": row.get("at", ""), "text": row["notes"],
                 "url": f"https://notes.example/{id}", "fields": {"title": row["title"]} if "title" in row else {},
                 "identity": {"email": row["email"]} if "email" in row else {}}
        return self.reply(given)

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def delete_row(id: str) -> str:
        self = SERVED[0]
        self.calls.append(("delete_row", {"id": id}))
        self.rows.pop(id, None)
        return "deleted"

    @server.tool()
    def update_row(id: str, notes: str) -> str:
        SERVED[0].calls.append(("update_row", {"id": id}))
        return "updated"

    return server


def expected(style: str, row: dict[str, Any]) -> Record:
    keys = {"email": [row["email"]]} if "email" in row else {}
    fields = {"title": row["title"]} if "title" in row else {}
    at = row.get("at", "").replace("+00:00", "Z")
    if style == "warehouse":
        return Record(id=row["id"], source="warehouse", revision=row["revision"], at=at, keys=keys, fields=fields,
                      url=f"https://warehouse.example/rows/{quote(row['id'], safe='')}", body=row["notes"],
                      extract="records", into="accounts")
    return Record(id=row["id"], source="notes", revision=row["revision"], at=at, keys=keys, fields=fields,
                  url=f"https://notes.example/{row['id']}", body=row["notes"], extract="facts")


def source_file(name: str, **changes: Any) -> SourceFile:
    return replace(read_source(SOURCES / f"{name}.yaml"), **changes)


def make(warehouse: Warehouse, source: SourceFile, **options: Any) -> McpSource:
    return McpSource(source, Path("."), client=lambda: McpClient(Reach(source.name, server=warehouse.server)),
                     now=warehouse.now, **options)


@pytest.fixture
def warehouse() -> Warehouse:
    return Warehouse()


@pytest.fixture(params=["warehouse", "notes"])
def style(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def held(style: str) -> list[Record]:
    return [expected(style, row) for row in ROWS]


@pytest.fixture
def connector(warehouse: Warehouse, style: str) -> Iterator[McpSource]:
    connector = make(warehouse, source_file(style), page_size=2)
    yield connector
    connector.close()


@pytest.fixture
def add(warehouse: Warehouse) -> Callable[[Record], str]:
    def add(record: Record) -> str:
        row = {"id": record.id, "revision": record.revision, "at": record.at, "notes": record.body or ""}
        if record.keys.get("email"):
            row["email"] = record.keys["email"][0]
        row.update({k: v for k, v in record.fields.items() if k == "title"})
        warehouse.put({k: v for k, v in row.items() if v})
        return record.id
    return add


@pytest.fixture
def remove(warehouse: Warehouse) -> Callable[[str], None]:
    return lambda id: warehouse.rows.pop(id) and None


def test_an_incremental_listing_sends_when_the_last_one_began_and_lists_only_what_changed(warehouse: Warehouse) -> None:
    connector = make(warehouse, source_file("changes"))
    first = every_page(connector)
    assert sorted(r.id for r in first.records) == sorted(row["id"] for row in ROWS)
    warehouse.put({**ROWS[1], "revision": "r2"})
    warehouse.rows.pop("acct-1")
    later = every_page(connector, first.cursor)
    assert [(r.id, r.revision, r.at) for r in later.records] == [("acct-2", "r2", "")]
    assert later.removed == []  # only a listing that reads the whole window can tell a record is gone
    sent = [re.search("'(.*)'", q).group(1) for tool, args in warehouse.calls if "updated" in (q := args["query"])]
    assert stamp(NOW - timedelta(days=30)) < sent[0] < stamp(NOW - timedelta(days=29))  # the window's start
    assert stamp(NOW) < sent[1] < warehouse.rows["acct-2"]["updated"]  # when the first listing began
    assert later.records[0].body is None and later.records[0].extract == "facts"
    assert connector.get("acct-2").body == as_text({"rows": [warehouse.rows["acct-2"]]})


def test_the_source_calls_only_the_tools_its_binding_names(warehouse: Warehouse, style: str) -> None:
    connector = make(warehouse, source_file(style), page_size=1)
    cursor = every_page(connector).cursor
    warehouse.rows.pop("acct-2")
    every_page(connector, cursor)
    for id in ("acct-1", "acct-2", "x'; delete from rows; --"):
        try:
            connector.get(id)
        except RecordNotFound:
            pass
    with pytest.raises(ToolNotOffered, match="a source reads and never writes"):
        connector._reply("delete_row", {"id": "acct-1"})
    reader = connector.client().reader("execute_sql")
    with pytest.raises(ToolNotOffered, match="update_row is not a tool this source reads with"):
        reader("update_row", {"id": "acct-1", "notes": "x"})
    assert {tool for tool, _ in warehouse.calls} == set(connector.binding.tools())
    assert "acct-1" in warehouse.rows


def test_a_binding_tool_the_server_does_not_offer_or_marks_destructive_is_refused(warehouse: Warehouse) -> None:
    base = source_file("warehouse")
    offered = "offers no tool execute_sqll (it offers cerebro.get, cerebro.list, delete_row"
    for tool, problem in (("execute_sqll", offered),
                          ("delete_row", "marks delete_row as one that may destroy data")):
        binding = {**base.settings["binding"], "get": {**base.settings["binding"]["get"], "tool": tool}}
        connector = make(warehouse, replace(base, settings={**base.settings, "binding": binding}))
        with pytest.raises(ToolNotOffered, match=re.escape(f"sources/warehouse.yaml binding: the server {problem}")):
            connector.list()
    assert warehouse.calls == []


def test_a_tool_the_server_does_not_mark_read_only_is_refused_unless_the_file_vouches_for_it(
        warehouse: Warehouse) -> None:
    base = source_file("warehouse")
    unvouched = make(warehouse, replace(base, settings={"binding": base.settings["binding"]}))
    with pytest.raises(ToolNotOffered, match=re.escape("the server does not mark execute_sql as read only, and a tool "
                                                       "it does not mark may write: if it only reads, vouch for it as "
                                                       "read_only: [execute_sql]")):
        unvouched.list()
    assert warehouse.calls == [] and make(warehouse, base).list().records
    with pytest.raises(SourceError, match="read_only names update_row, which the binding does not call"):
        McpSource(replace(base, settings={**base.settings, "read_only": ["update_row"]}), Path("."))


@pytest.mark.parametrize(("change", "line"), [
    ({"window": Window(behind="30d")},
     "window: binding.list.args never name {since}, so this source reads everything the server lists"),
    ({"capabilities": ["incremental_since"]}, "capabilities incremental_since: binding.list.args never name {since}"),
    ({"capabilities": ["bulk_export"]}, "capabilities bulk_export is not read by an mcp source: delete it"),
    ({"capabilities": ["fetch_everything"]}, "capabilities fetch_everything: write enumerate_history, fetch_by_id"),
    ({"settings": {}}, "has no binding: add binding: contract, or a binding with list and get"),
    ({"settings": {"binding": {"list_one": {}, "get": {}}}}, "binding.list_one is not a key a binding reads"),
    ({"settings": {"binding": {"list": {"tool": "t", "id": "rows"}, "get": {"tool": "t"}}}},
     "binding.list.id rows: write a JSONPath that starts with $"),
    ({"settings": {"binding": {"list": {"tool": "t", "id": "$..id"}, "get": {"tool": "t"}}}},
     "recursive descent (..) is not supported"),
    ({"settings": {"binding": {"list": {"tool": "t", "id": "$.ids", "args": {"q": "{when}"}}, "get": {"tool": "t"}}}},
     "binding.list.args: {when} is not a template it fills: use {since}"),
    ({"settings": {"binding": {"list": {"tool": "t", "id": "$.ids"}}}}, "binding.get needs tool"),
    ({"kind": "files"}, "is kind files: an mcp connector reads kind mcp alone"),
])
def test_a_source_file_that_asks_what_its_binding_cannot_do_is_refused_with_one_line(change: dict[str, Any],
                                                                                     line: str) -> None:
    with pytest.raises(SourceError) as refused:
        McpSource(source_file("warehouse", **change), Path("."))
    assert str(refused.value).startswith("sources/warehouse.yaml ") and line in str(refused.value)
    assert "\n" not in str(refused.value)


def test_select_reads_the_ids_its_globs_match_and_a_later_entry_sets_extract_and_into(warehouse: Warehouse) -> None:
    source = source_file("warehouse", select=[Select(glob="acct-*", into="customers"),
                                              Select(glob="acct-2", extract="facts")])
    connector = make(warehouse, source)
    listed = {r.id: (r.extract, r.into) for r in every_page(connector).records}
    assert listed == {"acct-1": ("records", "customers"), "acct-2": ("facts", "accounts")}
    with pytest.raises(RecordNotFound):
        connector.get("acct 3/x")


def test_a_list_path_that_matches_nothing_stops_the_listing_and_an_empty_list_is_no_records(
        warehouse: Warehouse) -> None:
    binding = source_file("warehouse").settings["binding"]
    connector = make(warehouse, source_file("warehouse", settings={"read_only": ["execute_sql"], "binding": {
        **binding, "list": {**binding["list"], "id": "$.results[*].id"}}}))
    with pytest.raises(ReplyError, match=r"binding.list.id \$.results\[\*\].id matched nothing in the reply of "
                                         r"execute_sql, so the listing stops here: \{\"rows\""):
        connector.list()
    warehouse.rows.clear()
    assert make(warehouse, source_file("warehouse")).list().records == []
    assert JsonPath.parse("$['a.b'][-1].*").eval({"a.b": [{"z": 2, "y": 1}]}) == ([1, 2], True)
    assert JsonPath.parse("$.a[3]").value({"a": [1]}) == (None, False)
    with pytest.raises(CursorError, match="list with no cursor to start over"):
        connector.list(since="files1.abc")


def test_a_changed_binding_lists_every_record_anew(warehouse: Warehouse) -> None:
    cursor = every_page(make(warehouse, source_file("warehouse"))).cursor
    again = every_page(make(warehouse, source_file("warehouse", extract="facts")), cursor)
    assert sorted(r.id for r in again.records) == sorted(row["id"] for row in ROWS)
    assert {r.extract for r in again.records} == {"facts"}


def test_the_connector_keeps_no_copy_of_a_records_text(warehouse: Warehouse, style: str) -> None:
    connector = make(warehouse, source_file(style))
    listing = every_page(connector)
    for record in listing.records:
        connector.get(record.id)
    kept = repr(vars(connector)) + str(listing.cursor)
    assert not any(word in kept for word in ("renewed", "demo", "escaping"))


def test_the_client_gives_plain_data_and_turns_a_refusal_into_one_line(warehouse: Warehouse) -> None:
    with McpClient(Reach("warehouse", server=warehouse.server)) as client:
        assert client.call("execute_sql", {"query": "select id from rows"}) == {
            "rows": [{"id": "acct 3/x"}, {"id": "acct-1"}, {"id": "acct-2"}]}
        assert client.call("update_row", {"id": "acct-1", "notes": "x"}) == {"result": "updated"}
        with pytest.raises(ToolRefused) as refused:
            client.call("execute_sql", {"query": "drop table rows"})
        assert str(refused.value) == ("warehouse: execute_sql returned an error: Error executing tool execute_sql: "
                                      "syntax error in drop table rows")
        with pytest.raises(ToolNotOffered, match="warehouse: the server offers no tool sql "):
            client.call("sql", {})
        assert {t.name: t.destructive for t in client.tools}["delete_row"] is True
    with pytest.raises(McpError, match="warehouse: the session is closed: open a new client"):
        client.call("execute_sql", {"query": "select id from rows"})
    client.close()


class Served(httpx2.AsyncBaseTransport):
    """An MCP server's HTTP app, served on an event loop of its own: a network that never leaves the process."""

    def __init__(self, server: MCPServer):
        self._loop = start_blocking_portal()
        self.portal = self._loop.__enter__()
        self.app = server.streamable_http_app(json_response=True, stateless_http=True)
        self._run = self.portal.wrap_async_context_manager(server.session_manager.run())
        self._run.__enter__()

    async def _send(self, request: httpx2.Request) -> httpx2.Response:
        response = await httpx2.ASGITransport(app=self.app).handle_async_request(request)
        return httpx2.Response(response.status_code, headers=response.headers, content=await response.aread())

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        return await anyio.to_thread.run_sync(lambda: self.portal.call(self._send, request))

    def stop(self) -> None:
        self._run.__exit__(None, None, None)
        self._loop.__exit__(None, None, None)


def test_credentials_are_asked_on_every_http_request_and_sent_as_headers() -> None:
    server, seen, given = MCPServer("crm"), [], iter(["t1", "t2", "t3", "t4", "t5", "t6"])

    @server.tool()
    def whoami(ctx: Context) -> dict[str, str]:
        seen.append(ctx.request_context.request.headers.get("authorization"))
        return {"me": "ada"}

    served = Served(server)
    try:
        source = SourceFile(name="crm", kind="mcp", url="http://localhost:8000/mcp", auth="oauth")
        where = replace(reach(source, Path("."), credentials=lambda: next(given)), transport=served)
        with McpClient(where) as client:
            assert client.call("whoami") == {"me": "ada"} and client.call("whoami") == {"me": "ada"}
        assert len(seen) == 2 and seen[0] != seen[1] and all(s.startswith("Bearer t") for s in seen)
        where = replace(where, credentials=lambda: {"Authorization": "Token k1"})
        with McpClient(where) as client:
            client.call("whoami")
        assert seen[-1] == "Token k1"
    finally:
        served.stop()
    with pytest.raises(McpError, match="crm: auth oauth needs a login: run cerebro connect crm"):
        reach(source, Path("."))


def test_a_401_renews_credentials_that_can_renew_once_and_a_refused_login_is_one_line() -> None:
    server, seen = MCPServer("crm"), []

    @server.tool()
    def whoami(ctx: Context) -> dict[str, str]:
        seen.append(ctx.request_context.request.headers.get("authorization"))
        return {"me": "ada"}

    class Refusing(httpx2.AsyncBaseTransport):
        """Answers 401 to any token but the renewed one, as a server whose token expired does."""

        def __init__(self, served: Served):
            self.served = served

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            if request.headers.get("authorization") != "Bearer renewed":
                return httpx2.Response(401, request=request)
            return await self.served.handle_async_request(request)

    class Token:
        def __init__(self) -> None:
            self.value, self.renewed = "expired", 0

        def __call__(self) -> str:
            return self.value

        def refresh(self) -> None:
            self.renewed += 1
            self.value = "renewed"

    served = Served(server)
    try:
        source = SourceFile(name="crm", kind="mcp", url="http://localhost:8000/mcp", auth="oauth")
        token = Token()
        where = replace(reach(source, Path("."), credentials=token), transport=Refusing(served))
        with McpClient(where) as client:
            assert client.call("whoami") == {"me": "ada"}
        assert token.renewed == 1 and seen == ["Bearer renewed"]
        stale = replace(where, credentials=lambda: "expired")  # nothing to renew: the 401 stands
        with pytest.raises(McpError) as refused:
            McpClient(stale)
        assert str(refused.value) == ("crm: the server at http://localhost:8000/mcp refused the login (401): run "
                                      "cerebro connect crm to log in again, then try again")
    finally:
        served.stop()


def test_a_server_that_does_not_answer_is_one_line_that_names_it() -> None:
    source = SourceFile(name="crm", kind="mcp", url="http://localhost:9/mcp")
    with pytest.raises(McpError) as refused:
        McpClient.open(source, Path("."))
    assert str(refused.value).startswith("crm: the server at http://localhost:9/mcp did not open a session (")
    assert str(refused.value).endswith("): check the address, and that the server runs")


def test_a_command_gets_the_variables_its_file_names_and_no_other(tmp_path: Path) -> None:
    source = source_file("notes")
    environ = {"PATH": "/bin", "HOME": "/home/ada", "AWS_SECRET_ACCESS_KEY": "no", "LANG": "C"}
    kept = {"notes-NOTES_TOKEN": "from-the-store"}
    where = reach(source, tmp_path, environ=environ, secret=kept.get)
    assert where.command == ["notes-server", "--stdio"] and where.cwd == tmp_path
    assert where.env == {"PATH": "/bin", "HOME": "/home/ada", "LANG": "C", "NOTES_TOKEN": "from-the-store"}
    assert reach(source, tmp_path, environ={"NOTES_TOKEN": "set"}).env == {"NOTES_TOKEN": "set"}
    with pytest.raises(McpError, match="notes: env NOTES_TOKEN has no value: set NOTES_TOKEN in the environment "
                                       "cerebro runs in, or keep it in the secrets store as notes-NOTES_TOKEN"):
        reach(source, tmp_path, environ={})
    with pytest.raises(McpError, match="credentials reach a server over http alone"):
        reach(source, tmp_path, environ={"NOTES_TOKEN": "x"}, credentials=lambda: "t")


def test_a_command_that_does_not_start_is_one_line_that_names_it(tmp_path: Path) -> None:
    source = SourceFile(name="notes", kind="mcp", command="no-such-notes-server --stdio")
    with pytest.raises(McpError) as refused:
        McpClient.open(source, tmp_path, environ={})
    assert str(refused.value).startswith("notes: the command no-such-notes-server did not open a session (")
    assert str(refused.value).endswith("): check its command line") and "\n" not in str(refused.value)


def test_a_pass_cut_into_pages_keeps_its_since_and_the_next_pass_starts_from_when_it_began(warehouse: Warehouse) -> None:
    connector = make(warehouse, source_file("changes"), page_size=1)
    pages = [connector.list()]
    while pages[-1].more:
        pages.append(connector.list(since=pages[-1].cursor))
    assert [len(p.records) for p in pages] == [1, 1, 1]
    sent = [re.search("'(.*)'", q).group(1) for tool, args in warehouse.calls if "updated" in (q := args["query"])]
    assert len(set(sent)) == 1
    began = warehouse.time
    warehouse.put({**ROWS[0], "revision": "r2"})
    later = connector.list(since=pages[-1].cursor)
    assert [r.id for r in later.records] == ["acct-1"] and not later.more
    sent = [re.search("'(.*)'", q).group(1) for tool, args in warehouse.calls if "updated" in (q := args["query"])]
    assert sent[0] < sent[-1] <= stamp(began)  # when the cut pass began, before the change


def test_a_record_listed_then_gone_is_a_notice_and_the_rest_are_still_listed(warehouse: Warehouse) -> None:
    warehouse.ghosts.append("acct-0")
    connector = make(warehouse, source_file("warehouse"), page_size=3)
    listing = connector.list()
    assert [r.id for r in listing.records] == ["acct 3/x", "acct-1", "acct-2"] and not listing.more
    assert connector.notices[0] == "acct-0: listed, then gone before it was fetched"


def test_with_no_revision_path_a_record_goes_by_a_hash_of_its_reply_and_lands_as_records(
        warehouse: Warehouse) -> None:
    binding = source_file("changes").settings["binding"]
    get = {k: v for k, v in binding["get"].items() if k != "revision"}
    source = source_file("changes", into="accounts", window=Window(behind="all"),
                         settings={"read_only": ["execute_sql"], "binding": {**binding, "get": get}})
    record = make(warehouse, source).get("acct-1")
    text = as_text({"rows": [warehouse.rows["acct-1"]]})
    assert record.revision == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    assert (record.extract, record.into, record.body) == ("records", "accounts", text)
    make(warehouse, source).list()
    assert "updated >= '1970-01-01T00:00:00Z'" in warehouse.calls[-4][1]["query"]
    contract = make(warehouse, source_file("notes"))
    with pytest.raises(ReplyError, match=r"notes: cerebro.list gave no ids list \(\{\"results\": null\}\)"):
        contract._ids("broken")


def test_a_get_binding_fills_templates_with_the_id_and_reads_paths_into_the_reply(warehouse: Warehouse) -> None:
    binding = source_file("warehouse").settings["binding"]
    get = {"tool": "execute_sql", "args": binding["get"]["args"], "revision": "$.rows[*].revision",
           "url": "$.rows[0].title", "into": "team-{id}", "identity": {"row": "{id}", "email": "$.rows[*].email"},
           "fields": {"origin": "db/{id}", "ids": "$.rows[*].id", "all": "$.rows[0].*", "gone": "$.rows[0].nope"}}
    source = source_file("warehouse", into="", extract="", settings={"read_only": ["execute_sql"], "binding": {**binding, "get": get}})
    connector = make(warehouse, source)
    record = connector.get("acct-1")
    assert (record.revision, record.url, record.into, record.extract) == ("r1", "Acme", "team-acct-1", "records")
    assert record.keys == {"row": ["acct-1"], "email": ["ada@acme.example"]}
    assert record.fields["origin"] == "db/acct-1" and record.fields["ids"] == ["acct-1"]
    assert record.fields["all"][:2] == ["2026-11-01T09:00:00Z", "ada@acme.example"] and "gone" not in record.fields
    assert connector.notices == [("acct-1: binding.get.fields.gone $.rows[0].nope matched nothing in the reply, "
                                  "so it is left out")]
    assert connector.get("acct 3/x").keys == {"row": ["acct 3/x"]}


def test_a_changed_binding_on_an_incremental_source_reads_its_whole_window_again(warehouse: Warehouse) -> None:
    cursor = every_page(make(warehouse, source_file("changes"))).cursor
    warehouse.rows.pop("acct-2")
    again = every_page(make(warehouse, source_file("changes", into="accounts")), cursor)
    assert sorted(r.id for r in again.records) == ["acct 3/x", "acct-1"] and again.removed == ["acct-2"]
    sent = [re.search("'(.*)'", q).group(1) for tool, args in warehouse.calls if "updated" in (q := args["query"])]
    assert sent[-1] < stamp(NOW - timedelta(days=29))


def test_a_command_servers_error_output_never_reaches_the_terminal_and_its_last_line_says_why(
        tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    script = "import sys; print('starting', file=sys.stderr); print('NOTES_TOKEN is not set', file=sys.stderr)"
    source = SourceFile(name="notes", kind="mcp", command=f"{sys.executable} -c \"{script}\"")
    with pytest.raises(McpError, match="notes: the command .* did not open a session: it said NOTES_TOKEN is not "
                                       "set: check its command line"):
        McpClient.open(source, tmp_path, environ={})
    assert "NOTES_TOKEN" not in capfd.readouterr().err
