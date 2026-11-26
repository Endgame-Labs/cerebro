"""A told record whose extraction did not finish, however the process ended, is extracted by a later pass, once."""

from __future__ import annotations

import os
import pickle
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from cerebro import Caller, Cerebro, Record
from cerebro.article import Identity
from cerebro.facts import Extracted, ExtractionFailed
from cerebro.fakes import FakeExtraction, MemoryStore, ScriptedRun
from cerebro.records import CLAIMED, RETRY, TOLD

YOU = Caller("you", "person")
HERE = Path(__file__).resolve().parent
SAID = "The beta moved to 11 December."


class FileExtraction(FakeExtraction):
    """The fake facts layer, kept in a file so a child process and its parent read one layer. Each call to extract is
    a line of `calls`. `kill` ends the process with SIGKILL at a step of a told record's extraction: `start`, as it is
    handed over, or `made`, once its facts are kept in the layer and before they reach the store. `hold` waits at the
    start for a file named `go` beside the layer, and leaves a file named `started` there first."""

    def __init__(self, path: Path, kill: str = "") -> None:
        super().__init__()
        self.path, self.kill = path, kill
        self.calls = path.with_name("calls")
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self.by_record = pickle.loads(self.path.read_bytes())

    def _save(self) -> None:
        self.path.write_bytes(pickle.dumps(self.by_record))

    def extract(self, record: Record) -> Extracted:
        self._load()
        with self.calls.open("a") as calls:
            calls.write(record.id + "\n")
        if record.source == TOLD and self.kill == "start":
            os.kill(os.getpid(), signal.SIGKILL)
        if record.source == TOLD and self.kill == "hold":
            self.path.with_name("started").write_text("")
            for _ in range(1200):
                if self.path.with_name("go").exists():
                    break
                time.sleep(0.05)
            self._load()  # another process may have written the layer while this one waited
        extracted = super().extract(record)
        self._save()
        if record.source == TOLD and self.kill == "made":
            os.kill(os.getpid(), signal.SIGKILL)
        return extracted

    def facts(self, **filters: Any) -> Any:
        self._load()
        return super().facts(**filters)

    def retract(self, fact_id: str) -> None:
        self._load()
        super().retract(fact_id)
        self._save()

    def called(self) -> list[str]:
        return self.calls.read_text().split() if self.calls.exists() else []


def make_workspace(root: Path, extraction: Any = None, store: Any = None) -> Cerebro:
    (root / "settings.yaml").write_text("{}\n")
    return Cerebro(root, store=store, extraction=extraction, connectors=[], trusted=True,
                   agent_run=ScriptedRun(lambda prompt: {}))


CHILD = """
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from cerebro.run import block_model_requests
block_model_requests()
import cerebro.records
from cerebro import Caller
import test_tell_retry as t

root, step = Path(sys.argv[1]), sys.argv[2]
if step == "landing":
    cerebro.records.link = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
workspace = t.make_workspace(root, t.FileExtraction(root / "layer", kill="" if step == "landing" else step))
workspace.tell(Caller("you", "person"), t.SAID, "projects/harbor", "fact")
"""


def child(root: Path, step: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", CHILD, str(root), step, str(HERE)])


def harbor(root: Path) -> None:
    with make_workspace(root) as workspace:
        workspace.store.create(Identity("projects/harbor", "projects", "Harbor"))


def started(root: Path, teller: subprocess.Popen[bytes]) -> bool:
    """Whether the child's extraction has started, waiting up to a minute while the child runs."""
    for _ in range(1200):
        if (root / "started").exists():
            return True
        if teller.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def told(workspace: Cerebro) -> str:
    return workspace.store.records(TOLD)[-1].id


@pytest.mark.parametrize("step", ["start", "made", "landing"])
def test_a_tell_whose_process_was_killed_before_its_facts_landed_is_extracted_by_the_next_pass_once(
        tmp_path: Path, step: str) -> None:
    harbor(tmp_path)
    teller = child(tmp_path, step)
    try:
        assert teller.wait(60) == -signal.SIGKILL
    finally:
        teller.kill()
    layer = FileExtraction(tmp_path / "layer")
    with make_workspace(tmp_path, layer) as workspace:
        id = told(workspace)
        assert workspace.store.state(RETRY) == [id] and workspace.status()["tells_waiting"] == 1
        workspace.run_once(agents=False)
        facts = layer.facts(record=id)
        assert [f.text for f in facts] == [SAID]
        assert workspace.store.facts_of("projects/harbor") == [facts[0].id]
        assert workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED)
        assert workspace.status()["tells_waiting"] == 0
        workspace.run_once(agents=False)
    assert layer.called() == [id, id]  # the killed child's and the first pass's: the second pass reads nothing


