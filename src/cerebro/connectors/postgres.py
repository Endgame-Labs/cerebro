"""A Postgres database, read the way a database source reads a SQLite file: one checked read, read only, bounded and
masked, that an agent file's `sql` evidence line asks for before the model starts.

Its source file names the engine and nothing else: `url: postgres` (or `postgresql`), with `auth: { keychain: <item> }`
naming the item in the workspace's secrets store that holds the connection string. A url that holds anything past the
engine's name is refused, since an address may hold a login, and a source file never holds a host, a user, a password
or a connection string. The connection string is read from the store when a query runs, never written in a line: each
error line masks it and the password inside it, as the workspace's mask does every value it has read.

Read only is three layers, as for SQLite. Before anything is sent, the query must pass `check_query` as SQLite reads
it and again as Postgres reads it, with the words that write in Postgres refused too (`copy`, `merge`, and `share`,
which locks rows), and no call of a function that reads the server's files, reaches the network, waits, signals
another session or changes a setting (`REFUSED_CALLS`). Then a statement timeout for the time the query has left, set
before anything else and again before each fetch, and the session and its one transaction are read only at the
server: `SET default_transaction_read_only = on`, `BEGIN READ ONLY`, a refusal of a login that is a superuser or may
read the server's files or run its programs (`TOO_STRONG`), the one query through a server-side cursor, wrapped so the
server stops a row far past the size limit before sending it (`WRAPPED`), and always `ROLLBACK`, never `COMMIT`; the
connection is opened in autocommit, so the driver sends no `BEGIN` or `COMMIT` of its own. Last, the same limits bound
what comes back: at most `ROWS` rows, read a few at a time from the cursor, at most `SIZE` bytes of them, no value
longer than `SIZE`, and no more than `SECONDS` in all. The real guard is a role that can only read the tables it should: the docs give the SQL
to make one. A NOTICE or WARNING the server sends is dropped: psycopg passes one only to a handler, and none is added.

Each value comes back as the SQLite source gives one, None, a number or text (`cell` writes down each choice).

The driver is psycopg 3, the `cerebro[postgres]` extra, imported only when a query runs, and `connect` stands in for
it in a test.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from ..clock import seconds
from ..connector import one_line
from ..errors import CerebroError
from ..secrets import Scrubber, SecretsError, check_name
from ..source import SourceError, SourceFile
from .database import DatabaseError, Engine, Limits, QueryRefused, Rows, bounded, check_query

__all__ = ["CURSOR_NAME", "FETCH_ROWS", "REFUSED_CALLS", "WRITE_WORDS", "DriverMissing", "PostgresEngine", "cell",
           "check_postgres", "driver", "postgres_engine"]

# Words that write, lock or copy in Postgres, beyond those SQLite's check refuses; a column of one of these names
# is written in double quotes.
WRITE_WORDS = ("copy", "merge", "share")
# Functions a read never calls: each reads the server's files, reaches the network, waits, takes a lock, signals
# another session, changes a setting, writes outside the transaction or runs a query given as text. A name ending
# in `_` stands for every function it begins.
REFUSED_CALLS = (
    "dblink", "dblink_", "lo_", "loread", "lowrite", "pg_read_file", "pg_read_binary_file", "pg_ls_", "pg_stat_file",
    "pg_file_", "pg_sleep", "pg_sleep_", "pg_advisory_", "pg_try_advisory_", "pg_terminate_backend",
    "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile", "pg_promote", "pg_switch_wal",
    "pg_create_restore_point", "pg_create_", "pg_drop_replication_slot", "pg_copy_", "pg_replication_",
    "pg_logical_", "pg_log_", "pg_wal_replay_", "pg_backup_", "pg_stat_reset", "pg_stat_reset_", "pg_notify",
    "pg_import_system_collations", "set_config", "nextval", "setval", "query_to_xml", "query_to_xml_",
    "cursor_to_xml", "cursor_to_xmlschema", "http", "http_", "txid_current", "pg_current_xact_id",
)
_NAME_CHAR = r"A-Za-z0-9_$\u0080-\U0010ffff"
_CALLS = re.compile(r"(?<![" + _NAME_CHAR + r"])\"?(" + "|".join(
    re.escape(name) + (r"[A-Za-z0-9_]*" if name.endswith("_") else "") for name in REFUSED_CALLS)
    + r")(?![" + _NAME_CHAR + r"])\"?\s*\(", re.IGNORECASE)
CURSOR_NAME = "cerebro_sql"  # the one server-side cursor a query reads through
FETCH_ROWS = 50  # rows asked of the cursor at a time: no more than this many are held past the limits
APPLICATION = "cerebro"  # the application_name the server shows for each connection
READ_ONLY = "SET default_transaction_read_only = on"
BEGIN = "BEGIN READ ONLY"
# A role that can read the server's files or run programs there could do so through any function in the database,
# which no check of the query's text can see.
TOO_STRONG = ("SELECT rolsuper OR pg_has_role(current_user, 'pg_read_server_files', 'MEMBER') "
              "OR pg_has_role(current_user, 'pg_write_server_files', 'MEMBER') "
              "OR pg_has_role(current_user, 'pg_execute_server_program', 'MEMBER') "
              "FROM pg_roles WHERE rolname = current_user")
TIMEOUT = "SET statement_timeout = {ms}"  # for the session: the first is set before the transaction begins
ROLLBACK = "ROLLBACK"
ROW_MARK = "cerebro_row_bytes:"
ROW_FACTOR = 4  # a row's text past this many times the size limit is stopped at the server, before it is sent
# The query, wrapped so the server stops a row whose text runs past `{cap}` bytes before it sends one byte of it. The
# cast of text that is not a number is the one way plain SQL raises, and its text is not a constant, so the planner
# never raises it early.
WRAPPED = ("SELECT cerebro_q.* FROM ({query}) AS cerebro_q WHERE (CASE WHEN octet_length(cerebro_q::text) > {cap} "
           "THEN ('" + ROW_MARK + "' || octet_length(cerebro_q::text))::int END) IS NULL")
NONFINITE = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}  # as Postgres spells them
# The SQLSTATE codes read apart from a failure: 22P02 is how the wrapped query stops a row too long.
STATES = {"25006": "write", "57014": "time", "42501": "privilege", "22P02": "text"}


class DriverMissing(DatabaseError):
    """psycopg, the driver a Postgres source reads through, is not installed. The line names the extra."""


def check_postgres(query: Any) -> str:
    """The query, as `check_query` gives it, once it reads as one read both as SQLite and as Postgres read quotes, holds
    no word that writes in Postgres and calls no function in `REFUSED_CALLS`, anywhere in its text, quotes and all.
    Raises `QueryRefused` otherwise."""
    query = check_query(check_query(query), "postgres", also=WRITE_WORDS)
    called = _CALLS.search(query)
    if called:
        raise QueryRefused(f"the query calls {called.group(1).lower()}, which reaches past the tables a sql line "
                           "reads, to files, the network, locks or the server's settings: take it out")
    return query


def driver(label: str) -> Callable[..., Any]:
    """psycopg's `connect`, imported now, each connection reading an infinite date or timestamp as its text, or
    `DriverMissing` naming the extra that installs it."""
    try:
        import psycopg
        from psycopg.types import datetime as dates
    except ImportError:
        raise DriverMissing(f"{label}: a postgres database is read through psycopg, which is not installed: install "
                            "cerebro with its postgres extra, as pip install 'cerebro[postgres]'") from None
    loaders = {name: _finite(base) for name, base in (("date", dates.DateLoader), ("timestamp", dates.TimestampLoader),
                                                      ("timestamptz", dates.TimestamptzLoader))}

    def connect(dsn: str, **options: Any) -> Any:
        db = psycopg.connect(dsn, **options)
        for name, loader in loaders.items():
            db.adapters.register_loader(name, loader)
        return db

    return connect


def _finite(base: type) -> type:
    """A psycopg text loader that gives `infinity` and `-infinity` as text, where Python's dates have no such value
    and the base loader fails the whole query."""

    class Loader(base):  # type: ignore[valid-type, misc]
        def load(self, data: Any) -> Any:
            text = bytes(data)
            return text.decode() if text in (b"infinity", b"-infinity") else super().load(data)

    return Loader


# -- values ---------------------------------------------------------------------------------------------------


def _decimal(value: Decimal) -> int | float | str:
    """A `numeric`: a whole one as an integer; another as a float when the float is the same number; else its exact
    text, so no digit is lost. NaN and the infinities as Postgres spells them."""
    if not value.is_finite():
        return NONFINITE["nan" if value.is_nan() else ("-inf" if value < 0 else "inf")]
    if value == value.to_integral_value():
        return int(value)
    near = float(value)
    return near if Decimal(repr(near)) == value else str(value)


def _plain(value: Any) -> Any:
    """A value inside json or an array, as JSON can hold it."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return cell(value)


