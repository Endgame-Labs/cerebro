"""A database source: one SQL database, read only, that an agent file's `sql` evidence line queries before the model
starts.

Its source file names the database by `url:` and nothing else: `sqlite:<path>` for a SQLite file, the path relative
to the workspace's folder, absolute, or under `~/`, or `postgres` for a Postgres database, read as `postgres.py` says. A
login, for a kind of database that takes one, is kept in the secrets store under the item `auth: { keychain: <item> }`
names, never in the file and never in the url. A SQLite file takes none, and a Postgres database's is its connection
string. The query comes from the agent file alone: a source file says where to read, an agent file what.

Read only is how it opens, not a promise. Before anything runs, `check_query` refuses what is not one `SELECT`, or a
`WITH` that ends in one: a second statement after a semicolon, a comment, an unclosed quote, and any word that
writes, attaches a database or sets a pragma. Then SQLite opens the file in read-only URI mode with `query_only` on
and no room to attach another database, and an authorizer lets the query read tables and call functions alone, so
what the text check missed is refused as SQLite prepares the query, still before it runs. A query gives at most
`ROWS` rows and `SIZE` bytes of them, and is stopped past `SECONDS`. SQLite itself refuses to build any value longer
than `SIZE`, and rows are read one at a time, each counted as it arrives.

Every column name and value comes back through the workspace's mask, so a secret kept in a table never reaches a model.

A database's rows never reach the facts layer, so a database source lists no records: `list`
gives none, with one notice, `get` finds none, and `ask` answers no question.

Each kind of database is an `Engine` that opens it read only and hands its cursor to `bounded`, picked by the url's
scheme in `_engine`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..clock import seconds
from ..connector import Connector, CursorError, Listing, Record, RecordNotFound, one_line
from ..errors import CerebroError
from ..secrets import SecretsError, secrets_dir
from ..source import SourceError, SourceFile

__all__ = ["ROWS", "SECONDS", "SIZE", "DatabaseConnector", "DatabaseError", "Engine", "Limits", "QueryRefused",
           "Rows", "SqliteEngine", "bounded", "check_query", "open_read_only", "row_text"]

ROWS = 500  # rows a query gives at most; one more is read, to tell that more matched
SIZE = 1 << 18  # bytes of rows, as `row_text` writes them, a query gives at most
SECONDS = 10.0  # how long a query may run before it is stopped
QUERY_CHARS = 10_000  # the longest query a sql line may hold
PROGRESS_STEPS = 1000  # SQLite steps between two looks at the time
CURSOR = "database:none"  # the one cursor a database source gives: it lists nothing, now or later
SOURCE_KEYS = ("url", "auth")
# Words a read never needs, outside quotes: each writes, attaches a database, sets a pragma or changes the schema.
# A column of one of these names is written in double quotes.
WRITE_WORDS = ("alter", "attach", "create", "delete", "detach", "drop", "insert", "into", "pragma", "reindex",
               "update", "vacuum")
# Functions that reach outside the query, or the library: never called, even where a build of SQLite has them.
REFUSED_FUNCTIONS = ("edit", "fts3_tokenizer", "load_extension", "readfile", "writefile")
READS = (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE)
_ACTIONS = {sqlite3.SQLITE_INSERT: "insert into", sqlite3.SQLITE_UPDATE: "update", sqlite3.SQLITE_DELETE: "delete from",
            sqlite3.SQLITE_ATTACH: "attach a database", sqlite3.SQLITE_DETACH: "detach a database",
            sqlite3.SQLITE_PRAGMA: "set or read a pragma", sqlite3.SQLITE_TRANSACTION: "open or close a transaction",
            sqlite3.SQLITE_SAVEPOINT: "make a savepoint", sqlite3.SQLITE_ANALYZE: "analyze",
            sqlite3.SQLITE_REINDEX: "reindex", sqlite3.SQLITE_ALTER_TABLE: "alter a table"}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# A Postgres name: a letter, `_` or any character past ASCII, then those, digits and `$`, as its lexer reads one.
_POSTGRES_WORD = re.compile(r"[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_$\u0080-\U0010ffff]*")
# A Postgres dollar quote's opening: `$$`, or a tag between two, such as `$body$`.
_DOLLAR = re.compile(r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")
_CLOSE = {"'": "'", '"': '"', "`": "`", "[": "]"}
_SPACING = re.compile(r"[ \t\n\v\f\r]+")  # spacing as SQL reads it: a space past ASCII is part of a name
EXAMPLE = 'write - sql: { source: warehouse, query: "select kind, count(*) from things group by kind" }'


class QueryRefused(CerebroError, ValueError):
    """A query a `sql` line may not run: not one read. Refused before it runs, in one line that says why."""


class DatabaseError(CerebroError, RuntimeError):
    """A database that could not be read: missing, not a database, a query that failed or ran past its time. The
    line names the source."""


@dataclass
class Limits:
    """How much one query may give and take: rows, bytes of them, and seconds."""

    rows: int = ROWS
    size: int = SIZE
    seconds: float = SECONDS


@dataclass
class Rows:
    """What one query gave: its column names, each once, and its rows, each a list of plain values (None, a number
    or text; a blob reads as its length). `more` says rows past the limit were not read, and `cut` that the rows
    ran past the size limit, so the rest were not read."""

    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    more: bool = False
    cut: bool = False


def row_text(columns: list[str], row: list[Any]) -> str:
    """One row as evidence writes it, and as the size limit counts it: a JSON object of its columns."""
    return json.dumps(dict(zip(columns, row, strict=True)), ensure_ascii=False, default=str)


# -- the query --------------------------------------------------------------------------------------------------


def _scan(query: str, dialect: str = "sqlite") -> tuple[list[str], int | None, list[tuple[int, int]]]:
    """The words of a query outside quotes, in lower case, where a semicolon outside quotes falls, if one does, and
    where each quote starts and ends, its marks and all.
    Refuses a comment, an unclosed quote and a NUL. The `postgres` dialect reads quotes as Postgres does, where it
    differs from SQLite: `[` and a backquote quote nothing; `$$` or `$tag$` opens a quote its twin closes; a backslash
    inside a quote is refused, since an `E''` string reads it as an escape, as a server with standard_conforming_strings
    off does in any string, so with none an `E''` string reads as a plain one; `U&` before a quote is refused, since
    its escapes can spell what a check cannot read; a name may hold `$` and any character past ASCII; and parentheses
    must balance, since the engine wraps the query in its own."""
    postgres = dialect == "postgres"
    words, i, n, depth, quoted = [], 0, len(query), 0, []
    while i < n:
        c = query[i]
        if c == "\0":
            raise QueryRefused("the query holds a NUL character: write it as plain text")
        if postgres and c in "`[":
            i += 1  # Postgres quotes nothing with either
            continue
        dollar = _DOLLAR.match(query, i) if postgres else None
        if dollar:
            j = query.find(dollar.group(), dollar.end())
            if j < 0:
                raise QueryRefused(f"the query opens a quote with {dollar.group()} and never closes it: close it")
            if "\0" in query[i:j]:
                raise QueryRefused("the query holds a NUL character: write it as plain text")
            quoted.append((i, j + len(dollar.group())))
            i = quoted[-1][1]
            continue
        if c in _CLOSE:
            close = _CLOSE[c]
            j = i + 1
            while True:
                j = query.find(close, j)
                if j < 0:
                    raise QueryRefused(f"the query opens a quote with {c} and never closes it: close it")
                if close != "]" and query.startswith(close * 2, j):
                    j += 2  # a doubled quote stands for itself
                    continue
                break
            if "\0" in query[i:j]:
                raise QueryRefused("the query holds a NUL character: write it as plain text")
            if postgres and c == "'" and "\\" in query[i:j]:
                raise QueryRefused("the query holds a backslash inside a quote, which Postgres may read as an escape, "
                                   "as in an E'' string: write the text without one, as chr(10) for a line break")
            quoted.append((i, j + 1))
            i = j + 1
            continue
        if query.startswith(("--", "/*"), i):
            raise QueryRefused("the query holds a comment, which can hide a second statement: write it without one")
        if c == ";":
            break
        if postgres and c in "()":
            depth += 1 if c == "(" else -1
            if depth < 0:
                raise QueryRefused("the query closes a parenthesis it never opened: balance its parentheses")
        word = (_POSTGRES_WORD if postgres else _WORD).match(query, i)
        if word:
            words.append(word.group().lower())
            i = word.end()
            if postgres and words[-1] == "u" and query.startswith(("&'", '&"'), i):
                raise QueryRefused("the query writes text or a name with U&, whose escapes can spell what a check "
                                   "cannot read: write it plainly")
            continue
        i += 1
    if depth:
        raise QueryRefused("the query opens a parenthesis it never closes: balance its parentheses")
    return words, (i if i < n else None), quoted


def check_query(query: Any, dialect: str = "sqlite", also: tuple[str, ...] = ()) -> str:
    """The query a `sql` line may run, as written and without a semicolon that ends it, or `QueryRefused`: it must
    be one `SELECT`, or a `WITH` that ends in one, with no comment, no second statement, and no word that writes,
    attaches a database or sets a pragma, outside quotes. `dialect` is `postgres` for a Postgres engine, which reads
    quotes as `_scan` says, and `also` are more words that write in its language, refused as those are."""
    if not isinstance(query, str) or not query.strip():
        raise QueryRefused(f"the query is empty: {EXAMPLE}")
    if len(query) > QUERY_CHARS:
        raise QueryRefused(f"the query is {len(query)} characters, past the {QUERY_CHARS} a sql line holds: write a "
                           "shorter one, or a view in the database")
    words, semicolon, _ = _scan(query, dialect)
    if semicolon is not None:
        if query[semicolon + 1:].strip():
            raise QueryRefused("the query holds a second statement after a semicolon: write one select")
        query = query[:semicolon]
        if not query.strip():
            raise QueryRefused(f"the query is empty: {EXAMPLE}")
    if not re.match(r"\s*(select|with)\b", query, re.IGNORECASE):
        first = query.split()[0][:20].lower()
        raise QueryRefused(f"the query starts with {first}, and a sql line runs one select, or a with that ends in "
                           "one: write a select")
    for word in words:
        if word in WRITE_WORDS or word in also or word.startswith("pragma_"):
            raise QueryRefused(f"the query holds {word}, and a sql line only reads: take it out, or write it in "
                               "double quotes if it names a column")
    return query.strip()


# -- the engine ---------------------------------------------------------------------------------------------------


class Engine:
    """One kind of database: it runs one checked query read only, within its limits, and gives its rows unmasked.
    SQLite's is here, and Postgres's is in `postgres.py`."""

    dialect = "sqlite"  # how `_scan` reads its quotes

    def run(self, query: str, limits: Limits) -> Rows:
        raise NotImplementedError


def _value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return value


def _unique(columns: list[str]) -> list[str]:
    """Column names, each once: a second `n` is `n 2`, as a row is written as an object of its columns."""
    seen: dict[str, int] = {}
    out = []
    for name in columns:
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name} {seen[name]}")
    return out


def bounded(columns: list[str], fetch: Callable[[int], list[Any]], limits: Limits) -> Rows:
    """The rows a cursor gives through `fetch(n)`, DB-API's `fetchmany`, cut at `limits.rows` rows and `limits.size`
    bytes, with its column names made unique."""
    unique = _unique(columns)
    out, used = Rows(unique), 0
    while len(out.rows) <= limits.rows:
        batch = fetch(1)  # one row at a time, so no more than one row is held past the limits
        if not batch:
            return out
        for raw in batch:
            if len(out.rows) == limits.rows:
                out.more = True
                return out
            row = [_value(v) for v in raw]
            cost = len(row_text(unique, row).encode()) + 1
            if used + cost > limits.size:
                out.cut = True
                return out
            used += cost
            out.rows.append(row)
    return out


def _authorizer(denied: list[str]) -> Callable[..., int]:
    """An authorizer that lets a query read tables and call functions alone, and notes what else it asked for."""

    def authorize(action: int, first: str | None, second: str | None, *_: Any) -> int:
        if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in REFUSED_FUNCTIONS:
            denied.append(f"call {second}")
            return sqlite3.SQLITE_DENY
        if action in READS:
            return sqlite3.SQLITE_OK
        denied.append(_ACTIONS.get(action, "do something other than read"))
        return sqlite3.SQLITE_DENY

    return authorize


def open_read_only(path: Path, seconds: float = SECONDS, longest: int = SIZE) -> sqlite3.Connection:
    """A connection to the SQLite file at `path` that cannot write it: read-only URI mode, `query_only` on, and no
    other database attached. The path goes in as a file URI, so nothing in it adds a parameter. No string or blob,
    the query's own text and every value it builds on the way included, may pass `longest` bytes, so one huge value
    is refused by SQLite before it is built, not cut after."""
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=seconds, isolation_level=None)
    try:
        db.execute("pragma query_only = on")
        db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max(longest, QUERY_CHARS * 4))
    except sqlite3.Error:
        db.close()
        raise
    return db


class SqliteEngine(Engine):
    """A SQLite file, opened read only for each query. `ticks` is how the time limit reads elapsed seconds."""

    def __init__(self, path: Path, label: str, ticks: Callable[[], float] = seconds):
        self.path, self.label, self.ticks = path, label, ticks

    def run(self, query: str, limits: Limits) -> Rows:
        if not self.path.is_file():
            raise DatabaseError(f"{self.label}: the database file it names does not exist: make it, or fix the url")
        try:
            with self.path.open("rb") as file:
                head = file.read(20)
        except OSError as error:
            raise DatabaseError(f"{self.label}: the database file does not read ({error.strerror}): check its "
                                "permissions") from None
        if head.startswith(b"SQLite format 3\x00") and 2 in (head[18], head[19]):
            raise DatabaseError(f"{self.label}: the database is in write-ahead mode, and reading it would create "
                                "files beside it: run pragma journal_mode=delete on it, or point url at a copy")
        denied: list[str] = []
        stopped: list[bool] = []
        try:
            db = open_read_only(self.path, limits.seconds, limits.size)
        except sqlite3.Error as error:
            raise DatabaseError(f"{self.label}: the database does not open ({one_line(error)}): check the file it "
                                "names is a SQLite database") from None
        try:
            db.set_authorizer(_authorizer(denied))
            deadline = self.ticks() + limits.seconds

            def progress() -> int:
                if self.ticks() > deadline:
                    stopped.append(True)
                    return 1
                return 0

            db.set_progress_handler(progress, PROGRESS_STEPS)
            cursor = db.execute(query)
            columns = [d[0] for d in cursor.description or ()]
            return bounded(columns, cursor.fetchmany, limits)
        except sqlite3.Error as error:
            if denied:
                raise QueryRefused(f"the query would {denied[0]}, and a sql line only reads: write a select") from None
            if stopped:
                raise DatabaseError(f"{self.label}: the query ran past {limits.seconds:g} seconds and was stopped: "
                                    "narrow it, or aggregate in the database") from None
            if "too big" in str(error):
                raise DatabaseError(f"{self.label}: the query made a value longer than {limits.size} bytes, the most "
                                    "a query gives: select a shorter part of it, as substr(notes, 1, 2000)") from None
            raise DatabaseError(f"{self.label}: the query failed ({one_line(error)}): run it by hand against the "
                                "database to see why, and fix it") from None
        finally:
            db.close()


# -- the source ---------------------------------------------------------------------------------------------------

def _owns(path: Path, own: Path) -> bool:
    """Whether a resolved path is one of the workspace's own files: `own` itself, a file under it, or a file SQLite
    keeps beside it, such as its `-wal` and `-journal`."""
    own = own.resolve()
    return path == own or path.is_relative_to(own) or (path.parent == own.parent
                                                       and path.name.startswith(own.name + "-"))


def _engine(source: SourceFile, root: Path, label: str, login: Callable[[], str] | None,
            ticks: Callable[[], float], own: Iterable[Path] = ()) -> Engine:
    """The engine a source file's url names. The url is never repeated in a line, since it may hold a login."""
    url = source.url.strip()
    if not url:
        raise SourceError(f"{label} url is missing: name the database, as url: sqlite:data/warehouse.sqlite")
    scheme, colon, rest = url.partition(":")
    scheme = scheme.lower()
    if scheme in ("postgres", "postgresql"):
        from .postgres import postgres_engine  # it imports this module, and its driver only when a query runs

        return postgres_engine(source, label, scheme, rest, login, ticks)
    if colon and scheme in ("mysql", "mariadb", "sqlserver", "mssql"):
        raise SourceError(f"{label} url: Cerebro reads SQLite and Postgres, not {scheme}: point url at one, "
                          "as url: sqlite:data/warehouse.sqlite")
    if not colon or scheme != "sqlite":
        raise SourceError(f"{label} url: write sqlite:<path>, as url: sqlite:data/warehouse.sqlite")
    if source.auth or login is not None:
        raise SourceError(f"{label} auth: a SQLite file takes no login: delete auth")
    if rest.startswith("//"):
        raise SourceError(f"{label} url: a SQLite file is named by its path, with no host: write sqlite:<path>, as "
                          "sqlite:data/warehouse.sqlite or sqlite:/srv/warehouse.sqlite")
    if "?" in rest or "#" in rest:
        raise SourceError(f"{label} url: a SQLite url takes no parameters, since it always opens read only: delete "
                          "what follows ? or #")
    if not rest.strip() or rest.strip() == ":memory:":
        raise SourceError(f"{label} url: name a SQLite file to read, as url: sqlite:data/warehouse.sqlite")
    path = Path(rest).expanduser() if rest.startswith("~/") else Path(rest)
    path = (path if path.is_absolute() else root / path).resolve()
    base = root.resolve()
    if path.is_relative_to(base) and any(p.startswith(".") for p in path.relative_to(base).parts):
        raise SourceError(f"{label} url: it names a file in a hidden folder of the workspace, such as its own state, "
                          "which a source never reads: move the database to a visible folder, such as data/")
    if ".cerebro" in path.parts:
        raise SourceError(f"{label} url: it names a file under a .cerebro folder, a workspace's own state, which a source "
                          "never reads: point it at a database of your own, such as data/warehouse.sqlite")
    try:
        config = secrets_dir().parent
    except SecretsError:
        config = None
    if config is not None and path.is_relative_to(config.resolve()):
        raise SourceError(f"{label} url: it names a file in cerebro's own config folder, where keys and logins are "
                          "kept, which a source never reads: point it at a database of your own")
    if any(_owns(path, mine) for mine in own):
        raise SourceError(f"{label} url: it names the workspace's own store or the facts layer's files, which a source "
                          "never reads: point it at a database of your own, such as data/warehouse.sqlite")
    return SqliteEngine(path, label, ticks)


