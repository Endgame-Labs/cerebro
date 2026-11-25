"""An article admitted from a record binds that record, and later records that are not its own do not land on it.

A connector says which of a record's keys it only carries (`Record.carried`): values that point at something else,
such as its parties' addresses and their domains, or the header lines of a note filed under a collection. A carried
key never binds the record, an admit of the record does not take it, and the record's facts still link by it. A
record is matched within its own collection, so it stays held for that
collection's curator while another collection's article holds a key it carries. The article an admit makes of a
record binds that record, whatever keys it holds.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cerebro.agent import load_agent
from cerebro.app import Cerebro
from cerebro.article import Identity
from cerebro.connector import Record, event_key
from cerebro.connectors.files import FilesConnector
from cerebro.curate import in_scope, run_curator
from cerebro.facts import Fact
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.held import held_for, waits_for_own
from cerebro.identity import bound_to, link, reached
from cerebro.lookup import StoreLookup
from cerebro.source import Select, SourceFile

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)
CURATOR = "---\nauthority: { admit: true }\nevidence:\n  - held: {}\n---\nKeep parts.\n"


def _hold(store: MemoryStore, source: str, *records: Record) -> None:
    store.set_state(f"held:{source}", [asdict(r) | {"body": None, "by": None} for r in records])


def _admit(store: MemoryStore, root: Path, record: Record, **more: Any) -> str:
    (root / "agents" / "parts").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "parts" / "curator.md").write_text(CURATOR)
    run = ScriptedRun(lambda p: {}, [("admit", {"record": {"source": record.source, "id": record.id},
                                                "reason": "a part we keep", **more})])
    done = run_curator(load_agent(root / "agents" / "parts" / "curator.md", root / "agents"),
                       lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW), store=store, agent_run=run,
                       root=root, functions={}, agent_tools={}, clock=lambda: NOW,
                       sources={record.source: FakeConnector(record.source, [])})
    return str(next(e.text for e in done.run.transcript if e.role == "tool"))


def _part(id: str, *addresses: str, **keys: list[str]) -> Record:
    return Record(id=id, source="src", revision="1", at="2026-11-10T15:00:00Z",
                  keys={"part_id": [id], "email": sorted(addresses),
                        "domain": sorted({a.rpartition("@")[2] for a in addresses}), **keys},
                  fields={"title": f"Part {id}"}, into="parts", carried=["email", "domain"])


def test_an_admit_by_record_takes_the_keys_that_name_it_and_a_later_record_of_the_same_parties_stays_held(
        tmp_path: Path) -> None:
    store = MemoryStore()
    first, other, same = _part("1", "ann@alder.example"), _part("2", "bo@alder.example"), _part("3", "ann@alder.example")
    _hold(store, "src", first, other, same)
    assert "'admitted': 'parts/part-1'" in _admit(store, tmp_path, first, keys=first.keys)  # as its held line shows
    assert store.article("parts/part-1").identity.keys == {"part_id": ["1"]}  # nothing it only carries
    assert bound_to(store, first) == ["parts/part-1"]
    assert {h.id for h in held_for(store, "parts", ["src"]).items} == {"2", "3"}
    fact = Fact("f3", "Ann said the order ships Friday.", "Ann", "fact", "", "src", same.id)
    assert link(store, fact, same) == []  # a fact of a later record is not the first one's


def test_a_record_is_held_for_its_own_collection_while_another_collections_article_holds_a_key_it_carries() -> None:
    store = MemoryStore()
    record = _part("4", "ann@alder.example")
    _hold(store, "src", record)
    store.create(Identity("things/alder", "things", "Alder", {"domain": ["alder.example"]}, "active"))
    assert bound_to(store, record) == [] and reached(store, record) == ["things/alder"]
    assert in_scope(store, record, curated=["parts", "things"])  # read: its facts reach its parent
    assert waits_for_own(store, record, ["parts"])
    assert [h.id for h in held_for(store, "parts", ["src"]).items] == ["4"]
    fact = Fact("f4", "Ann said the order ships Friday.", "Ann", "fact", "", "src", record.id)
    assert link(store, fact, record) == ["things/alder"]  # by the key it carries: evidence by link


def test_a_note_filed_under_a_collection_binds_its_own_article_and_its_header_lines_bind_nothing(
        tmp_path: Path) -> None:
    root = tmp_path / "w"
    (root / "notes" / "parts").mkdir(parents=True)
    for day in ("03", "14"):
        (root / "notes" / "parts" / f"2026-11-{day}.md").write_text(
            f"Parent: Alder\nWith: Ann <ann@alder.example>\nDate: 2026-11-{day}\n\nAnn said something on the {day}th.\n")
    files = FilesConnector(SourceFile(name="notes", kind="files", select=[Select(glob="notes/parts/*.md",
                                                                                 into="parts")]), root)
    first, later = sorted(files.list(None).records, key=lambda r: r.id)
    assert later.carried == list(later.keys)  # every header line points at what it hangs off
    store = MemoryStore()
    store.create(Identity("things/alder", "things", "Alder", {}, "active"))
    _hold(store, "notes", first, later)
    assert "'admitted': 'parts/2026-11-03'" in _admit(store, tmp_path, first, name="2026-11-03")
    assert bound_to(store, first) == ["parts/2026-11-03"]
    assert [h.id for h in held_for(store, "parts", ["notes"]).items] == [later.id]
    own = Fact("f5", "Ann said something on the 3th.", "Ann", "fact", "", "notes", first.id)
    assert set(link(store, own, first)) == {"parts/2026-11-03", "things/alder"}
    fact = Fact("f6", "Ann said something on the 14th.", "Ann", "fact", "", "notes", later.id)
    assert link(store, fact, later) == ["things/alder"]


def test_a_files_entry_identity_still_names_its_records(tmp_path: Path) -> None:
    root = tmp_path / "w"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "a.md").write_text("Parent: Alder\n\nA note.\n")
    entry = Select(glob="notes/*.md", into="parts", identity={"file": "{path}"})
    record = FilesConnector(SourceFile(name="notes", kind="files", select=[entry]), root).list(None).records[0]
    assert record.keys["file"] == ["notes/a.md"] and "file" not in record.carried and "parent" in record.carried


def test_a_record_read_in_scope_stays_held_for_its_own_curator_and_is_read_once(tmp_path: Path) -> None:
    root = tmp_path / "w"
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    (root / "sources" / "notes.yaml").write_text(
        'kind: files\nselect:\n  - glob: "notes/**/*.md"\n    extract: facts\n    into: parts\n')
    (root / "agents" / "parts").mkdir(parents=True)
    (root / "agents" / "parts" / "curator.md").write_text(CURATOR)
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("With: Ann <ann@alder.example>\nDate: 2026-11-02\n\nAnn said it shipped.\n")
    with Cerebro(root, extraction=FakeExtraction()) as workspace:
        workspace.store.create(Identity("things/alder", "things", "Alder", {"domain": ["alder.example"]}, "active"))
        assert workspace.run_once(agents=False).read == 1
        assert [(r["id"], r.get("read")) for r in workspace.store.state("held:notes")] == [("notes/a.md", True)]
        assert workspace.run_once(agents=False).read == 0  # read once: it waits for its own article alone
        assert [r["id"] for r in workspace.store.state("held:notes")] == ["notes/a.md"]


def test_an_event_id_is_one_key_however_a_source_writes_it_and_one_with_no_date_takes_the_records_day() -> None:
    at = datetime(2026, 11, 14, 15, tzinfo=UTC)
    forms = ["abc123_20261114T150000Z", "ABC123@google.com#20261114T150000Z", "abc123#20261114t150000z"]
    assert {event_key(f, at) for f in forms} == {"abc123#20261114t150000z"}
    assert event_key("abc_20261114", None) == "abc#20261114"
    # a series' id: each occurrence its own key, so a later week's recording binds nothing of an earlier week's
    assert event_key("Evt-1@google.com", at) == "evt-1#20261114" != event_key("evt-1", datetime(2026, 11, 21, tzinfo=UTC))
    assert event_key("", at) == "" and event_key("a_b", None) == ""
