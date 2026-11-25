"""The `Index` suite against the in-memory fake and the SQLite index, and how the index follows the store."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from suites.index import *

from cerebro.article import Compilation, Identity
from cerebro.fakes import MemoryIndex, MemoryStore
from cerebro.search import (
    ARTICLE,
    ARTICLES,
    FACT,
    FUSE_K,
    RERANK_TOP,
    Hit,
    Index,
    RerankError,
    chunks,
    fuse,
    ranked,
    reorder,
    sync,
)
from cerebro.store import SQLiteIndex, SQLiteStore, Store


@pytest.fixture(params=["memory", "sqlite", "sqlite-alone"])
def index(request: pytest.FixtureRequest) -> Iterator[Index]:
    if request.param == "memory":
        yield MemoryIndex()
        return
    with SQLiteStore() as store:
        yield SQLiteIndex(store) if request.param == "sqlite" else SQLiteIndex()


@pytest.fixture(params=["memory", "sqlite"])
def pair(request: pytest.FixtureRequest) -> Iterator[tuple[Index, Store]]:
    """An empty store and an index beside it: the fake over the in-memory store, or the SQLite index in the file."""
    if request.param == "memory":
        yield MemoryIndex(), MemoryStore()
        return
    with SQLiteStore() as store:
        yield SQLiteIndex(store), store


def found(index: Index, text: str) -> list[str]:
    return [hit.id for hit in index.search(ARTICLES, text, top_k=10)]


def test_the_sqlite_index_matches_a_word_by_its_stem_ranks_rarer_words_first_and_reads_any_text_as_words() -> None:
    index = SQLiteIndex()
    index.upsert(ARTICLES, [{"id": "a", "text": "the launch moves to march"},
                            {"id": "b", "text": "the launch is set"},
                            {"id": "c", "text": "the budget is set"}])
    assert found(index, "moved") == ["a"]
    assert found(index, "launch budget")[0] == "c"  # budget is the rarer word
    assert found(index, 'launch" OR budget NOT "x') == found(index, "launch budget x")
    assert found(index, "(*)") == [] and found(index, "") == []


def test_the_sqlite_index_keeps_its_rows_and_cursor_in_the_store_file(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "cerebro.sqlite") as store:
        index = SQLiteIndex(store)
        index.upsert(ARTICLES, [{"id": "a", "text": "launch", "article": "projects/apollo"}])
        index.mark(ARTICLES, 7)
        index.mark(ARTICLES, 3)  # never back
    with SQLiteStore(tmp_path / "cerebro.sqlite") as store:
        again = SQLiteIndex(store)
        assert found(again, "launch") == ["a"] and again.synced(ARTICLES) == 7 and again.synced("notes") == 0
        assert again.ids(ARTICLES, {"article": "projects/apollo"}) == ["a"]
        assert again.ids(ARTICLES, {"article": "projects/vega"}) == []


def test_an_article_is_a_row_per_part_under_its_title_and_heading_or_one_row_of_its_title() -> None:
    store = MemoryStore()
    store.create(Identity("projects/apollo", "projects", "Apollo"))
    assert chunks(store.article("projects/apollo")) == [  # type: ignore[arg-type]
        {"id": "projects/apollo#0", "text": "Apollo", "article": "projects/apollo", "collection": "projects",
         "title": "Apollo", "heading": ""}]
    store.write_compilation(Compilation("projects/apollo", "Overview", "It moved.", by="projects/article"))
    store.write_compilation(Compilation("projects/apollo", "Risks", "The budget.", by="projects/article"))
    rows = chunks(store.article("projects/apollo"))  # type: ignore[arg-type]
    assert [(r["id"], r["heading"], r["text"]) for r in rows] == [
        ("projects/apollo#0", "Overview", "Apollo\nOverview\nIt moved."),
        ("projects/apollo#1", "Risks", "Apollo\nRisks\nThe budget.")]


def test_a_part_written_whole_is_a_row_per_heading_in_its_text_and_one_for_text_before_any_heading() -> None:
    store = MemoryStore()
    store.create(Identity("calls/kickoff", "calls", "Kickoff"))
    text = "Who met.\n\n## Decisions\n\nA pilot.\n\n```yaml\n## not a heading\n```\n\n# Next steps\n\nA call."
    store.write_compilation(Compilation("calls/kickoff", "calls/article", text, by="calls/article", whole=True))
    rows = chunks(store.article("calls/kickoff"))  # type: ignore[arg-type]
    assert [(r["id"], r["heading"], r["text"]) for r in rows] == [
        ("calls/kickoff#0", "", "Kickoff\n\nWho met."),
        ("calls/kickoff#1", "Decisions", "Kickoff\nDecisions\nA pilot.\n\n```yaml\n## not a heading\n```"),
        ("calls/kickoff#2", "Next steps", "Kickoff\nNext steps\nA call.")]


def test_sync_follows_the_store_adding_each_written_article_and_dropping_a_part_it_lost(
        pair: tuple[Index, Store], monkeypatch: pytest.MonkeyPatch) -> None:
    index, store = pair
    asked, read = [], store.changed_since
    monkeypatch.setattr(store, "changed_since", lambda cursor: asked.append(cursor) or read(cursor))
    store.create(Identity("projects/apollo", "projects", "Apollo"))
    store.write_compilation(Compilation("projects/apollo", "Overview", "It moved.", by="projects/article"))
    store.append("proposals", {"id": "p_1"})  # a change of another kind first: sync reads on past it
    store.write_compilation(Compilation("projects/apollo", "Risks", "The budget.", by="projects/article"))
    mark = sync(index, store)
    assert mark == store.cursor() and found(index, "budget") == ["projects/apollo#1"]
    assert sync(index, store) == mark and asked == [0, mark]  # nothing new: it reads on from where it was
    store.delete_compilation("projects/apollo", "Risks")
    store.create(Identity("projects/vega", "projects", "Vega"))
    sync(index, store)
    assert found(index, "budget") == [] and found(index, "vega") == ["projects/vega#0"]
    store.write_compilation(Compilation("projects/vega", "Overview", "The budget is set.", by="projects/article"))
    sync(index, store)
    assert found(index, "vega") == ["projects/vega#0"] and index.search(ARTICLES, "budget", 5)[0].attributes == {
        "article": "projects/vega", "collection": "projects", "title": "Vega", "heading": "Overview"}


def test_sync_of_a_store_file_reopened_reads_on_from_its_cursor_and_drops_a_part_lost_meanwhile(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with SQLiteStore(tmp_path / "cerebro.sqlite") as store:
        for id in ("projects/apollo", "projects/Apollo"):  # two ids apart only by case are two articles
            store.create(Identity(id, "projects", "Apollo"))
            store.write_compilation(Compilation(id, "Overview", "It moved.", by="projects/article"))
            store.write_compilation(Compilation(id, "Risks", "The budget.", by="projects/article"))
        index = SQLiteIndex(store)
        assert sync(index, store) == store.cursor() == index.synced(ARTICLES)
    with SQLiteStore(tmp_path / "cerebro.sqlite") as store:
        mark = store.cursor()
        store.delete_compilation("projects/apollo", "Risks")
        index, asked = SQLiteIndex(store), []
        read = store.changed_since
        monkeypatch.setattr(store, "changed_since", lambda cursor: asked.append(cursor) or read(cursor))
        assert found(index, "budget") == ["projects/Apollo#1", "projects/apollo#1"]  # still there until it syncs
        sync(index, store)
        assert asked == [mark] and index.synced(ARTICLES) == store.cursor()
        assert found(index, "budget") == ["projects/Apollo#1"]


def test_a_rerank_function_reorders_the_best_hits_and_may_not_add_or_drop_one() -> None:
    index = MemoryIndex()
    index.upsert(ARTICLES, [{"id": "a", "text": "launch budget", "collection": "projects"},
                            {"id": "b", "text": "launch", "collection": "projects"}])
    assert [h.id for h in ranked(index, ARTICLES, "launch budget", 5)] == ["a", "b"]
    assert [h.id for h in ranked(index, ARTICLES, "launch budget", 1)] == ["a"]
    index.upsert(ARTICLES, [{"id": "c", "text": "launch", "collection": "teams"}])
    assert [h.id for h in ranked(index, ARTICLES, "launch", 5, {"collection": "teams"})] == ["c"]
    seen = []
    assert [h.id for h in ranked(index, ARTICLES, "launch budget", 5, {"collection": "projects"},
                                 rerank=lambda text, hits: seen.append(text) or hits[::-1])] == ["b", "a"]
    assert seen == ["launch budget"]
    with pytest.raises(RerankError, match="gave back 1 hits for the 2 it was given, or other ids: reorder the hits "
                                          "it is handed, and add or drop none"):
        ranked(index, ARTICLES, "launch", 5, {"collection": "projects"}, rerank=lambda text, hits: hits[:1])
    with pytest.raises(RerankError):
        ranked(index, ARTICLES, "launch", 5, rerank=lambda text, hits: [hits[0], Hit("z", 1.0)])


def hits(*pairs: tuple[str, float]) -> list[Hit]:
    """A hand-built ranking, best first, with the scores given, whatever they say."""
    return [Hit(id, score) for id, score in pairs]


def order(merged: list[Hit]) -> list[tuple[str, str]]:
    return [(hit.attributes["kind"], hit.id) for hit in merged]


def test_fuse_merges_two_rankings_by_rank_whatever_their_scores_say() -> None:
    articles = hits(("a1", 12.0), ("a2", 11.0), ("a3", 10.0))
    want = [(ARTICLE, "a1"), (FACT, "f1"), (ARTICLE, "a2"), (FACT, "f2"), (ARTICLE, "a3")]
    # By score the facts would come all first, then all last: by rank they take turns, an article first at a tie.
    assert order(fuse(articles, hits(("f1", 900.0), ("f2", 800.0)), {})) == want
    assert order(fuse(articles, hits(("f1", 0.002), ("f2", 0.001)), {})) == want
    # A ranking's order is what counts, even where its own scores run the other way.
    assert order(fuse(hits(("a1", 1.0), ("a2", 5.0)), hits(("f1", 1.0), ("f2", 9.0)), {})) == [
        (ARTICLE, "a1"), (FACT, "f1"), (ARTICLE, "a2"), (FACT, "f2")]
    merged = fuse(articles, hits(("f1", 900.0)), {})
    assert [round(hit.score * (FUSE_K + 1), 6) for hit in merged][:2] == [1.0, 1.0]  # 1 / (k + 1) each
    assert merged[0].score > merged[2].score == 1 / (FUSE_K + 2)


def test_fuse_folds_a_fact_under_a_listed_article_it_is_linked_to_and_lifts_that_article() -> None:
    articles = hits(("projects/apollo", 3.0), ("projects/vega", 2.0))
    facts = hits(("f1", 5.0), ("f2", 4.0), ("f3", 3.0), ("f4", 2.0))
    links = {"f1": ["projects/vega"], "f2": ["projects/orion"], "f3": ["projects/vega", "projects/apollo"]}
    merged = fuse(articles, facts, links)
    # vega earns from both rankings, second in the articles' and first in the facts' through f1, and passes apollo.
    assert order(merged) == [(ARTICLE, "projects/vega"), (ARTICLE, "projects/apollo"), (FACT, "f2"), (FACT, "f4")]
    assert merged[0].score == 1 / (FUSE_K + 2) + 1 / (FUSE_K + 1)
    assert merged[1].score == 1 / (FUSE_K + 1) + 1 / (FUSE_K + 3)  # f3 names both: it goes under the better placed
    assert [hit.attributes["folded"] for hit in merged] == [["f1"], ["f3"], [], []]
    # f2 is linked to an article that is not in the list, so it stays an item of its own, at its own place.
    assert merged[2].score == 1 / (FUSE_K + 2) and merged[3].score == 1 / (FUSE_K + 4)
    # Equal sums go to the earlier article.
    tied = fuse(articles, hits(("f1", 1.0), ("f2", 1.0)), {"f1": ["projects/vega"], "f2": ["projects/apollo"]})
    assert tied[0].score == tied[1].score and order(tied) == [(ARTICLE, "projects/apollo"), (ARTICLE, "projects/vega")]
    # The better placed is the one the articles' ranking puts first, whatever its id.
    flipped = fuse(hits(("teams/west", 1.0), ("projects/apollo", 2.0)), hits(("f1", 1.0)),
                   {"f1": ["projects/apollo", "teams/west"]})
    assert [hit.attributes["folded"] for hit in flipped] == [["f1"], []] and flipped[0].id == "teams/west"


def test_fuse_keeps_each_hits_attributes_and_lists_an_article_or_a_fact_once() -> None:
    articles = [Hit("projects/apollo", 2.0, {"collection": "projects", "section": "Overview"}),
                Hit("projects/apollo", 1.0, {"collection": "projects", "section": "Risks"})]
    facts = [Hit("f1", 2.0, {"source": "notes"}), Hit("f1", 1.0, {"source": "calls"}), Hit("f2", 0.5)]
    merged = fuse(articles, facts, {})
    assert [hit.attributes for hit in merged] == [
        {"collection": "projects", "section": "Overview", "kind": ARTICLE, "folded": []},
        {"source": "notes", "kind": FACT, "folded": []},
        {"kind": FACT, "folded": []}]
    assert [hit.score for hit in merged] == [1 / (FUSE_K + 1)] * 2 + [1 / (FUSE_K + 3)]  # f1 at its first place
    assert fuse([], [], {}) == [] and order(fuse([], hits(("f1", 1.0)), {"f1": ["projects/apollo"]})) == [
        (FACT, "f1")]


def test_a_rerank_function_reorders_only_the_top_of_a_merged_list_and_sees_both_kinds() -> None:
    count = RERANK_TOP // 2 + 10
    articles = hits(*((f"a{n}", 1.0) for n in range(count)))
    facts = hits(*((f"f{n}", 1.0) for n in range(count)))
    merged = fuse(articles, facts, {})
    seen: list[list[str]] = []

    def backwards(text: str, top: list[Hit]) -> list[Hit]:
        seen.append([hit.attributes["kind"] for hit in top])
        return top[::-1]

    again = reorder("launch", merged, backwards)
    assert len(seen) == 1 and len(seen[0]) == RERANK_TOP and set(seen[0]) == {ARTICLE, FACT}
    assert again[:RERANK_TOP] == merged[:RERANK_TOP][::-1] and again[RERANK_TOP:] == merged[RERANK_TOP:]


OTHER = """
import importlib.util, sys, types
from pathlib import Path
found = importlib.util.find_spec("cerebro")
package = types.ModuleType("cerebro")
package.__path__ = list(found.submodule_search_locations)
sys.modules["cerebro"] = package
from cerebro.article import Compilation
from cerebro.search import sync
from cerebro.store import SQLiteIndex, SQLiteStore
store = SQLiteStore(Path(sys.argv[1]))
store.write_compilation(Compilation("things/alder", "Overview", "new mango", by="w", at="2026-11-18T00:00:00Z"))
sync(SQLiteIndex(store), store)
"""


def test_a_sync_and_a_write_from_another_process_never_leave_older_text_indexed(tmp_path: Path) -> None:
    import subprocess
    import sys
    import time

    from cerebro.article import Compilation, Identity
    from cerebro.search import ARTICLES, sync
    from cerebro.store import SQLiteIndex, SQLiteStore

    path = tmp_path / "cerebro.sqlite"
    store = SQLiteStore(path)
    store.create(Identity("things/alder", "things", "Alder"))
    store.write_compilation(Compilation("things/alder", "Overview", "old pineapple", by="w", at="2026-11-18T00:00:00Z"))
    index, read, other = SQLiteIndex(store), store.article, []

    def slow(id: str) -> object:  # another process writes newer text while this sync holds the older
        got = read(id)
        other.append(subprocess.Popen([sys.executable, "-c", OTHER, str(path)]))
        time.sleep(0.15)  # long enough for it to load the store's modules and reach its write
        return got

    store.article = slow  # type: ignore[method-assign]
    sync(index, store)
    store.article = read  # type: ignore[method-assign]
    assert other[0].wait(timeout=30) == 0
    assert [hit.id for hit in index.search(ARTICLES, "mango", 5)] == ["things/alder#0"]
    store.close()
