"""The `Index` suite. Fixture: `index`, a new, empty index."""

from __future__ import annotations

import pytest

from cerebro.search import DurableIndex, Hit, Index


def ids(hits: list[Hit]) -> list[str]:
    return [hit.id for hit in hits]


def test_search_finds_the_matching_text_and_ranks_nothing_unrelated_above_it(index: Index) -> None:
    index.upsert("articles", [
        {"id": "a", "text": "the launch moved"},
        {"id": "b", "text": "launch review notes for the launch"},
        {"id": "c", "text": "nothing related here"},
    ])
    found = [hit.id for hit in index.search("articles", "launch review", top_k=10)]
    assert "b" in found
    assert "c" not in found[:found.index("b")]


def test_search_returns_at_most_top_k(index: Index) -> None:
    index.upsert("articles", [{"id": str(n), "text": f"launch {n}"} for n in range(5)])
    assert len([h.id for h in index.search("articles", "launch", top_k=2)]) == 2


def test_namespaces_are_kept_apart(index: Index) -> None:
    index.upsert("articles", [{"id": "a", "text": "launch"}])
    index.upsert("notes", [{"id": "n", "text": "launch"}])
    assert ids(index.search("articles", "launch", top_k=5)) == ["a"]
    assert ids(index.search("notes", "launch", top_k=5)) == ["n"]


def test_filters_keep_only_rows_whose_attributes_match(index: Index) -> None:
    index.upsert("articles", [{"id": "a", "text": "launch", "collection": "projects"},
                              {"id": "b", "text": "launch", "collection": "teams"}])
    assert ids(index.search("articles", "launch", top_k=5, filters={"collection": "teams"})) == ["b"]


def test_upsert_replaces_and_delete_removes(index: Index) -> None:
    index.upsert("articles", [{"id": "a", "text": "launch"}])
    index.upsert("articles", [{"id": "a", "text": "budget"}])
    assert ids(index.search("articles", "launch", top_k=5)) == []
    assert ids(index.search("articles", "budget", top_k=5)) == ["a"]
    index.delete("articles", ["a"])
    assert ids(index.search("articles", "budget", top_k=5)) == []


def test_one_id_in_two_namespaces_is_two_rows(index: Index) -> None:
    index.upsert("articles", [{"id": "a", "text": "launch"}])
    index.upsert("notes", [{"id": "a", "text": "budget"}])
    assert ids(index.search("articles", "launch", top_k=5)) == ["a"]
    index.delete("notes", ["a"])
    assert ids(index.search("articles", "launch", top_k=5)) == ["a"]
    assert ids(index.search("notes", "budget", top_k=5)) == []


def test_a_hit_carries_its_score_and_attributes_and_a_list_attribute_matches_any_item(index: Index) -> None:
    index.upsert("articles", [{"id": "a", "text": "launch", "tags": ["q4", "launch"], "collection": "projects"},
                              {"id": "b", "text": "launch", "tags": ["q1"], "collection": "projects"}])
    hits = index.search("articles", "launch", top_k=5, filters={"tags": "q4"})
    assert ids(hits) == ["a"]
    assert hits[0].score > 0 and hits[0].attributes == {"tags": ["q4", "launch"], "collection": "projects"}


# -- a durable index: `ids`, `synced` and `mark`, for an index that outlives the process

def _durable(index: Index) -> DurableIndex:
    if not isinstance(index, DurableIndex):
        pytest.skip(f"{type(index).__name__} is not a durable index, and cerebro ships none yet: this contract is "
                    "for the first one a workspace builds")
    return index


def test_a_durable_index_gives_the_ids_whose_attributes_match_whatever_their_text(index: Index) -> None:
    durable = _durable(index)
    durable.upsert("articles", [{"id": "a", "text": "launch", "collection": "projects"},
                                {"id": "b", "text": "nothing alike", "collection": "projects"},
                                {"id": "c", "text": "launch", "collection": "teams"}])
    durable.upsert("notes", [{"id": "n", "text": "launch", "collection": "projects"}])
    assert sorted(durable.ids("articles", {"collection": "projects"})) == ["a", "b"]
    durable.delete("articles", ["b"])
    assert durable.ids("articles", {"collection": "projects"}) == ["a"]
    assert durable.ids("articles", {"collection": "none"}) == []


def test_a_durable_index_keeps_the_cursor_it_reached_by_namespace_and_never_moves_it_back(index: Index) -> None:
    durable = _durable(index)
    assert durable.synced("articles") == 0
    durable.mark("articles", 7)
    durable.mark("articles", 3)
    durable.mark("notes", 2)
    assert durable.synced("articles") == 7 and durable.synced("notes") == 2
    durable.mark("articles", 9)
    assert durable.synced("articles") == 9
