"""The database source and the `sql` line: an agent file's one read of a SQLite file a test makes, refused before it
runs unless it is one select, opened so it cannot write, bounded, masked, cited and counted in the digest. Every
database here is a file the test makes under its own tmp_path, with invented regions and round amounts, and each
statement, allowed or refused, leaves the file's bytes as they were."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from cerebro.agent import Agent, AgentFileError, load_agent
from cerebro.app import Masked
from cerebro.connector import CursorError, QuestionError, RecordNotFound
from cerebro.connectors.database import (
    CURSOR,
    DatabaseConnector,
    DatabaseError,
    Limits,
    QueryRefused,
    SqliteEngine,
    check_query,
    open_read_only,
)
from cerebro.connectors.files import FilesConnector
from cerebro.evidence import SqlError, digest, gather, render
from cerebro.lookup import Lookup
from cerebro.secrets import MARK, Mask
from cerebro.source import SourceError, SourceFile, load_sources, read_source

DEALS = [("North", 1000, "open"), ("North", 2000, "won"), ("South", 3000, "open"), ("East", 4000, "open")]
PIPELINE = "select region, sum(amount) from deals group by region"
SOURCE = "kind: database\nurl: sqlite:data/warehouse.sqlite\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A workspace folder whose data/warehouse.sqlite holds invented deals, and whose
    sources/warehouse.yaml names it."""
    assert tmp_path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    (tmp_path / "data").mkdir()
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "warehouse.yaml").write_text(SOURCE)
    db = sqlite3.connect(tmp_path / "data" / "warehouse.sqlite")
    db.execute("create table deals (region text, amount integer, stage text)")
    db.executemany("insert into deals values (?, ?, ?)", DEALS)
    db.commit()
    db.close()
    return tmp_path


def file_of(root: Path) -> Path:
    return root / "data" / "warehouse.sqlite"


@contextmanager
def unchanged(root: Path) -> Iterator[None]:
    """The database file's bytes, and the folder that holds it, are the same after the block as before it: no write,
    and no journal, WAL or other file left beside it."""
    path = file_of(root)
    before, beside = path.read_bytes(), sorted(p.name for p in path.parent.iterdir())
    yield
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == beside


def warehouse(root: Path, **kwargs: object) -> DatabaseConnector:
    found = load_sources(root, {"database": lambda source, at: DatabaseConnector(source, at, **kwargs)})
    assert found.refused == [] and found.notices == []
    (connector,) = found.connectors
    assert isinstance(connector, DatabaseConnector)
    return connector


def agent(root: Path, front: str, rel: str = "reports/pipeline.md") -> Agent:
    path = root / "agents" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n\n# Instructions\n\nWrite the pipeline report, by region. Cite the rows you "
                    "use.\n")
    return load_agent(path, root / "agents")


def pipeline(root: Path, query: str = PIPELINE) -> Agent:
    return agent(root, f'evidence:\n  - sql: {{ source: warehouse, query: "{query}" }}\nheartbeat: [weekly]\n')


# -- the main path ------------------------------------------------------------------------------------------------


def test_an_agent_file_with_a_sql_line_gathers_its_rows_as_cited_data_and_the_file_is_unchanged(root: Path) -> None:
    writer = pipeline(root)
    with unchanged(root):
        # the app hands in its sources wrapped in its mask, which passes select through
        (entry,) = gather(writer, Lookup(), {}, root=root, sources={"warehouse": Masked(warehouse(root), lambda: str)})
    rows = ['{"region": "East", "sum(amount)": 4000}', '{"region": "North", "sum(amount)": 3000}',
            '{"region": "South", "sum(amount)": 3000}']
    assert entry.heading == f"Sql warehouse, the rows of {PIPELINE}"
    assert entry.lines == [f"- [^sql:warehouse:e3d5d721:{i}] {row}" for i, row in enumerate(rows, 1)]
    # each row is its citation's quote, so the article's footnote shows the row a sentence rests on
    assert [(c.kind, c.id, c.source, c.quote) for c in entry.citations] == [
        ("record", f"sql:warehouse:e3d5d721:{i}", "sql:warehouse", row) for i, row in enumerate(rows, 1)]
    assert "## Sql warehouse" in render([entry]) and writer.notice == ""
    (wide,) = gather(pipeline(root, "select hex(zeroblob(200)) as pad"), Lookup(), {}, root=root,
                     sources={"warehouse": warehouse(root)})
    assert wide.lines[0].endswith("0" * 400 + '"}') and len(wide.citations[0].quote) <= 301  # cut as a fact's quote


