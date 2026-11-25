"""What `cerebro sources check` says of each record is what the next pass does with it, for every state a record can
be in and every kind of source: the check runs, a real pass runs straight after, and each line is held against what
that pass did. Both decide through `runner.decide`, so the tie holds even when the decision changes; and the check's
module tests no record itself."""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cerebro import Cerebro, cli, runner, sources_check
from cerebro.article import Identity
from cerebro.cli import main
from cerebro.connector import CursorError, Record
from cerebro.curate import FIRST_RUN_KEY
from cerebro.fakes import FakeChanges, FakeConnector, FakeExtraction

NAME = "things"
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
CURATOR = "---\nheartbeat: [change]\n---\nKeep things.\n"


@pytest.fixture(autouse=True)
def a_facts_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace always has a facts layer, Factoid when it is handed none; here a stand-in that calls no model."""
    monkeypatch.setattr("cerebro.app.FactoidExtraction", lambda *args, **kwargs: _OwnFacts())


class _OwnFacts(FakeExtraction):
    """A stand-in facts layer the workspace made itself, and closes with it."""

    def close(self) -> None:
        pass


class _Logged:
    """A stand-in source that keeps its gets, and fails the get of an id in `down`."""

    down: set[str]
    gets: list[str]

    def get(self, id: str) -> Record:
        self.gets.append(id)
        if id in self.down:
            raise OSError("the stand-in source did not answer")
        return super().get(id)  # type: ignore[misc]


class Plain(_Logged, FakeConnector):
    def __init__(self, records: list[Record]) -> None:
        super().__init__(NAME, records)
        self.down, self.gets = set(), []


class Changes(_Logged, FakeChanges):
    def __init__(self, records: list[Record]) -> None:
        super().__init__(NAME, records)
        self.down, self.gets = set(), []


class Facts(FakeExtraction):
    """A stand-in facts layer that keeps the ids it read, and fails those in `fail`."""

    def __init__(self) -> None:
        super().__init__()
        self.fail: set[str] = set()
        self.read: list[str] = []

    def extract(self, record: Record) -> Any:
        self.read.append(record.id)
        if record.id in self.fail:
            raise RuntimeError("the stand-in facts layer failed")
        return super().extract(record)


def _record(id: str, extract: str, topic: str | None = None, fields: dict[str, Any] | None = None) -> Record:
    return Record(id=id, source=NAME, revision="1", at="2026-11-01T09:00:00+00:00",
                  body=f"Date: 2026-11-01\n\nRecord {id} says something.\n", extract=extract, into=NAME,
                  keys={"topic": [topic]} if topic else {}, fields=dict(fields or {}))


def _revise(source: Any, id: str) -> None:
    new = copy.deepcopy(source.records[id])
    new.revision = str(int(new.revision) + 1)
    source.add(new)


def _article(root: Path, source: Any, id: str, topic: str, status: str = "active") -> None:
    with Cerebro(root, connectors=[source]) as workspace:
        if workspace.store.article(id) is None:
            workspace.store.create(Identity(id, NAME, id.split("/")[1].title(), {"topic": [topic]}))
        found = workspace.store.article(id)
        assert found is not None
        found.identity.status = status
        workspace.store.set_identity(found.identity)


@dataclass
class Setup:
    """One workspace with a record in each state, and the time its check and next pass run at."""

    root: Path
    source: Any
    facts: Facts
    at: datetime
    kind: str
    ids: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)


def _pass(setup: Setup, at: datetime) -> runner.PassResult:
    with Cerebro(setup.root, connectors=[setup.source], extraction=setup.facts, clock=lambda: at) as workspace:
        return workspace.run_once(only=NAME, agents=False)


def _setup(root: Path, *, kind: str = "facts", was: str = "facts", lister: str = "plain", scope: bool = True,
           first_run: bool = False, cap: bool = False) -> Setup:
    """A workspace whose source `things` holds a record in every state a pass knows: new, revised, unchanged, removed,
    failed last time and still there, or since removed, or now out of scope, or now held for the first run, held and
    still out of scope, held and in scope now, out of scope, bound only to an archived article, and one whose fields
    the source maps. Its records were `was` and are `kind` now."""
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    curator = root / "agents" / NAME / "curator.md"
    curator.parent.mkdir(parents=True)
    curator.write_text(CURATOR)
    records = [_record("unchanged", was, "alpha"), _record("revised", was, "alpha"), _record("removed", was, "alpha"),
               _record("failed-still", was, "alpha"), _record("failed-gone", was, "alpha"),
               _record("failed-out", was, "delta"), _record("failed-held", was, "gamma"),
               _record("held-still", was), _record("held-now-in", was, "beta"), _record("held-gone", was),
               _record("mapped", was, "alpha", {"stage": "open"})]
    source = Plain(records) if lister == "plain" else Changes(records)
    facts = Facts()
    for id, topic in (("things/alpha", "alpha"), ("things/gamma", "gamma"), ("things/delta", "delta")):
        _article(root, source, id, topic)
    _article(root, source, "things/old", "old", status="archived")
    setup = Setup(root, source, facts, T0 + (timedelta(hours=25) if lister == "full" else timedelta(hours=1)), kind)
    source.down = {"failed-still", "failed-gone", "failed-out", "failed-held"}
    _pass(setup, T0)
    _article(root, source, "things/gamma", "gamma", status="archived")
    _revise(source, "failed-held")
    _pass(setup, T0 + timedelta(minutes=30))
    source.down = set()
    for record in (_record("new", was, "alpha"), _record("out-new", was), _record("bound-archived", was, "old")):
        source.add(record)
    for id in ("revised", "failed-still", "failed-out", "failed-held"):  # the last two listed again, and held
        _revise(source, id)
    for id in ("removed", "failed-gone", "held-gone"):
        source.remove(id)
    _article(root, source, "things/beta", "beta")
    _article(root, source, "things/delta", "delta", status="archived")
    for record in source.records.values():
        record.extract = kind
    if not scope:
        curator.unlink()
    with Cerebro(root, connectors=[source]) as workspace:
        if first_run:
            workspace.store.set_state(FIRST_RUN_KEY, {"status": "proposing"})
        assert workspace.store.state(f"failed:{NAME}") or was != "facts", "a record waits to be tried again"
        assert workspace.store.state(f"held:{NAME}"), "a record is held"
    if cap:
        (root / "settings.yaml").write_text("timezone: UTC\nbudget:\n  extraction_daily_usd: 0.000000001\n")
        with Cerebro(root, connectors=[source]) as workspace:
            workspace.store.append("spend", {"day": setup.at.date().isoformat(), "at": setup.at.strftime(
                "%Y-%m-%dT%H:%M:%SZ"), "layer": "extraction", "usd": 1.0})
    setup.ids = sorted({*source.records, "removed", "failed-gone", "held-gone"})
    with Cerebro(root, connectors=[source]) as workspace:
        setup.failed = list(workspace.store.state(f"failed:{NAME}") or [])
        setup.held = [row["id"] for row in workspace.store.state(f"held:{NAME}") or []]
    return setup


def _check(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(setup.root, connectors=[setup.source],
                                                                  clock=lambda: setup.at))
    done = CliRunner().invoke(main, ["sources", "check", NAME, "--workspace", str(setup.root)])
    assert done.exception is None or isinstance(done.exception, SystemExit), done.output
    return done.output


@dataclass
class Did:
    """What the pass did with each record."""

    got: set[str]
    read: set[str]
    held: list[str]
    failed: list[str]
    refused: set[str]
    let_go: int


def _did(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> Did:
    gets, reads = len(setup.source.gets), len(setup.facts.read)
    result = _pass(setup, setup.at)
    with Cerebro(setup.root, connectors=[setup.source]) as workspace:
        held = [row["id"] for row in workspace.store.state(f"held:{NAME}") or []]
        failed = list(workspace.store.state(f"failed:{NAME}") or [])
    let_go = sum(int(m.group(1)) for line in result.notices if (m := re.search(r"(\d+) held records past", line)))
    return Did(set(setup.source.gets[gets:]), set(setup.facts.read[reads:]), held, failed,
               {key.rsplit("/", 1)[-1] for key, why in result.failed.items() if "BudgetSpent" in why}, let_go)


def _count(output: str, pattern: str) -> int:
    return sum(int(m.group(1)) for m in re.finditer(pattern, output, re.MULTILINE))


def _tie(setup: Setup, output: str, did: Did) -> list[str]:
    """Each place where what the check printed is not what the pass did, as a line; none when they agree."""
    rows = {m.group(1): m.group(2) for m in re.finditer(r"^record    (\S+)\s+(?:\S+\s+)?(for the next pass.*|held: .*|"
                                                         r"let go: .*)$", output, re.MULTILINE)}
    off = [f"a row no line of this test reads: {line}" for line in output.splitlines()
           if line.startswith("record    ") and line.split()[1] not in rows]
    for id, said in rows.items():
        got, read = id in did.got, id in did.read
        if said == sources_check.TRIED:
            ok = got and read and id in setup.failed
        elif said == sources_check.UNHELD:
            ok = got and read and id in setup.held and id not in setup.failed
        elif said == sources_check.FACTS:
            ok = got and read and id not in setup.held and id not in setup.failed
        elif said.startswith(sources_check.UNREAD):
            ok = not got and not read
        elif said.startswith(sources_check.GOT_NOTHING):
            ok = got and not read
        elif said == sources_check.REFUSED:
            ok = got and not read and id in did.failed and id not in did.refused
        elif said.startswith("held: "):
            ok = not got and not read and id in did.held
        elif said == sources_check.LET_GO:
            ok = not got and id not in did.held
        else:
            ok = False
        if not ok:
            off.append(f"{id}: the check says {said!r}; the pass got={got} read={read} held={id in did.held}")
    for id in did.got - set(rows) - {k.rsplit("/", 1)[-1] for k in re.findall(r"^failed    (\S+):", output, re.MULTILINE)}:
        off.append(f"{id}: the check names no get of it; the pass got it")
    waiting = _count(output, r"^waiting   (\d+) records? waits? for the next pass")
    if waiting != len(did.read):
        off.append(f"the check says {waiting} wait to be taken in; the pass read {sorted(did.read)}")
    if re.search(r"takes (it|them) in", output) and not did.read:
        off.append("the check names a command that takes records in; the pass read none")
    held = _count(output, r"^held      (\d+) records? stays? held")
    if held != len(did.held):
        off.append(f"the check says {held} stay held; the pass holds {len(did.held)}")
    if _count(output, r"^held      (\d+) held records? past the bound") != did.let_go:
        off.append(f"the check's let go line is not the pass's {did.let_go}")
    if _count(output, r"^refused   (\d+) records?") != len(did.refused):
        off.append(f"the check refuses another number than the pass's {sorted(did.refused)}")
    only = {id for id, said in rows.items() if said.startswith(sources_check.UNREAD)}
    if _count(output, r"^unread    (\d+) records?") < len(only):
        off.append(f"the check's unread line is not the records it reads nothing of: {sorted(only)}")
    return off


SETUPS = {
    **{f"{kind}-{lister}-{'scope' if scope else 'noscope'}": {"kind": kind, "was": kind, "lister": lister, "scope": scope}
       for kind in ("facts", "records", "none") for lister in ("plain", "changes", "full") for scope in (True, False)},
    **{f"{kind}-after-facts-{lister}": {"kind": kind, "was": "facts", "lister": lister}
       for kind in ("records", "none") for lister in ("plain", "full")},
    **{f"{kind}-first-run-{lister}": {"kind": kind, "was": kind, "lister": lister, "first_run": True}
       for kind in ("facts", "records") for lister in ("plain", "full")},
    "facts-cap-spent-plain": {"cap": True},
    "facts-cap-spent-full": {"cap": True, "lister": "full"},
    "records-cap-spent-plain": {"kind": "records", "was": "records", "cap": True},
}


@pytest.mark.parametrize("name", SETUPS)
def test_each_line_of_the_check_is_what_the_next_pass_does(name: str, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    setup = _setup(tmp_path / "w", **SETUPS[name])
    output = _check(setup, monkeypatch)
    assert not re.search(r"\b(brains?|pages?|sections?)\b", output.replace(str(setup.root), ""), re.IGNORECASE), output
    assert _tie(setup, output, _did(setup, monkeypatch)) == [], output


def test_past_the_bound_the_check_lets_go_the_records_the_pass_lets_go(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "HELD_MAX", 3)
    setup = _setup(tmp_path / "w")
    for n in range(3):
        setup.source.add(_record(f"z-out-{n}", "facts"))
    output = _check(setup, monkeypatch)
    did = _did(setup, monkeypatch)
    assert did.let_go and len(did.held) == 3, did
    assert "past the bound of 3 are let go" in output, output
    assert _tie(setup, output, did) == [], output


def _kind_before_scope(workspace: Any, record: Record | str, ahead: runner.Ahead | None = None) -> runner.Decision:
    """The pass's decision with its test of what a source extracts into moved ahead of its test of scope."""
    if isinstance(record, Record) and ahead is not None and record.extract in ("records", "none"):
        return runner.Decision(runner.NOTHING, f"its source extracts it into {record.extract}")
    return REAL_DECIDE(workspace, record, ahead)


