"""`query` with `matching`: a collection's articles ranked through the search index, which follows the store by
itself, the same filters as any query, facts handed to the facts layer's own search, and the MCP reply."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from cerebro import Caller, Cerebro, Record
from cerebro.article import Compilation, Identity, Property
from cerebro.curate import FIRST_RUN_KEY
from cerebro.facts import Quote
from cerebro.fakes import FakeExtraction, MemoryIndex, MemoryStore
from cerebro.lookup import OperationError, StoreLookup
from cerebro.mcp import server
from cerebro.records import Proposal, propose
from cerebro.search import RerankError
from cerebro.store import SQLiteIndex, SQLiteStore, Store, Where

YOU = Caller("you", "person")
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
PROJECTS = (("projects/apollo", "Apollo", "open", 3, "The launch moved to March. The budget is short."),
            ("projects/vega", "Vega", "open", 8, "The launch is on track."),
            ("projects/orion", "Orion", "closed", 5, "The budget review is done, and the budget is closed."))


def fill(store: Store) -> Store:
    for id, title, stage, size, text in PROJECTS:
        store.create(Identity(id, "projects", title))
        store.set_field(id, "stage", Property(stage, YOU, "2026-11-01T09:00:00Z"))
        store.set_field(id, "size", Property(size, YOU, "2026-11-01T09:00:00Z"))
        store.write_compilation(Compilation(id, "Overview", text, by="projects/article", at="2026-11-02T09:00:00Z"))
    store.create(Identity("teams/west", "teams", "West"))
    store.write_compilation(Compilation("teams/west", "Overview", "The budget is set.", by="teams/article"))
    return store


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    made: Store = MemoryStore() if request.param == "memory" else SQLiteStore()
    with made:
        yield fill(made)


def ids(result: Any) -> list[str]:
    return [item.identity.id for item in result.items]


def test_matching_ranks_a_collections_articles_best_first_with_the_same_filters_as_any_query(store: Store) -> None:
    lookup = StoreLookup(store, clock=lambda: NOW)
    assert ids(lookup.query(YOU, "projects", matching="budget")) == ["projects/orion", "projects/apollo"]
    assert ids(lookup.query(YOU, "projects", matching="launch budget"))[0] == "projects/apollo"  # holds both
    assert ids(lookup.query(YOU, "projects", {"stage": "open"}, matching="budget")) == ["projects/apollo"]
    assert ids(lookup.query(YOU, "projects", where=[Where("size", ">", 4)], matching="launch")) == ["projects/vega"]
    assert ids(lookup.query(YOU, "projects", matching="budget", order_by="size")) == ["projects/apollo",
                                                                                     "projects/orion"]
    paged = lookup.query(YOU, "projects", matching="the", limit=1, offset=1)
    assert paged.count == 3 and len(paged.items) == 1
    assert ids(lookup.query(YOU, "projects", matching="orion")) == ["projects/orion"]  # by its title
    assert ids(lookup.query(YOU, "teams", matching="budget")) == ["teams/west"]  # one collection at a time


def test_matching_says_why_it_matched_nothing_and_leaves_out_an_archived_article(store: Store) -> None:
    lookup = StoreLookup(store, clock=lambda: NOW)
    none = lookup.query(YOU, "projects", matching="pricing")
    assert none.count == 0 and none.warnings == [
        "matching 'pricing' matched none of the 3 articles of projects: try other words, or fewer filters"]
    narrowed = lookup.query(YOU, "projects", {"stage": "closed"}, matching="launch")
    assert narrowed.warnings == [("matching 'launch' matched none of the 1 articles of projects that the other "
                                 "filters keep: try other words, or fewer filters")]
    store.set_identity(Identity("projects/orion", "projects", "Orion", status="archived"))
    assert ids(lookup.query(YOU, "projects", matching="budget")) == ["projects/apollo"]
    assert ids(lookup.query(YOU, "projects", {"status": "archived"}, matching="budget")) == ["projects/orion"]


def test_matching_on_a_first_run_leaves_out_a_proposed_stand_in_instead_of_failing_on_it(store: Store) -> None:
    store.set_state(FIRST_RUN_KEY, {"status": "waiting", "at": "2026-11-17T09:00:00Z"})
    propose(store, Proposal("p_a", "article.admit", "projects/budget-board", Caller("projects/curator", "agent"),
                            "2026-11-17T09:00:00Z", {"name": "Budget Board", "collection": "projects"}))
    lookup = StoreLookup(store, clock=lambda: NOW)
    assert any(isinstance(item, dict) for item in lookup.query(YOU, "projects").items)  # the listing gives it
    found = lookup.query(YOU, "projects", matching="budget")  # it has no text, so it matches no words
    assert ids(found) == ["projects/orion", "projects/apollo"] and found.warnings == []


def test_the_index_follows_the_store_with_no_other_call(store: Store) -> None:
    lookup = StoreLookup(store, clock=lambda: NOW)
    assert ids(lookup.query(YOU, "projects", matching="pricing")) == []
    store.write_compilation(Compilation("projects/vega", "Risks", "Pricing is the blocker.", by="projects/article"))
    assert ids(lookup.query(YOU, "projects", matching="pricing")) == ["projects/vega"]
    store.delete_compilation("projects/vega", "Risks")
    assert ids(lookup.query(YOU, "projects", matching="pricing")) == []


def test_a_given_index_is_used_and_the_sqlite_store_keeps_its_own_in_its_file(tmp_path: Path) -> None:
    given = MemoryIndex()
    assert ids(StoreLookup(fill(MemoryStore()), index=given).query(YOU, "projects", matching="vega")) == [
        "projects/vega"]
    assert given.rows["articles"]
    with SQLiteStore(tmp_path / "cerebro.sqlite") as store:
        lookup = StoreLookup(fill(store))
        lookup.query(YOU, "projects", matching="launch")
        assert isinstance(lookup.index, SQLiteIndex) and lookup.index.store is store
    with SQLiteStore(tmp_path / "cerebro.sqlite") as store:
        index = SQLiteIndex(store)
        assert index.synced("articles") > 0 and index.search("articles", "launch", 5)


def test_matching_on_facts_hands_off_to_the_facts_layers_search_and_keeps_its_filters() -> None:
    extraction = FakeExtraction()
    extraction.extract(Record(id="memo-1", source="notes", revision="1", at="2026-11-01T09:00:00Z",
                              body="The budget moved.\n\nThe launch moved.\n\nThe launch budget is set."))
    extraction.extract(Record(id="call-1", source="calls", revision="1", at="2026-11-02T09:00:00Z",
                              body="Budget talk."))
    lookup = StoreLookup(MemoryStore(), extraction, clock=lambda: NOW)
    texts = [f.text for f in lookup.query(YOU, "facts", matching="launch budget").items]
    assert texts[0] == "The launch budget is set." and len(texts) == 4
    kept = lookup.query(YOU, "facts", {"source": "notes"}, matching="budget")
    assert [f.record for f in kept.items] == ["memo-1", "memo-1"]
    none = lookup.query(YOU, "facts", {"source": "calls"}, matching="launch")
    assert none.count == 0 and none.warnings == [("matching 'launch' matched none of the 1 facts that the other "
                                                 "filters keep: try other words, or fewer filters")]
    with pytest.raises(OperationError, match="needs a facts layer"):
        StoreLookup(MemoryStore()).query(YOU, "facts", matching="budget")


def test_matching_refuses_no_words_and_a_log(store: Store) -> None:
    lookup = StoreLookup(store, clock=lambda: NOW)
    with pytest.raises(OperationError, match="matching is empty"):
        lookup.query(YOU, "projects", matching="  ")
    with pytest.raises(OperationError, match="proposals is a log: filter it by its fields"):
        lookup.query(YOU, "proposals", matching="budget")


def test_a_rerank_function_handed_in_reorders_the_best_matches(store: Store) -> None:
    lookup = StoreLookup(store, clock=lambda: NOW)
    lookup.rerank = lambda text, hits: hits[::-1]
    assert ids(lookup.query(YOU, "projects", matching="budget")) == ["projects/apollo", "projects/orion"]
    lookup.rerank = lambda text, hits: hits[:1]
    with pytest.raises(RerankError):
        lookup.query(YOU, "projects", matching="budget")


def test_query_with_matching_passes_through_mcp_and_renders(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("budget:\n  daily_usd: 20\n")
    workspace = Cerebro(root, store=fill(MemoryStore()), clock=lambda: NOW)

    async def calls() -> Any:
        async with Client(server(workspace, clock=lambda: NOW, log=False)) as client:
            return await client.call_tool("query", {"from": "projects", "matching": "budget", "stage": "open"})

    reply = anyio.run(calls)
    assert reply.structured_content["count"] == 1
    assert reply.content[0].text.splitlines() == ["1 match in projects, best match first; 1 to 1 shown",
                                                  "- projects/apollo: Apollo (size: 3; stage: open)"]


# -- a search across everything: no from ----------------------------------------------------------------------------

MEMO = Record(id="memo-1", source="notes", revision="1", at="2026-11-03T09:00:00Z",
              body="The Apollo budget is short by a third.\n\nThe Vega budget review waits on finance.\n\n"
                   "The lunch moved to noon.")


def everything(store: Store, extraction: FakeExtraction | None = None) -> tuple[StoreLookup, dict[str, str]]:
    """A lookup over two collections and the memo's facts, the Apollo fact linked to Apollo and the Vega fact linked
    to Vega, and the facts' ids by their first word after "The"."""
    extraction = extraction if extraction is not None else FakeExtraction()
    extraction.extract(MEMO)
    by_word = {f.text.split()[1]: f.id for f in extraction.facts()}
    store.link(by_word["Apollo"], ["projects/apollo"])
    store.link(by_word["Vega"], ["projects/vega"])
    return StoreLookup(store, extraction, clock=lambda: NOW), by_word