def test_two_sql_lines_on_one_source_each_give_their_rows_and_one_query_spaced_apart_keeps_its_ids(root: Path) -> None:
    queries = [PIPELINE, "select 'one  two' as name", "select 'one two' as name"]  # the last two alike once spaces fold
    writer = agent(root, "evidence:\n" + "".join(f'  - sql: {{ source: warehouse, query: "{q}" }}\n' for q in queries))
    first, spaced, single = gather(writer, Lookup(), {}, root=root, sources={"warehouse": warehouse(root)})
    assert len(first.lines) == 3 and [spaced.lines, single.lines] == [
        ['- [^sql:warehouse:fd245967:1] {"name": "one  two"}'], ['- [^sql:warehouse:af3b59a1:1] {"name": "one two"}']]
    (apart,) = gather(pipeline(root, PIPELINE.replace(" ", "\\n  ")), Lookup(), {}, root=root,
                      sources={"warehouse": warehouse(root)})
    assert apart.lines == first.lines


def test_a_changed_row_moves_the_digest_and_an_unchanged_database_does_not(root: Path) -> None:
    writer, sources = pipeline(root), {"warehouse": warehouse(root)}
    with unchanged(root):
        first = digest(writer, gather(writer, Lookup(), {}, root=root, sources=sources))
        assert digest(writer, gather(writer, Lookup(), {}, root=root, sources=sources)) == first
    db = sqlite3.connect(file_of(root))  # the outside system changing, as a test of its own
    db.execute("update deals set amount = 5000 where region = 'East'")
    db.commit()
    db.close()
    with unchanged(root):
        assert digest(writer, gather(writer, Lookup(), {}, root=root, sources=sources)) != first


def test_a_caller_that_hands_in_no_sources_gets_a_line_that_names_the_source_it_lacks(root: Path) -> None:
    with unchanged(root), pytest.raises(SqlError, match=r"evidence 1, sql: the workspace has no source warehouse"):
        gather(pipeline(root), Lookup(), {}, root=root)


# -- read only: refused before it runs --------------------------------------------------------------------------

REFUSED = [
    ("select region from deals; delete from deals", "a second statement after a semicolon"),
    ("SeLeCt 1;DeLeTe FROM deals;", "a second statement after a semicolon"),
    ("select 1; ; select 2", "a second statement after a semicolon"),
    ("select 'it''s' ; delete from deals", "a second statement after a semicolon"),
    ("select 'a';delete from deals", "a second statement after a semicolon"),
    ("select 'a'; delete from deals where region = 'b'", "a second statement after a semicolon"),
    ("select \"a\"\"b\", 'c'; delete from deals", "a second statement after a semicolon"),
    ("select 1;;", "a second statement after a semicolon"),
    ("select 1 --\n; delete from deals", "a comment"),
    ("select 1 -- a note", "a comment"),
    ("select 1 /* ; drop table deals */", "a comment"),
    ("select ';' /*", "a comment"),
    ("attach database 'copy.sqlite' as copy", "starts with attach"),
    ("select * from deals where 1 = (attach 'copy.sqlite' as copy)", "holds attach"),
    ("pragma writable_schema = 1", "starts with pragma"),
    ("select * from pragma_table_info('deals')", "holds pragma_table_info"),
    ("with gone as (delete from deals returning *) select * from gone", "holds delete"),
    ("with x as (select 1) delete from deals", "holds delete"),
    ("with x as (select 1) insert into deals values ('West', 1000, 'open')", "holds insert"),
    ("with x as (select 1) replace into deals values ('West', 1000, 'open')", "holds into"),
    ("with x as (select 1) update deals set amount = 0", "holds update"),
    ("insert into deals values ('West', 1000, 'open')", "starts with insert"),
    ("update deals set amount = 0", "starts with update"),
    ("vacuum into 'copy.sqlite'", "starts with vacuum"),
    ("values (1)", "starts with values"),
    ("(select 1)", "starts with ("),
    ("select 'unclosed", "never closes it"),
    ('select "unclosed', "never closes it"),
    ("select 1\0; delete from deals", "a NUL character"),
    ("select '\0'", "a NUL character"),
    ("", "the query is empty"),
    (" ; ", "the query is empty"),
    ("   ", "the query is empty"),
    ("select " + "1, " * 5000 + "1", "past the 10000 a sql line holds"),
]


