"""The database source over Postgres: the `sql` line's one read, against a stand-in server that records every statement
the engine sends, in order. Refused before it runs unless it is one read as SQLite and Postgres both read it; opened
read only, in one read-only transaction that is always rolled back and never committed; bounded, masked, cited and
counted in the digest as the SQLite source is. The connection string lives only in a file secrets store under the
test's own folder, with `db.example` as its host and made-up credentials, and never reaches a line. psycopg is never
imported: the stand-in takes its place."""

from __future__ import annotations

import ipaddress
import logging
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_connector_database import REFUSED

from cerebro.agent import Agent, load_agent
from cerebro.app import Cerebro, Masked
from cerebro.connector import CursorError, QuestionError, RecordNotFound
from cerebro.connectors import postgres
from cerebro.connectors.database import CURSOR, DatabaseConnector, DatabaseError, Limits, QueryRefused
from cerebro.connectors.postgres import DriverMissing, PostgresEngine, cell, check_postgres, driver
from cerebro.evidence import SqlError, digest, gather
from cerebro.fakes import FakeExtraction
from cerebro.lookup import Lookup
from cerebro.secrets import MARK, FileSecrets, Mask, SecretsError, login_getter
from cerebro.source import SourceError, SourceFile, load_sources, source_of

PASSWORD = "hunter-two-two"
DSN = f"postgresql://reader:{PASSWORD}@db.example:5432/sales?sslmode=require"
SOURCE = "kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\n"
PIPELINE = "select region, sum(amount) from deals group by region order by region"
DEALS = [("East", Decimal(4000)), ("North", Decimal("3000.00")), ("South", Decimal(3000))]


# -- the stand-in server ------------------------------------------------------------------------------------------


class StandInError(Exception):
    """An error as psycopg raises one: its line, and the SQLSTATE the server gave, None for one it did not."""

    def __init__(self, message: str, sqlstate: str | None = None):
        super().__init__(message)
        self.sqlstate = sqlstate


@dataclass
class Server:
    """A Postgres server as psycopg 3 shows it: `connect` opens a connection, whose `execute` sends one statement and
    whose `cursor(name=...)` is a server-side cursor that `execute`s one query and `fetchmany`s its rows. Every
    statement sent is recorded on its connection, in order. `fails` maps a step, `connect`, a statement's text,
    `query` or `fetch`, to the error the server gives there."""

    columns: list[str] = field(default_factory=lambda: ["region", "sum"])
    rows: list[tuple[Any, ...]] = field(default_factory=lambda: list(DEALS))
    fails: dict[str, Exception] = field(default_factory=dict)
    dsns: list[str] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    superuser: bool = False

    def connect(self, dsn: str, **options: Any) -> Connection:
        self.dsns.append(dsn)
        self.options.append(options)
        if "connect" in self.fails:
            raise self.fails["connect"]
        connection = Connection(self)
        self.connections.append(connection)
        return connection


@dataclass
class Connection:
    server: Server
    statements: list[str] = field(default_factory=list)
    cursors: list[str | None] = field(default_factory=list)
    fetched: list[int] = field(default_factory=list)
    cursor_closed: bool = False
    closed: bool = False

    def execute(self, statement: str) -> SimpleNamespace:
        self.statements.append(statement)
        if statement in self.server.fails:
            raise self.server.fails[statement]
        return SimpleNamespace(fetchone=lambda: (self.server.superuser,))

    def cursor(self, name: str | None = None) -> Cursor:
        self.cursors.append(name)
        return Cursor(self)

    def commit(self) -> None:  # never called: a test that sees it here fails
        self.statements.append("COMMIT")

    def close(self) -> None:
        self.closed = True


@dataclass
class Cursor:
    connection: Connection
    description: list[tuple[str, int]] | None = None
    left: list[tuple[Any, ...]] = field(default_factory=list)

    def execute(self, query: str) -> None:
        self.connection.statements.append(query)
        if "query" in self.connection.server.fails:
            raise self.connection.server.fails["query"]
        self.description = [(name, 25) for name in self.connection.server.columns]
        self.left = list(self.connection.server.rows)

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        self.connection.fetched.append(size)
        if "fetch" in self.connection.server.fails:
            raise self.connection.server.fails["fetch"]
        out, self.left = self.left[:size], self.left[size:]
        return out

    def close(self) -> None:
        self.connection.cursor_closed = True


AGAIN = re.compile(r"SET LOCAL statement_timeout = \d+")


def opened(query: str, ms: int = 10_000) -> list[str]:
    """Every statement one read sends, in order, but the timeout set again before each fetch: the session read only,
    one read-only transaction, the check that the login is not too strong, its timeout, the query wrapped so the
    server stops a row far too long, and the rollback."""
    return ["SET default_transaction_read_only = on", "BEGIN READ ONLY", postgres.TOO_STRONG,
            f"SET LOCAL statement_timeout = {ms}", postgres.WRAPPED.format(query=query, cap=4 << 18), "ROLLBACK"]


