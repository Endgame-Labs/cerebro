"""When the first run's work happens, not what it gives: each test here holds that the same result comes with less
work."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cerebro import held
from cerebro.agent import load_agent
from cerebro.curate import run_curator
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.lookup import StoreLookup

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)


def _held_things(store: MemoryStore, n: int) -> None:
    store.set_state("held:crm", [{"id": f"r{k}", "source": "crm", "revision": "1", "at": "2026-11-10T09:00:00Z",
                                  "keys": {"name": [f"Thing {k}"], "domain": [f"t{k}.example"]}, "fields": {},
                                  "body": None, "into": "things"} for k in range(n)])


def _curate(tmp_path: Path, store: MemoryStore, calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    (tmp_path / "things").mkdir(exist_ok=True)
    (tmp_path / "things" / "curator.md").write_text(
        "---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - held: {}\nheartbeat: [daily]\nbudget: { tool_calls: 1000 }\n---\n\n"
        "Keep the things.\n")
    agent = load_agent(tmp_path / "things" / "curator.md", tmp_path)
    done = run_curator(agent, lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW), store=store,
                       agent_run=ScriptedRun(lambda p: {}, calls), root=tmp_path, functions={}, agent_tools={},
                       clock=lambda: NOW, sources={"crm": FakeConnector("crm", [])})
    assert done.run is not None
    return [e.text for e in done.run.transcript if e.role == "tool"]


def test_a_call_that_names_held_records_weighs_those_alone_and_answers_as_before(tmp_path: Path,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    weighed: list[str] = []
    real = held.bound_to
    monkeypatch.setattr(held, "bound_to", lambda store, record: weighed.append(record.id) or real(store, record))
    calls = [("leave_out", {"record": {"source": "crm", "id": f"r{k}"}, "reason": "a vendor"}) for k in range(5)]
    calls += [("leave_out", {"records": [{"source": "crm", "id": "r6"}, {"source": "crm", "id": "r404"}],
                             "reason": "a vendor"}),
              ("admit", {"record": {"source": "crm", "id": "r7"}, "reason": "two calls with it", "group": "named"}),
              ("admit", {"records": [{"source": "crm", "id": "r8"}], "reason": "two calls with it",
                         "group": "named"})]
    with MemoryStore() as store:
        _held_things(store, 200)
        replies = _curate(tmp_path, store, calls)
        rows = [r.data["id"] for r in store.rows("left-out")]
    assert rows == ["r0", "r1", "r2", "r3", "r4", "r6"]
    assert "no record of crm with id r404 is held" in replies[5]
    assert "things/thing-7" in replies[6] and "things/thing-8" in replies[7], replies
    # the held list weighs every record before the run, and before and after each act; the calls weigh what they name
    assert len(weighed) - 200 * (2 * len(calls) + 1) < 3 * len(calls), len(weighed)


def test_the_list_reads_each_sources_held_records_once_however_many_rows_it_left_out(tmp_path: Path) -> None:
    from cerebro.app import Cerebro
    from cerebro.connector import Record
    from cerebro.first_run import LocalApproval, discover

    records = [Record(id=f"r{k}", source="crm", revision="1", at="2026-11-10T09:00:00Z",
                      keys={"domain": [f"t{k}.example"], "name": [f"Thing {k}"]}, fields={"name": f"Thing {k}"},
                      body=f"Thing {k} asked for a plan.", into="things") for k in range(40)]
    calls = [("admit", {"record": {"source": "crm", "id": "r0"}, "reason": "two calls with it", "group": "named"})]
    calls += [("leave_out", {"record": {"source": "crm", "id": f"r{k}"}, "reason": "a vendor"}) for k in range(1, 31)]
    root = tmp_path / "ws"
    (root / "agents" / "things").mkdir(parents=True)
    (root / "agents" / "things" / "curator.md").write_text(
        "---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - held: {}\nheartbeat: [daily]\nbudget: { tool_calls: 1000 }\n---\n\n"
        "Keep the things.\n")
    (root / "settings.yaml").write_text("{}\n")
    with Cerebro(root, agent_run=ScriptedRun(lambda p: {}, calls), extraction=FakeExtraction(),
                 connectors=[FakeConnector("crm", records)], trusted=True) as workspace:
        before = discover(workspace)
        assert before is not None
        reads: list[str] = []
        state = workspace.store.state

        def reading(key: str) -> Any:
            reads.append(key)
            return state(key)

        workspace.store.state = reading  # type: ignore[method-assign]
        pile = LocalApproval(workspace).pile()
    left = [(row.name, row.reason, row.keys) for row in pile.rows() if row.left_out]
    assert left == [(row.name, row.reason, row.keys) for row in before.rows() if row.left_out]
    assert ("Thing 1", "a vendor", {"domain": ["t1.example"], "name": ["Thing 1"]}) in left
    assert len([r for r in left if r[1] == "a vendor"]) == 30
    assert reads.count("held:crm") < 10, reads.count("held:crm")


def test_a_held_list_finds_the_proposals_a_record_links_to_without_asking_each_one(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.caller import Caller
    from cerebro.curate import FIRST_RUN_KEY
    from cerebro.records import Proposal, propose

    with MemoryStore() as store:
        store.set_state(FIRST_RUN_KEY, {"status": "waiting", "at": "2026-11-17T09:00:00Z"})
        for k in range(20):  # a proposed parent for each of twenty domains, by keys, and one by name alone
            propose(store, Proposal(f"p_{k:02d}", "article.admit", f"parents/parent-{k}", Caller("parents/curator",
                    "agent"), "2026-11-17T09:00:00Z", {"name": f"Parent {k}", "keys": {"domain": [f"P{k}.example"]},
                                                       "collection": "parents"}))
        store.set_state("held:crm", [{"id": f"r{k}", "source": "crm", "revision": "1", "at": "2026-11-10T09:00:00Z",
                                      "keys": {"name": [f"Part {k}"], "email": [f"ann{k}@p{k % 3}.example"]},
                                      "fields": {}, "body": None, "into": "parts"} for k in range(30)])
        expected = {h.id: h.links_to for h in held.held_for(store, "parts", ["crm"], limit=None).items}

        def asked(*_: Any) -> bool:
            raise AssertionError("a record's links were matched by asking each proposal in turn")

        monkeypatch.setattr(held, "links_to", asked)
        found = held.held_for(store, "parts", ["crm"], limit=None).items
    assert {h.id: h.links_to for h in found} == expected
    assert found[0].links_to and all(h.links_to == [{"id": f"parents/parent-{int(h.id[1:]) % 3}",
                                                     "proposed_by": "agents/parents/curator.md"}] for h in found)
