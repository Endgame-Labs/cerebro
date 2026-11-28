"""The suite for the six operations, over the in-memory store and over SQLite, with the fake facts layer."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import pytest
from suites.lookup import *

from cerebro.article import Identity
from cerebro.connector import Record
from cerebro.fakes import FakeExtraction, MemoryStore
from cerebro.lookup import Lookup, OperationError, StoreLookup
from cerebro.store import SQLiteStore, Store


@pytest.fixture(params=["memory", "sqlite"])
def make_lookup(request: pytest.FixtureRequest) -> Iterator[Callable[[list[Record]], tuple[Lookup, Store]]]:
    store: Store = MemoryStore() if request.param == "memory" else SQLiteStore()

    def make(records: list[Record]) -> tuple[Lookup, Store]:
        extraction = FakeExtraction()
        for record in records:
            extraction.extract(record)
        return StoreLookup(store, extraction, clock=lambda: NOW), store

    with store:
        yield make


def _spoken() -> tuple[StoreLookup, FakeExtraction]:
    """A lookup over facts whose quotes name their speakers."""
    extraction = FakeExtraction()
    for id, text, speaker in (("call-1", "Pricing casework is the blocker.", "Nessa Torvik"),
                              ("call-2", "The case study ships Friday.", "Dan"),
                              ("call-3", "He asked for a demo.", "Aaron Case")):
        extraction.extract(Record(id=id, source="calls", revision="1", at=f"2026-11-0{id[-1]}T09:00:00Z", body=text))
        extraction.by_record[id][0].quotes[0].speaker = speaker
    return StoreLookup(MemoryStore(), extraction, clock=lambda: NOW), extraction


def test_read_ranks_a_word_said_whole_before_one_it_starts_before_a_speakers_name() -> None:
    lookup, extraction = _spoken()
    lookup.store.create(Identity("deals/renewal", "deals", "Renewal"))
    for fact in extraction.facts():
        lookup.store.link(fact.id, ["deals/renewal"])
    read = lookup.read(YOU, "deals/renewal", about="case")
    assert [f.record for f in read.facts] == ["call-2", "call-1", "call-3"]  # whole, then casework, then by name
    assert [f.record for f in lookup.read(YOU, "deals/renewal", about="prices").facts] == ["call-1"]  # pric-


def test_a_query_of_facts_keeps_a_speaker_by_the_whole_name_and_says_why_a_side_matched_nothing() -> None:
    lookup, _ = _spoken()
    assert [f.record for f in lookup.query(YOU, "facts", {"speaker": "dan"}).items] == ["call-2"]
    assert lookup.query(YOU, "facts", {"speaker": "any"}).count == 3
    theirs = lookup.query(YOU, "facts", {"speaker_side": "theirs"})
    assert theirs.count == 0 and theirs.warnings == ["speaker_side theirs: no quote is on the theirs side"]
    assert lookup.query(YOU, "facts", {"speaker_side": "unknown"}).count == 3  # a quote with no side is unknown
    with pytest.raises(OperationError, match="speaker_side takes one of yours, theirs, unknown"):
        lookup.query(YOU, "facts", {"speaker_side": ["yours", "theirs"]})
    assert [f.record for f in lookup.query(YOU, "facts", {"record": "call-2", "kind": "note"}).items] == ["call-2"]
    assert lookup.query(YOU, "facts", {"kind": "risk"}).count == 0
    assert [f.record for f in lookup.query(YOU, "facts", {"text": "he asked for a demo."}).items] == ["call-3"]
    none = lookup.query(YOU, "facts", {"about": "pri"})
    assert none.count == 0 and "but one holds 'pricing', which starts with 'pri'" in none.warnings[0]
    by_about = lookup.query(YOU, "facts", {"about": "case"})  # newest first, not best match first
    assert [f.record for f in by_about.items] == ["call-3", "call-2", "call-1"]


def test_read_takes_ids_as_json_text_and_refuses_one_with_tool_call_markup() -> None:
    lookup, extraction = _spoken()
    ids = [f.id for f in extraction.facts()]
    assert len(lookup.read(YOU, json.dumps(ids[:2])).facts) == 2
    with pytest.raises(OperationError, match="holds the tool call markup <arg_key>"):
        lookup.read(YOU, [f"{ids[0]}<arg_key>about</arg_key>"])


def test_a_query_of_an_app_kind_gives_its_records_and_agent_runs_come_newest_first_by_when_they_ended() -> None:
    store = MemoryStore()
    lookup = StoreLookup(store, None, ["note"], clock=lambda: NOW)
    for id, at, owner in (("n1", "2026-11-16T09:00:00Z", "west"), ("n2", "2026-10-01T09:00:00Z", "west"),
                          ("n3", "2026-11-15T09:00:00Z", "east")):
        store.keep(Record(id=id, source="note", revision="1", at=at, fields={"owner": owner}, body=id))
    notes = lookup.query(YOU, "note", {"owner": "west", "window": "last_7_days"})
    assert [r.id for r in notes.items] == ["n1"] and isinstance(notes.items[0], Record)
    assert [r.id for r in lookup.query(YOU, "note").items] == ["n3", "n2", "n1"]  # the last kept first
    for run, ended in (("a", "2026-11-17T10:00:00Z"), ("b", "2026-11-17T09:00:00Z"), ("c", "2026-11-17T11:00:00Z")):
        store.append("agent-runs", {"run": run, "ended_at": ended})
    assert [r["run"] for r in lookup.query(YOU, "agent-runs").items] == ["c", "a", "b"]


def test_a_query_or_read_of_an_app_kind_says_which_records_it_shows_a_retract_took_back_and_no_other() -> None:
    from dataclasses import replace

    from cerebro.records import retract

    store = MemoryStore()
    lookup = StoreLookup(store, None, ["note"], clock=lambda: NOW)
    for id, owner in (("n1", "west"), ("n2", "west"), ("n3", "east")):
        store.keep(Record(id=id, source="note", revision="1", at="2026-11-16T09:00:00Z", fields={"owner": owner},
                          body=id, by=YOU))
        retract(store, None, YOU, id, clock=lambda: NOW)
    store.keep(replace(store.record("n2"), revision="2", at="2026-11-17T13:00:00Z"))  # its maker's word after it
    shown = lookup.query(YOU, "note", {"owner": "west"})
    assert [r.id for r in shown.items] == ["n2", "n1"] and shown.retracted == ["n1"]
    assert shown.warnings == ["record n1 was retracted"]
    assert lookup.read(YOU, ["n3", "n2"]).warnings == ["record n3 was retracted"]


def test_speaker_side_reads_the_side_each_quote_records(make_lookup: Callable[..., tuple[Lookup, Store]]) -> None:
    from cerebro.facts import Quote

    lookup, _ = make_lookup([])
    extraction = lookup.extraction  # type: ignore[attr-defined]
    facts = extraction.extract(Record(id="call-1", source="calls", revision="1", at="2026-11-01T09:00:00Z",
                                      body="We can start in March.\n\nWe need a security review.")).facts
    facts[0].quotes = [Quote(facts[0].text, "Ada", side="yours")]
    facts[1].quotes = [Quote(facts[1].text, "Lin", side="theirs")]
    extraction.by_record["call-1"] = facts
    assert [f.text for f in lookup.query(YOU, "facts", {"speaker_side": "theirs"}).items] == [facts[1].text]
    assert [f.text for f in lookup.query(YOU, "facts", {"speaker_side": "yours"}).items] == [facts[0].text]


def test_a_filtered_query_assembles_only_what_matches() -> None:
    from cerebro.article import Property
    from cerebro.caller import Caller

    class Counting(SQLiteStore):
        assembled = 0

        def article(self, id: str):
            Counting.assembled += 1
            return super().article(id)

    you = Caller("you", "person")
    with Counting() as store:
        for n in range(30):
            store.create(Identity(f"projects/p{n}", "projects", f"P{n}"))
            store.set_field(f"projects/p{n}", "stage", Property("open" if n == 7 else "closed", you, "2026-11-01T09:00:00Z"))
        Counting.assembled = 0
        result = StoreLookup(store).query(you, "projects", {"stage": "open"})
        assert [a.identity.id for a in result.items] == ["projects/p7"] and Counting.assembled <= 2


def test_a_read_after_a_record_changed_gives_only_its_new_facts() -> None:
    from cerebro.caller import Caller

    you = Caller("you", "person")
    store, extraction = MemoryStore(), FakeExtraction()
    store.create(Identity("projects/apollo", "projects", "Apollo"))
    old = extraction.extract(Record(id="m", source="notes", revision="1", at="2026-11-01T09:00:00Z",
                                    body="The review is in March.")).facts[0]
    new = extraction.extract(Record(id="m", source="notes", revision="2", at="2026-11-02T09:00:00Z",
                                    body="The review is in April.")).facts[0]
    store.link(old.id, ["projects/apollo"])
    store.link(new.id, ["projects/apollo"])
    assert [f.text for f in StoreLookup(store, extraction).read(you, "projects/apollo").facts] == [new.text]


def test_a_merged_article_is_left_out_like_an_archived_one_and_status_is_the_systems() -> None:
    from cerebro.article import Property
    from cerebro.caller import Caller

    store = MemoryStore()
    lookup = StoreLookup(store)
    you = Caller("you", "person")
    for id, status in (("meetings/qbr", "active"), ("meetings/kickoff", "active"), ("meetings/old", "merged"),
                       ("meetings/gone", "archived")):
        store.create(Identity(id, "meetings", id, status=status))
    for id, value in (("meetings/qbr", "past"), ("meetings/kickoff", "upcoming"), ("meetings/old", "past")):
        store.set_field(id, "status", Property(value, Caller("meetings/prep", "agent"), "2026-11-01T00:00:00Z"))
    assert [a.identity.id for a in lookup.query(you, "meetings").items] == ["meetings/kickoff", "meetings/qbr"]
    assert lookup.query(you, "meetings", {"status": "past"}).items == []  # no writer's status stands
    assert [a.identity.id for a in lookup.query(you, "meetings", {"status": "merged"}).items] == ["meetings/old"]
    assert [a.identity.id for a in lookup.query(you, "meetings", {"status": "archived"}).items] == ["meetings/gone"]


def test_a_read_of_a_merged_id_follows_it_to_the_article_it_was_merged_into() -> None:
    from cerebro.caller import Caller

    store = MemoryStore()
    store.create(Identity("things/alder", "things", "Alder"))
    store.create(Identity("things/alder-inc", "things", "Alder Inc.", status="merged"))
    store.append("merges", {"redirect": "things/alder-inc", "to": "things/alder"})
    result = StoreLookup(store).read(Caller("you", "person"), ["things/alder-inc"])
    assert [a.identity.id for a in result.articles] == ["things/alder"]
    assert result.warnings == ["things/alder-inc was merged into things/alder: read things/alder from now on"]


def test_a_matching_query_of_too_many_words_or_characters_is_refused_before_it_searches() -> None:
    from cerebro.caller import Caller

    store = MemoryStore()
    store.create(Identity("things/alder", "things", "Alder"))
    lookup = StoreLookup(store)
    for words in ("word " * 33, "x" * 401):
        with pytest.raises(OperationError, match="matching takes at most 32 words and 400 characters"):
            lookup.query(Caller("you", "person"), "things", matching=words)


def test_matching_ranks_only_the_collection_it_names() -> None:
    from cerebro.article import Compilation
    from cerebro.caller import Caller

    store = MemoryStore()
    for id in ("things/alder", "people/ann"):
        store.create(Identity(id, id.split("/")[0], id))
        store.write_compilation(Compilation(id, "Overview", "The renewal is on track.", by="w"))
    found = StoreLookup(store).query(Caller("you", "person"), "things", matching="renewal")
    assert [a.identity.id for a in found.items] == ["things/alder"] and found.best_first


def test_a_facts_filter_that_gives_kind_and_classes_applies_both_or_refuses_in_one_line() -> None:
    import pytest

    from cerebro.lookup import OperationError

    extraction = FakeExtraction(kinds=["risk_objection", "next_step"])
    extraction.extract(Record(id="memo", source="notes", revision="1", at="2026-11-01T09:00:00Z",
                              body="Vega worries the budget is a risk.\n\nThe next step is a demo on 3 March."))
    lookup = StoreLookup(MemoryStore(), extraction, clock=lambda: NOW)
    you = Caller("you", "person")
    both = lookup.query(you, "facts", {"kind": ["risk_objection", "next_step"], "classes": ["next_step"]})
    assert [item["kind"] if isinstance(item, dict) else item.kind for item in both.items] == ["next_step"]
    with pytest.raises(OperationError, match="share no kind, so no fact could match both"):
        lookup.query(you, "facts", {"kind": "risk_objection", "classes": "next_step"})


def test_a_query_of_a_collection_no_agent_keeps_says_there_is_none_and_names_those_there_are() -> None:
    from cerebro.caller import Caller

    you = Caller("you", "person")
    store = MemoryStore()
    store.create(Identity("things/alder", "things", "Alder"))
    lookup = StoreLookup(store, collections=lambda: {"things", "parts"})
    (missing,) = lookup.query(you, "widgets").warnings  # no such collection: not one that is empty
    assert "widgets" in missing and "parts, things" in missing and "no articles yet" not in missing, missing
    (empty,) = lookup.query(you, "parts").warnings  # a kept collection with nothing in it yet is still one
    assert "parts has no articles yet" in empty, empty