def sent(connection: Connection) -> list[str]:
    """What a connection was sent, without the timeout set again before each fetch, once each fetch is seen to have
    one."""
    st = connection.statements
    at = next((i + 1 for i, s in enumerate(st) if s.startswith("SELECT cerebro_q.*")), len(st))
    assert len([s for s in st[at:] if AGAIN.fullmatch(s)]) == len(connection.fetched)
    return st[:at] + [s for s in st[at:] if not AGAIN.fullmatch(s)]


def read_only(server: Server, sent_: list[str]) -> Connection:
    """The one connection the server saw sent exactly `sent_`, read through the one named cursor, never committed,
    and closed."""
    (connection,) = server.connections
    assert sent(connection) == sent_
    assert not any("commit" in s.lower() for s in connection.statements)
    assert connection.cursors in ([], ["cerebro_sql"]) and connection.closed
    assert connection.cursor_closed == bool(connection.cursors)
    assert server.options == [{"autocommit": True, "connect_timeout": 10, "application_name": "cerebro"}]
    return connection


# -- a workspace folder ---------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> FileSecrets:
    """The workspace's file secrets store, under the test's own folder, holding the connection string."""
    kept = FileSecrets(tmp_path / "secrets", "workspace-1")
    kept.set("warehouse-dsn", DSN)
    return kept


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "workspace" / "sources").mkdir(parents=True)
    (tmp_path / "workspace" / "sources" / "warehouse.yaml").write_text(SOURCE)
    return tmp_path / "workspace"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    """The stand-in, in psycopg's place wherever the engine asks for its driver."""
    stand_in = Server()
    monkeypatch.setattr(postgres, "driver", lambda label: stand_in.connect)
    return stand_in


def build(store: FileSecrets, **kwargs: Any) -> Any:
    """A database source as the app would build one for Postgres: its login read from the store when a query runs."""
    return lambda source, at: DatabaseConnector(source, at, login=lambda: login_getter(store, source)(), **kwargs)


def warehouse(root: Path, store: FileSecrets, **kwargs: Any) -> DatabaseConnector:
    kwargs.setdefault("ticks", lambda: 0.0)  # so each timeout sent is the whole limit
    found = load_sources(root, {"database": build(store, **kwargs)})
    assert found.refused == [] and found.notices == []
    (connector,) = found.connectors
    assert isinstance(connector, DatabaseConnector) and isinstance(connector.engine, PostgresEngine)
    return connector


def pipeline(root: Path, query: str = PIPELINE) -> Agent:
    path = root / "agents" / "reports" / "pipeline.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nevidence:\n  - sql: {{ source: warehouse, query: "{query}" }}\n---\n\n# Instructions\n\n'
                    "Write the pipeline report, by region. Cite the rows you use.\n")
    return load_agent(path, root / "agents")


# -- the main path ------------------------------------------------------------------------------------------------


def test_an_agent_file_with_a_sql_line_reads_postgres_through_one_read_only_transaction_as_cited_data(
        root: Path, store: FileSecrets, server: Server) -> None:
    writer = pipeline(root)
    assert writer.cooldown is None and not any("cooldown" in w for w in writer.warnings)  # schedule.articles covers it
    (entry,) = gather(writer, Lookup(), {}, root=root, sources={"warehouse": Masked(warehouse(root, store), lambda: str)})
    assert entry.heading == f"Sql warehouse, the rows of {PIPELINE}"
    assert entry.lines == ['- [^sql:warehouse:2705d7ff:1] {"region": "East", "sum": 4000}',
                           '- [^sql:warehouse:2705d7ff:2] {"region": "North", "sum": 3000}',
                           '- [^sql:warehouse:2705d7ff:3] {"region": "South", "sum": 3000}']
    assert [(c.kind, c.id, c.source) for c in entry.citations] == [
        ("record", f"sql:warehouse:2705d7ff:{i}", "sql:warehouse") for i in (1, 2, 3)]
    assert server.dsns == [DSN]  # read from the store as the query ran
    connection = read_only(server, opened(PIPELINE))
    assert connection.fetched and all(n <= 50 for n in connection.fetched)


def test_a_changed_row_moves_the_digest_and_unchanged_rows_do_not(root: Path, store: FileSecrets,
                                                                 server: Server) -> None:
    writer, sources = pipeline(root), {"warehouse": warehouse(root, store)}
    first = digest(writer, gather(writer, Lookup(), {}, root=root, sources=sources))
    assert digest(writer, gather(writer, Lookup(), {}, root=root, sources=sources)) == first
    server.rows[0] = ("East", Decimal(5000))
    assert digest(writer, gather(writer, Lookup(), {}, root=root, sources=sources)) != first
    assert len(server.connections) == 3 and all(sent(c) == opened(PIPELINE) for c in server.connections)


# -- read only: refused before anything is sent ----------------------------------------------------------------

