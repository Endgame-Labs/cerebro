"""The suite for the six operations. Fixture: `make_lookup`, which takes records to extract and gives a `Lookup`
and the `Store` under it, so the suite can write articles through the store's parts. The lookup reads windows at
`NOW`, in UTC."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from cerebro.article import Compilation, Identity, Property
from cerebro.connector import Record
from cerebro.facts import Fact
from cerebro.lookup import (
    Caller,
    CallerError,
    DecideResult,
    Decision,
    FindResult,
    Lookup,
    OperationError,
    QueryResult,
    ReadResult,
    RetractResult,
    TellResult,
)
from cerebro.records import InputError, NothingStands
from cerebro.store import Store, Where

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
YOU = Caller("you", "person")
MEMO = Record(id="memo-1", source="notes", revision="1", at="2026-11-01T09:00:00Z", body="The review moved to March.")


@pytest.fixture
def parts(make_lookup: Callable[[list[Record]], tuple[Lookup, Store]]) -> tuple[Lookup, Store]:
    lookup, store = make_lookup([MEMO])
    for path, title, stage, size in (("projects/apollo", "Apollo", "open", 3), ("projects/vega", "Vega", "closed", 8)):
        store.create(Identity(path, "projects", title))
        store.set_field(path, "stage", Property(stage, YOU, "2026-11-01T09:00:00Z"))
        store.set_field(path, "size", Property(size, YOU, "2026-11-01T09:00:00Z"))
    store.write_compilation(Compilation("projects/apollo", "Overview", "It moved.", by="projects/article"))
    store.append("proposals", {"id": "p_1", "kind": "admit", "target": "projects/apollo"})
    store.append("proposals", {"id": "p_2", "kind": "archive", "target": "projects/vega"})
    return lookup, store


@pytest.fixture
def lookup(parts: tuple[Lookup, Store]) -> Lookup:
    return parts[0]


def test_read_returns_articles_whole_as_data_and_names_the_missing(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    result = lookup.read(YOU, ["projects/apollo", "projects/none"])
    assert isinstance(result, ReadResult)
    assert result.articles == [store.article("projects/apollo")]
    assert result.articles[0].compilations[0].text == "It moved." and result.missing == ["projects/none"]


def test_read_takes_one_id_as_a_string_a_path_as_an_id_and_a_fact_by_its_id(lookup: Lookup) -> None:
    assert [a.identity.id for a in lookup.read(YOU, "articles/projects/vega.md").articles] == ["projects/vega"]
    fact = lookup.query(YOU, "facts", {"record": "memo-1"}).items[0]
    assert lookup.read(YOU, [fact.id]).facts == [fact]


def test_read_refuses_more_than_twenty_ids_and_none(lookup: Lookup) -> None:
    with pytest.raises(OperationError, match="at most 20"):
        lookup.read(YOU, [f"projects/{n}" for n in range(21)])
    with pytest.raises(OperationError):
        lookup.read(YOU, [])


def test_query_returns_the_matches_and_how_many_as_data(lookup: Lookup) -> None:
    result = lookup.query(YOU, "projects", {"stage": "open"})
    assert isinstance(result, QueryResult)
    assert [a.identity.id for a in result.items] == ["projects/apollo"] and result.count == 1
    paged = lookup.query(YOU, "projects", limit=1, offset=1)
    assert [a.identity.id for a in paged.items] == ["projects/vega"] and paged.count == 2
    assert lookup.query(YOU, "projects", limit=0).items == [] and lookup.query(YOU, "projects", limit=0).count == 2
    past = lookup.query(YOU, "projects", offset=2)
    assert past.items == [] and past.warnings == ["offset 2 is past the 2 matches: pass a smaller offset"]
    ranged = lookup.query(YOU, "projects", where=[Where("size", ">", 5)])
    assert [a.identity.id for a in ranged.items] == ["projects/vega"]
    ordered = lookup.query(YOU, "projects", order_by="size", descending=True)
    assert [a.identity.id for a in ordered.items] == ["projects/vega", "projects/apollo"]


def test_order_by_a_time_field_comes_by_its_moment_and_compiled_at_is_one(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts  # apollo's start is the later moment, though its text sorts first
    store.set_field("projects/apollo", "start", Property("2026-12-01T23:00:00-08:00", YOU, "2026-11-01T09:00:00Z"))
    store.set_field("projects/vega", "start", Property("2026-12-02T01:00:00Z", YOU, "2026-11-01T09:00:00Z"))
    ordered = lookup.query(YOU, "projects", order_by="start", descending=True)
    assert [a.identity.id for a in ordered.items] == ["projects/apollo", "projects/vega"]
    store.write_compilation(Compilation("projects/vega", "Overview", "It ran.", by="projects/article", at=NOW.isoformat()))
    compiled = lookup.query(YOU, "projects", order_by="compiled_at", descending=True)
    assert compiled.warnings == [] and [a.identity.id for a in compiled.items] == ["projects/vega", "projects/apollo"]


def test_query_reads_facts_and_logs_too(lookup: Lookup) -> None:
    facts = lookup.query(YOU, "facts", {"source": "notes"})
    assert facts.count == 1 and isinstance(facts.items[0], Fact) and facts.items[0].record == "memo-1"
    proposals = lookup.query(YOU, "proposals", {"kind": "archive"})
    assert [row["target"] for row in proposals.items] == ["projects/vega"]
    assert [row["kind"] for row in lookup.query(YOU, "proposals").items] == ["archive", "admit"]  # newest first
    for source, what in (("facts", "no fact"), ("proposals", "no line of proposals")):  # warned as a collection is
        warned = lookup.query(YOU, source, order_by="nope", descending=True).warnings
        assert warned[0].startswith(f"{what} has a field nope to order by, so they come in the default order")


def test_query_refuses_a_negative_limit_or_offset(lookup: Lookup) -> None:
    with pytest.raises(OperationError):
        lookup.query(YOU, "projects", limit=-1)
    with pytest.raises(OperationError):
        lookup.query(YOU, "projects", offset=-1)


def test_query_of_an_empty_collection_says_why_it_matched_nothing(lookup: Lookup) -> None:
    result = lookup.query(YOU, "teams")
    assert result.count == 0 and result.warnings


def test_every_operation_takes_a_caller_it_can_name(lookup: Lookup) -> None:
    with pytest.raises(CallerError):
        lookup.read("you", ["projects/apollo"])  # type: ignore[arg-type]
    with pytest.raises(CallerError):
        lookup.query(Caller("you", "robot"), "projects")


def test_find_resolves_a_name_an_id_a_path_a_key_and_an_email_by_its_domain(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    store.create(Identity("teams/west", "teams", "West", keys={"email": ["west@example.com"], "domain": ["west.example"]}))
    store.write_compilation(Compilation("teams/west", "Overview", "West sells.", by="teams/article"))
    result = lookup.find(YOU, "apollo")
    assert isinstance(result, FindResult) and result.matches == ["projects/apollo"]
    assert result.matched_on == {"projects/apollo": "name"}
    assert lookup.find(YOU, "projects/vega").matched_on == {"projects/vega": "id"}
    assert lookup.find(YOU, "articles/projects/vega.md").matched_on == {"projects/vega": "path"}
    assert lookup.find(YOU, "WEST@example.com").matched_on == {"teams/west": "email"}
    assert lookup.find(YOU, "ada@west.example").matched_on == {"teams/west": "domain"}
    assert lookup.find(YOU, "Orion").matches == []
    with pytest.raises(OperationError, match="find needs a name"):
        lookup.find(YOU, " ")


def test_tell_keeps_its_record_whole_and_links_its_facts_to_the_target(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    result = lookup.tell(YOU, "The review moved to April.", "projects/apollo", as_="fact")
    assert isinstance(result, TellResult) and result.record and result.proposals == []
    kept = store.record(result.record)
    assert kept is not None and kept.body == "The review moved to April." and kept.by == YOU
    assert len(result.facts) == 1 and result.facts[0] in store.facts_of("projects/apollo")
    assert lookup.read(YOU, "projects/apollo").facts[0].text == "The review moved to April."


def test_decide_lands_whole_or_not_at_all_and_an_accept_is_left_for_the_curator(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    result = lookup.decide(YOU, accept=["p_2", "p_none"], reject=[Decision("p_1", "not yet")])
    assert isinstance(result, DecideResult)
    assert result.answers == {"p_2": "not decided", "p_none": "unknown", "p_1": "not decided"}
    assert store.article("projects/vega").identity.status == "active"
    assert lookup.decide(YOU, accept=["p_2"], reject=[Decision("p_1", "not yet")]).answers == {
        "p_2": "decided", "p_1": "decided"}
    assert store.article("projects/vega").identity.status == "active"
    assert {row["id"]: row["status"] for row in lookup.query(YOU, "proposals").items} == {
        "p_1": "rejected", "p_2": "accepted"}
    assert lookup.decide(YOU, accept=["p_2"]).answers == {"p_2": "already decided"}
    with pytest.raises(InputError, match="in both accept and reject"):
        lookup.decide(YOU, accept=["p_9"], reject=["p_9"])


def test_retract_takes_back_a_tell_and_what_it_gave(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    told = lookup.tell(YOU, "The review moved to April.", "projects/apollo", as_="fact")
    result = lookup.retract(YOU, told.record, "it was May")
    assert isinstance(result, RetractResult) and result.id == told.record
    assert result.withdrew == [told.record, *told.facts] and result.kind == "record.retract"
    assert lookup.read(YOU, "projects/apollo").facts == [] and store.record(told.record) is not None
    with pytest.raises(NothingStands, match="nothing stands on"):
        lookup.retract(YOU, told.record)


def test_query_with_matching_ranks_by_what_the_items_discuss(lookup: Lookup) -> None:
    result = lookup.query(YOU, "projects", matching="moved")
    assert result.items[0].identity.id == "projects/apollo"
    assert result.count == 1 and lookup.query(YOU, "projects", {"stage": "open"}, matching="moved").count == 1
    assert lookup.query(YOU, "projects", {"stage": "closed"}, matching="moved").count == 0


def test_query_with_matching_and_no_from_searches_articles_and_facts_as_one_list(lookup: Lookup) -> None:
    result = lookup.query(YOU, "", matching="moved")
    assert result.best_first and result.count == len(result.items) == 2
    assert {(item.kind, item.id) for item in result.items} == {
        ("article", "projects/apollo"), ("fact", lookup.query(YOU, "facts").items[0].id)}
    assert all(item.id for item in result.items) and lookup.read(YOU, [i.id for i in result.items]).missing == []
    with pytest.raises(OperationError, match="name from"):
        lookup.query(YOU, "", {"stage": "open"}, matching="moved")


def test_query_with_neither_from_nor_matching_is_refused_naming_both(lookup: Lookup) -> None:
    with pytest.raises(OperationError, match="query needs from or matching: .*pass matching alone to search"):
        lookup.query(YOU, "")


def test_read_gives_each_article_its_linked_facts_with_quotes_and_when_it_was_compiled(
        parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    fact = lookup.query(YOU, "facts", {"record": "memo-1"}).items[0]
    store.link(fact.id, ["projects/vega"], at="2026-10-30T10:30:00+01:00")  # learned after the run landed
    store.link(fact.id, ["projects/apollo"], at="2026-10-30T08:00:00Z")
    store.write_compilation(Compilation("projects/vega", "Overview", "Moved.", by="projects/article", at="2026-10-30T09:05:00Z",
                                started="2026-10-30T09:00:00Z"))
    store.write_compilation(Compilation("projects/apollo", "Overview", "It moved.", by="projects/article",
                                at="2026-10-30T09:05:00Z", started="2026-10-30T09:00:00Z"))
    result = lookup.read(YOU, ["projects/vega", "projects/apollo"])
    assert result.facts == [fact] and result.facts[0].quotes[0].text == "The review moved to March."
    assert result.linked == {"projects/vega": [fact.id], "projects/apollo": [fact.id]}
    assert result.facts == [fact]  # shown once, though two articles link it
    assert result.articles[0].properties["stage"].by == YOU and result.articles[0].properties["stage"].at
    assert result.compiled_at == {"projects/vega": "2026-10-30T09:00:00+00:00",
                                  "projects/apollo": "2026-10-30T09:00:00+00:00"}
    assert result.facts_since_compiled == {"projects/vega": 1, "projects/apollo": 0}
    assert store.articles_of(fact.id) == ["projects/vega", "projects/apollo"]


def test_read_with_about_keeps_the_facts_that_hold_its_words(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    fact = lookup.query(YOU, "facts", {"record": "memo-1"}).items[0]
    store.link(fact.id, ["projects/apollo"])
    assert lookup.read(YOU, ["projects/apollo"], about="the reviews").facts == [fact]
    missed = lookup.read(YOU, ["projects/apollo"], about="rev")
    assert missed.facts == [] and missed.linked == {"projects/apollo": []}
    assert missed.warnings == [("about 'rev' did not match the one fact of projects/apollo, which holds 'review': "
                                "about matches a word of three letters whole, and a longer word by its start")]
    assert "holds no word to search by" in lookup.read(YOU, [fact.id], about="AI").warnings[0]
    assert lookup.read(YOU, [fact.id], about="budget").warnings == [f"about 'budget' did not match fact {fact.id}"]
    assert lookup.read(YOU, ["projects/apollo"], about="moved").warnings == []
    assert lookup.read(YOU, ["memo-1"], about="march").facts == [fact]  # a record's facts, by its id


def test_a_date_field_takes_a_window_and_a_collection_comes_newest_first_by_it(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    store.set_field("projects/apollo", "due", Property("2026-12-01", YOU, "2026-11-01T09:00:00Z"))
    store.set_field("projects/vega", "due", Property("2026-08-17", YOU, "2026-11-01T09:00:00Z"))
    result = lookup.query(YOU, "projects")
    assert [a.identity.id for a in result.items] == ["projects/apollo", "projects/vega"]
    assert result.ordered_by == "due"
    soon = lookup.query(YOU, "projects", {"stage": "open", "due": "next_30_days"})
    assert [a.identity.id for a in soon.items] == ["projects/apollo"]
    assert lookup.query(YOU, "projects", {"id": "articles/projects/vega.md"}).count == 1
    assert [a.identity.id for a in lookup.query(YOU, "projects", {"due": "last_92_days"}).items] == ["projects/vega"]
    assert lookup.query(YOU, "projects", {"due": "before_last_92_days"}).count == 0
    assert lookup.query(YOU, "projects", {"due": "this_quarter"}).count == 1
    with pytest.raises(OperationError, match="give the window to a date field, due"):
        lookup.query(YOU, "projects", {"window": "last_30_days"})
    with pytest.raises(OperationError, match="'last_month' is not a window: use last_N_days"):
        lookup.query(YOU, "projects", {"due": "last_month"})


def test_window_selects_facts_and_log_lines_by_when_they_happened(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    store.append("mcp-requests", {"tool": "read", "at": "2026-11-17T09:00:00Z"})
    store.append("mcp-requests", {"tool": "find", "at": "2026-09-01T09:00:00Z"})
    assert [r["tool"] for r in lookup.query(YOU, "mcp-requests", {"window": "last_7_days"}).items] == ["read"]
    assert lookup.query(YOU, "facts", {"window": "last_30_days"}).count == 1
    assert lookup.query(YOU, "facts", {"window": "last_7_days"}).count == 0


def test_a_query_of_facts_keeps_those_linked_to_an_article_or_holding_a_word(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    fact = lookup.query(YOU, "facts", {"record": "memo-1"}).items[0]
    store.link(fact.id, ["projects/apollo"])
    assert lookup.query(YOU, "facts", {"articles": "articles/projects/apollo.md"}).items == [fact]
    assert lookup.query(YOU, "facts", {"articles": "projects/vega"}).count == 0
    assert lookup.query(YOU, "facts", {"about": "moved"}).items == [fact]
    with pytest.raises(OperationError, match="holds no word to search by"):
        lookup.query(YOU, "facts", {"about": "to"})


def test_a_query_that_matches_nothing_says_why(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    assert lookup.query(YOU, "projects", {"owner": "west"}).warnings == [
        "no article of projects has a field owner: check its name"]
    assert lookup.query(YOU, "projects", {"stage": "won"}).warnings == [
        "no article of projects holds stage: won, which holds closed, open"]
    store.set_field("projects/apollo", "due", Property("2026-12-01", YOU, "2026-11-01T09:00:00Z"))
    assert lookup.query(YOU, "projects", {"due": "last_7_days"}).warnings == [
        "no article of projects matches due: last_7_days"]
    assert lookup.query(YOU, "projects", {"compiled_at": "2020"}).warnings == [
        "no article of projects matches compiled_at: 2020"]  # a field of the article itself, not a property
    assert lookup.query(YOU, "proposals", {"owner": "west"}).warnings == [
        "no line of proposals has a field owner: check its name"]
    assert lookup.query(YOU, "proposals", {"kind": "merge"}).warnings == ["no line of proposals matches kind: merge"]
    assert lookup.query(YOU, "merges", {"status": "won"}).warnings == ["merges has no lines yet"]
    # a where predicate is named by no line, so none blames a filter or the data for what it kept out
    assert lookup.query(YOU, "projects", {"stage": "open"}, where=[Where("size", ">", 5)]).warnings == []
    assert lookup.query(YOU, "proposals", where=[Where("kind", "=", "merge")]).warnings == []


def test_an_archived_article_is_left_out_unless_a_filter_names_status(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    store.set_identity(Identity("projects/vega", "projects", "Vega", status="archived"))
    assert [a.identity.id for a in lookup.query(YOU, "projects").items] == ["projects/apollo"]
    assert [a.identity.id for a in lookup.query(YOU, "projects", {"status": "archived"}).items] == ["projects/vega"]
    assert lookup.query(YOU, "projects", {"id": "projects/vega"}).warnings == [(
        "no active article of projects matches id: projects/vega: name status, as in status: archived, to include "
        "the rest")]
    store.set_identity(Identity("projects/apollo", "projects", "Apollo", status="archived"))
    assert lookup.query(YOU, "projects").warnings == [
        "every article of projects is archived or merged: name status, as in status: archived, to list them"]


def test_where_and_order_apply_to_logs_and_facts_and_a_collection_is_newest_first(parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    store.append("proposals", {"id": "p_3", "kind": "merge", "n": 3})
    store.append("proposals", {"id": "p_4", "kind": "split", "n": 1})
    logged = lookup.query(YOU, "proposals", where=[Where("n", ">", 0)], order_by="n")
    assert [row["kind"] for row in logged.items] == ["split", "merge"] and logged.count == 2
    assert lookup.query(YOU, "facts", where=[Where("kind", "=", "no-such-kind")]).count == 0
    with pytest.raises(Exception, match="operator"):
        lookup.query(YOU, "proposals", where=[Where("n", "~", 1)])
    store.write_compilation(Compilation("projects/vega", "Overview", "Newer.", by="projects/article", at="2026-11-05T09:00:00Z"))
    assert [a.identity.id for a in lookup.query(YOU, "projects").items] == ["projects/vega", "projects/apollo"]


def test_a_query_of_proposals_reads_each_ones_latest_state(lookup: Lookup, parts: tuple[Lookup, Store]) -> None:
    store = parts[1]
    store.append("proposals", {"id": "p_9", "kind": "admit", "status": "waiting"})
    store.append("proposals", {"id": "p_9", "kind": "admit", "status": "rejected"})
    assert lookup.query(YOU, "proposals", {"id": "p_9", "status": "waiting"}).count == 0
    assert lookup.query(YOU, "proposals", {"id": "p_9"}).items[0]["status"] == "rejected"



def test_find_does_not_match_part_of_a_title(lookup: Lookup) -> None:
    assert lookup.find(YOU, "Apol").matches == [] and lookup.find(YOU, "apollo").matches == ["projects/apollo"]


WRITTEN = Compilation("reports/pipeline", "Summary", "Two deals are open.", by="reports/pipeline", at="2026-11-12T09:01:00Z",
                  started="2026-11-12T09:00:00Z")  # as a run lands it, with when the run started


def test_an_article_no_run_has_written_is_shown_to_every_caller_and_marked_pending_until_one_writes(
        parts: tuple[Lookup, Store]) -> None:
    lookup, store = parts
    store.create(Identity("reports/pipeline", "reports", "Pipeline"))
    for caller in (YOU, Caller("reports/pipeline", "agent")):
        found = lookup.find(caller, "reports/pipeline")
        assert found.matches == ["reports/pipeline"] and found.pending == ["reports/pipeline"]
        shown = lookup.query(caller, "reports")
        assert [a.identity.id for a in shown.items] == ["reports/pipeline"] and shown.count == 1
        assert shown.warnings == []
    store.write_compilation(WRITTEN)
    found = lookup.find(YOU, "reports/pipeline")
    assert found.matches == ["reports/pipeline"] and found.pending == []
    assert lookup.query(YOU, "reports").count == 1


def test_a_pending_article_is_marked_in_every_reply_of_read_find_and_query_and_never_passed_off_as_written(
        parts: tuple[Lookup, Store]) -> None:
    from cerebro.article import PENDING
    from cerebro.mcp import render_find, render_query, render_read
    from cerebro.reply import outside

    lookup, store = parts
    store.create(Identity("reports/pipeline", "reports", "Pipeline"))
    read = lookup.read(YOU, ["reports/pipeline"])
    [article] = outside(read)["articles"]
    assert article["pending"] is True and "text" not in article and "compiled_at" not in article
    assert "unwritten" not in article and "written_at" not in article
    assert "pending: article admitted but not yet written" in render_read(read) and PENDING in render_read(read)
    found = lookup.find(YOU, "Pipeline")
    assert outside(found)["pending"] == ["reports/pipeline"]
    assert "- reports/pipeline (matched on name) [pending: article admitted but not yet written]" in render_find(found)
    queried = lookup.query(YOU, "reports")
    assert outside(queried)["items"][0]["pending"] is True
    assert "- reports/pipeline: Pipeline [pending]" in render_query(queried)
    store.write_compilation(WRITTEN)
    read = lookup.read(YOU, ["reports/pipeline"])
    assert "pending" not in outside(read)["articles"][0] and PENDING not in render_read(read)
    assert "pending" not in outside(lookup.find(YOU, "Pipeline"))
    assert "[pending]" not in render_query(lookup.query(YOU, "reports"))


def test_fields_its_curator_set_at_admit_do_not_make_an_article_written(parts: tuple[Lookup, Store]) -> None:
    from cerebro.mcp import render_find, render_query, render_read
    from cerebro.reply import outside

    lookup, store = parts
    store.create(Identity("reports/pipeline", "reports", "Pipeline"))
    store.set_field("reports/pipeline", "stage", Property("open", Caller("reports/curator", "agent"),
                                                          "2026-11-12T08:00:00Z"))
    assert outside(lookup.read(YOU, ["reports/pipeline"]))["articles"][0]["pending"] is True
    assert "[pending: article admitted but not yet written]" in render_find(lookup.find(YOU, "Pipeline"))
    assert "- reports/pipeline: Pipeline (stage: open) [pending]" in render_query(lookup.query(YOU, "reports"))
    assert "stage: open\npending: article admitted but not yet written" in render_read(
        lookup.read(YOU, ["reports/pipeline"]))


def test_an_archived_article_no_run_wrote_is_not_marked_pending(parts: tuple[Lookup, Store]) -> None:
    from cerebro.mcp import render_read
    from cerebro.reply import outside

    lookup, store = parts
    store.create(Identity("reports/pipeline", "reports", "Pipeline", status="archived"))
    read = lookup.read(YOU, ["reports/pipeline"])
    assert "pending" not in outside(read)["articles"][0] and "pending" not in render_read(read)
    assert lookup.find(YOU, "Pipeline").pending == []


def test_read_gives_compiled_at_as_the_front_matter_does_and_no_written_at(parts: tuple[Lookup, Store]) -> None:
    from cerebro.article import markdown
    from cerebro.mcp import render_read
    from cerebro.reply import outside

    lookup, store = parts
    store.create(Identity("reports/pipeline", "reports", "Pipeline"))
    store.write_compilation(WRITTEN)
    store.write_compilation(Compilation("reports/pipeline", "Next", "Close both.", by="reports/pipeline",
                                at="2026-11-14T10:05:00Z", started="2026-11-14T10:00:00Z"))
    read = lookup.read(YOU, ["reports/pipeline"])
    [article] = outside(read)["articles"]
    assert article["compiled_at"] == "2026-11-12T09:00:00Z" and "written_at" not in article
    assert "compiled_at: '2026-11-12T09:00:00Z'" in markdown(store.article("reports/pipeline"))  # YAML quotes it
    text = render_read(read)
    assert "compiled_at: 2026-11-12T09:00:00Z" in text and "written_at" not in text