REAL_DECIDE = runner.decide


@pytest.mark.parametrize("name", ["records-plain-scope", "none-full-scope"])
def test_when_the_pass_decides_otherwise_the_check_says_so_too(name: str, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    before = _check(_setup(tmp_path / "a", **SETUPS[name]), monkeypatch)
    setup = _setup(tmp_path / "b", **SETUPS[name])
    monkeypatch.setattr(runner, "decide", _kind_before_scope)
    output = _check(setup, monkeypatch)
    assert output.replace(str(tmp_path / "b"), "") != before.replace(str(tmp_path / "a"), "")
    assert re.search(r"^record    out-new\s+\S+\s+" + re.escape(sources_check.UNREAD), output, re.MULTILINE), output
    assert _tie(setup, output, _did(setup, monkeypatch)) == [], output


NAMES = {"in_scope", "bound_to", "working", "in_first_run", "proposing", "FIRST_RUN_KEY", "KEY", "HELD_MAX", "session"}
ATTRIBUTES = NAMES | {"ledger", "may_spend", "extract", "kind", "status", "role", "into", "target", "keys", "agents",
                      "fields", "body", "revision", "lists_nothing"}
ALLOWED = {("refusal", "lists_nothing"), ("check", "in_first_run"), ("_lines", "HELD_MAX"),
           ("_since", "kind"), ("_plan", "into"), ("curate", "ledger"),
           *(("_curators", name) for name in ("into", "agents", "role", "target", "proposing"))}
# `_since` reads a source's kind to say its window; `_plan` keeps the collection a held record waits on; `_curators`
# names the curators of those collections and says what a curator's run is, as `run_curator` and `Cerebro.curate` read
# it, and decides no record.


def _shown(function: ast.FunctionDef, node: ast.AST) -> bool:
    """Whether `node` is only put on a row, as the record's kind is shown: an element of a tuple itself, never a test."""
    return any(isinstance(parent, ast.Tuple) and node in parent.elts for parent in ast.walk(function))


def test_an_id_left_to_try_again_that_is_not_text_is_tried_by_the_check_as_by_the_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w"
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    source = Plain([Record(id=5, source=NAME, revision="1", body="text")])  # type: ignore[arg-type]
    source.down = {5}  # type: ignore[arg-type]
    setup = Setup(root, source, Facts(), T0 + timedelta(hours=1), "facts")
    _pass(setup, T0)
    source.down = set()
    output = _check(setup, monkeypatch)
    assert "record    5  for the next pass, which tries it again" in output.splitlines(), output
    assert _pass(setup, setup.at).read == 1


def test_with_all_the_ids_left_to_try_again_are_said_as_the_pass_tries_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = _setup(tmp_path / "w")
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(setup.root, connectors=[setup.source],
                                                                  clock=lambda: setup.at))
    done = CliRunner().invoke(main, ["sources", "check", NAME, "--all", "--workspace", str(setup.root)])
    assert "record    failed-out      facts  for the next pass, which tries it again" in done.output, done.output