POSTGRES_REFUSED = [
    ("select [region; delete from deals]", "a second statement after a semicolon"),  # [ quotes nothing in Postgres
    ("select `region; delete from deals`", "a second statement after a semicolon"),  # nor does a backquote
    ("select $$'$$; delete from deals; select $$'$$", "a second statement after a semicolon"),  # a dollar quote
    ("select $tag$ open from deals", "opens a quote with $tag$ and never closes it"),
    ("select E'a\\'' as x, 1; delete from deals; select ''''", "never closes it"),
    ("select E'a\\b' as path from deals", "a backslash inside a quote"),
    ("select 'C:\\temp' as path from deals", "a backslash inside a quote"),
    ('select U&"d\\0061ta" from deals', "with U&"),
    ("select U&'d!0061ta' uescape '!' from deals", "with U&"),
    ("select region from deals for share", "holds share"),
    ("select region from deals for key share nowait", "holds share"),
    ("select region from deals for no key update", "holds update"),
    ("select copy from deals", "holds copy"),
    ("select merge from deals", "holds merge"),
    ("select * into backup from deals", "holds into"),
    ("copy deals to program 'curl db.example'", "starts with copy"),
    ("select 1; copy deals to program 'sh'", "a second statement after a semicolon"),
    ("select pg_read_file('/etc/passwd')", "calls pg_read_file"),
    ("select pg_read_binary_file('global/pg_control')", "calls pg_read_binary_file"),
    ("select * from pg_ls_dir('.')", "calls pg_ls_dir"),
    ("select pg_stat_file('postgresql.conf')", "calls pg_stat_file"),
    ("select * from dblink('host=db.example', 'select 1') as t(x int)", "calls dblink"),
    ("select dblink_exec('host=db.example', 'select 1')", "calls dblink_exec"),
    ("select lo_import('/etc/passwd')", "calls lo_import"),
    ("select lo_export(16384, '/srv/out')", "calls lo_export"),
    ("select pg_sleep(60)", "calls pg_sleep"),
    ("select pg_catalog.pg_sleep_for('1 minute')", "calls pg_sleep_for"),
    ("select PG_ADVISORY_LOCK(1)", "calls pg_advisory_lock"),
    ('select "pg_read_file" (\'/etc/passwd\')', "calls pg_read_file"),
    ("select set_config('default_transaction_read_only', 'off', false)", "calls set_config"),
    ("select query_to_xml('select 1', true, true, '')", "calls query_to_xml"),
    ("select pg_terminate_backend(42)", "calls pg_terminate_backend"),
    ("select pg_reload_conf()", "calls pg_reload_conf"),
    ("select nextval('deals_id_seq')", "calls nextval"),
    ("select http_get('https://db.example/')", "calls http_get"),
    ("select pg_notify('x', 'y')", "calls pg_notify"),
    ("select txid_current()", "calls txid_current"),
    ("select pg_current_xact_id()", "calls pg_current_xact_id"),
    ("select 1) as cerebro_q union select (2", "closes a parenthesis it never opened"),
    ("select (1", "opens a parenthesis it never closes"),
    ("select (1;", "opens a parenthesis it never closes"),
]


@pytest.mark.parametrize(("query", "why"), REFUSED + POSTGRES_REFUSED)
def test_a_query_that_is_not_one_read_is_refused_before_a_connection_is_opened(
        root: Path, store: FileSecrets, server: Server, query: str, why: str) -> None:
    connector = warehouse(root, store)
    with pytest.raises(QueryRefused) as refused:
        connector.select(query)
    assert why in str(refused.value) and "\n" not in str(refused.value)
    assert server.dsns == [] and server.connections == []  # nothing sent, and the store not even read


ALLOWED = [
    "select region, ';' as mark, '-- no comment' as dash, '/* none */' from deals where region = 'North;'",
    'select region as "update", stage as "share", amount as "copy" from deals where amount = 4000;',
    "  with won as (select * from deals where stage = 'won') select region, amount from won  ;  ",
    "with recursive n(x) as (select 1 union all select x + 1 from n where x < 3) select x from n",
    "select tags[1] as first, $$plain$$ as note, $body$x$body$ as body from deals",
    "select E'plain' as e, 'it''s' as s from deals where note like 'pg_sleep%'",
    "select sleep_seconds, http_status, lo_mark, dblink_count from deals",
    "SELECT région, \"Amount\" FROM deals WHERE amount = 1000",
    "select region, '(' as open, $$)$$ as close, \")\" from deals where (amount > (1))",
]


@pytest.mark.parametrize("query", ALLOWED)
def test_a_read_with_quotes_names_and_words_postgres_allows_reaches_the_server_as_checked(
        root: Path, store: FileSecrets, server: Server, query: str) -> None:
    assert warehouse(root, store).select(query).rows == [["East", 4000], ["North", 3000], ["South", 3000]]
    read_only(server, opened(query.strip().removesuffix(";").strip()))


def test_check_postgres_gives_the_query_as_check_query_does() -> None:
    assert check_postgres("  select 1 ;  ") == "select 1"
    assert check_postgres("select $body$x$body$ as b") == "select $body$x$body$ as b"
    with pytest.raises(QueryRefused, match="a second statement"):  # SQLite's reading sees the semicolon, so it is out
        check_postgres("select $$a;b$$")