def shown(result: Any) -> list[tuple[str, str, list[str]]]:
    return [(item.kind, item.id, [f.id for f in item.folded]) for item in result.items]


def test_matching_with_no_from_searches_every_collection_and_the_facts_as_one_list_best_first(store: Store) -> None:
    lookup, fact = everything(store)
    result = lookup.query(YOU, "", matching="budget")
    assert result.source == "articles and facts" and result.best_first and result.warnings == []
    best_article = lookup.query(YOU, "projects", matching="budget").items[0].identity.id
    best_fact = lookup.query(YOU, "facts", matching="budget").items[0].id
    assert best_article == "projects/orion" and best_fact == fact["Vega"]
    # The articles rank orion, west, apollo, and the facts Vega, Apollo. Apollo earns from both engines, its own
    # place and its linked fact's, which is folded under it, and so comes first. The Vega fact is linked to Vega,
    # which does not hold the word, so it stands on its own.
    assert shown(result) == [("article", "projects/apollo", [fact["Apollo"]]),
                             ("article", "projects/orion", []),
                             ("fact", fact["Vega"], []),
                             ("article", "teams/west", [])]
    assert result.count == 4 and not any(item.id == fact["Apollo"] for item in result.items)  # folded, not twice
    apollo = result.items[0]
    assert (apollo.source, apollo.title, apollo.heading) == ("projects", "Apollo", "Overview")
    assert apollo.item.identity.id == "projects/apollo" and apollo.folded[0].text.startswith("The Apollo budget")
    vega = result.items[2]
    assert (vega.source, vega.title, vega.item.record) == ("notes", "The Vega budget review waits on finance.",
                                                           "memo-1")
    read = lookup.read(YOU, [apollo.folded[0].id, vega.id])  # each item and each folded fact reads by its id
    assert [f.id for f in read.facts] == [fact["Apollo"], fact["Vega"]] and read.missing == []