class Breaking(FakeConnector):
    """A stand-in source, whole-listing or listing changes, whose cursor stops reading in one `way`: the cursor the
    last pass kept is refused by `cursor_for` or by the first listing from it, the cursor of a later page is refused,
    or, for one that lists changes, the listing in full after the cursor is dropped fails too."""

    def __init__(self, changes: bool, way: str) -> None:
        super().__init__(NAME, [_record("a", "facts"), _record("b", "facts")], page_size=1)
        self.lists_changes, self.way, self.bad, self.down = changes, way, set(), False
        self.last: str | None = None  # the cursor the last page gave: a listing from it reads on, one from another starts
        self.from_kept = False

    def cursor_for(self, cursor: str | None, *, full: bool) -> str | None:
        if cursor is not None and cursor in self.bad and self.way == "cursor_for":
            raise CursorError(f"{self.name}: the stand-in source no longer knows the cursor {cursor}")
        return cursor

    def list(self, since: str | None = None) -> Any:
        if since is None or since != self.last:
            self.from_kept = since is not None
        if since is not None and since in self.bad and self.way != "cursor_for" and self.from_kept:
            raise CursorError(f"{self.name}: the stand-in source no longer knows the cursor {since}")
        if since is None and self.down:
            raise OSError("the stand-in source did not answer")
        listing = super().list(since)
        self.last = listing.cursor
        return listing

    def get(self, id: str) -> Record:
        record = super().get(id)
        record.body = f"Date: 2026-11-01\n\nRecord {id} says something.\n"
        return record