# -- read only: what the server refuses, and what fails there ---------------------------------------------------

WRITE = StandInError("cannot execute INSERT in a read-only transaction", "25006")


@pytest.mark.parametrize("step", ["query", "fetch"])
def test_a_write_the_text_check_missed_is_refused_by_the_read_only_transaction_and_rolled_back(
        root: Path, store: FileSecrets, server: Server, step: str) -> None:
    server.fails[step] = WRITE  # as a function that writes would be, called by a select
    query = "select archive_old_deals()"
    with pytest.raises(QueryRefused) as refused:
        warehouse(root, store).select(query)
    assert str(refused.value) == ("the query would write (cannot execute INSERT in a read-only transaction), and a "
                                  "sql line only reads: write a select")
    read_only(server, opened(query))


@pytest.mark.parametrize(("fails", "error", "line"), [
    ({"query": StandInError('relation "nothing_here" does not exist', "42P01")}, DatabaseError,
     ('sources/warehouse.yaml: the query failed (relation "nothing_here" does not exist): run it by hand against the '
      "database to see why, and fix it")),
    ({"fetch": StandInError("canceling statement due to statement timeout", "57014")}, DatabaseError,
     "sources/warehouse.yaml: the query ran past 10 seconds and was stopped: narrow it, or aggregate in the database"),
    ({"query": StandInError("permission denied for table deals", "42501")}, DatabaseError,
     ("sources/warehouse.yaml: the database's role may not read what the query asks for (permission denied for table "
      "deals): query what it may read, or ask the database's owner to grant it")),
])
def test_a_failure_at_the_server_is_one_line_by_its_sqlstate_and_the_transaction_is_rolled_back(
        root: Path, store: FileSecrets, server: Server, fails: dict[str, Exception], error: type[Exception],
        line: str) -> None:
    server.fails.update(fails)
    with pytest.raises(error) as failed:
        warehouse(root, store).select("select region from deals")
    assert str(failed.value) == line
    read_only(server, opened("select region from deals"))


def test_a_session_the_server_will_not_make_read_only_never_sends_the_query(root: Path, store: FileSecrets,
                                                                           server: Server) -> None:
    server.fails["BEGIN READ ONLY"] = StandInError("server closed the connection unexpectedly", None)
    with pytest.raises(DatabaseError, match="the query failed .server closed the connection unexpectedly."):
        warehouse(root, store).select("select region from deals")
    read_only(server, ["SET default_transaction_read_only = on", "BEGIN READ ONLY", "ROLLBACK"])


def test_a_login_too_strong_is_refused_before_the_query_is_sent(root: Path, store: FileSecrets,
                                                                server: Server) -> None:
    server.superuser = True
    with pytest.raises(DatabaseError) as refused:
        warehouse(root, store).select("select region from deals")
    assert str(refused.value) == (
        "sources/warehouse.yaml: the connection string kept as warehouse-dsn logs in as a superuser, or a role that "
        "may read the server's files or run its programs, which a sql line could reach through any function: make a "
        "role that can only read, as the sources docs show, and keep its connection string instead")
    read_only(server, ["SET default_transaction_read_only = on", "BEGIN READ ONLY", postgres.TOO_STRONG, "ROLLBACK"])


def test_a_row_the_server_stopped_for_its_length_is_one_line(root: Path, store: FileSecrets, server: Server) -> None:
    server.fails["fetch"] = StandInError('invalid input syntax for type integer: "cerebro_row_bytes:2000000"',
                                         "22P02")
    with pytest.raises(DatabaseError) as failed:
        warehouse(root, store).select("select notes from deals")
    assert str(failed.value) == ("sources/warehouse.yaml: the query made a row longer than 1048576 bytes, and the "
                                 "server stopped it before sending it: select a shorter part of each value, as "
                                 "substr(notes, 1, 2000)")
    server.fails["fetch"] = StandInError('invalid input syntax for type integer: "abc"', "22P02")
    with pytest.raises(DatabaseError, match="the query failed"):
        warehouse(root, store).select("select notes from deals")


def test_a_server_that_does_not_open_sends_nothing_and_says_where_the_connection_string_is_kept(
        root: Path, store: FileSecrets, server: Server) -> None:
    server.fails["connect"] = OSError("connection refused")
    with pytest.raises(DatabaseError) as failed:
        warehouse(root, store).select("select 1")
    assert str(failed.value) == ("sources/warehouse.yaml: the database does not open (connection refused): check the "
                                 "connection string kept as warehouse-dsn, and that the server is up")
    assert server.connections == []


def test_an_error_that_is_not_the_drivers_is_not_dressed_as_one(root: Path, store: FileSecrets,
                                                                server: Server) -> None:
    server.fails["query"] = KeyError("a stand-in's own fault")
    with pytest.raises(KeyError):
        warehouse(root, store).select("select 1")
    read_only(server, opened("select 1"))  # and the transaction still ends in a rollback


# -- bounds, masks, values --------------------------------------------------------------------------------------


