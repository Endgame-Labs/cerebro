"""What the in-memory store promises past the `Store` suite: it holds still while an exclusive block copies it."""

from __future__ import annotations

import threading
from typing import Any

from cerebro.fakes import MemoryStore


class Slow:
    """A value whose copy waits until `wrote` is set, or half a second, so a write from another thread lands while
    an exclusive block is still copying the store, if the store lets it."""

    def __init__(self, copying: threading.Event, wrote: threading.Event):
        self.copying, self.wrote = copying, wrote

    def __deepcopy__(self, memo: dict[int, Any]) -> Slow:
        self.copying.set()
        self.wrote.wait(0.5)
        return self


def test_a_write_from_another_thread_waits_while_an_exclusive_block_copies_the_store() -> None:
    store = MemoryStore()
    copying, wrote = threading.Event(), threading.Event()
    store.values["slow"] = Slow(copying, wrote)

    def write() -> None:
        copying.wait(5)
        store.set_state("other", 1)
        wrote.set()

    other = threading.Thread(target=write)
    other.start()
    try:
        with store.exclusive():
            assert store.state("other") is None
    finally:
        other.join(5)
    assert not other.is_alive() and store.state("other") == 1


def inside(first: Any, then: Any) -> None:
    """Runs `then` on another thread from inside `first`'s call, once, and waits for it, or 0.3 s while the store
    makes it wait: without the store's lock the second call runs whole between two steps of the first."""
    if not first.done:
        first.done = True
        other = threading.Thread(target=then)
        other.start()
        other.join(0.3)
        first.others.append(other)


class Grows(list):  # type: ignore[type-arg]
    """A list that runs a second call once, from inside the first call that grows it."""

    def __init__(self, items: list[Any], then: Any) -> None:
        super().__init__(items)
        self.then, self.done, self.others = then, False, []

    def append(self, item: Any) -> None:
        super().append(item)
        inside(self, self.then)


class Counted(list):  # type: ignore[type-arg]
    """A list that runs a second call once, from inside the first call that counts it."""

    def __init__(self, then: Any) -> None:
        super().__init__()
        self.then, self.done, self.others = then, False, []

    def __len__(self) -> int:
        n = super().__len__()
        inside(self, self.then)
        return n


def test_a_row_appended_from_another_thread_mid_append_gets_its_own_seq_and_paging_finds_it() -> None:
    store = MemoryStore()
    rows: dict[str, Any] = {}
    store.history = Grows(store.history, lambda: rows.setdefault("second", store.append("spend", {"n": 2})))
    rows["first"] = store.append("spend", {"n": 1})
    for other in store.history.others:
        other.join(5)
    assert rows["first"].seq != rows["second"].seq, (rows["first"].seq, rows["second"].seq)
    older, newer = sorted((rows["first"], rows["second"]), key=lambda r: r.seq)
    assert [r.data for r in store.rows("spend", since=older.seq)] == [newer.data]