def cell(value: Any) -> Any:
    """One value as a record of the SQLite source holds it: None, a number or text.

    - null: None. `boolean`: True or False, which JSON writes as true and false.
    - `smallint`, `integer`, `bigint`, `real`, `double precision`: the number. NaN and the infinities: their text.
    - `numeric`: as `_decimal` says.
    - `timestamp with time zone`: ISO 8601 text in UTC, as 2026-11-17T09:30:00+00:00, whatever the session's zone.
    - `timestamp`, `date`, `time`, `time with time zone`: ISO 8601 text, as the value holds it.
    - `interval`: Python's text of the span, as 1 day, 2:00:00.
    - `json`, `jsonb` and arrays: compact JSON text, each value inside written by these same rules.
    - `bytea`: its length, as <2 bytes>, as a SQLite blob reads.
    - `uuid`, `inet`, `cidr`, ranges, enums and any other type: its text.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else NONFINITE[repr(value)]
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, datetime):
        return (value.astimezone(UTC) if value.tzinfo is not None else value).isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_plain(value), ensure_ascii=False, separators=(",", ":"))
    return str(value)


# -- the engine -----------------------------------------------------------------------------------------------


def _secrets_in(dsn: str) -> list[str]:
    """The connection string, each password and each host written in it, as a URL's or as a `password=`, `host=` or
    `hostaddr=` keyword, so a line can mask them all."""
    found = [dsn]
    if "://" in dsn:
        try:
            parts = urlsplit(dsn)
            password = parts.password
        except ValueError:
            parts, password = None, None
        if password:
            found += [password, unquote(password)]
        if parts is not None:
            hosts = parts.netloc.rpartition("@")[2]
            found += [_host(h) for h in hosts.split(",")]
            found += [v for k, v in parse_qsl(parts.query) if k in ("password", "host", "hostaddr")]
    for match in re.finditer(r"(?:host|hostaddr)\s*=\s*('(?:[^'\\]|\\.)*'|[^\s&]+)", dsn):
        found += match.group(1).strip("'").split(",")
    for match in re.finditer(r"password\s*=\s*('(?:[^'\\]|\\.)*'|[^\s&]+)", dsn):
        value = match.group(1)
        found.append(value[1:-1].replace("\\'", "'").replace("\\\\", "\\") if value.startswith("'") else value)
    return found


def _host(written: str) -> str:
    """The host of one `host:port` in a URL's address, an IPv6 one written in brackets."""
    if written.startswith("["):
        return unquote(written[1:].partition("]")[0])
    return unquote(written.partition(":")[0])