def test_rows_size_and_time_are_bounded_with_a_note_that_says_what_was_not_read(
        root: Path, store: FileSecrets, server: Server) -> None:
    server.rows = [("North", i) for i in range(200)]
    (entry,) = gather(pipeline(root, "select region, amount from deals"), Lookup(), {}, root=root,
                      sources={"warehouse": warehouse(root, store, limits=Limits(rows=2))})
    assert len(entry.items) == 2 and entry.notes == [
        "it gave more than 2 rows, so the rest were not read: aggregate or filter in the query"]
    assert server.connections[0].fetched == [3]  # never asked for more than one row past the limit
    fifty = warehouse(root, store, limits=Limits(rows=50)).select("select region, amount from deals")
    assert len(fifty.rows) == 50 and fifty.more and server.connections[-1].fetched == [50, 1]
    rows = warehouse(root, store, limits=Limits(size=70)).select("select region, amount from deals")
    assert rows.cut and not rows.more and rows.rows == [["North", 0], ["North", 1]]
    clock = iter(range(1_000_000))
    slow = warehouse(root, store, limits=Limits(seconds=3), ticks=lambda: float(next(clock)))
    with pytest.raises(DatabaseError, match="ran past 3 seconds and was stopped"):
        slow.select("select region, amount from deals")
    assert sent(server.connections[-1]) == opened("select region, amount from deals", 2000)[:-1] + ["ROLLBACK"]
    assert server.connections[-1].statements[-2] == "SET LOCAL statement_timeout = 1000"  # the time left, at the server
    assert server.connections[-1].fetched == [50]  # it stopped between fetches, on the stand-in's seconds
    assert server.options[-1]["connect_timeout"] == 3
    whole = warehouse(root, store, limits=Limits(seconds=3), ticks=lambda: 100.0)
    assert len(whole.select("select region, amount from deals").rows) == 200


def test_one_value_past_the_size_limit_is_refused_and_the_transaction_rolled_back(
        root: Path, store: FileSecrets, server: Server) -> None:
    server.columns, server.rows = ["notes"], [("x" * ((1 << 18) + 1),)]
    with pytest.raises(DatabaseError, match="made a value longer than 262144 bytes, the most a query gives"):
        warehouse(root, store).select("select notes from deals")
    read_only(server, opened("select notes from deals"))
    server.rows = [("x" * (1 << 18),)]  # a value of the most bytes is no longer than the limit: its row is cut
    rows = warehouse(root, store).select("select notes from deals")
    assert rows.cut and rows.rows == []


def test_every_name_value_and_failure_line_comes_back_masked(root: Path, store: FileSecrets,
                                                            server: Server) -> None:
    token = "tok-9f8e7d6c5b4a"
    server.columns = ["region", "body", "blob", token, "n", "n"]
    server.rows = [("West", f"key {token} pasted", b"\x00\xff", "West", 1, 2)]
    mask = Mask()
    mask.add(token)
    connector = warehouse(root, store, mask=mask)
    rows = connector.select("select region, body, blob, region, 1 as n, 2 as n from notes")
    assert rows.columns == ["region", "body", "blob", MARK, "n", "n 2"]
    assert rows.rows == [["West", f"key {MARK} pasted", "<2 bytes>", "West", 1, 2]]
    server.fails["query"] = StandInError(f'relation "{token}" does not exist', "42P01")
    with pytest.raises(DatabaseError) as failed:
        connector.select("select * from notes")
    assert token not in str(failed.value) and MARK in str(failed.value)


