"""An article admitted from a record binds that record, and later records that are not its own do not land on it.

A connector says which of a record's keys it only carries (`Record.carried`): values that point at something else,
such as its parties' addresses and their domains, or the header lines of a note filed under a collection. A carried
key never binds the record, an admit of the record does not take it, and the record's facts still link by it. A
record is matched within its own collection, so it stays held for that
collection's curator while another collection's article holds a key it carries. The article an admit makes of a
record binds that record, whatever keys it holds. A name binds a record of that name only while no key that names one
thing holds other values on the two.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cerebro.agent import load_agent
from cerebro.app import Cerebro
from cerebro.article import Identity
from cerebro.connector import Record, event_key
from cerebro.connectors.files import FilesConnector
from cerebro.curate import in_scope, run_curator
from cerebro.facts import Fact, Mention
from cerebro.fakes import FakeConnector, FakeDecider, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.held import held_for, waits_for_own
from cerebro.identity import bound_to, link, reached, ties, would_bind
from cerebro.lookup import StoreLookup
from cerebro.source import Select, SourceFile

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)
CURATOR = "---\nauthority: { admit: true }\nevidence:\n  - held: {}\n---\nKeep parts.\n"


def _hold(store: MemoryStore, source: str, *records: Record) -> None:
    store.set_state(f"held:{source}", [asdict(r) | {"body": None, "by": None} for r in records])


def _admit(store: MemoryStore, root: Path, record: Record | None, **more: Any) -> str:
    (root / "agents" / "parts").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "parts" / "curator.md").write_text(CURATOR)
    ref = {"record": {"source": record.source, "id": record.id}} if record else {}
    run = ScriptedRun(lambda p: {}, [("admit", {**ref, "reason": "a part we keep", **more})])
    source = record.source if record else "src"
    done = run_curator(load_agent(root / "agents" / "parts" / "curator.md", root / "agents"),
                       lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW), store=store, agent_run=run,
                       root=root, functions={}, agent_tools={}, clock=lambda: NOW,
                       sources={source: FakeConnector(source, [])})
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


@pytest.mark.parametrize("by_record", [False, True])  # an admit by record takes the name as a key
def test_a_record_that_shares_only_its_name_with_an_article_binds_it_only_when_no_key_that_names_one_thing_differs(
        tmp_path: Path, by_record: bool) -> None:
    store = MemoryStore()
    store.weights = {"code": 100, "serial": 100}  # declared by a source to name one thing: never `tag`
    one, other, bare = (Record(id=id, source="src", revision="1", keys={"name": ["Unit 7"], **keys}, into="parts")
                        for id, keys in (("1", {"code": ["A1"]}), ("2", {"code": ["A2"]}),
                                         ("3", {"serial": ["S9"], "tag": ["blue"]})))
    carrier = replace(bare, id="4", keys={"name": ["Unit 7"], "serial": ["S4"], "code": ["A9"]}, carried=["code"])
    _hold(store, "src", one, other, bare, carrier)  # a key the record only carries tells nothing apart
    keys = one.keys if by_record else {"code": ["A1"], "tag": ["red"]}
    assert [r.id for r in (one, other, bare, carrier) if would_bind(store, keys, "Unit 7", r)] == ["1", "3", "4"]
    admitted = _admit(store, tmp_path, one) if by_record else _admit(store, tmp_path, None, name="Unit 7", keys=keys)
    assert "'admitted': 'parts/unit-7'" in admitted
    assert [h.id for h in held_for(store, "parts", ["src"]).items] == ["2"]
    assert link(store, Fact("f2", "It ships Friday.", "", "fact", "", "src", other.id), other) == []  # read all the same
    assert "'admitted': 'parts/unit-7-2'" in _admit(store, tmp_path, other)  # a thing of its own
    gone = store.article("parts/unit-7-2").identity
    gone.status = "archived"
    store.set_identity(gone)
    _hold(store, "src", replace(other, revision="2"))
    assert "'admitted': 'parts/unit-7-2'" in _admit(store, tmp_path, other)  # back by its own record: no merge asked


@pytest.mark.parametrize("names, title", [(["Ann Lee"], "Ann Lee"), (None, "Ann Lee"), (["Ann Lee"], "Ann Lee Park")],
                         ids=["by its name", "by the record's keys", "by a judge's near match"])
def test_a_fact_whose_subject_is_its_records_own_name_ties_to_no_article_the_record_is_told_apart_from(
        tmp_path: Path, names: list[str] | None, title: str) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    other = Record(id="2", source="src", revision="1", at="2026-11-10", body="Ann Lee joined Birch as CFO.",
                   keys={"name": ["Ann Lee"], "email": ["ann@birch.example"]}, fields={"subject": "Ann Lee"})
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(names=names),
                        connectors=[FakeConnector("src", [other])])  # no curator: every record is read
    workspace.decider = FakeDecider()  # a judge that says yes to every near match it is asked about
    workspace.store.create(Identity("things/ann-lee", "things", title,
                                    {"name": ["Ann Lee"], "email": ["ann@alder.example"]}, "active"))
    workspace.run_once(agents=False)
    assert [f.subject for f in workspace.extraction.facts(record="2")] == ["Ann Lee"]
    assert workspace.store.facts_of("things/ann-lee") == []


@pytest.mark.parametrize("role, how", [("subject", "subject"), ("about", "mentions")])
def test_a_mention_whose_own_key_names_an_article_ties_it_whatever_the_record_it_came_from(role: str, how: str) -> None:
    store = MemoryStore()
    store.create(Identity("things/ann-lee", "things", "Ann Lee", {"name": ["Ann Lee"], "email": ["ann@alder.example"]},
                          "active"))
    other = Record(id="2", source="src", revision="1", keys={"name": ["Ann Lee"], "email": ["ann@birch.example"]})
    said = Fact("f2", "Ann Lee moved to Birch.", "Ann Lee", "fact", "", "src", "2",
                mentions=[Mention("Ann Lee", role, {"email": ["ann@alder.example"]})])  # what the fact says decides
    assert ("things/ann-lee", how) in ties(store, said, other)


def test_a_fact_that_mentions_its_records_own_name_ties_to_no_article_the_record_is_told_apart_from() -> None:
    store = MemoryStore()
    store.create(Identity("things/ann-lee", "things", "Ann Lee", {"name": ["Ann Lee"], "email": ["ann@alder.example"]},
                          "active"))
    other = Record(id="2", source="src", revision="1", keys={"name": ["Ann Lee"], "email": ["ann@birch.example"]})
    said = Fact("f3", "Birch thanked Ann Lee.", "Birch", "fact", "", "src", "2",
                mentions=[Mention("Birch", "subject"), Mention("Ann Lee")])
    assert ties(store, said, other) == []


def test_a_fact_tied_by_its_records_own_name_comes_off_an_article_once_a_key_tells_the_two_apart(
        tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    other = Record(id="2", source="src", revision="1", at="2026-11-10", body="Ann Lee moved to Birch.",
                   keys={"name": ["Ann Lee"], "email": ["ann@birch.example"]})
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(names=["Ann Lee"]),
                        connectors=[FakeConnector("src", [other])])
    workspace.store.create(Identity("things/ann-lee", "things", "Ann Lee", {"name": ["Ann Lee"]}, "active"))
    workspace.run_once(agents=False)  # nothing tells the two apart yet: the record binds it by its name
    facts = [f.id for f in workspace.extraction.facts(record="2")]
    assert facts and workspace.store.facts_of("things/ann-lee") == facts
    ann = workspace.store.article("things/ann-lee").identity
    ann.keys = {**ann.keys, "email": ["ann@alder.example"]}  # as a link adds a key: now they are two things
    workspace.store.set_identity(ann)
    workspace.run_once(agents=False)
    assert workspace.store.facts_of("things/ann-lee") == []


def test_a_fact_named_as_its_record_is_by_a_field_ties_to_no_article_it_is_told_apart_from_when_linked_again(
        tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    other = Record(id="2", source="src", revision="1", at="2026-11-10", body="Unit 7 moved to the north bay.",
                   keys={"email": ["desk@birch.example"]}, fields={"subject": "Unit 7"})  # its name is no key
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(names=["Unit 7"]),
                        connectors=[FakeConnector("src", [other])])
    workspace.store.create(Identity("things/unit-7", "things", "Unit 7", {"email": ["desk@alder.example"]}, "active"))
    workspace.run_once(agents=False)
    workspace.store.create(Identity("things/bay", "things", "Bay", {}, "active"))  # every fact is linked again
    workspace.run_once(agents=False)
    assert workspace.store.facts_of("things/unit-7") == []


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


def test_a_notes_facts_stay_on_the_article_its_header_names_when_that_articles_keys_change(tmp_path: Path) -> None:
    root = tmp_path / "w"
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    (root / "sources" / "notes.yaml").write_text('kind: files\nselect:\n  - glob: "notes/*.md"\n    extract: facts\n')
    (root / "notes").mkdir()
    (root / "notes" / "call.md").write_text(  # a party's address, carried: it tells the note apart from nothing
        "Parent: Alder Works\nWith: Bo <bo@birch.example>\nDate: 2026-11-02\n\nThe order is signed.\n")
    with Cerebro(root, extraction=FakeExtraction()) as workspace:
        workspace.store.create(Identity("things/alder", "things", "Alder Works", {"domain": ["alder.example"]}, "active"))
        workspace.run_once(agents=False)
        facts = [f.id for f in workspace.extraction.facts()]
        assert facts and workspace.store.facts_of("things/alder") == facts
        alder = workspace.store.article("things/alder").identity
        alder.keys["alias"] = ["Alder"]
        workspace.store.set_identity(alder)
        workspace.run_once(agents=False)  # every fact is linked again, as landing linked it
        assert workspace.store.facts_of("things/alder") == facts


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
