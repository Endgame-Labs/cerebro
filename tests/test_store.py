"""The `Store` suite against the in-memory fake, and against the SQLite store in memory and in a file. Then what only
the SQLite store promises: an index behind every query, two processes on one file, a schema version, durability, and
the settings that open it."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from suites.store import *

from cerebro.article import Compilation, Identity, Property
from cerebro.caller import Caller
from cerebro.connector import Record
from cerebro.fakes import MemoryStore
from cerebro.store import (
    DEFAULT_PATH,
    SchemaError,
    SQLiteStore,
    Store,
    StoreSettingsError,
    Where,
    open_store,
)

YOU = Caller("you", "person")
T1 = "2026-11-01T09:00:00Z"


@pytest.fixture(params=["memory", "sqlite", "sqlite file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    if request.param == "memory":
        made: Store = MemoryStore()
    elif request.param == "sqlite file":
        made = open_store(tmp_path, {"store": {"path": "data/cerebro.sqlite"}})
    else:
        made = SQLiteStore()
    with made:
        yield made


@pytest.fixture
def reopen(request: pytest.FixtureRequest, tmp_path: Path, store: Store) -> Callable[[], Store]:
    """The store as the workspace's settings open it again; a store with nothing on disk gives itself."""
    if isinstance(store, SQLiteStore) and store.path is not None:
        return lambda: open_store(tmp_path, {"store": {"path": "data/cerebro.sqlite"}})
    return lambda: store


def test_a_sqlite_file_keeps_everything_written_when_opened_again(tmp_path: Path) -> None:
    path = tmp_path / "cerebro.sqlite"
    with SQLiteStore(path) as store:
        store.create(Identity("projects/apollo", "projects", "Apollo", {"email": ["a@apollo.example"]}))
        store.write_compilation(Compilation("projects/apollo", "Summary", "It moved.", by="projects/article"))
        store.set_field("projects/apollo", "due", Property("2026-12-01", YOU, T1))
        store.keep(Record(id="r_1", source="note", revision="1", body="kept"))
        row = store.append("agent-runs", {"agent": "projects/article"})
        store.set_state("cursor:notes", {"page": 2})
        store.link("f_1", ["projects/apollo"])
        mark = store.cursor()
        before = store.article("projects/apollo")
    with SQLiteStore(path) as store:
        assert store.article("projects/apollo") == before
        assert [a.identity.id for a in store.query("projects", [Where("due", ">", "2026-11")])] == ["projects/apollo"]
        assert store.bound("email", "a@apollo.example") == store.named("apollo") == ["projects/apollo"]
        assert store.record("r_1").body == "kept" and store.rows("agent-runs") == [row]
        assert store.state("cursor:notes") == {"page": 2} and store.facts_of("projects/apollo") == ["f_1"]
        assert store.cursor() == mark and store.changed_since(mark) == ([], mark)
        store.set_field("projects/apollo", "due", Property("2026-12-15", YOU, "2026-11-02T09:00:00Z"))
        assert store.article("projects/apollo").properties["due"].value == "2026-12-15"
        assert store.append("agent-runs", {}).seq > row.seq
        assert store.changed_since(mark)[0] == [("article", "projects/apollo"), ("log", "agent-runs")]


def _plans(store: SQLiteStore, run: Callable[[], object]) -> list[str]:
    """The steps SQLite planned for each query `run` made."""
    said: list[str] = []
    store.db.set_trace_callback(said.append)
    try:
        run()
    finally:
        store.db.set_trace_callback(None)
    return [step for sql in said if sql.lstrip().lower().startswith("select")
            for (_, _, _, step) in store.db.execute("explain query plan " + sql).fetchall()]