@pytest.mark.parametrize(("query", "why"), REFUSED)
def test_a_query_that_is_not_one_read_is_refused_before_it_runs_and_the_file_is_unchanged(
        root: Path, query: str, why: str) -> None:
    connector = warehouse(root)
    with unchanged(root), pytest.raises(QueryRefused) as refused:
        connector.select(query)
    assert why in str(refused.value) and "\n" not in str(refused.value)


@pytest.mark.parametrize(("query", "why"), [r for r in REFUSED if "\0" not in r[0] and len(r[0]) < 200])
def test_an_agent_file_whose_sql_line_is_not_one_read_is_refused_when_it_loads(root: Path, query: str,
                                                                               why: str) -> None:
    quoted = query.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    with pytest.raises(AgentFileError) as refused:
        pipeline(root, quoted)
    assert str(refused.value).startswith("agents/reports/pipeline.md: line 3: evidence 1, sql query: ") and why in str(
        refused.value)


ALLOWED = [
    ("select region, ';' as mark, '-- no comment' as dash, '/* none */' from deals where region = 'North;'", []),
    ('select region as "update", stage as "into" from deals where amount = 4000;', [["East", "open"]]),
    ("  with won as (select * from deals where stage = 'won') select region, amount from won  ;  ",
     [["North", 2000]]),
    ("with recursive n(x) as (select 1 union all select x + 1 from n where x < 3) select x from n",
     [[1], [2], [3]]),
    ("select [region] from deals where region = 'it''s' or amount = 3000", [["South"]]),
    ("select region from deals where region = 'a''; delete from deals; --' or region = ''''", []),
    ("SELECT region FROM deals WHERE amount = 1000", [["North"]]),
]


@pytest.mark.parametrize(("query", "rows"), ALLOWED)
def test_a_read_with_semicolons_comments_and_write_words_inside_quotes_runs_and_the_file_is_unchanged(
        root: Path, query: str, rows: list[list[object]]) -> None:
    connector = warehouse(root)
    with unchanged(root):
        assert connector.select(query).rows == rows


@pytest.mark.parametrize(("query", "would"), [
    ("insert into deals values ('West', 1000, 'open')", "insert into"),
    ("with x as (select 1) delete from deals", "delete from"),
    ("update deals set amount = 0", "update"),
    ("pragma writable_schema = 1", "set or read a pragma"),
    ("select * from pragma_table_info('deals')", "set or read a pragma"),
    ("attach database ':memory:' as other", "attach a database"),
    ("begin", "open or close a transaction"),
    ("select load_extension('nothing')", "call load_extension"),
    ("drop table deals", "delete from"),  # a change of schema writes the schema table first
    ("create table other (x)", "insert into"),
])
def test_what_the_text_check_missed_sqlite_refuses_as_it_prepares_the_query_and_the_file_is_unchanged(
        root: Path, query: str, would: str) -> None:
    engine = SqliteEngine(file_of(root).resolve(), "sources/warehouse.yaml")
    if would == "call load_extension":  # a build of SQLite without the function refuses it as unknown instead
        with unchanged(root), pytest.raises((QueryRefused, DatabaseError)) as refused:
            engine.run(query, Limits())
        assert (f"the query would {would}, and a sql line only reads" in str(refused.value)
                if isinstance(refused.value, QueryRefused)
                else "no such function: load_extension" in str(refused.value))
        return
    with unchanged(root), pytest.raises(QueryRefused, match=f"the query would {would}, and a sql line only reads"):
        engine.run(query, Limits())