def test_a_search_across_everything_merges_by_rank_however_the_two_engines_score(store: Store) -> None:
    class Written(FakeExtraction):
        """A facts layer whose search gives hits written here, with scores far from the index's, and, first, a fact
        near by meaning that holds none of the words, as Factoid's nearest facts come back however far they are."""

        scores = (900.0, 800.0, 700.0)

        def search(self, text: str, top_k: int = 10, *, source: str | None = None) -> list[Any]:
            from cerebro.search import Hit
            texts = {f.text.split()[1]: f.id for f in self.facts()}
            return [Hit(texts[word], score) for word, score in zip(("lunch", "Vega", "Apollo"), self.scores)][:top_k]

    for scores in ((900.0, 800.0, 700.0), (0.003, 0.002, 0.001)):
        extraction = Written()
        extraction.scores = scores
        lookup, fact = everything(store, extraction)
        lunch = next(f for f in extraction.by_record["memo-1"] if f.id == fact["lunch"])
        lunch.quotes = [Quote("The lunch moved to noon, and the budget with it.")]  # a quote's words do not count
        result = lookup.query(YOU, "", matching="budget")
        assert shown(result) == [("article", "projects/apollo", [fact["Apollo"]]), ("article", "projects/orion", []),
                                 ("fact", fact["Vega"], []), ("article", "teams/west", [])]  # no lunch: no budget
        none = lookup.query(YOU, "", matching="pricing")
        assert none.items == [] and none.warnings == [("matching 'pricing' matched no article and no fact: try "
                                                       "other words, or fewer of them")]


