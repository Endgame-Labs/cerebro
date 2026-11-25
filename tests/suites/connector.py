"""The `Connector` suite. Fixtures: `connector`; `held`, the records it holds, bodies included; `add`, which adds a
record to the source as the outside system changing would and gives the id the source holds it by; and `remove`,
which deletes one by its id. A connector may list in pages, so the suite follows `more` to the end.

`ask` is optional: a connector that answers no question refuses each one, and passes the cases for it as one that
answers does. A module may give `asks`, the questions to ask with their params, as `[("header_values", {"word":
"account"})]`; with none given, the suite asks each question that needs no param.

`removed` means deleted or cancelled at the source. A record that only left the window or the scope still exists,
and is never in `removed`: the runner retracts a removed record's facts. Each connector's own tests show one aging
out is not removed, since the suite has no clock to move."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from cerebro.connector import Connector, Listing, QuestionError, Record, RecordNotFound


def every_page(connector: Connector, since: str | None = None) -> Listing:
    """Every page from `since` on, as one listing: its records, what was removed, the last cursor and its notices."""
    whole = Listing([], since)
    for _ in range(1000):
        page = connector.list(since=whole.cursor)
        whole.records += page.records
        whole.removed += page.removed
        whole.cursor = page.cursor
        whole.notices = page.notices  # each page says the same of the whole source
        if not page.more:
            return whole
    raise AssertionError("the listing never ended: the last page must say more is False")


def test_a_first_listing_gives_every_record_whole_but_its_body(connector: Connector, held: list[Record]) -> None:
    listing = every_page(connector)
    assert sorted(r.id for r in listing.records) == sorted(r.id for r in held)
    listed = {r.id: r for r in listing.records}
    for record in held:
        assert listed[record.id] == replace(record, body=None)
    assert all(r.body is None for r in listing.records)
    assert {r.source for r in listing.records} == {connector.name} and connector.name
    assert listing.removed == []


def test_listing_from_a_cursor_gives_only_what_came_since(connector: Connector, held: list[Record],
                                                           add: Callable[[Record], str]) -> None:
    cursor = every_page(connector).cursor
    assert cursor is not None
    nothing = connector.list(since=cursor)
    assert nothing.records == [] and nothing.removed == [] and not nothing.more
    new = add(Record(id="n9", source=connector.name, revision="1", at="2026-11-17T09:00:00Z", body="New since."))
    later = every_page(connector, cursor)
    assert [r.id for r in later.records] == [new] and later.cursor != cursor
    assert every_page(connector, later.cursor).records == []


def test_a_changed_record_is_listed_again_alone_with_a_new_revision(connector: Connector, held: list[Record],
                                                                    add: Callable[[Record], str]) -> None:
    cursor = every_page(connector).cursor
    changed = add(replace(held[0], revision="changed", body="Changed since."))
    later = every_page(connector, cursor)
    assert [r.id for r in later.records] == [changed] == [held[0].id]
    assert later.records[0].revision != held[0].revision and later.records[0].body is None
    assert "Changed since." in connector.get(changed).body  # a source may render lines around a record's text


def test_a_record_removed_between_two_listings_is_listed_as_removed_once(connector: Connector, held: list[Record],
                                                                         remove: Callable[[str], None]) -> None:
    cursor = every_page(connector).cursor
    remove(held[0].id)
    later = every_page(connector, cursor)
    assert later.removed == [held[0].id] and later.records == []
    assert every_page(connector, later.cursor).removed == []
    with pytest.raises(RecordNotFound):
        connector.get(held[0].id)


def test_pages_give_each_record_once_and_a_cursor_resumes_after_its_page(connector: Connector, held: list[Record],
                                                                         add: Callable[[Record], str]) -> None:
    for n in range(5):
        add(Record(id=f"p{n}", source=connector.name, revision="1", body=f"Page record {n}."))
    first = connector.list()
    rest = every_page(connector, first.cursor) if first.more else Listing([], first.cursor)
    ids = [r.id for r in first.records + rest.records]
    assert len(ids) == len(set(ids)) == len(held) + 5
    assert every_page(connector, rest.cursor).records == []


def test_get_gives_one_record_whole(connector: Connector, held: list[Record]) -> None:
    for record in held:
        assert connector.get(record.id) == record


@pytest.mark.parametrize("id", ["no-such-record", "", "../outside.md", "/etc/hostname", "~/.bashrc", "./"])
def test_get_of_an_id_the_source_does_not_hold_raises_record_not_found(connector: Connector, id: str) -> None:
    with pytest.raises(RecordNotFound):
        connector.get(id)


def test_a_caller_changing_a_record_changes_nothing_in_the_source(connector: Connector, held: list[Record]) -> None:
    got = connector.get(held[0].id)
    got.body = "changed"
    got.fields["x"] = 1
    connector.list().records[0].keys["x"] = ["1"]
    assert connector.get(held[0].id) == held[0]
    assert connector.list().records[0] == replace({r.id: r for r in held}[connector.list().records[0].id], body=None)


def test_a_cursor_the_source_did_not_give_is_refused_as_one(connector: Connector) -> None:
    from cerebro.connector import CursorError

    with pytest.raises(CursorError):
        connector.list(since="not-a-cursor-this-source-gave")


def test_a_listing_keeps_the_keys_collection_and_extraction_the_runner_reads(connector: Connector,
                                                                              held: list[Record]) -> None:
    listed = {r.id: r for r in every_page(connector).records}
    for record in held:
        whole = connector.get(record.id)
        assert (listed[record.id].keys, listed[record.id].into, listed[record.id].extract) == (
            whole.keys, whole.into, whole.extract)


@pytest.mark.parametrize("question", ["no-such-question", "", "https://crm.example/api"])
def test_a_question_the_source_does_not_answer_is_refused_in_one_line_that_names_the_source(
        connector: Connector, question: str) -> None:
    with pytest.raises(QuestionError) as refused:
        connector.ask(question)
    assert connector.name in str(refused.value) and "\n" not in str(refused.value)


def test_a_question_asked_with_a_param_it_does_not_take_is_refused_in_one_line(connector: Connector) -> None:
    question = next(iter(connector.questions), "no-such-question")
    with pytest.raises(QuestionError) as refused:
        connector.ask(question, {"no_such_param": "1"})
    assert connector.name in str(refused.value) and "\n" not in str(refused.value)


def test_each_question_a_source_lists_is_named_with_one_line(connector: Connector) -> None:
    for name, line in connector.questions.items():
        assert name.isidentifier() and line.strip() and "\n" not in line


def test_a_question_needing_no_params_gives_rows_of_this_source_without_bodies_within_the_limit(
        connector: Connector, held: list[Record]) -> None:
    for question in connector.questions:
        try:
            answer = connector.ask(question, limit=1)
        except QuestionError:
            continue  # it takes a param the suite cannot know
        assert answer.cursor is None and len(answer.records) <= 1
        assert all(r.body is None and r.source == connector.name and r.id for r in answer.records)
    assert connector.get(held[0].id) == held[0]  # asking changed nothing the source holds


@pytest.fixture
def asks() -> list[tuple[str, dict[str, object]]]:
    """The questions to ask with their params. None by default: a module that knows its params gives its own."""
    return []


def _askable(connector: Connector, asks: list[tuple[str, dict[str, object]]]) -> list[tuple[str, dict[str, object]]]:
    plain = []
    for question in connector.questions:
        try:
            connector.ask(question, limit=1)
        except QuestionError:
            continue  # it takes a param the suite cannot know
        plain.append((question, {}))
    return [*asks, *plain]


def test_a_larger_limit_gives_more_rows_and_more_says_whether_rows_were_left_out(
        connector: Connector, asks: list[tuple[str, dict[str, object]]]) -> None:
    for question, params in _askable(connector, asks):
        whole = connector.ask(question, dict(params), limit=50)
        assert len(whole.records) <= 50 and isinstance(whole.more, bool)
        assert all(r.body is None and r.source == connector.name and r.id for r in whole.records)
        one = connector.ask(question, dict(params), limit=1)
        assert len(one.records) <= 1 and one.more is (len(whole.records) > 1), question
        if whole.records and not whole.more:
            assert {r.id for r in one.records} <= {r.id for r in whole.records}
