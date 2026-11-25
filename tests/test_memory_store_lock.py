"""The test fake `MemoryStore` holds every write to one lock with `exclusive`, as the real store holds every write
under one lock and a transaction, so a write from another thread never lands inside a snapshot being taken."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from cerebro import fakes
from cerebro.fakes import MemoryStore


def test_a_write_from_another_thread_waits_for_an_exclusive_block_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    for n in range(50):
        store.set_state(f"k{n}", n)
    copying = threading.Event()
    real = fakes.deepcopy

    def slow(value: Any) -> Any:  # the snapshot takes a while, as it does under load
        if value is not store.values:
            return real(value)
        copied = {}
        for key, item in value.items():  # as deepcopy walks a dict: a key added meanwhile breaks the walk
            copying.set()
            time.sleep(0.002)
            copied[key] = real(item)
        return copied

    monkeypatch.setattr(fakes, "deepcopy", slow)
    failed: list[BaseException] = []

    def snapshot() -> None:
        try:
            with store.exclusive():
                store.set_state("inside", True)
        except BaseException as error:  # noqa: BLE001
            failed.append(error)

    first = threading.Thread(target=snapshot)
    first.start()
    assert copying.wait(2)
    store.set_state("from another thread", True)
    first.join(2)
    assert failed == []
    assert store.state("inside") is True and store.state("from another thread") is True