def test_a_rerank_function_reorders_the_merged_list_once_and_sees_articles_and_facts(store: Store) -> None:
    lookup, fact = everything(store)
    seen: list[list[tuple[str, str]]] = []

    def backwards(text: str, top: list[Any]) -> list[Any]:
        seen.append([(hit.attributes["kind"], hit.attributes.get("title") or hit.attributes.get("text"))
                     for hit in top])
        return top[::-1]

    lookup.rerank = backwards
    result = lookup.query(YOU, "", matching="budget")
    assert seen == [[("article", "Apollo"), ("article", "Orion"),
                     ("fact", "The Vega budget review waits on finance."), ("article", "West")]]
    assert [item.id for item in result.items] == ["teams/west", fact["Vega"], "projects/orion", "projects/apollo"]
    assert result.items[-1].folded[0].id == fact["Apollo"]  # an article's folded facts travel with it


def test_past_the_extraction_cap_a_search_across_everything_gives_articles_alone_and_says_why(store: Store) -> None:
    from cerebro.budget import Caps, Ledger, Metered
    from cerebro.facts import Extracted
    from cerebro.models import ModelSpec

    model = ModelSpec("fireworks", "glm-5p3-flash")
    ledger = Ledger(store, Caps(extraction=0.01))
    held, _ = everything(store, FakeExtraction())
    metered = Metered(held.extraction, ledger, model, lambda: NOW)  # type: ignore[arg-type]
    lookup = StoreLookup(store, metered, clock=lambda: NOW)
    assert "fact" in {item.kind for item in lookup.query(YOU, "", matching="budget").items}  # under the cap
    ledger.record("extraction", NOW, model, Extracted([], input_tokens=10_000_000))
    result = lookup.query(YOU, "", matching="budget")
    assert [item.id for item in result.items] == ["projects/orion", "teams/west", "projects/apollo"]
    assert result.items[2].folded == [] and len(result.warnings) == 1
    assert result.warnings[0].startswith("the facts were not searched, so these are articles alone: extraction "
                                         "spent $") and "raise budget.extraction_daily_usd" in result.warnings[0]