def test_the_connection_itself_cannot_write_or_attach_even_with_no_authorizer(root: Path) -> None:
    with unchanged(root):
        db = open_read_only(file_of(root).resolve())
        try:
            assert db.execute("pragma query_only").fetchone() == (1,)
            for statement in ("insert into deals values ('West', 1000, 'open')", "delete from deals",
                              "create table other (x)", "attach database ':memory:' as other"):
                with pytest.raises(sqlite3.Error):
                    db.execute(statement)
            db.execute("pragma query_only = off")  # even with query_only off, the file is opened read only
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute("delete from deals")
        finally:
            db.close()


# -- an agent file names a source, never an address or a login ------------------------------------------------


@pytest.mark.parametrize(("line", "why"), [
    ('{ source: warehouse, query: "select 1", url: "sqlite:data/other.sqlite" }', "it names url"),
    ('{ source: warehouse, query: "select 1", login: reader }', "it names login"),
    ('{ source: warehouse, query: "select 1", password: hunter-two-two }', "it names password"),
    ('{ source: warehouse, query: "select 1", auth: { keychain: warehouse } }', "it names auth"),
    ('{ source: "sqlite:data/warehouse.sqlite", query: "select 1" }', "is not the name of a source"),
    ('{ source: "../data/warehouse", query: "select 1" }', "is not the name of a source"),
    ('{ source: "/srv/warehouse.sqlite", query: "select 1" }', "is not the name of a source"),
    ('{ source: warehouse }', "it takes source and query"),
    ('"select 1"', "it takes source and query"),
])
def test_an_agent_file_cannot_name_an_address_or_a_login(root: Path, line: str, why: str) -> None:
    with pytest.raises(AgentFileError) as refused:
        agent(root, f"evidence:\n  - sql: {line}\n")
    assert why in str(refused.value) and "hunter-two-two" not in str(refused.value)


def test_a_sql_line_reads_only_a_database_source_the_caller_hands_in_by_name(root: Path) -> None:
    writer = pipeline(root)
    with unchanged(root):
        with pytest.raises(SqlError, match=r"^agents/reports/pipeline.md: evidence 1, sql: the workspace has no source "
                                           r"warehouse: add sources/warehouse.yaml with kind: database"):
            gather(writer, Lookup(), {}, root=root, sources={})
        files = FilesConnector(SourceFile("warehouse", "files"), root)
        with pytest.raises(SqlError, match="source warehouse is not a database source"):
            gather(writer, Lookup(), {}, root=root, sources={"warehouse": files})


# -- bounds, masks, failures ------------------------------------------------------------------------------------


def test_rows_size_and_time_are_bounded_with_a_note_that_says_what_was_not_read(root: Path) -> None:
    sources = {"warehouse": warehouse(root, limits=Limits(rows=2))}
    with unchanged(root):
        (entry,) = gather(pipeline(root, "select region, amount from deals"), Lookup(), {}, root=root,
                          sources=sources)
    assert len(entry.items) == 2 and entry.notes == [
        "it gave more than 2 rows, so the rest were not read: aggregate or filter in the query"]
    sized = warehouse(root, limits=Limits(size=72))
    with unchanged(root):
        rows = sized.select("select region, amount from deals")
    assert rows.cut and not rows.more and rows.rows == [["North", 1000], ["North", 2000]]
    clock = iter(range(1_000_000))
    slow = warehouse(root, limits=Limits(seconds=3), ticks=lambda: float(next(clock)))
    with unchanged(root), pytest.raises(DatabaseError, match="ran past 3 seconds and was stopped"):
        slow.select("with recursive n(x) as (select 1 union all select x + 1 from n) select count(*) from n")
    assert 3 < next(clock) < 10  # the limit read the stand-in's seconds, not the machine's
    still = warehouse(root, limits=Limits(seconds=3), ticks=lambda: 100.0)  # a long query within its time ends
    with unchanged(root):
        assert still.select("with recursive n(x) as (select 1 union all select x + 1 from n where x < 50000) "
                            "select count(*) from n").rows == [[50000]]


