"""What a server counts again for a session that connects: only the collections whose articles changed or in which a
first run's proposed article came or went, never for a token's use stamp, and everything once the store under it was
replaced in place."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_mcp import YOU

from cerebro import Caller, Cerebro
from cerebro.article import Compilation, Identity
from cerebro.fakes import MemoryStore
from cerebro.held import FIRST_RUN_KEY
from cerebro.mcp import Counts
from cerebro.records import Proposal, propose
from cerebro.store import own_key
from cerebro.tokens import Tokens

KINDS = ["memory", "sqlite"]
FILED = "2026-11-03T09:00:00Z"


@contextmanager
def make_workspace(root: Path, kind: str) -> Iterator[Cerebro]:
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    with Cerebro(root, store=MemoryStore() if kind == "memory" else None) as workspace:
        yield workspace


def article(workspace: Cerebro, id: str) -> None:
    collection, _, name = id.rpartition("/")
    workspace.store.create(Identity(id, collection, name.capitalize()))
    workspace.store.write_compilation(Compilation(id, "Overview", f"{name} as it stands.", by=f"{collection}/article",
                                      at="2026-11-02T09:00:00Z"))


def admit(workspace: Cerebro, id: str, target: str) -> Proposal:
    collection, _, name = target.rpartition("/")
    proposal = Proposal(id, "article.admit", target, Caller(f"{collection}/curator", "agent"), FILED, reason="seen",
                        payload={"collection": collection, "name": name.capitalize()})
    propose(workspace.store, proposal)
    return proposal


def archive(workspace: Cerebro, id: str, status: str = "archived") -> None:
    found = workspace.store.article(id)
    assert found is not None
    workspace.store.set_identity(dataclasses.replace(found.identity, status=status))


def counting(counts: Counts, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The collections `counts` counts again from now on, each time it does."""
    counted: list[str] = []
    count = counts._count

    def counted_too(name: str) -> int:
        counted.append(name)
        return count(name)

    monkeypatch.setattr(counts, "_count", counted_too)
    return counted


def fresh(workspace: Cerebro) -> tuple[dict[str, int], int]:
    counts = Counts(workspace)
    return counts.collections(), counts.waiting()