def test_a_range_query_over_many_articles_is_an_index_lookup() -> None:
    with SQLiteStore() as store:
        dates = {f"notes/n{n}": f"2026-{1 + n % 12:02d}-{1 + n % 28:02d}" for n in range(600)}
        for id, date in dates.items():
            store.create(Identity(id, "notes"))
            store.set_field(id, "due", Property(date, YOU, T1))
        due = [Where("due", ">=", "2026-03-01"), Where("due", "<", "2026-03-08")]
        found = store.query("notes", due)
        assert [a.identity.id for a in found] == sorted(id for id, d in dates.items() if "2026-03-01" <= d < "2026-03-08")
        assert 10 < len(found) < 20
        for run in (lambda: store.query("notes", due), lambda: store.query("notes", [Where("due", "=", "x")]),
                    lambda: store.query("notes", [Where("due", "in", ["x", 1])]),
                    lambda: store.query("notes", order_by="due", descending=True, limit=10),
                    lambda: store.linking_to("due", ["2026-03-01", "notes/n1"])):
            steps = _plans(store, run)
            assert steps and not [s for s in steps if s.startswith("SCAN")], steps


def test_two_connections_writing_fields_at_once_all_land(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "cerebro.sqlite"
    with SQLiteStore(path) as first, SQLiteStore(path) as second:
        first.create(Identity("projects/apollo", "projects"))

        def write(n: int) -> None:
            store = first if n % 2 else second
            store.set_field("projects/apollo", f"f{n}", Property(n, YOU, f"2026-11-01T09:00:{n:02d}Z"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(40)))
        assert {k: p.value for k, p in second.article("projects/apollo").properties.items()} == {
            f"f{n}": n for n in range(40)}


WRITER = """
import importlib.util, sys, time, types
from pathlib import Path
# The store's own modules alone, not the package's __init__, which loads the whole app: this test is about the file.
found = importlib.util.find_spec("cerebro")
package = types.ModuleType("cerebro")
package.__path__ = list(found.submodule_search_locations)
sys.modules["cerebro"] = package
from cerebro.article import Property, Compilation
from cerebro.caller import Caller
from cerebro.store import SQLiteStore

path, name, go = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
with SQLiteStore(path) as store:
    print("ready", flush=True)
    while not go.exists():
        time.sleep(0.002)
    for n in range(40):
        store.set_field("projects/apollo", f"{name} {n}", Property(n, Caller(name, "agent"), "2026-11-01T09:00:00Z"))
        store.write_compilation(Compilation("projects/apollo", f"{name} {n}", "text", by=name))
        store.append("agent-runs", {"by": name, "n": n})
"""


def test_two_processes_writing_fields_compilations_and_log_rows_to_one_file_lose_nothing(tmp_path: Path) -> None:
    path, go = tmp_path / "cerebro.sqlite", tmp_path / "go"
    with SQLiteStore(path) as store:
        store.create(Identity("projects/apollo", "projects"))
    writers = [subprocess.Popen([sys.executable, "-c", WRITER, str(path), name, str(go)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True) for name in ("cli", "serve")]
    try:
        assert [w.stdout.readline().strip() for w in writers] == ["ready", "ready"]
        go.touch()
        for writer in writers:
            assert writer.wait(timeout=30) == 0, writer.stderr.read()
    finally:
        for writer in writers:
            writer.kill()
            writer.stdout.close()
            writer.stderr.close()
    with SQLiteStore(path) as store:
        assert len(store.article("projects/apollo").properties) == 80
        assert len(store.article("projects/apollo").compilations) == 80
        assert sorted((r.data["by"], r.data["n"]) for r in store.rows("agent-runs")) == sorted(
            (name, n) for name in ("cli", "serve") for n in range(40))


def test_a_file_of_another_schema_version_or_program_is_refused_with_what_to_do(tmp_path: Path) -> None:
    old = tmp_path / "old.sqlite"
    SQLiteStore(old).close()
    with sqlite3.connect(old) as db:
        db.execute("pragma user_version = 99")
    with pytest.raises(SchemaError, match=r"made by a different version of Cerebro, which this one cannot read: move the file aside"):
        SQLiteStore(old)
    other = tmp_path / "other.sqlite"
    with sqlite3.connect(other) as db:
        db.execute("create table things (x)")
    with pytest.raises(SchemaError, match=r"other.sqlite is not a cerebro store .*move this one aside"):
        SQLiteStore(other)
    text = tmp_path / "notes.txt"
    text.write_text("not a database at all\n" * 100)
    with pytest.raises(SchemaError, match="notes.txt is not a cerebro store"):
        SQLiteStore(text)
    with sqlite3.connect(other) as db:
        assert db.execute("select count(*) from sqlite_master").fetchone()[0] == 1  # left as it was


def test_a_workspace_with_no_store_settings_opens_sqlite_in_its_state_folder(tmp_path: Path) -> None:
    with open_store(tmp_path, {}) as store:
        assert isinstance(store, SQLiteStore) and store.path == tmp_path / DEFAULT_PATH
    with open_store(tmp_path, {"store": {"path": "data/deep/cerebro.db"}}) as store:
        assert store.path == tmp_path / "data" / "deep" / "cerebro.db"
        assert store.db.execute("pragma journal_mode").fetchone()[0] == "wal"
    absolute = tmp_path / "elsewhere" / "cerebro.db"
    with open_store(tmp_path / "workspace", {"store": {"class": "sqlite", "path": str(absolute)}}) as store:
        assert store.path == absolute and absolute.is_file()


class PluggedStore(MemoryStore):
    """A store a workspace names in its settings, which reads its own key and the workspace folder."""

    def __init__(self, root: Path, shards: int):
        super().__init__()
        self.root, self.shards = root, shards

    @classmethod
    def from_settings(cls, root: Path, *, shards: int = 1) -> PluggedStore:
        return cls(root, shards)


def test_a_workspace_plugs_in_its_own_store_by_naming_its_class(tmp_path: Path) -> None:
    with open_store(tmp_path, {"store": {"class": "cerebro.fakes:MemoryStore"}}) as store:
        assert type(store) is MemoryStore
    with open_store(tmp_path, {"store": {"class": f"{__name__}:PluggedStore", "shards": 4}}) as store:
        assert isinstance(store, PluggedStore) and (store.root, store.shards) == (tmp_path, 4)


@pytest.mark.parametrize(("block", "says"), [
    ("sqlite", "settings.yaml store is not a map"),
    ({"paht": "x.db"}, "does not suit the sqlite store .*paht"),
    ({"path": ""}, "settings.yaml store.path is ''"),
    ({"class": 3}, "settings.yaml store.class is 3"),
    ({"class": "no_such_module:Store"}, "store.class no_such_module:Store does not import"),
    ({"class": "json:dumps"}, "store.class json:dumps is not a Store class"),
    ({"class": "cerebro.store:"}, "store.class cerebro.store: is not a Store class"),
    ({"class": ":SQLiteStore"}, "store.class :SQLiteStore is not a Store class"),
    ({"class": "cerebro.store:SQLiteStore:x"}, "is not a Store class"),
    ({"class": "cerebro.store.NoSuchStore"}, "store.class cerebro.store.NoSuchStore is not a Store class"),
    ({"class": "cerebro.fakes.MemoryStore", "shards": 2}, "does not suit the .*MemoryStore store .*shards"),
])
def test_store_settings_no_store_can_open_are_refused_with_one_line(tmp_path: Path, block: object, says: str) -> None:
    with pytest.raises(StoreSettingsError, match=says) as refused:
        open_store(tmp_path, {"store": block})
    assert "\n" not in str(refused.value) and str(refused.value).startswith("settings.yaml store")
    assert not (tmp_path / ".cerebro").exists()


def test_an_exclusive_block_holds_against_a_writer_in_another_process(tmp_path: Path) -> None:
    import subprocess
    import sys

    bump = ("import sys, time\nfrom pathlib import Path\nfrom cerebro.store import open_store\n"
            "with open_store(Path(sys.argv[1]), {'store': {'path': 'cerebro.sqlite'}}) as store:\n"
            "    for _ in range(15):\n"
            "        with store.exclusive():\n"
            "            seen = store.state('n') or 0\n"
            "            time.sleep(0.002)\n"
            "            store.set_state('n', seen + 1)\n")
    with open_store(tmp_path, {"store": {"path": "cerebro.sqlite"}}) as store:
        store.set_state("n", 0)
    workers = [subprocess.Popen([sys.executable, "-c", bump, str(tmp_path)]) for _ in range(2)]
    assert [w.wait(timeout=60) for w in workers] == [0, 0]
    with open_store(tmp_path, {"store": {"path": "cerebro.sqlite"}}) as store:
        assert store.state("n") == 30  # no read and write of one process landed between the other's