def test_one_huge_value_is_refused_by_the_database_before_it_is_built(root: Path) -> None:
    with unchanged(root), pytest.raises(DatabaseError, match="made a value longer than 262144 bytes"):
        warehouse(root).select("select length(replace(hex(zeroblob(200000)), '0', '00')) as n")


def test_every_name_value_and_failure_line_comes_back_masked(root: Path) -> None:
    token = "tok-9f8e7d6c5b4a"
    db = sqlite3.connect(file_of(root))
    db.execute("create table notes (region text, body text, blob blob)")
    db.execute("insert into notes values ('West', ?, x'00ff')", (f"key {token} pasted",))
    db.commit()
    db.close()
    mask = Mask()
    mask.add(token)
    connector = warehouse(root, mask=mask)
    with unchanged(root):
        rows = connector.select(f"select region, body, blob, region as \"{token}\", 1 as n, 2 as n from notes")
        assert rows.columns == ["region", "body", "blob", MARK, "n", "n 2"]
        assert rows.rows == [["West", f"key {MARK} pasted", "<2 bytes>", "West", 1, 2]]
        with pytest.raises(DatabaseError) as failed:
            connector.select(f'select * from "{token}"')
    assert token not in str(failed.value) and MARK in str(failed.value)


def test_a_failing_query_stops_the_gather_with_one_line_that_names_the_file_and_the_source(root: Path) -> None:
    writer = pipeline(root, "select * from nothing_here")
    with unchanged(root), pytest.raises(SqlError) as failed:
        gather(writer, Lookup(), {}, root=root, sources={"warehouse": warehouse(root)})
    assert str(failed.value) == (
        "agents/reports/pipeline.md: evidence 1, sql warehouse: sources/warehouse.yaml: the query failed (no such "
        "table: nothing_here): run it by hand against the database to see why, and fix it")


def test_a_missing_file_or_one_that_is_not_a_database_is_read_as_neither_and_nothing_is_made(root: Path) -> None:
    (root / "sources" / "warehouse.yaml").write_text("kind: database\nurl: sqlite:data/missing.sqlite\n")
    with pytest.raises(DatabaseError, match="the database file it names does not exist"):
        warehouse(root).select("select 1")
    assert not (root / "data" / "missing.sqlite").exists()
    (root / "data" / "notes.sqlite").write_text("North sold 1000.\n")
    (root / "sources" / "warehouse.yaml").write_text("kind: database\nurl: sqlite:data/notes.sqlite\n")
    with pytest.raises(DatabaseError, match="sources/warehouse.yaml: the query failed .*not a database"):
        warehouse(root).select("select 1")
    assert (root / "data" / "notes.sqlite").read_text() == "North sold 1000.\n"


# -- the source file ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "why"), [
    ("kind: database\n", "url is missing"),
    ("kind: database\nurl: postgres://reader:pw-hunter-two@db.example/sales\n",
     "url: it holds an address"),
    ("kind: database\nurl: mysql://db.example/sales\n", "Cerebro reads SQLite and Postgres, not mysql"),
    ("kind: database\nurl: data/warehouse.sqlite\n", "write sqlite:<path>"),
    ("kind: database\nurl: \"file:data/warehouse.sqlite\"\n", "write sqlite:<path>"),
    ("kind: database\nurl: sqlite:data/warehouse.sqlite\nretention: { mode: transient }\n",
     "line 3: retention is not a key a source of kind database reads"),
    ("kind: database\nurl: sqlite://db.example/warehouse.sqlite\n", "with no host"),
    ("kind: database\nurl: \"sqlite:data/warehouse.sqlite?mode=rw\"\n", "takes no parameters"),
    ("kind: database\nurl: \"sqlite:data/warehouse.sqlite#x\"\n", "takes no parameters"),
    ("kind: database\nurl: \"sqlite::memory:\"\n", "name a SQLite file to read"),
    ("kind: database\nurl: \"sqlite:\"\n", "name a SQLite file to read"),
    ("kind: database\nurl: sqlite:.cerebro/cerebro.sqlite\n", "a hidden folder of the workspace"),
    ("kind: database\nurl: sqlite:data/warehouse.sqlite\nauth: { keychain: warehouse }\n", "takes no login"),
    ("kind: database\nurl: sqlite:data/warehouse.sqlite\nwindow: { behind: 30d }\n", "line 3: window belongs to a source of kind"),
    ("kind: database\nurl: sqlite:data/warehouse.sqlite\nselect: [{ glob: \"*.csv\" }]\n", "select belongs to a source of kind"),
    ("kind: database\nurl: sqlite:data/warehouse.sqlite\nextract: facts\n", "extract belongs to a source of kind"),
    ("kind: database\nurl: sqlite:data/warehouse.sqlite\npassword: pw-hunter-two\n", "line 3: password is not a key a source"),
])
def test_a_source_file_that_does_not_name_one_sqlite_file_to_read_is_refused_alone(root: Path, text: str,
                                                                                   why: str) -> None:
    (root / "sources" / "warehouse.yaml").write_text(text)
    found = load_sources(root, {"database": DatabaseConnector})
    assert found.connectors == [] and len(found.refused) == 1
    assert found.refused[0].startswith("sources/warehouse.yaml ") and why in found.refused[0]
    assert "pw-hunter-two" not in found.refused[0] and "reader" not in found.refused[0]


