"""A told record that waits is named where a person looks, and a claim on its extraction holds only while its holder
renews it: one no holder renews lapses, whatever its pid says, and a pass that leaves a record says why."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_tell_retry import HERE, SAID, YOU, FileExtraction, harbor, in_memory, make_workspace, started, told

from cerebro import Record
from cerebro.cli import main
from cerebro.clock import stamp
from cerebro.facts import ExtractionFailed
from cerebro.fakes import FakeExtraction
from cerebro.records import CLAIMED, RETRY, TOLD

HOLDER = """
import json, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from cerebro.run import block_model_requests
block_model_requests()
import cerebro.records
from cerebro import Caller
import test_tell_retry as t

root, host, lapse = Path(sys.argv[1]), sys.argv[2], float(sys.argv[4])
cerebro.records.CLAIM_LAPSE, cerebro.records.CLAIM_RENEW = lapse, lapse / 10
if host:
    socket.gethostname = lambda: host
workspace = t.make_workspace(root, t.FileExtraction(root / "layer", kill="hold"))
print(json.dumps(workspace.tell(Caller("you", "person"), t.SAID, "projects/harbor", "fact").notes))
"""


def holder(root: Path, host: str, lapse: float) -> subprocess.Popen[bytes]:
    """A tell in its own process, held mid-extraction until `go`, as `host` when one is named, its claim lapsing
    `lapse` seconds after each renewal."""
    return subprocess.Popen([sys.executable, "-c", HOLDER, str(root), host, str(HERE), str(lapse)],
                            stdout=subprocess.PIPE)


def kept(workspace: Any, claim: dict[str, Any]) -> str:
    record = Record(id="r_0held0000000", source=TOLD, revision="1", at="2026-11-17T09:00:00Z", body=SAID, by=YOU,
                    fields={"target": "projects/harbor", "as": "fact"}, extract="facts")
    workspace.store.keep(record)
    workspace.store.set_state(RETRY, [record.id])
    workspace.store.set_state(CLAIMED, {record.id: {"by": "x", "host": socket.gethostname(), **claim}})
    return record.id


def test_a_claim_whose_pid_another_live_program_holds_lapses_and_the_next_pass_extracts_it(tmp_path: Path) -> None:
    layer = FakeExtraction()
    workspace = in_memory(tmp_path, layer)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        id = kept(workspace, {"pid": other.pid, "until": "2026-11-17T09:30:00Z"})
        result = workspace.run_once(agents=False)
    finally:
        other.kill()
        other.wait(10)
    assert result.read == 1 and [f.text for f in layer.facts(record=id)] == [SAID]
    assert workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED) and f"tell/{id}" not in result.skipped


@pytest.mark.parametrize("lapsed", [True, False])
def test_a_claim_whose_pid_a_process_of_another_user_holds_stands_until_it_lapses(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lapsed: bool) -> None:
    def refused(pid: int, signal: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    layer = FakeExtraction()
    workspace = in_memory(tmp_path, layer)
    until = "2026-11-17T09:30:00Z" if lapsed else stamp(workspace.clock() + timedelta(minutes=20))
    id = kept(workspace, {"pid": 1 if os.getpid() != 1 else 2, "until": until})
    monkeypatch.setattr(os, "kill", refused)
    result = workspace.run_once(agents=False)
    monkeypatch.undo()
    if lapsed:
        assert result.read == 1 and workspace.store.state(RETRY) == [] and f"tell/{id}" not in result.skipped
    else:
        assert result.read == 0 and workspace.store.state(RETRY) == [id] and layer.facts(record=id) == []
        assert result.skipped[f"tell/{id}"] == ("its tell or another pass extracts it now: this pass leaves it to "
                                                f"that, or to its claim to lapse at {until}")


def test_a_holder_that_lives_and_renews_keeps_its_claim_past_the_lapse_and_a_pass_says_until_when(
        tmp_path: Path) -> None:
    harbor(tmp_path)
    teller = holder(tmp_path, "elsewhere.example", 1.0)  # another machine's, so no pid keeps its claim: renewing does
    try:
        assert started(tmp_path, teller)
        time.sleep(2.5)
        layer = FileExtraction(tmp_path / "layer")
        with make_workspace(tmp_path, layer) as workspace:
            id = told(workspace)
            result = workspace.run_once(agents=False)
            assert layer.called() == [id] and workspace.store.state(RETRY) == [id]
            until = workspace.store.state(CLAIMED)[id]["until"]
            assert datetime.fromisoformat(until) > workspace.clock() - timedelta(seconds=1)
            assert result.skipped[f"tell/{id}"].endswith(f"or to its claim to lapse at {until}")
            (tmp_path / "go").write_text("")
            assert teller.wait(60) == 0
            workspace.run_once(agents=False)
            assert layer.called() == [id] and [f.text for f in layer.facts(record=id)] == [SAID]
            assert workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED)
    finally:
        teller.kill()


def test_a_holder_on_another_machine_that_was_killed_is_taken_over_once_its_claim_lapses(tmp_path: Path) -> None:
    harbor(tmp_path)
    teller = holder(tmp_path, "elsewhere.example", 30.0)
    try:
        assert started(tmp_path, teller)
    finally:
        teller.kill()
        teller.wait(60)
    layer = FileExtraction(tmp_path / "layer")
    with make_workspace(tmp_path, layer) as workspace:
        id = told(workspace)
        until = datetime.fromisoformat(workspace.store.state(CLAIMED)[id]["until"])
        assert "or to its claim to lapse at" in workspace.run_once(agents=False).skipped[f"tell/{id}"]
        assert layer.called() == [id] and workspace.store.state(RETRY) == [id]
        workspace.clock = lambda: until + timedelta(seconds=1)
        workspace.run_once(agents=False)
        assert layer.called() == [id, id] and [f.text for f in layer.facts(record=id)] == [SAID]
        assert workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED)


@pytest.mark.parametrize("killed", [False, True])
def test_a_clock_set_back_an_hour_leaves_a_live_holder_its_claim_and_takes_a_dead_ones_at_once(
        tmp_path: Path, killed: bool) -> None:
    harbor(tmp_path)
    teller = holder(tmp_path, "", 1.0)
    try:
        assert started(tmp_path, teller)
        if killed:
            teller.kill()
            teller.wait(60)
        layer = FileExtraction(tmp_path / "layer")
        with make_workspace(tmp_path, layer) as workspace:
            id = told(workspace)
            workspace.clock = lambda: datetime.now(UTC) - timedelta(hours=1)
            result = workspace.run_once(agents=False)
            if killed:
                assert layer.called() == [id, id] and workspace.store.state(RETRY) == []
                assert f"tell/{id}" not in result.skipped
            else:
                assert layer.called() == [id] and f"tell/{id}" in result.skipped
                (tmp_path / "go").write_text("")
                assert teller.wait(60) == 0
            assert [f.text for f in layer.facts(record=id)] == [SAID] and not workspace.store.state(CLAIMED)
    finally:
        teller.kill()


@pytest.mark.parametrize("finished", [True, False])
def test_a_holder_that_lost_its_claim_while_stopped_neither_links_nor_takes_back_another_holders_fact(
        tmp_path: Path, finished: bool) -> None:
    harbor(tmp_path)
    teller = holder(tmp_path, "", 1.0)
    try:
        assert started(tmp_path, teller)
        os.kill(teller.pid, signal.SIGSTOP)
        time.sleep(2.0)
        layer = FileExtraction(tmp_path / "layer")
        with make_workspace(tmp_path, layer) as workspace:
            id = told(workspace)
            if finished:
                assert workspace.run_once(agents=False).read == 1 and workspace.store.state(RETRY) == []
            else:
                workspace.store.set_state(CLAIMED, {id: {"by": "a pass", "host": "elsewhere.example", "pid": 1,
                                                     "until": stamp(workspace.clock() + timedelta(minutes=20))}})
            (tmp_path / "go").write_text("")
            os.kill(teller.pid, signal.SIGCONT)
            said, _ = teller.communicate(timeout=60)
            note = f"Kept {id}; its extraction was held up past its claim, so another process took it over" + (
                " and gave its facts." if finished else ", and it or a later pass extracts its facts.")
            assert teller.returncode == 0 and json.loads(said) == [note]
            layer._load()
            if finished:
                assert [f.text for f in layer.facts(record=id)] == [SAID]
            assert len(workspace.store.facts_of("projects/harbor")) == (1 if finished else 0)
            assert workspace.store.state(RETRY) == ([] if finished else [id])
            assert (workspace.store.state(CLAIMED) or {}).get(id, {}).get("by") in (None if finished else "a pass",)
    finally:
        teller.kill()


class Down(FakeExtraction):
    def extract(self, record: Record) -> Any:
        raise ExtractionFailed("the facts layer is down")


@pytest.mark.parametrize("n", [0, 1, 40])
def test_status_names_every_tell_whose_facts_wait(tmp_path: Path, n: int) -> None:
    harbor(tmp_path)
    with make_workspace(tmp_path, Down()) as workspace:
        ids = [workspace.tell(YOU, f"{SAID} Note {i}.", "projects/harbor", "fact").record for i in range(n)]
    printed = CliRunner().invoke(main, ["status", "--workspace", str(tmp_path)]).output
    as_json = json.loads(CliRunner().invoke(main, ["status", "--json", "--workspace", str(tmp_path)]).output)
    rows = [line for line in printed.splitlines() if line.startswith("underived ")]
    row = (f"underived {n} tell{'' if n == 1 else 's'} whose facts have not derived yet: {', '.join(ids)}. The next "
           "pass tries again.")
    assert rows == ([] if n == 0 else [row])
    assert as_json["tells_waiting"] == n and as_json["tells_waiting_ids"] == ids


def test_a_claim_renewed_part_way_through_a_second_holds_the_whole_lapse(monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import records

    monkeypatch.setattr(records, "CLAIM_LAPSE", 1.0)
    for fraction in (0.0, 0.05, 0.5, 0.95, 0.999):
        renewed = datetime(2026, 11, 20, 9, 0, 0, tzinfo=UTC) + timedelta(seconds=fraction)
        until = datetime.fromisoformat(records._holder("token", renewed)["until"])
        assert timedelta(seconds=1) <= until - renewed < timedelta(seconds=2)


TWO = f"{SAID}\n\nThe launch is in Lisbon."


class Fresh(FakeExtraction):
    """The fake facts layer, but each extraction gives new fact ids and leaves the earlier ones current, as Factoid
    does. `before` runs once, as the first extraction starts."""

    def __init__(self) -> None:
        super().__init__()
        self.n, self.before = 0, None

    def extract(self, record: Record) -> Any:
        self.n += 1
        n, before, self.before = self.n, self.before, None
        if before:
            before()
        kept = self.by_record.pop(record.id, [])
        out = super().extract(record)
        for fact in [*self.by_record[record.id], *out.facts]:
            fact.id = f"{fact.id}x{n}"
        self.by_record[record.id] = kept + self.by_record.pop(record.id)
        return out


def lapsed(workspace: Any) -> str:
    """Make the claim on the waiting told record one of another machine's whose time has passed, and give its id."""
    id = workspace.store.state(RETRY)[-1]
    claim = workspace.store.state(CLAIMED)[id]
    workspace.store.set_state(CLAIMED, {id: {**claim, "host": "elsewhere.example", "until": "2000-01-01T00:00:00Z"}})
    return id