def test_the_connection_string_and_its_password_never_reach_an_error_line_a_notice_or_a_log_row(
        root: Path, tmp_path: Path, server: Server, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    encoded = "pw%40hunter-two"
    kept_as = [(DSN, PASSWORD), (f"postgresql://reader:{encoded}@db.example/sales", "pw@hunter-two"),
               (f"host=db.example user=reader password='{PASSWORD}' dbname=sales", PASSWORD),
               (f"host=db.example password={PASSWORD} dbname=sales", PASSWORD),
               ("postgresql://reader@db.example/sales?password=query-hunter-two", "query-hunter-two"),
               ("postgresql://reader@db.example/sales?sslmode=require&password=query%20hunter", "query hunter"),
               ("host=db.example password='hunter\\'s two\\\\deep' dbname=sales", "hunter's two\\deep")]
    leaks = [secret for pair in kept_as for secret in pair] + [encoded, "db.example"]
    lines: list[str] = []
    for dsn, password in kept_as:
        kept = FileSecrets(tmp_path / "leak", "workspace-2")
        kept.set("warehouse-dsn", dsn)
        connector = warehouse(root, kept)
        lines += connector.list().notices
        for fails in ({"connect": StandInError(f"could not connect with {dsn}: password {password} refused")},
                      {"connect": StandInError('connection to server at "db.example", port 5432 failed', None)},
                      {"query": StandInError(f"syntax error near {password} in {dsn}", "42601")},
                      {"fetch": StandInError(f"{dsn} {password} cannot execute UPDATE", "25006")}):
            server.fails = dict(fails)
            with pytest.raises(SqlError) as failed:
                gather(pipeline(root, "select 1"), Lookup(), {}, root=root, sources={"warehouse": connector})
            lines.append(str(failed.value))
            assert MARK in lines[-1]
    for text in ("kind: database\nurl: postgres://reader:hunter-two-two@db.example/sales\n",
                 f'kind: database\nurl: postgres\nauth: "{DSN}"\n',
                 f'kind: database\nurl: postgres\nauth: {{ keychain: "{DSN}" }}\n',
                 f"kind: database\nurl: postgres\nauth: {{ keychain: warehouse-dsn }}\npassword: {PASSWORD}\n"):
        (root / "sources" / "warehouse.yaml").write_text(text)
        found = load_sources(root, {"database": build(store=FileSecrets(tmp_path / "leak", "workspace-2"))})
        lines += found.refused + found.notices
    lines += [record.getMessage() for record in caplog.records]
    assert len(lines) >= 35
    assert not [line for line in lines for leak in leaks if leak in line]
    assert not [line for line in lines if re.search(r"\w\[secret\]|\[secret\]\w", line)]  # none masked in part


# -- the values ---------------------------------------------------------------------------------------------------


def test_each_postgres_type_comes_back_as_the_sqlite_source_gives_a_value(root: Path, store: FileSecrets,
                                                                          server: Server) -> None:
    zone = timezone(timedelta(hours=-5))
    given = {
        "null": None, "bool": True, "int": 7, "float": 1.5, "nan": float("nan"), "inf": float("-inf"),
        "whole": Decimal("1000.00"), "money": Decimal("1234.50"), "exact": Decimal("0.12345678901234567890"),
        "huge": Decimal("1E+30"), "numeric_nan": Decimal("NaN"), "numeric_inf": Decimal("-Infinity"),
        "stamp_zone": datetime(2026, 11, 17, 4, 30, tzinfo=zone), "stamp": datetime(2026, 11, 17, 9, 30),
        "day": date(2026, 11, 17), "clock": time(9, 30), "span": timedelta(days=1, hours=2),
        "jsonb": {"stage": "won", "amounts": [1, Decimal("2.5")], "at": date(2026, 11, 17)},
        "array": [1, None, 3], "dates": [date(2026, 1, 2)], "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "bytea": memoryview(b"\x00\xff\x10"), "inet": ipaddress.ip_address("192.0.2.1"), "text": "plain",
    }
    server.columns, server.rows = list(given), [tuple(given.values())]
    (row,) = warehouse(root, store).select("select * from types").rows
    assert dict(zip(given, row, strict=True)) == {
        "null": None, "bool": True, "int": 7, "float": 1.5, "nan": "NaN", "inf": "-Infinity",
        "whole": 1000, "money": 1234.5, "exact": "0.12345678901234567890", "huge": 10**30,
        "numeric_nan": "NaN", "numeric_inf": "-Infinity",
        "stamp_zone": "2026-11-17T09:30:00+00:00", "stamp": "2026-11-17T09:30:00",
        "day": "2026-11-17", "clock": "09:30:00", "span": "1 day, 2:00:00",
        "jsonb": '{"stage":"won","amounts":[1,2.5],"at":"2026-11-17"}',
        "array": "[1,null,3]", "dates": '["2026-01-02"]', "uuid": "12345678-1234-5678-1234-567812345678",
        "bytea": "<3 bytes>", "inet": "192.0.2.1", "text": "plain",
    }
    assert cell(datetime(2026, 11, 17, 9, 30, tzinfo=UTC)) == "2026-11-17T09:30:00+00:00"
    assert cell(float("inf")) == "Infinity" and cell(Decimal("Infinity")) == "Infinity"
    assert cell(b"ab") == "<2 bytes>" and cell(bytearray(b"a")) == "<1 bytes>" and cell(False) is False
    assert cell((1, "a")) == '[1,"a"]' and cell({"é": "ü"}) == '{"é":"ü"}'


# -- the source file -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "why"), [
    ("kind: database\nurl: postgres://reader:hunter-two-two@db.example/sales\nauth: { keychain: warehouse-dsn }\n",
     "url: it holds an address, which may hold a login: write url: postgres alone"),
    ('kind: database\nurl: "postgresql:host=db.example password=hunter-two-two"\nauth: { keychain: warehouse-dsn }\n',
     "url: it holds an address"),
    ('kind: database\nurl: "postgres:"\n', ("auth is missing: a postgres source reads its connection string from "
                                            "the secrets store: keep it with cerebro secrets set warehouse, then "
                                            "write auth: { keychain: warehouse }")),
    ("kind: database\nurl: postgres\nauth: oauth\n", "auth: a postgres source reads its connection string"),
    ("kind: database\nurl: postgres\nauth: \"postgresql://reader:hunter-two-two@db.example/sales\"\n",
     "under the item auth: { keychain: <item> } names"),
    ("kind: database\nurl: postgres\nauth: { keychain: \"host=db.example password=hunter-two-two\" }\n",
     "auth keychain: the item is not a secret's name"),
    ("kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\nhost: db.example\n", "host is not a key"),
    ("kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\nuser: reader\n", "user is not a key"),
    ("kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\npassword: hunter-two-two\n",
     "password is not a key"),
    ("kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\ndsn: postgres://db.example/sales\n",
     "dsn is not a key"),
    ("kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\nserver: db.example\n",
     "line 4: server is not a key a source of kind database reads"),
    ("kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\nwindow: { behind: 30d }\n",
     "line 4: window belongs to a source of kind"),
    ("kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\nextract: facts\n", "extract belongs to a source of kind"),
])
def test_a_source_file_that_holds_an_address_a_login_or_more_than_url_and_auth_is_refused_alone(
        root: Path, store: FileSecrets, text: str, why: str) -> None:
    (root / "sources" / "warehouse.yaml").write_text(text)
    found = load_sources(root, {"database": build(store)})
    assert found.connectors == [] and len(found.refused) == 1
    assert found.refused[0].startswith("sources/warehouse.yaml ") and why in found.refused[0]
    for secret in ("hunter-two-two", "db.example", "reader"):
        assert secret not in found.refused[0]