def _quietly(step: Callable[[], Any]) -> None:
    """A step of the way out, after the answer or the failure is known: one that fails changes neither."""
    with contextlib.suppress(Exception):  # the connection may already be gone, and the server rolls back as it closes
        step()


class PostgresEngine(Engine):
    """A Postgres database, opened for each query. `login` reads the connection string from the secrets store, kept
    as `item`; `ticks` is how the time limit reads elapsed seconds; `connect` stands in for psycopg's, imported when
    a query runs if none is given."""

    dialect = "postgres"

    def __init__(self, login: Callable[[], str], label: str, *, item: str = "",
                 ticks: Callable[[], float] = seconds, connect: Callable[..., Any] | None = None):
        self.login, self.label, self.item, self.ticks, self.connect = login, label, item, ticks, connect

    def run(self, query: str, limits: Limits) -> Rows:
        return self._read(check_postgres(query), limits)

    def _read(self, query: str, limits: Limits) -> Rows:
        """The rows of a query already checked: the server's read-only layers and the limits, and nothing else."""
        connect = self.connect if self.connect is not None else driver(self.label)
        dsn = self.login()
        if not dsn.strip():
            raise DatabaseError(f"{self.label}: the connection string kept as {self.item} is empty: keep one with "
                                f"cerebro secrets set {self.item}")
        hide = Scrubber.of(_secrets_in(dsn))
        try:
            db = connect(dsn, autocommit=True, connect_timeout=max(2, math.ceil(limits.seconds)),
                         application_name=APPLICATION)
        except Exception as error:  # noqa: BLE001 - whatever the driver raises holds the string it was handed
            raise DatabaseError(f"{self.label}: the database does not open ({one_line(hide(str(error)))}): check "
                                f"the connection string kept as {self.item}, and that the server is up") from None
        cursor = None
        try:
            deadline = self.ticks() + limits.seconds
            self._time_left(db, deadline, limits)
            db.execute(READ_ONLY)
            db.execute(BEGIN)
            if (db.execute(TOO_STRONG).fetchone() or (True,))[0]:
                raise DatabaseError(f"{self.label}: the connection string kept as {self.item} logs in as a superuser, "
                                    "or a role that may read the server's files or run its programs, which a sql line "
                                    "could reach through any function: make a role that can only read, as the sources "
                                    "docs show, and keep its connection string instead")
            cursor = db.cursor(name=CURSOR_NAME)
            cursor.execute(WRAPPED.format(query=query, cap=ROW_FACTOR * limits.size))
            columns = [str(d[0]) for d in cursor.description or ()]
            return bounded(columns, self._fetch(db, cursor, limits, deadline), limits)
        except CerebroError:
            raise
        except Exception as error:
            if not hasattr(error, "sqlstate"):
                raise
            raise self._failure(error, hide, limits) from None
        finally:
            if cursor is not None:
                _quietly(cursor.close)
            _quietly(lambda: db.execute(ROLLBACK))
            _quietly(db.close)

    def _time_left(self, db: Any, deadline: float, limits: Limits) -> None:
        """The server's timeout for the next statement set to the time the query has left, or the time line when
        none is left."""
        left = deadline - self.ticks()
        if left <= 0:
            raise DatabaseError(f"{self.label}: the query ran past {limits.seconds:g} seconds and was stopped: narrow "
                                "it, or aggregate in the database")
        db.execute(TIMEOUT.format(ms=max(1, round(left * 1000))))

    def _fetch(self, db: Any, cursor: Any, limits: Limits, deadline: float) -> Callable[[int], list[Any]]:
        """`fetch(n)` for `bounded`: the cursor's next rows, asked for `FETCH_ROWS` at a time and never past one row
        more than the limit, each value as `cell` gives it, refused past the size limit or the time limit, which the
        server holds for each fetch too."""
        held: list[Any] = []
        left = [limits.rows + 1]

        def fetch(n: int) -> list[Any]:
            if not held and left[0] > 0:
                self._time_left(db, deadline, limits)
                batch = list(cursor.fetchmany(min(FETCH_ROWS, left[0])))
                left[0] = left[0] - len(batch) if batch else 0
                held.extend(batch)
            out, held[:] = held[:n], held[n:]
            return [self._row(raw, limits) for raw in out]

        return fetch

    def _row(self, raw: Any, limits: Limits) -> list[Any]:
        row = [cell(value) for value in raw]
        if any(isinstance(v, str) and len(v.encode()) > limits.size for v in row):
            raise DatabaseError(f"{self.label}: the query made a value longer than {limits.size} bytes, the most a "
                                "query gives: select a shorter part of it, as substr(notes, 1, 2000)")
        return row

    def _failure(self, error: Exception, hide: Callable[[str], str], limits: Limits) -> CerebroError:
        """The line for what the server refused or failed at, by its SQLSTATE, with the connection string masked."""
        detail = one_line(hide(str(error)))
        state = STATES.get(str(error.sqlstate))  # psycopg's errors all have one, None where the server gave none
        if state == "write":
            return QueryRefused(f"the query would write ({detail}), and a sql line only reads: write a select")
        if state == "time":
            return DatabaseError(f"{self.label}: the query ran past {limits.seconds:g} seconds and was stopped: "
                                 "narrow it, or aggregate in the database")
        if state == "text" and ROW_MARK in str(error):
            return DatabaseError(f"{self.label}: the query made a row longer than {ROW_FACTOR * limits.size} bytes, "
                                 "and the server stopped it before sending it: select a shorter part of each value, as "
                                 "substr(notes, 1, 2000)")
        if state == "privilege":
            return DatabaseError(f"{self.label}: the database's role may not read what the query asks for "
                                 f"({detail}): query what it may read, or ask the database's owner to grant it")
        return DatabaseError(f"{self.label}: the query failed ({detail}): run it by hand against the database to "
                             "see why, and fix it")