def test_a_pass_leaves_a_tell_a_live_process_extracts_and_takes_it_once_that_process_is_killed(
        tmp_path: Path) -> None:
    harbor(tmp_path)
    teller = child(tmp_path, "hold")
    try:
        assert started(tmp_path, teller)
        layer = FileExtraction(tmp_path / "layer")
        with make_workspace(tmp_path, layer) as workspace:
            id = told(workspace)
            workspace.run_once(agents=False)
            assert layer.called() == [id] and workspace.store.state(RETRY) == [id]
            teller.kill()
            teller.wait(60)
            workspace.run_once(agents=False)
            assert layer.called() == [id, id] and [f.text for f in layer.facts(record=id)] == [SAID]
            assert workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED)
    finally:
        teller.kill()


class Held(FakeExtraction):
    """The fake facts layer, whose extraction of a told record waits for `go`, after it sets `started`."""

    def __init__(self) -> None:
        super().__init__()
        self.started, self.go, self.calls = threading.Event(), threading.Event(), []

    def extract(self, record: Record) -> Extracted:
        self.calls.append(record.id)
        if record.source == TOLD and not self.go.is_set():
            self.started.set()
            assert self.go.wait(10)
        return super().extract(record)


def in_memory(tmp_path: Path, extraction: Any) -> Cerebro:
    workspace = make_workspace(tmp_path, extraction, MemoryStore())
    workspace.store.create(Identity("projects/harbor", "projects", "Harbor"))
    return workspace


def telling(workspace: Cerebro) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def tell() -> None:
        try:
            out.append(workspace.tell(YOU, SAID, "projects/harbor", "fact"))
        except BaseException as cut:  # noqa: BLE001 - a cut tell is what some tests make, and they read it here
            out.append(cut)

    thread = threading.Thread(target=tell)
    thread.start()
    return thread, out


def test_a_pass_under_way_beside_a_tell_does_not_extract_its_record_a_second_time(tmp_path: Path) -> None:
    layer = Held()
    workspace = in_memory(tmp_path, layer)
    thread, out = telling(workspace)
    assert layer.started.wait(10)
    id = told(workspace)
    assert workspace.store.state(RETRY) == [id]
    workspace.run_once(agents=False)
    assert layer.calls == [id]
    layer.go.set()
    thread.join(10)
    assert len(out[0].facts) == 1 and workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED)
    workspace.run_once(agents=False)
    assert layer.calls == [id] and len(layer.facts(record=id)) == 1


def test_a_record_taken_back_while_its_facts_were_extracted_keeps_no_fact(tmp_path: Path) -> None:
    layer = Held()
    workspace = in_memory(tmp_path, layer)
    thread, out = telling(workspace)
    assert layer.started.wait(10)
    id = told(workspace)
    workspace.retract(YOU, id, "said in error")
    layer.go.set()
    thread.join(10)
    assert out[0].facts == [] and layer.facts(record=id) == [] and workspace.store.facts_of("projects/harbor") == []
    assert workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED)