@pytest.mark.parametrize("kind", KINDS)
def test_a_tokens_use_stamp_or_another_state_key_during_a_first_run_counts_no_collection_again(
        tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with make_workspace(tmp_path / "desk", kind) as workspace:
        for i in range(6):
            article(workspace, f"c{i % 3}/a{i}")
            admit(workspace, f"p_{i}", f"c{i % 3}/n{i}")
        workspace.store.set_state(FIRST_RUN_KEY, {"status": "listed"})
        token = workspace.make_token("robin").token
        counts = Counts(workspace)
        assert counts.collections() == {"c0": 4, "c1": 4, "c2": 4} == fresh(workspace)[0]
        counted = counting(counts, monkeypatch)
        now = [datetime(2026, 11, 19, 9, 0, tzinfo=UTC)]
        tokens = Tokens(workspace.store, lambda: now[0])
        for _ in range(3):
            now[0] += timedelta(seconds=1)
            before = workspace.store.cursor()
            assert tokens.check(f"Bearer {token}") is not None and workspace.store.cursor() > before  # its use is stamped
            assert counts.collections() == {"c0": 4, "c1": 4, "c2": 4}
        workspace.store.set_state(own_key("checked", "notes"), {"at": FILED})
        counts.collections()
        propose(workspace.store, Proposal("p_old", "article.archive", "c0/a0", Caller("c0/curator", "agent"), FILED))
        counts.collections()
        assert counted == []
        admit(workspace, "p_6", "c1/n6")
        assert counts.collections() == {"c0": 4, "c1": 5, "c2": 4} == fresh(workspace)[0] and counted == ["c1"]


@pytest.mark.parametrize("kind", KINDS)
def test_the_counts_stay_what_a_fresh_count_gives_and_count_again_only_what_moved_after_each_kind_of_change(
        tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with make_workspace(tmp_path / "desk", kind) as workspace:
        counts = Counts(workspace)
        assert (counts.collections(), counts.waiting()) == ({}, 0)
        counted = counting(counts, monkeypatch)

        def after(change: str, again: set[str]) -> tuple[dict[str, int], int]:
            now = counts.collections(), counts.waiting()
            assert now == fresh(workspace), change
            assert set(counted) == again, change
            counted.clear()
            return now

        article(workspace, "things/alder")
        assert after("an article written", {"things"}) == ({"things": 1}, 0)
        article(workspace, "parts/bolt")
        assert after("a collection added", {"parts"}) == ({"parts": 1, "things": 1}, 0)
        cedar = admit(workspace, "p_cedar", "things/cedar")
        assert after("an admit filed before a first run", set()) == ({"parts": 1, "things": 1}, 1)
        workspace.store.set_state(FIRST_RUN_KEY, {"status": "listed"})
        assert after("a first run opened", {"things"}) == ({"parts": 1, "things": 2}, 1)
        admit(workspace, "p_dune", "places/dune")
        assert after("an admit filed on a first run", {"places"}) == ({"parts": 1, "places": 1, "things": 2}, 2)
        workspace.store.set_state(FIRST_RUN_KEY, {"status": "discovering", "pinned": ["p_cedar"]})
        assert after("its status changed", {"places"}) == ({"parts": 1, "things": 2}, 2)
        propose(workspace.store, dataclasses.replace(cedar, status="rejected", decided_by=YOU, decided_at=FILED))
        assert after("a proposal decided", {"things"}) == ({"parts": 1, "things": 1}, 1)
        workspace.store.set_state(FIRST_RUN_KEY, {"status": "listed"})
        assert after("its status changed back", {"places"}) == ({"parts": 1, "places": 1, "things": 1}, 1)
        article(workspace, "things/birch")
        assert after("another article written", {"things"}) == ({"parts": 1, "places": 1, "things": 2}, 1)
        archive(workspace, "things/alder")
        assert after("an article archived", {"things"}) == ({"parts": 1, "places": 1, "things": 1}, 1)
        archive(workspace, "things/birch", "merged")
        assert after("an article merged away", {"things"}) == ({"parts": 1, "places": 1}, 1)
        workspace.store.set_state(FIRST_RUN_KEY, {"status": "done"})
        assert after("the first run closed", {"places"}) == ({"parts": 1}, 1)
        archive(workspace, "parts/bolt")
        assert after("a collection's last article archived", {"parts"}) == ({}, 1)


def backup(source: Path, onto: Path) -> None:
    """Copy one SQLite file onto another in place, as a restore with SQLite's backup does."""
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(onto)) as dst:
        src.backup(dst)


def test_a_store_restored_in_place_under_a_running_server_is_counted_whole_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with make_workspace(tmp_path / "desk", "sqlite") as workspace:
        live = tmp_path / "desk" / ".cerebro" / "cerebro.sqlite"
        article(workspace, "things/alder")
        propose(workspace.store, Proposal("p_a", "article.archive", "things/alder", Caller("things/curator", "agent"),
                                      FILED))
        backup(live, tmp_path / "before.sqlite")  # taken before the server counted anything
        counts = Counts(workspace)
        assert (counts.collections(), counts.waiting()) == ({"things": 1}, 1)
        backup(live, tmp_path / "counted.sqlite")
        for id in ("things/birch", "parts/bolt", "places/dune"):
            article(workspace, id)
            propose(workspace.store, Proposal(f"p_{id.replace('/', '_')}", "article.archive", id,
                                          Caller("things/curator", "agent"), FILED))
        assert (counts.collections(), counts.waiting()) == ({"parts": 1, "places": 1, "things": 2}, 4)

        backup(tmp_path / "counted.sqlite", live)  # its change feed goes back
        assert (counts.collections(), counts.waiting()) == fresh(workspace) == ({"things": 1}, 1)
        article(workspace, "parts/cog")
        assert (counts.collections(), counts.waiting()) == fresh(workspace) == ({"parts": 1, "things": 1}, 1)

        reached = workspace.store.cursor()
        backup(tmp_path / "before.sqlite", live)  # then written past where the server last counted
        for n in range(reached + 1):
            if workspace.store.cursor() > reached:
                break
            article(workspace, f"tools/t{n}")
        assert workspace.store.cursor() > reached
        assert (counts.collections(), counts.waiting()) == fresh(workspace)
        assert counts.collections()["things"] == 1 and "parts" not in counts.collections()
        article(workspace, "things/cedar")
        assert (counts.collections(), counts.waiting()) == fresh(workspace)
        assert counts.collections()["things"] == 2
        other = Counts(workspace)  # a second server on the same store makes no mark of its own
        assert (other.collections(), other.waiting()) == fresh(workspace)
        counted = counting(counts, monkeypatch)
        assert (counts.collections(), counts.waiting()) == fresh(workspace) and counted == []