def test_a_path_may_be_absolute_or_under_home_and_a_source_of_another_kind_is_unchanged(root: Path) -> None:
    where = file_of(root).resolve()
    (root / "sources" / "warehouse.yaml").write_text(f"kind: database\nurl: sqlite:{where}\n")
    with unchanged(root):
        assert warehouse(root).select("select count(*) from deals").rows == [[4]]
    source = replace(read_source(root / "sources" / "warehouse.yaml"), url="sqlite:~/no/such.sqlite")
    engine = DatabaseConnector(source, root).engine
    assert isinstance(engine, SqliteEngine) and engine.path == (Path.home() / "no" / "such.sqlite").resolve()


def test_a_database_source_lists_no_records_with_one_notice_and_finds_none(root: Path) -> None:
    connector = warehouse(root)
    assert connector.name == "warehouse"
    listing = connector.list()
    assert listing.records == [] and listing.removed == [] and not listing.more and listing.cursor == CURSOR
    assert listing.notices == [("sources/warehouse.yaml: a database source lists no records: an agent reads it "
                                "with a sql evidence line")]
    assert connector.list(listing.cursor).records == []
    with pytest.raises(CursorError):
        connector.list("not-a-cursor-this-source-gave")
    with pytest.raises(RecordNotFound):
        connector.get("sql:warehouse:1")
    with pytest.raises(QuestionError):
        connector.ask("rows", {"query": "select 1"})
    assert connector.questions == {}


def test_a_login_handed_to_a_sqlite_source_is_refused(root: Path) -> None:
    with pytest.raises(SourceError, match="a SQLite file takes no login: delete auth"):
        DatabaseConnector(read_source(root / "sources" / "warehouse.yaml"), root, login=lambda: "reader-login")


def test_check_query_gives_the_query_without_its_last_semicolon() -> None:
    assert check_query("  select 1 ;  ") == "select 1"
    longest = "select " + "1" * (10_000 - len("select "))
    assert check_query(longest) == longest
    assert check_query("with a as (select 1) select * from a") == "with a as (select 1) select * from a"


# -- the workspace's own files: never a database source's, by any path or link ---------------------------------

def _refused(root: Path, url: str, settings: str = "timezone: UTC\n") -> list[str]:
    from cerebro.app import Cerebro
    from cerebro.fakes import FakeExtraction

    (root / "settings.yaml").write_text(settings)
    (root / "sources" / "warehouse.yaml").write_text(f"kind: database\nurl: \"sqlite:{url}\"\n")
    with Cerebro(root, extraction=FakeExtraction()) as workspace:
        return [line for line in workspace.refused if line.startswith("sources/warehouse.yaml")]