def test_with_no_facts_layer_a_search_across_everything_gives_articles_alone_and_says_so(store: Store) -> None:
    result = StoreLookup(store, clock=lambda: NOW).query(YOU, "", matching="budget")
    assert [item.kind for item in result.items] == ["article"] * 3
    assert result.warnings == ["this workspace has no facts layer, so matching searched its articles alone"]


def test_a_search_across_everything_leaves_out_an_archived_article_and_pages_the_merged_list(store: Store) -> None:
    lookup, fact = everything(store)
    store.set_identity(Identity("projects/apollo", "projects", "Apollo", status="archived"))
    result = lookup.query(YOU, "", matching="budget")
    # Apollo is out of the list, so its fact is no longer folded: it stands on its own.
    assert shown(result) == [("article", "projects/orion", []), ("fact", fact["Vega"], []),
                             ("article", "teams/west", []), ("fact", fact["Apollo"], [])]
    page = lookup.query(YOU, None, matching="budget", limit=2, offset=1)  # type: ignore[arg-type]
    assert page.count == 4 and [item.id for item in page.items] == [fact["Vega"], "teams/west"]
    past = lookup.query(YOU, "", matching="budget", offset=9)
    assert past.items == [] and past.warnings[-1] == "offset 9 is past the 4 matches: pass a smaller offset"
    none = lookup.query(YOU, "", matching="pricing")
    assert none.count == 0 and none.warnings == [("matching 'pricing' matched no article and no fact: try other "
                                                  "words, or fewer of them")]


def test_a_search_across_everything_refuses_a_filter_an_order_and_no_words(store: Store) -> None:
    lookup, _ = everything(store)
    with pytest.raises(OperationError, match="the filter stage applies to one of them: name from, a collection or "
                                             "facts, to filter by it"):
        lookup.query(YOU, "", {"stage": "open"}, matching="budget")
    with pytest.raises(OperationError, match="the filter window applies"):
        lookup.query(YOU, "", {"window": "last_7_days"}, matching="budget")
    with pytest.raises(OperationError, match="the filter size applies"):
        lookup.query(YOU, "", where=[Where("size", ">", 4)], matching="budget")
    with pytest.raises(OperationError, match="order_by size orders one source by a field: name from"):
        lookup.query(YOU, "", matching="budget", order_by="size")
    with pytest.raises(OperationError, match="matching is empty"):
        lookup.query(YOU, "", matching="  ")
    with pytest.raises(OperationError, match="matching takes at most 32 words"):
        lookup.query(YOU, "", matching="budget " * 33)
    with pytest.raises(OperationError, match="query needs from or matching"):
        lookup.query(YOU, "")