class DatabaseConnector(Connector):
    """A database source, read by `sql` lines alone. `login` reads the source's login from the secrets store, for a
    kind of database that takes one; `mask` masks every value read from the secrets store, and `masks`, when given,
    gives a mask for each query of every value the store holds; `ticks` is how the time
    limit reads elapsed seconds, so a test can stand in for it. `own` are the workspace's own files, its store and the
    facts layer's, which the url may never name, by any path or link: the app hands them in."""

    lists_nothing = "a database source lists no records: an agent reads it with a sql evidence line"

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> DatabaseConnector:
        """Every value it gives masked, and never one that names the workspace's own files. A source with an auth line
        is handed its login, which a SQLite file refuses and a Postgres database reads."""
        login = app.login(source) if source.auth else None
        return cls(source, root, login=login, mask=app.mask, masks=app.mask_every, own=app.own_files())

    def __init__(self, source: SourceFile, root: Path, *, login: Callable[[], str] | None = None,
                 mask: Callable[[str], str] | None = None, limits: Limits | None = None,
                 ticks: Callable[[], float] = seconds, engine: Engine | None = None,
                 own: Iterable[Path] = (), masks: Callable[[], Callable[[str], str]] | None = None):
        self.name = source.name
        self.label = f"sources/{source.name}.yaml"
        self.engine = engine or _engine(source, root, self.label, login, ticks, own)
        self.mask = mask or (lambda text: text)
        self.masks = masks  # gives a mask afresh for each query, as of every value the secrets store holds then
        self.limits = limits or Limits()

    @staticmethod
    def _masked(value: Any, mask: Callable[[str], str]) -> Any:
        if value is None:
            return None
        text = str(value)
        masked = mask(text)
        return value if masked == text else masked

    def select(self, query: str) -> Rows:
        """The rows of one query, read only, within the limits, each name and value masked. Raises `QueryRefused`
        before it runs for anything but one read, and `DatabaseError` when the database cannot answer."""
        mask = self.masks() if self.masks is not None else self.mask
        try:
            rows = self.engine.run(check_query(query), self.limits)
        except CerebroError as error:
            error.args = (mask(str(error)),)
            raise
        return Rows(_unique([str(self._masked(c, mask)) for c in rows.columns]),
                    [[self._masked(v, mask) for v in row] for row in rows.rows], rows.more, rows.cut)

    def folded(self, query: str) -> str:
        """The query as its rows' ids read it: as `check_query` gives it, each run of spacing outside its quotes, as
        this source's engine reads them, one space. Spaced apart, a query keeps its ids, and no other gets them."""
        query = check_query(query, self.engine.dialect)
        out, at = [], 0
        for start, end in [*_scan(query, self.engine.dialect)[2], (len(query), len(query))]:
            out += [_SPACING.sub(" ", query[at:start]), query[start:end]]
            at = end
        return "".join(out)

    def list(self, since: str | None = None) -> Listing:
        if since not in (None, CURSOR):
            raise CursorError(f"source {self.name}: cursor {since!r} is not one this source gave: list from the "
                              "start")
        return Listing([], CURSOR, notices=[f"{self.label}: {self.lists_nothing}"])

    def get(self, id: str) -> Record:
        raise RecordNotFound(f"source {self.name} holds no records: a database source is read by a sql evidence line")