BREAKS = [(changes, way) for changes in (False, True) for way in ("kept", "cursor_for", "later page", "relist fails")
          if changes or way not in ("cursor_for", "relist fails")]


def _broken(root: Path, changes: bool, way: str) -> tuple[Breaking, Facts]:
    """A workspace whose source's kept cursor stops reading in `way`, once a pass took in its first two records and
    three more came."""
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    source, facts = Breaking(changes, way), Facts()
    with Cerebro(root, connectors=[source], extraction=facts, clock=lambda: T0) as workspace:
        workspace.run_once(only=NAME, agents=False)
        kept = workspace.store.state(f"cursor:{NAME}")
    for id in ("c", "d", "e"):
        source.add(_record(id, "facts"))
    source.bad = {source.list(kept).cursor} if way == "later page" else {kept}
    source.down, source.last = way == "relist fails", None
    return source, facts


@pytest.mark.parametrize(("changes", "way"), BREAKS)
def test_when_a_cursor_does_not_read_the_check_says_what_the_next_pass_then_does(
        changes: bool, way: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w"
    source, facts = _broken(root, changes, way)
    at = T0 + timedelta(hours=1)
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source], clock=lambda: at))
    output = CliRunner().invoke(main, ["sources", "check", NAME, "--workspace", str(root)]).output
    read, source.last = len(facts.read), None
    with Cerebro(root, connectors=[source], extraction=facts, clock=lambda: at) as workspace:
        done = workspace.run_once(only=NAME, agents=False)
        cursor = workspace.store.state(f"cursor:{NAME}")
    taken = facts.read[read:]
    said = re.findall(r"^record    (\S+)\s+facts\s+for the next pass$", output, re.MULTILINE)
    assert said == taken, (output, taken)
    dropped = "so the next pass drops it and lists the source in full" in output
    cleared = "so the next pass clears it: the pass after lists the source from the start" in output
    assert dropped == any("so it was dropped and the source was listed in full" in n for n in done.notices), output
    assert cleared == (cursor is None and not done.failed), (output, cursor, done.failed)
    assert dropped or cleared, output
    assert ("failed    sources/things: " in output) == (f"sources/{NAME}" in done.failed), (output, done.failed)
    listed = re.search(r"^listed    things: \d+ records? (.*)$", output, re.MULTILINE)
    assert listed is None or (listed.group(1) == "from the start") == dropped, output