def taken_over(workspace: Any, layer: Any) -> None:
    from cerebro.records import extract_told

    id = lapsed(workspace)
    extract_told(workspace.store, layer, workspace.store.record(id), stamp(workspace.clock()))


@pytest.mark.parametrize("when", ["in the model call", "before the model call", "before the linking write"])
def test_a_holder_whose_claim_was_taken_over_leaves_the_new_holders_facts_alone_and_none_of_its_own(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str) -> None:
    from contextlib import contextmanager

    from cerebro import records

    layer = Fresh()
    workspace = in_memory(tmp_path, layer)
    if when == "in the model call":
        layer.before = lambda: taken_over(workspace, layer)
    else:
        once, renewing, exclusive = [True], records._renewing, workspace.store.exclusive

        @contextmanager
        def first(of: Any, *args: Any, **options: Any) -> Any:
            if once[0] and (when == "before the model call") == (of is renewing):
                once[0] = False
                taken_over(workspace, layer)
            with of(*args, **options):
                yield

        if when == "before the model call":
            monkeypatch.setattr(records, "_renewing", lambda *a, **o: first(renewing, *a, **o))
        else:  # the takeover lands once the tell's extraction is done, as it looks at the store again
            layer.before = lambda: monkeypatch.setattr(workspace.store, "exclusive", lambda: first(exclusive))
    result = workspace.tell(YOU, TWO, "projects/harbor", "fact")
    current = layer.facts(record=result.record)
    assert sorted(f.text for f in current) == sorted(TWO.split("\n\n"))
    assert sorted(workspace.store.facts_of("projects/harbor")) == sorted(f.id for f in current)
    assert layer.n == (1 if when == "before the model call" else 2)
    assert result.notes == [(f"Kept {result.record}; its extraction was held up past its claim, so another process "
                             "took it over and gave its facts.")]
    assert workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED)


def test_a_record_taken_back_while_its_holder_was_held_up_past_its_claim_keeps_none_of_its_facts(
        tmp_path: Path) -> None:
    from cerebro.records import retract

    layer = Fresh()
    workspace = in_memory(tmp_path, layer)
    layer.before = lambda: retract(workspace.store, layer, YOU, lapsed(workspace), "said in error")
    result = workspace.tell(YOU, TWO, "projects/harbor", "fact")
    assert layer.facts(record=result.record) == [] and workspace.store.facts_of("projects/harbor") == []
    assert result.notes == [(f"Kept {result.record}; its extraction was held up past its claim, and it was taken "
                             "back meanwhile, so none of its facts stand.")]
    assert workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED)
