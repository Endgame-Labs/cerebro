"""The extraction handler suite. Fixture: `extraction`, a facts layer that holds no facts yet."""

from __future__ import annotations

import pytest

from cerebro.connector import Record
from cerebro.facts import Extraction, FactNotFound, NoBody

MEMO = Record(id="memo-1", source="notes", revision="1", at="2026-11-01T09:00:00Z",
              body="The launch review moved to March 3.\n\nVega leads the review.")
OTHER = Record(id="memo-2", source="mail", revision="1", at="2026-11-05T09:00:00Z",
               body="The budget for the launch is set.")


def test_facts_quote_the_record_they_came_from(extraction: Extraction) -> None:
    facts = extraction.extract(MEMO).facts
    assert facts
    for fact in facts:
        assert fact.record == "memo-1" and fact.source == "notes" and fact.id and fact.text and fact.kind
        assert isinstance(fact.subject, str) and fact.at and fact.status == "current"
        assert all(m.text and m.role for m in fact.mentions)
        assert fact.quotes and all(quote.text in MEMO.body for quote in fact.quotes)


def test_a_record_without_its_body_is_refused(extraction: Extraction) -> None:
    with pytest.raises(NoBody):
        extraction.extract(Record(id="memo-1", source="notes", revision="1"))


def test_extracting_a_record_again_replaces_its_facts(extraction: Extraction) -> None:
    extraction.extract(MEMO)
    revised = Record(id="memo-1", source="notes", revision="2", body="The launch review is cancelled.")
    new = extraction.extract(revised).facts
    kept = extraction.facts(record="memo-1")
    assert {f.id for f in kept} == {f.id for f in new}
    assert all("cancelled" in q.text for f in kept for q in f.quotes)


def test_facts_filter_by_source_and_by_record(extraction: Extraction) -> None:
    extraction.extract(MEMO)
    extraction.extract(OTHER)
    assert {f.record for f in extraction.facts(source="mail")} == {"memo-2"}
    assert {f.record for f in extraction.facts(record="memo-1")} == {"memo-1"}
    assert {f.record for f in extraction.facts()} == {"memo-1", "memo-2"}
    assert len(extraction.facts(limit=1)) == 1
    assert extraction.facts()[0].record == "memo-2"  # newest first by when they hold
    assert {f.record for f in extraction.facts(since="2026-11-03T00:00:00Z")} == {"memo-2"}
    one = extraction.facts(record="memo-2")[0]
    assert extraction.facts(ids=[one.id]) == [one]
    assert extraction.facts(kinds=[one.kind]) and extraction.facts(kinds=["no-such-kind"]) == []


def test_the_facts_of_two_records_never_share_an_id(extraction: Extraction) -> None:
    first = {f.id for f in extraction.extract(MEMO).facts}
    second = {f.id for f in extraction.extract(OTHER).facts}
    assert first and second and not first & second


def test_a_retracted_fact_leaves_every_read_but_its_own(extraction: Extraction) -> None:
    gone = extraction.extract(MEMO).facts[0]
    extraction.retract(gone.id)
    assert gone.id not in {f.id for f in extraction.facts()}
    assert [(f.id, f.status) for f in extraction.facts(ids=[gone.id])] == [(gone.id, "retracted")]
    with pytest.raises(FactNotFound):
        extraction.retract("f_no_such_fact")


def test_search_finds_current_facts_by_their_words(extraction: Extraction) -> None:
    extraction.extract(MEMO)
    extraction.extract(OTHER)
    hits = extraction.search("budget launch", top_k=5)
    assert hits and extraction.facts(ids=[hits[0].id])[0].record == "memo-2" and hits[0].score > 0
    assert [h.id for h in extraction.search("budget", source="notes")] == []


def test_every_fact_is_of_a_kind_the_layer_says_it_gives(extraction: Extraction) -> None:
    kinds = extraction.kinds()
    assert kinds and all(isinstance(k, str) and k for k in kinds)
    assert {f.kind for f in extraction.extract(MEMO).facts} <= set(kinds)
