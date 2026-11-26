"""A store another process is writing is busy, never broken, and a command that only reads, such as `status`, `review`
or `agents list`, does not wait behind it. Each test holds the store's write lock from a connection of
its own on a thread it ends, and talks to fakes only: a fake connector, a fake extraction and a scripted run."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_runner import cited

from cerebro import Cerebro, Record, cli
from cerebro.article import Compilation, Identity
from cerebro.caller import Caller
from cerebro.cli import main
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
from cerebro.lookup import SEARCH_BEHIND
from cerebro.store import DEFAULT_PATH, OPEN_WAIT, SchemaError, SQLiteStore, StoreBusy

WEEKLY = "---\nevidence:\n  - query: { from: facts }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n"
NEVER = re.compile(r"not a cerebro store|\bmove (it|this)|\baside\b|\brename|\bdelete", re.IGNORECASE)  # never of a busy store
YOU = Caller("you", "person")


@contextmanager
def held(file: Path, *, steps: threading.Event | None = None) -> Iterator[None]:
    """The store's write lock, held by a connection of its own on a thread, as a pass in another process holds it
    while it writes, and given back as the block ends."""
    taken, done = threading.Event(), threading.Event()

    def hold() -> None:
        db = sqlite3.connect(file, isolation_level=None)
        try:
            db.execute("begin immediate")
            taken.set()
            done.wait(60)
            db.execute("rollback")
        finally:
            db.close()

    thread = threading.Thread(target=hold)
    thread.start()
    assert taken.wait(10)
    try:
        yield
    finally:
        done.set()
        thread.join()


class Workspace:
    """A workspace with one fixed agent and one note, after one pass: a store that is already stamped."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (root / "agents" / "reports").mkdir(parents=True)
        (root / "settings.yaml").write_text("{}\n")
        (root / "agents" / "reports" / "weekly.md").write_text(WEEKLY)
        self.root, self.file = root, root / DEFAULT_PATH
        self.notes = FakeConnector("notes", [Record(id="n1", source="notes", revision="1", at="2026-11-10T09:00:00Z",
                                                    body="The weekly sync moved to Thursday.")])
        self.extraction = FakeExtraction()
        monkeypatch.setattr(cli, "open_workspace", lambda folder: self.open())
        # a writer waits this long, then says the store is busy
        monkeypatch.setitem(SQLiteStore.__init__.__kwdefaults__, "timeout", 3.0)
        with self.open() as workspace:
            workspace.run_once()

    def open(self) -> Cerebro:
        return Cerebro(self.root, extraction=self.extraction, connectors=[self.notes],
                       agent_run=ScriptedRun(cited))

    def command(self, *args: str) -> tuple[int, str, float]:
        began = time.monotonic()
        done = CliRunner().invoke(main, [*args, "--workspace", str(self.root)])
        return done.exit_code, done.output, time.monotonic() - began


@pytest.mark.parametrize("args", [["status"], ["agents", "list"], ["review", "--json"], ["tokens", "list"]],
                         ids=lambda a: " ".join(a))
def test_a_command_that_only_reads_answers_at_once_while_another_process_writes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    w = Workspace(tmp_path / "w", monkeypatch)
    with held(w.file):
        code, out, took = w.command(*args)
    assert code == 0 and took < 1.5, (took, out)
    assert not NEVER.search(out), out


def test_find_read_and_query_answer_at_once_while_another_process_writes(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch)
    with w.open() as workspace:
        workspace.call(YOU, "find", {"name": "weekly"})  # what a first call loads, loaded before the clock starts
    with held(w.file), w.open() as workspace:
        began = time.monotonic()
        assert "reports/weekly" in str(workspace.call(YOU, "find", {"name": "reports/weekly"}))
        assert workspace.call(YOU, "read", {"ids": ["reports/weekly"]})["articles"][0]["id"] == "reports/weekly"
        assert workspace.call(YOU, "query", {"from": "facts"})["count"] == 1
        assert time.monotonic() - began < 1.5
        began = time.monotonic()  # with from, test_busy_store_process asks the same through cerebro serve
        found = workspace.call(YOU, "query", {"matching": "weekly"})
        assert time.monotonic() - began < OPEN_WAIT + 1.5  # a moment for the search index, and no more
        assert SEARCH_BEHIND in found.get("warnings", []), found  # ranked as the index stood, and said so