def postgres_engine(source: SourceFile, label: str, scheme: str, rest: str, login: Callable[[], str] | None,
                    ticks: Callable[[], float] = seconds) -> PostgresEngine:
    """The engine a source file with `url: postgres` names, or `SourceError` in one line that never repeats the url
    or the auth line, since either may hold a login."""
    if rest.strip():
        raise SourceError(f"{label} url: it holds an address, which may hold a login: write url: {scheme} alone, and "
                          "keep the connection string in the secrets store under the item auth: { keychain: <item> } "
                          "names")
    if not source.auth:
        raise SourceError(f"{label} auth is missing: a {scheme} source reads its connection string from the secrets "
                          f"store: keep it with cerebro secrets set {source.name}, then write auth: {{ keychain: "
                          f"{source.name} }}")
    if not source.auth.startswith("keychain:"):
        raise SourceError(f"{label} auth: a {scheme} source reads its connection string from the secrets store, "
                          "under the item auth: { keychain: <item> } names: write that, and delete any other auth")
    item = source.auth.removeprefix("keychain:").strip()
    try:
        check_name(item)
    except SecretsError:
        raise SourceError(f"{label} auth keychain: the item is not a secret's name, and a source file never holds "
                          "the connection string itself: name it with letters, digits, _ and -, as auth: { "
                          f"keychain: {source.name} }}") from None
    if login is None:
        raise SourceError(f"{label} auth: nothing hands this source the secrets store, so the connection string kept "
                          f"as {item} cannot be read: Cerebro must hand a {scheme} source its login")
    return PostgresEngine(login, label, item=item, ticks=ticks)