def test_query_with_matching_and_no_from_passes_through_mcp_and_reads_well(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("budget:\n  daily_usd: 20\n")
    store = fill(MemoryStore())
    extraction = FakeExtraction()
    workspace = Cerebro(root, store=store, extraction=extraction, clock=lambda: NOW)
    _, fact = everything(store, extraction)

    async def calls() -> Any:
        async with Client(server(workspace, clock=lambda: NOW)) as client:
            return await client.call_tool("query", {"matching": "budget", "limit": 3})

    reply = anyio.run(calls)
    data = reply.structured_content
    assert data["source"] == "articles and facts" and data["count"] == 4
    assert [(item["kind"], item["id"]) for item in data["items"]] == [
        ("article", "projects/apollo"), ("article", "projects/orion"), ("fact", fact["Vega"])]
    assert data["items"][0]["facts"][0]["id"] == fact["Apollo"]
    assert reply.content[0].text.splitlines() == [
        "4 matches in articles and facts, best match first; 1 to 3 shown",
        ("each line says what it is, an article or a fact, then its id: read takes either id, an article's for its "
         "text, a fact's for its quote. A fact under an article is linked to it."),
        "- article projects/apollo: Apollo (size: 3; stage: open) [matched in Overview]",
        f"  - fact {fact['Apollo']} [2026-11-03T09:00:00Z, notes: memo-1] The Apollo budget is short by a third.",
        "- article projects/orion: Orion (size: 5; stage: closed) [matched in Overview]",
        f"- fact {fact['Vega']} [2026-11-03T09:00:00Z, notes: memo-1] The Vega budget review waits on finance."]
    logged = store.rows("mcp-requests")[-1].data
    assert logged["args"] == {"matching": "budget", "limit": 3} and logged["result"] == "hit"
    assert logged["resolved"] == ["projects/apollo", "projects/orion", fact["Vega"]]


def test_a_search_reply_shows_the_best_three_facts_under_an_article_says_how_many_more_and_leaves_out_quotes() -> None:
    from cerebro.facts import Fact
    from cerebro.lookup import QueryResult
    from cerebro.mcp import render_query
    from cerebro.search import Found

    def fact(n: int, source: str, record: str) -> Fact:
        return Fact(f"fact_{n}", f"The budget is {n}.", "budget", "fact", "2026-11-03T09:00:00Z", source, record,
                    [Quote("Mara said the budget moved.", "Mara")])

    article = StoreLookup(fill(MemoryStore()), clock=lambda: NOW).query(YOU, "projects", matching="budget").items[0]
    folded = [fact(n, "local", f"local/memo-{n}.md") for n in range(5)]
    alone = fact(9, "slack", "C42/1700000000.1")
    result = QueryResult("articles and facts", [Found("article", article.identity.id, "projects", "Orion", article,
                                                      folded, "Overview"),
                                                Found("fact", alone.id, "slack", alone.text, alone)], 2,
                         best_first=True)
    lines = render_query(result).splitlines()[2:]
    assert lines == ["- article projects/orion: Orion (size: 5; stage: closed) [matched in Overview]",
                     "  - fact fact_0 [2026-11-03T09:00:00Z, local/memo-0.md] The budget is 0.",
                     "  - fact fact_1 [2026-11-03T09:00:00Z, local/memo-1.md] The budget is 1.",
                     "  - fact fact_2 [2026-11-03T09:00:00Z, local/memo-2.md] The budget is 2.",
                     "  - 2 more of its facts match: read projects/orion with about set to the same words",
                     "- fact fact_9 [2026-11-03T09:00:00Z, slack: C42/1700000000.1] The budget is 9."]


def test_a_query_over_mcp_with_neither_from_nor_matching_is_refused_in_cerebros_own_words(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("budget:\n  daily_usd: 20\n")
    workspace = Cerebro(root, store=fill(MemoryStore()), clock=lambda: NOW)

    async def calls() -> Any:
        async with Client(server(workspace, clock=lambda: NOW, log=False)) as client:
            return await client.call_tool("query", {"limit": 3})

    refused = anyio.run(calls)
    text = refused.content[0].text  # the SDK puts "Error executing tool query: " before the line
    assert refused.is_error and "\n" not in text and "Field required" not in text
    assert text.endswith("query needs from or matching: name in from a collection, facts, a log such as proposals, "
                         "or an app's kind, or pass matching alone to search articles and facts at once")


def test_a_search_across_everything_leaves_out_a_retracted_fact_its_search_still_finds() -> None:
    facts = FakeExtraction(searches_retracted=True)
    facts.extract(Record(id="memo", source="notes", revision="1", at="2026-11-01T09:00:00Z",
                         body="The Vega budget review waits on finance.\n\nThe Orion budget doubled."))
    gone = next(f for f in facts.facts() if "Vega" in f.text)
    facts.retract(gone.id)
    assert gone.id in [hit.id for hit in facts.search("budget")]  # the layer's search still finds it
    lookup = StoreLookup(MemoryStore(), facts)
    found = lookup.query(YOU, "", matching="budget")
    assert [item.id for item in found.items if item.kind == "fact"] == [
        next(f.id for f in facts.facts() if "Orion" in f.text)]