def test_a_store_that_must_be_written_on_open_says_it_is_busy_after_a_short_wait_and_never_to_move_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch)
    for file in w.file.parent.glob(w.file.name + "*"):
        file.unlink()
    db = sqlite3.connect(w.file, isolation_level=None)
    db.execute("pragma journal_mode=wal")  # a store another process is making: its next open writes the schema
    db.close()
    monkeypatch.setitem(SQLiteStore.__init__.__kwdefaults__, "timeout", OPEN_WAIT + 3)  # past the bound below
    with held(w.file):
        code, out, took = w.command("status")
    assert code == 1 and took < OPEN_WAIT + 1.5, (took, out)
    assert "is busy" in out and "try again in a moment" in out, out
    assert not NEVER.search(out), out
    assert SQLiteStore(w.file).state("x") is None  # once free, the open writes the schema


def test_a_write_that_waits_past_its_time_says_the_store_is_busy(tmp_path: Path) -> None:
    file = tmp_path / "s.sqlite"
    SQLiteStore(file).close()
    store = SQLiteStore(file, timeout=0.2)
    with held(file), pytest.raises(StoreBusy, match="is busy"):
        store.create(Identity("things/alder", "things", "Alder"))
    store.create(Identity("things/alder", "things", "Alder"))  # once free, it lands
    store.close()


@pytest.mark.parametrize("content", [b"not a database at all, just bytes " * 200, None], ids=["bytes", "other-tables"])
def test_a_file_that_is_not_a_store_is_still_called_one_that_is_not(tmp_path: Path, content: bytes | None) -> None:
    file = tmp_path / "s.sqlite"
    if content is None:
        db = sqlite3.connect(file)
        db.execute("create table elsewhere (x)")
        db.execute("pragma application_id = 7")
        db.commit()
        db.close()
    else:
        file.write_bytes(content)
    with pytest.raises(SchemaError, match="is not a cerebro store") as refused:
        SQLiteStore(file)
    assert not isinstance(refused.value, StoreBusy)


def test_opening_a_store_that_holds_its_schema_takes_no_write_lock(tmp_path: Path) -> None:
    file = tmp_path / "s.sqlite"
    SQLiteStore(file).close()
    statements: list[str] = []
    real = sqlite3.connect

    def traced(*args: object, **kwargs: object) -> sqlite3.Connection:
        db = real(*args, **kwargs)  # type: ignore[arg-type]
        db.set_trace_callback(statements.append)
        return db

    sqlite3.connect = traced  # type: ignore[assignment]
    try:
        SQLiteStore(file).close()
    finally:
        sqlite3.connect = real
    assert not [s for s in statements if s.lower().startswith(("begin immediate", "begin exclusive", "insert",
                                                                "update", "delete", "create"))], statements


def test_a_reader_never_sees_half_of_a_write_that_lands_in_steps(tmp_path: Path) -> None:
    file = tmp_path / "s.sqlite"
    writer, reader = SQLiteStore(file), SQLiteStore(file)
    step, go, seen = threading.Event(), threading.Event(), []

    def write() -> None:
        with writer.exclusive():
            writer.create(Identity("things/alder", "things", "Alder"))
            step.set()
            go.wait(10)
            writer.write_compilation(Compilation("things/alder", "Summary", "Alder moved [^f_1].", "things/article"))

    thread = threading.Thread(target=write)
    thread.start()
    assert step.wait(10)
    began = time.monotonic()
    seen.append(reader.article("things/alder"))  # mid-write: nothing of it yet
    assert time.monotonic() - began < 0.5
    go.set()
    thread.join()
    whole = reader.article("things/alder")
    assert seen == [None] and whole is not None and [s.heading for s in whole.compilations] == ["Summary"]
    writer.close()
    reader.close()
