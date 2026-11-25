"""How long a run takes to link facts whose subjects need the near-match judge, on the fakes, with a judge that
waits a fixed time a call. Not part of the test suite.

    PYTHONPATH=src python checks/link_width.py

It links 48 facts, 40 of which ask the judge, each call waiting 50 ms: one at a time that is 2 seconds, eight at a
time about a quarter of that. It prints each fact's links, a digest of them, and the costs booked, and exits 1
unless linking took under half the serial time and every judged call's cost was booked.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from _paths import ran_lines

from cerebro import Cerebro
from cerebro.article import Identity
from cerebro.connector import Record
from cerebro.facts import Fact
from cerebro.fakes import FakeDecider, MemoryStore
from cerebro.models import Decision, TokenCount
from cerebro.runner import PassResult, _link

WAIT = 0.05


class Slow(FakeDecider):
    """A decision model that waits `WAIT` seconds a call, and bills a few tokens for each."""

    def decide(self, state: Any, questions: dict[str, Any]) -> Decision:
        time.sleep(WAIT)
        return replace(super().decide(state, questions), usage=TokenCount(input_tokens=100, output_tokens=2))


def landed() -> list[tuple[Fact, Record]]:
    subjects = [*(f"Thing {n}'s review" for n in range(36)),  # one near candidate: linked by the judge
                *("Twin" for _ in range(4)),  # two near candidates that both pass: linked to neither
                *(f"Thing {n}" for n in range(4)),  # exact: no judge
                *(f"Nobody {n}" for n in range(4))]  # no candidate: no judge
    out = []
    for n, subject in enumerate(subjects):
        record = Record(id=f"r{n}", source="notes", revision="1", at="2026-11-10T09:00:00Z", body="x")
        out.append((Fact(id=f"f{n:02d}", text=f"fact {n}", subject=subject, kind="note", at="", source="notes",
                         record=f"notes:r{n}"), record))
    return out


def main() -> int:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "settings.yaml").write_text("{}\n")
        workspace = Cerebro(root, trusted=True, store=MemoryStore(), connectors=[])
        workspace.decider = Slow()
        for n in range(36):
            workspace.store.create(Identity(f"things/t{n}", "things", f"Thing {n}"))
        workspace.store.create(Identity("things/twin-a", "things", "Twin Alpha"))
        workspace.store.create(Identity("things/twin-b", "things", "Twin Beta"))
        facts = landed()
        result = PassResult()
        started = time.monotonic()
        _link(workspace, facts, result)
        took = time.monotonic() - started
        links = [f"{fact.id} {sorted(workspace.store.ties_of(fact.id).items())}" for fact, _ in facts]
        booked = [row for row in workspace.store.rows("spend") if row.data["run"].startswith("f")]
        asked = len(workspace.decider.asked)
    for line in links:
        print(line)
    serial = asked * WAIT
    print(f"digest   {hashlib.sha256(chr(10).join(links).encode()).hexdigest()[:16]}")
    print(f"linked   {result.linked}, failed {len(result.failed)}")
    for key, why in result.failed.items():
        print(f"  failed  {key}: {why}")
    for line in ran_lines(result):
        print(line)
    print(f"judged   {asked} calls, {len(booked)} costs booked")
    print(f"took     {took:.2f} s, against {serial:.2f} s one at a time")
    if len(booked) != asked:
        print("FAIL: a judged call's cost was not booked")
        return 1
    if took >= serial / 2:
        print("FAIL: linking asks the judge one fact at a time")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