class Failing(FakeExtraction):
    """The fake facts layer, whose first extraction of a told record raises `error` halfway."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error, self.calls = error, []

    def extract(self, record: Record) -> Extracted:
        self.calls.append(record.id)
        if record.source == TOLD and self.error is not None:
            error, self.error = self.error, None
            raise error
        return super().extract(record)


def test_a_model_error_mid_extraction_keeps_the_tell_waiting_and_says_so(tmp_path: Path) -> None:
    layer = Failing(ExtractionFailed("record r of tell: factoid failed: the model answered 529"))
    workspace = in_memory(tmp_path, layer)
    said = workspace.tell(YOU, SAID, "projects/harbor", "fact")
    assert said.facts == [] and said.record is not None
    assert any(note.startswith(f"Kept {said.record}; its facts did not come out yet") and
               note.endswith("a later pass extracts them.") for note in said.notes)
    assert workspace.status()["tells_waiting"] == 1 and not workspace.store.state(CLAIMED)
    workspace.run_once(agents=False)
    assert len(layer.facts(record=said.record)) == 1 and workspace.status()["tells_waiting"] == 0
    assert layer.calls == [said.record, said.record]


def test_a_tell_cut_by_an_interrupt_mid_extraction_waits_for_the_next_pass(tmp_path: Path) -> None:
    layer = Failing(KeyboardInterrupt())
    workspace = in_memory(tmp_path, layer)
    with pytest.raises(KeyboardInterrupt):
        workspace.tell(YOU, SAID, "projects/harbor", "fact")
    id = told(workspace)
    assert workspace.store.state(RETRY) == [id] and not workspace.store.state(CLAIMED)
    workspace.run_once(agents=False)
    assert len(layer.facts(record=id)) == 1 and workspace.store.state(RETRY) == []


def test_a_tell_with_no_facts_layer_says_its_facts_wait(tmp_path: Path) -> None:
    workspace = in_memory(tmp_path, None)
    said = workspace.tell(YOU, SAID, "projects/harbor", "fact")
    assert said.facts == [] and workspace.store.state(RETRY) == [said.record]
    assert any(note.startswith(f"Kept {said.record}; its facts did not come out yet") for note in said.notes)
    assert workspace.status()["tells_waiting"] == 1 and not workspace.store.state(CLAIMED)


def test_a_proposal_tell_waits_for_no_extraction(tmp_path: Path) -> None:
    workspace = in_memory(tmp_path, FakeExtraction())
    said = workspace.tell(YOU, "Lead with the beta date.", "projects/harbor", "proposal")
    assert said.proposals and workspace.store.state(RETRY) in (None, [])


def test_an_apps_record_a_pass_was_cut_while_extracting_is_extracted_by_the_next_pass_once(tmp_path: Path) -> None:
    from cerebro.fakes import notes_app

    class CutOnce(FakeExtraction):
        cut, calls = True, []

        def extract(self, record: Record) -> Extracted:
            self.calls.append(record.id)
            if self.cut:
                self.cut = False
                raise KeyboardInterrupt
            return super().extract(record)

    layer = CutOnce()
    (tmp_path / "settings.yaml").write_text("{}\n")
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=layer, apps=[notes_app()], connectors=[],
                    agent_run=ScriptedRun(lambda prompt: {}))
    kept = workspace.handle.keep(YOU, "note", SAID, {"collection": "notes"})  # an app keeps, and only a pass extracts
    assert layer.calls == []
    with pytest.raises(KeyboardInterrupt):
        workspace.run_once(agents=False)
    workspace.run_once(agents=False)
    workspace.run_once(agents=False)
    assert layer.calls == [kept.id, kept.id] and [f.text for f in layer.facts(record=kept.id)] == [SAID]


def landing_in_the_gap(workspace: Cerebro, layer: Held, thread: threading.Thread, then: Any = None) -> None:
    """Have the tell land its facts after a pass read the list and before it reaches the record, then run `then`."""
    id, real, fired = told(workspace), workspace.store.record, []

    def record(rid: str) -> Any:  # the pass reads a record after it read the list
        if rid == id and not fired:
            fired.append(1)
            layer.go.set()
            thread.join(10)
            if then is not None:
                then()
        return real(rid)

    workspace.store.record = record  # type: ignore[method-assign]
    try:
        workspace.run_once(agents=False)
    finally:
        workspace.store.record = real  # type: ignore[method-assign]


def test_a_tell_that_lands_after_a_pass_read_the_list_is_not_extracted_again(tmp_path: Path) -> None:
    layer = Held()
    workspace = in_memory(tmp_path, layer)
    thread, out = telling(workspace)
    assert layer.started.wait(10)
    landing_in_the_gap(workspace, layer, thread)
    assert layer.calls == [told(workspace)] and len(out[0].facts) == 1


def test_a_fact_taken_back_between_a_tell_landing_and_a_pass_reaching_it_stays_taken_back(tmp_path: Path) -> None:
    layer = Held()
    workspace = in_memory(tmp_path, layer)
    thread, out = telling(workspace)
    assert layer.started.wait(10)
    landing_in_the_gap(workspace, layer, thread, lambda: workspace.retract(YOU, out[0].facts[0], "wrong"))
    assert layer.facts(record=told(workspace)) == []


class RetractFails(Held):
    """The fake facts layer whose retract raises `error`, `times` times."""

    def __init__(self, error: BaseException, times: int) -> None:
        super().__init__()
        self.error, self.times = error, times

    def retract(self, fact_id: str) -> None:
        if self.times:
            self.times -= 1
            raise self.error
        super().retract(fact_id)


def test_a_record_taken_back_mid_extraction_and_cut_before_its_facts_are_retracted_loses_them_at_the_next_pass(
        tmp_path: Path) -> None:
    layer = RetractFails(KeyboardInterrupt(), 1)  # the cut, as the facts are retracted
    workspace = in_memory(tmp_path, layer)
    thread, out = telling(workspace)
    assert layer.started.wait(10)
    id = told(workspace)
    workspace.retract(YOU, id, "said in error")
    layer.go.set()
    thread.join(10)
    assert isinstance(out[0], KeyboardInterrupt) and layer.facts(record=id) != [] and workspace.store.state(CLAIMED)
    workspace.run_once(agents=False)
    assert layer.facts(record=id) == [] and not workspace.store.state(CLAIMED) and workspace.store.state(RETRY) == []


def test_a_tell_taken_back_whose_facts_cannot_be_retracted_never_says_a_pass_extracts_them(tmp_path: Path) -> None:
    layer = RetractFails(RuntimeError("the facts layer is down"), 1)
    workspace = in_memory(tmp_path, layer)
    thread, out = telling(workspace)
    assert layer.started.wait(10)
    id = told(workspace)
    workspace.retract(YOU, id, "said in error")
    layer.go.set()
    thread.join(10)
    assert not any("a later pass extracts them" in note for note in out[0].notes)
    assert any(note.startswith(f"Kept {id}; it was taken back while its facts came out") for note in out[0].notes)
    workspace.run_once(agents=False)
    assert layer.facts(record=id) == [] and not workspace.store.state(CLAIMED)


def test_a_claim_on_this_machine_holds_until_it_lapses_while_its_pid_answers(tmp_path: Path) -> None:
    from cerebro.records import Claimed, extract_told

    workspace = in_memory(tmp_path, FakeExtraction())
    record = Record(id="r_0slow0000000", source=TOLD, revision="1", at="2026-11-17T09:00:00Z", body=SAID, by=YOU,
                    fields={"target": "projects/harbor", "as": "fact"}, extract="facts")
    workspace.store.keep(record)
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        workspace.store.set_state(RETRY, [record.id])
        workspace.store.set_state(CLAIMED, {record.id: {"by": "x", "host": socket.gethostname(), "pid": alive.pid,
                                                    "until": "2026-11-17T09:30:00Z"}})
        with pytest.raises(Claimed):
            extract_told(workspace.store, workspace.extraction, record, "2026-11-17T09:29:59Z")
        assert extract_told(workspace.store, workspace.extraction, record, "2026-11-17T09:30:01Z")
    finally:
        alive.kill()
        alive.wait(10)
    assert workspace.store.state(RETRY) == [] and not workspace.store.state(CLAIMED)


def test_an_interrupt_before_the_extraction_starts_leaves_no_claim_this_process_holds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.records

    def cut(*args: Any, **options: Any) -> list[str]:
        raise KeyboardInterrupt

    layer = FakeExtraction()
    workspace = in_memory(tmp_path, layer)
    monkeypatch.setattr(cerebro.records, "extract_told", cut)
    with pytest.raises(KeyboardInterrupt):
        workspace.tell(YOU, SAID, "projects/harbor", "fact")
    monkeypatch.undo()
    workspace.run_once(agents=False)
    assert [f.text for f in layer.facts(record=told(workspace))] == [SAID] and workspace.store.state(RETRY) == []


def test_a_claim_of_a_dead_process_on_a_record_taken_back_is_dropped(tmp_path: Path) -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(10)
    workspace = in_memory(tmp_path, FakeExtraction())
    said = workspace.tell(YOU, SAID, "projects/harbor", "fact")
    workspace.store.set_state(RETRY, [said.record])
    workspace.store.set_state(CLAIMED, {said.record: {"by": "x", "host": socket.gethostname(), "pid": gone.pid,
                                                  "until": "2099-01-01T00:00:00Z"}})
    workspace.retract(YOU, said.record or "", "wrong")
    assert not workspace.store.state(CLAIMED) and workspace.store.state(RETRY) == []