@pytest.mark.parametrize("url", ["postgres", "postgresql", '"postgres:"', "PostgreSQL", '"postgresql:  "'])
def test_a_source_file_names_the_postgres_engine_by_url_alone(root: Path, store: FileSecrets, url: str) -> None:
    (root / "sources" / "warehouse.yaml").write_text(f"kind: database\nurl: {url}\nauth: {{ keychain: kept_dsn }}\n")
    engine = warehouse(root, store).engine
    assert isinstance(engine, PostgresEngine) and engine.item == "kept_dsn"
    assert engine.label == "sources/warehouse.yaml" and engine.connect is None


def test_a_postgres_source_handed_no_login_is_refused_in_one_line(root: Path) -> None:
    found = load_sources(root, {"database": DatabaseConnector})
    assert found.refused == [("sources/warehouse.yaml auth: nothing hands this source the secrets store, so the "
                              "connection string kept as warehouse-dsn cannot be read: Cerebro must hand a postgres "
                              "source its login")]


def test_a_connection_string_the_store_lacks_or_holds_empty_is_one_line_and_nothing_is_opened(
        root: Path, tmp_path: Path, server: Server) -> None:
    empty = FileSecrets(tmp_path / "empty", "workspace-3")
    with pytest.raises(SecretsError, match="no secret named warehouse-dsn"):
        warehouse(root, empty).select("select 1")
    blank = SourceFile("warehouse", "database", url="postgres", auth="keychain: warehouse-dsn")
    connector = DatabaseConnector(blank, root, login=lambda: "  ")
    with pytest.raises(DatabaseError, match="the connection string kept as warehouse-dsn is empty: keep one with "
                                            "cerebro secrets set warehouse-dsn"):
        connector.select("select 1")
    assert server.dsns == [] and server.connections == []


def test_a_missing_driver_is_one_line_that_names_the_extra(root: Path, store: FileSecrets,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)  # as if it were not installed, wherever this runs
    with pytest.raises(DriverMissing) as missing:
        warehouse(root, store).select("select 1")
    assert str(missing.value) == ("sources/warehouse.yaml: a postgres database is read through psycopg, which is not "
                                  "installed: install cerebro with its postgres extra, as pip install "
                                  "'cerebro[postgres]'")
    assert isinstance(missing.value, DatabaseError)


def test_the_driver_reads_an_infinite_date_or_timestamp_as_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is not installed: install cerebro[postgres] to run this")
    from psycopg.adapt import AdaptersMap
    from psycopg.pq import Format

    made: list[Any] = []
    monkeypatch.setattr(psycopg, "connect", lambda dsn, **options: made.append(
        SimpleNamespace(adapters=AdaptersMap(psycopg.adapters))) or made[-1])
    connect = driver("sources/warehouse.yaml")
    (db,) = [connect(DSN, autocommit=True)]
    for name in ("date", "timestamp", "timestamptz"):
        oid = psycopg.adapters.types[name].oid
        loader = db.adapters.get_loader(oid, Format.TEXT)(oid)
        assert loader.load(b"infinity") == "infinity" and loader.load(b"-infinity") == "-infinity"
    assert db.adapters.get_loader(psycopg.adapters.types["date"].oid, Format.TEXT)(1082).load(
        b"2026-11-17") == date(2026, 11, 17)
    assert psycopg.adapters.get_loader(psycopg.adapters.types["date"].oid, Format.TEXT) is not \
        db.adapters.get_loader(psycopg.adapters.types["date"].oid, Format.TEXT)  # psycopg's own map is not changed


