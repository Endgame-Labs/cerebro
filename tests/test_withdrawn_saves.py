"""Earlier saves when something is taken back, when a source lists a document again, and when a save fails part-way:
taking a thing back takes back every save of it, an expire holds while its document's revision holds, and a failure
at any step of a save loses nothing a source said."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cerebro import Caller, Cerebro, Record, fakes
from cerebro.article import Identity
from cerebro.expiry import marks
from cerebro.facts import Extracted, ExtractionFailed, Revisions
from cerebro.fakes import FakeChanges, FakeConnector, FakeExtraction, MemoryStore, ScriptedRun, notes_app
from cerebro.records import proposals
from cerebro.store import SQLiteStore, Store

NOW = datetime(2026, 11, 18, 9, tzinfo=UTC)
YOU, DESK, AGENT = Caller("you", "person"), Caller("desk-agent", "person"), Caller("notes/article", "agent")
DOC = "local/knowledge/discounts.md"
V1 = Record(id=DOC, source="local", revision="sha256:aaa1", at="2026-09-01", body="The discount limit is 10%.")
V2 = Record(id=DOC, source="local", revision="sha256:bbb2", at="2026-11-10", body="The discount limit is 15%.")


def make_store(kind: str, root: Path) -> Store:
    return MemoryStore() if kind == "memory" else SQLiteStore(root / "cerebro.sqlite")


@pytest.fixture(params=["memory", "sqlite"])
def kind(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def fresh_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """New fact ids on each extraction, as Factoid gives them."""
    made, count = fakes.fact_id, itertools.count()
    monkeypatch.setattr(fakes, "fact_id", lambda seed: made(f"{seed}:{next(count)}"))


def workspace_of(root: Path, kind: str, **options: Any) -> Cerebro:
    (root / "settings.yaml").write_text("{}\n")
    options.setdefault("extraction", FakeExtraction())
    return Cerebro(root, store=make_store(kind, root), clock=lambda: NOW, trusted=True,
                   agent_run=ScriptedRun(lambda prompt: {}, clock=lambda: NOW), **options)


def every_save(workspace: Cerebro, record: str) -> list[tuple[str, str]]:
    """What any caller reads of a record with latest: false."""
    return [(f.text, f.status) for f in workspace.lookup.query(DESK, "facts", {"record": record, "latest": False}).items]


# -- taking back takes back every save ------------------------------------------------------------------------------

def test_a_person_taking_back_a_kept_record_takes_back_every_save_of_it(tmp_path: Path, kind: str) -> None:
    workspace = workspace_of(tmp_path, kind, apps=[notes_app()])
    first = workspace.handle.keep(YOU, "note", "Ada's home address is 12 Elm Street.", {"collection": "x"})
    workspace.run_once()
    workspace.handle.keep(YOU, "note", "Ada moved; ask her for the new address.", {"collection": "x"}, id=first.id)
    workspace.run_once()
    assert ("Ada's home address is 12 Elm Street.", "superseded") in every_save(workspace, first.id)
    taken = workspace.retract(YOU, first.id, "this note should not be here")
    assert every_save(workspace, first.id) == []
    assert len([id for id in taken.withdrew if id.startswith("fact_")]) == 2
    assert {r["status"] for r in workspace.store.superseded(record=first.id)} == {"retracted"}


def test_a_correction_taken_back_leaves_no_fact_of_it_in_any_save(tmp_path: Path, kind: str) -> None:
    workspace = workspace_of(tmp_path, kind)
    workspace.store.create(Identity("projects/apollo", "projects", "Apollo"))
    told = workspace.tell(YOU, "The Apollo review moved to 3 March.", target="projects/apollo", as_="fact")
    assert every_save(workspace, told.record)
    workspace.retract(YOU, told.record)
    assert every_save(workspace, told.record) == []


def test_a_fact_taken_back_takes_back_an_earlier_save_of_it_in_the_same_words(tmp_path: Path, kind: str,
                                                                             fresh_ids: None) -> None:
    notes = FakeConnector("local", [V1])
    workspace = workspace_of(tmp_path, kind, connectors=[notes])
    workspace.run_once()
    notes.add(V2)
    workspace.run_once()
    notes.add(Record(id=DOC, source="local", revision="sha256:ccc3", at="2026-11-12", body=V1.body))
    workspace.run_once()
    again = workspace.extraction.facts(record=DOC)[0]
    assert again.text == V1.body and [s for t, s in every_save(workspace, DOC) if t == V1.body] == ["current"]
    workspace.retract(YOU, again.id)
    assert V1.body not in [t for t, _ in every_save(workspace, DOC)]
    assert every_save(workspace, DOC) == [(V2.body, "superseded")]


def test_an_expire_taken_back_marks_no_save_of_its_document(tmp_path: Path, kind: str, fresh_ids: None) -> None:
    notes = FakeChanges("local", [V1])
    workspace = workspace_of(tmp_path, kind, connectors=[notes])
    workspace.run_once()
    expire = workspace.tell(YOU, "This policy was replaced in March.", target=DOC, kind="expire").proposals[0]
    notes.add(V2)
    workspace.run_once()
    every = workspace.extraction.facts(record=DOC, latest=False)
    assert set(marks(workspace.store, workspace.extraction, every)) == {f.id for f in every if f.status == "superseded"}
    workspace.retract(YOU, expire)
    assert marks(workspace.store, workspace.extraction, workspace.extraction.facts(record=DOC, latest=False)) == {}


# -- an expire holds while its document's revision holds ------------------------------------------------------------

def expired(tmp_path: Path, kind: str) -> tuple[Cerebro, FakeChanges, str]:
    notes = FakeChanges("local", [V1])
    workspace = workspace_of(tmp_path, kind, connectors=[notes])
    workspace.run_once()
    return workspace, notes, workspace.tell(YOU, "This policy was replaced in March.", target=DOC, kind="expire").proposals[0]


def marked(workspace: Cerebro) -> set[str]:
    return {m.proposal for m in marks(workspace.store, workspace.extraction, workspace.extraction.facts(record=DOC)).values()}


def waiting_expire(workspace: Cerebro) -> str:
    """An expire the knowledge curator filed, waiting for a person, bound to the document as it stands."""
    from cerebro.expiry import revision
    from cerebro.records import Proposal, propose

    cites = [f.id for f in workspace.extraction.facts(record=DOC)]
    return propose(workspace.store, Proposal(id="p_curator00001", kind="record.expire", target=DOC,
                                         by=Caller("knowledge/curator", "agent"), at="2026-11-18T09:00:00Z",
                                         cites=cites, reason="replaced in March",
                                         payload={"revision": revision(workspace.store, workspace.extraction, DOC)})).id


def test_an_accepted_expire_holds_when_its_source_lists_the_same_revision_again(tmp_path: Path, kind: str,
                                                                               fresh_ids: None) -> None:
    workspace, notes, expire = expired(tmp_path, kind)
    before = {f.id for f in workspace.extraction.facts(record=DOC)}
    notes.forget()  # the next listing is in full, and gives the same revision
    assert workspace.run_once().extracted == 1
    assert {f.id for f in workspace.extraction.facts(record=DOC)}.isdisjoint(before) and marked(workspace) == {expire}
    notes.add(V2)
    workspace.run_once()
    assert marked(workspace) == set()  # a new revision is the document changed


def test_a_waiting_expire_does_not_stand_down_on_a_full_listing_of_the_same_revision(tmp_path: Path, kind: str,
                                                                                    fresh_ids: None) -> None:
    from cerebro.expiry import stood_down

    notes = FakeChanges("local", [V1])
    workspace = workspace_of(tmp_path, kind, connectors=[notes])
    workspace.run_once()
    filed = waiting_expire(workspace)
    notes.forget()
    workspace.run_once()
    row = next(p for p in proposals(workspace.store) if p.id == filed)
    assert row.status == "waiting" and not stood_down(workspace.store, workspace.extraction, row)


def test_with_two_accepted_expires_taking_back_the_newer_leaves_the_older_mark(tmp_path: Path, kind: str) -> None:
    from cerebro.records import Proposal, decide, propose

    workspace, _, older = expired(tmp_path, kind)
    current = workspace.extraction.facts(record=DOC)
    newer = propose(workspace.store, Proposal(id="p_newer000001", kind="record.expire", target=DOC, by=YOU,
                                         at="2026-11-18T10:00:00Z", cites=[current[0].id], reason="still replaced",
                                         payload={"revision": "sha256:aaa1"}))
    decide(workspace.store, YOU, accept=[newer.id], extraction=workspace.extraction, clock=lambda: NOW)
    assert marked(workspace) == {newer.id}
    workspace.retract(YOU, newer.id)
    assert marked(workspace) == {older}


# -- a failure at any step of a save loses nothing ------------------------------------------------------------------

class Staged(FakeExtraction):
    """A facts layer that fails once at a named step: before its model, or after retiring the old facts, as
    Factoid's ingest retires a document's facts before its model extracts the new ones."""

    def __init__(self) -> None:
        super().__init__()
        self.at = ""

    def extract(self, record: Record) -> Extracted:
        step, self.at = self.at, ""
        if step == "before the model":
            raise ExtractionFailed("the model did not answer")
        if step == "during the model":
            for fact in self.by_record.get(record.id, []):
                fact.status = "retracted"
            raise ExtractionFailed("the model did not answer after 3 tries")
        return super().extract(record)


def end_state(layer: Revisions, store: Store) -> tuple[Any, ...]:
    every = layer.facts(record=DOC, latest=False)
    return (sorted((f.text, f.status, f.revision) for f in every),
            sorted(r["fact"]["text"] for r in store.superseded() if r["status"] == "superseded"
                   and r["fact"]["text"] not in {f.text for f in layer.facts(record=DOC)}),
            store.state(f"saved:local/{DOC}")["revision"])


@pytest.mark.parametrize("step", ["none", "before the supersede", "before the model", "during the model",
                                  "after the model, before the save is noted"])
def test_a_save_that_fails_at_any_step_ends_after_its_retry_as_one_that_never_failed(step: str, kind: str,
                                                                                    tmp_path: Path) -> None:
    store = make_store(kind, tmp_path)
    handler = Staged()
    layer = Revisions(handler, store, clock=lambda: NOW)
    layer.extract(V1)
    if step in ("before the model", "during the model"):
        handler.at = step
    original_supersede, original_set = store.supersede, store.set_state
    if step == "before the supersede":
        def supersede(*args: Any, **options: Any) -> int:
            store.supersede = original_supersede  # type: ignore[method-assign]
            raise OSError("database is locked")
        store.supersede = supersede  # type: ignore[method-assign]
    if step == "after the model, before the save is noted":
        def set_state(key: str, value: Any) -> None:
            store.set_state = original_set  # type: ignore[method-assign]
            raise OSError("disk full")
        store.set_state = set_state  # type: ignore[method-assign]
    if step != "none":
        with pytest.raises((OSError, ExtractionFailed)):
            layer.extract(V2)
    layer.extract(V2)
    clean = Revisions(FakeExtraction(), MemoryStore(), clock=lambda: NOW)
    clean.extract(V1)
    clean.extract(V2)
    assert end_state(layer, store) == end_state(clean, clean.store) == (
        [(V1.body, "superseded", "sha256:aaa1"), (V2.body, "current", "")], [V1.body], "sha256:bbb2")