@pytest.mark.parametrize(("settings", "url"), [
    ("timezone: UTC\n", "{root}/.cerebro/cerebro.sqlite"),  # the default store, by its absolute path
    ("timezone: UTC\nstore: { path: data/cerebro.sqlite }\n", "data/cerebro.sqlite"),  # the store where settings move it
    ("timezone: UTC\nstore: { path: data/cerebro.sqlite }\n", "data/cerebro.sqlite-wal"),  # and the file beside it
    ("timezone: UTC\nstore: { path: data/cerebro.sqlite }\n", "data/../data/cerebro.sqlite"),  # by a longer way round
    ("timezone: UTC\nstore: { path: '{outside}/cerebro.sqlite' }\n", "{outside}/cerebro.sqlite"),  # outside the folder
    ("timezone: UTC\n", "facts/kinds.sqlite"),  # the facts layer's project
    ("timezone: UTC\n", "{root}/.cerebro/factoid/facts.sqlite"),  # and its data
])
def test_a_database_source_never_names_the_workspaces_own_store_or_the_facts_layers_files(
        root: Path, tmp_path_factory: pytest.TempPathFactory, settings: str, url: str) -> None:
    outside = tmp_path_factory.mktemp("elsewhere")
    fill = {"root": str(root.resolve()), "outside": str(outside.resolve())}
    refused = _refused(root, url.format(**fill), settings.replace("{outside}", fill["outside"]))
    assert len(refused) == 1 and ("the workspace's own store or the facts layer's files" in refused[0]
                                  or "a hidden folder of the workspace" in refused[0]), refused


def test_a_link_cannot_reach_the_store_from_a_visible_folder(root: Path) -> None:
    (root / ".cerebro").mkdir()
    (root / ".cerebro" / "cerebro.sqlite").write_bytes(b"")
    (root / "data" / "link.sqlite").symlink_to(root / ".cerebro" / "cerebro.sqlite")
    assert len(_refused(root, "data/link.sqlite")) == 1
    moved = "timezone: UTC\nstore: { path: data/kept/cerebro.sqlite }\n"
    (root / "data" / "kept").mkdir()
    (root / "data" / "kept" / "cerebro.sqlite").write_bytes(b"")
    (root / "data" / "moved.sqlite").symlink_to(root / "data" / "kept" / "cerebro.sqlite")
    refused = _refused(root, "data/moved.sqlite", moved)
    assert len(refused) == 1 and "the workspace's own store" in refused[0]
    assert _refused(root, "data/warehouse.sqlite", moved) == []  # a database of your own still reads


def test_a_workspace_says_once_that_a_database_source_lists_nothing_and_a_pass_does_not_list_it(root: Path) -> None:
    from cerebro.app import Cerebro
    from cerebro.fakes import FakeExtraction

    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, extraction=FakeExtraction()) as workspace:
        said = [line for line in workspace.notices if "lists no records" in line]
        assert said == [("sources/warehouse: a database source lists no records: an agent reads it with a sql "
                         "evidence line, so a pass does not list it")]
        for _ in range(2):
            result = workspace.run_once()
            assert "warehouse" not in result.listed and not [w for w in result.warnings if "lists no" in w]
            assert [line for line in result.notices if "lists no records" in line] == said  # the workspace's, once


def test_a_file_under_any_cerebro_folder_is_refused_and_a_write_ahead_database_creates_nothing_beside_it(
        root: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    other = tmp_path_factory.mktemp("elsewhere") / ".cerebro"
    other.mkdir(parents=True)
    sqlite3.connect(other / "cerebro.sqlite").close()
    with pytest.raises(SourceError, match="it names a file under a .cerebro folder"):
        DatabaseConnector(SourceFile(name="w", kind="database", url=f"sqlite:{other}/cerebro.sqlite"), root)
    db = sqlite3.connect(root / "wal.sqlite")
    db.execute("pragma journal_mode=wal")
    db.execute("create table t (x)")
    db.commit()
    db.close()
    before = sorted(p.name for p in root.iterdir())
    wal = DatabaseConnector(SourceFile(name="w", kind="database", url="sqlite:wal.sqlite"), root)
    with pytest.raises(DatabaseError, match="the database is in write-ahead mode"):
        wal.select("select x from t")
    assert sorted(p.name for p in root.iterdir()) == before