def test_an_engine_given_its_connect_uses_it_and_never_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)
    server = Server()
    engine = PostgresEngine(lambda: DSN, "sources/warehouse.yaml", item="warehouse-dsn", connect=server.connect,
                            ticks=lambda: 0.0)
    assert engine.run("select region, sum from deals", Limits()).rows == [["East", 4000], ["North", 3000],
                                                                          ["South", 3000]]
    with pytest.raises(QueryRefused, match="calls pg_sleep"):
        engine.run("select pg_sleep(1)", Limits())
    read_only(server, opened("select region, sum from deals"))
    brief = Server()
    PostgresEngine(lambda: DSN, "sources/warehouse.yaml", connect=brief.connect, ticks=lambda: 0.0).run(
        "select 1", Limits(seconds=0.0004))
    assert brief.options[0]["connect_timeout"] == 2  # libpq waits at least two seconds
    assert brief.connections[0].statements[3] == "SET LOCAL statement_timeout = 1"  # zero would mean no limit


def test_a_rollback_or_close_that_fails_on_the_way_out_changes_neither_the_rows_nor_the_failure(
        root: Path, store: FileSecrets, server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    gone = StandInError("server closed the connection unexpectedly")
    server.fails["ROLLBACK"] = gone

    def close(self: Any) -> None:
        raise gone

    monkeypatch.setattr(Cursor, "close", close)
    connector = warehouse(root, store)
    assert connector.select("select region, sum from deals").rows[0] == ["East", 4000]
    server.fails["query"] = StandInError('relation "nothing_here" does not exist', "42P01")
    with pytest.raises(DatabaseError, match="the query failed .relation \"nothing_here\" does not exist."):
        connector.select("select * from nothing_here")
    assert all(c.statements[-1] == "ROLLBACK" and c.closed for c in server.connections)


def test_a_connection_string_that_does_not_read_as_a_url_is_still_masked_whole() -> None:
    broken = f"postgresql://reader:{PASSWORD}@[db.example/sales"  # an unclosed address, which urlsplit refuses
    assert postgres._secrets_in(broken)[0] == broken


def test_a_refusal_is_one_whole_line_that_says_what_to_do(root: Path, store: FileSecrets) -> None:
    with pytest.raises(QueryRefused) as refused:
        check_postgres("select pg_ls_dir('.')")
    assert str(refused.value) == ("the query calls pg_ls_dir, which reaches past the tables a sql line reads, to "
                                  "files, the network, locks or the server's settings: take it out")
    lines = {
        "url: postgres://db.example/sales\nauth: { keychain: warehouse-dsn }": (
            "sources/warehouse.yaml url: it holds an address, which may hold a login: write url: postgres alone, "
            "and keep the connection string in the secrets store under the item auth: { keychain: <item> } names"),
        "url: postgresql\nauth: oauth": (
            "sources/warehouse.yaml auth: a postgresql source reads its connection string from the secrets store, "
            "under the item auth: { keychain: <item> } names: write that, and delete any other auth"),
        "url: postgres\nauth: { keychain: \"a b\" }": (
            "sources/warehouse.yaml auth keychain: the item is not a secret's name, and a source file never holds "
            "the connection string itself: name it with letters, digits, _ and -, as auth: { keychain: warehouse }"),
    }
    for text, line in lines.items():
        (root / "sources" / "warehouse.yaml").write_text(f"kind: database\n{text}\n")
        assert load_sources(root, {"database": build(store)}).refused == [line]


def test_a_postgres_source_lists_no_records_with_one_notice_finds_none_and_answers_no_question(
        root: Path, store: FileSecrets, server: Server) -> None:
    connector = warehouse(root, store)
    listing = connector.list()
    assert listing.records == [] and listing.cursor == CURSOR and not listing.more
    assert listing.notices == [("sources/warehouse.yaml: a database source lists no records: an agent reads it "
                                "with a sql evidence line")]
    with pytest.raises(CursorError):
        connector.list("not-a-cursor-this-source-gave")
    with pytest.raises(RecordNotFound):
        connector.get("sql:warehouse:1")
    with pytest.raises(QuestionError):
        connector.ask("rows", {"query": "select 1"})
    assert connector.questions == {} and server.connections == []


def test_a_postgres_source_is_refused_as_a_sqlite_one_would_be_when_handed_more_than_it_reads() -> None:
    with pytest.raises(SourceError, match="line 4: into belongs to a source of kind"):
        source_of("warehouse", "kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\ninto: deals\n")


def test_a_workspace_hands_a_postgres_source_its_login_and_masks_the_connection_string_once_read(
        tmp_path: Path, store: FileSecrets, server: Server) -> None:
    root = tmp_path / "workspace"
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("id: workspace-1\ntimezone: UTC\n")
    (root / "sources" / "warehouse.yaml").write_text(SOURCE)
    (root / "sources" / "local.yaml").write_text("kind: database\nurl: sqlite:data/x.sqlite\nauth: { keychain: x }\n")
    with Cerebro(root, extraction=FakeExtraction(), secrets=store) as workspace:
        assert list(workspace.connectors) == ["warehouse"]
        assert any("a SQLite file takes no login: delete auth" in line for line in workspace.refused)
        assert workspace.connectors["warehouse"].select("select region, sum from deals").rows[0] == ["East", 4000]
        assert server.dsns == [DSN] and workspace.mask(f"kept {DSN}") == f"kept {MARK}"
