"""Links between articles: an article's `linked` list holds the ids of the articles it is tied to, a query of a
collection with `linked: <id>` gives the articles whose list holds that id, and a read counts them by collection."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cerebro.article import Compilation, Identity, Property
from cerebro.caller import Caller
from cerebro.fakes import FakeExtraction, MemoryStore
from cerebro.lookup import OperationError, StoreLookup
from cerebro.mcp import render_read
from cerebro.reply import outside
from cerebro.store import SQLiteStore, Store

YOU = Caller("you", "person")
AT = "2026-11-01T09:00:00Z"


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    made: Store = MemoryStore() if request.param == "memory" else SQLiteStore(tmp_path / "cerebro.sqlite")
    with made:
        yield made


def make(store: Store, id: str, **values: object) -> None:
    store.create(Identity(id, id.rpartition("/")[0], id.rpartition("/")[2].title()))
    for name, value in values.items():
        store.set_field(id, name, Property(value, YOU, AT))


def pointing(store: Store, collection: str, id: str | list[str]) -> list[str]:
    return [a.identity.id for a in StoreLookup(store, FakeExtraction()).query(YOU, collection, {"linked": id}).items]


def set_status(store: Store, id: str, status: str) -> None:
    who = store.article(id).identity  # type: ignore[union-attr]
    who.status = status
    store.set_identity(who)


def merge(store: Store, gone: str, into: str) -> None:
    set_status(store, gone, "merged")
    store.append("merges", {"at": AT, "redirect": gone, "to": into, "path": gone})


def test_linked_keeps_the_articles_whose_list_holds_the_id_and_no_other_field_or_text_counts(store: Store) -> None:
    make(store, "labs/optics")
    make(store, "people/ada", linked=["labs/optics", "projects/never"])
    make(store, "people/bo", lab="labs/optics", linked="Labs/Optics")
    make(store, "people/cy", lab="labs/optics")
    make(store, "people/dee")
    store.write_compilation(Compilation("people/dee", "Overview", "Dee works at [Optics](../labs/optics.md).", by="x"))
    assert pointing(store, "people", "labs/optics") == ["people/ada", "people/bo"]
    assert pointing(store, "people", "projects/never") == ["people/ada"]  # a link to nothing is kept as written
    assert pointing(store, "people", "labs/nowhere") == []


def test_linked_on_facts_or_a_log_is_refused_with_what_to_ask_instead(store: Store) -> None:
    lookup = StoreLookup(store, FakeExtraction())
    for source in ("facts", "proposals"):
        with pytest.raises(OperationError, match=f"query: linked keeps the articles of a collection .* {source} is "
                                                 "no collection: .*articles: <id>"):
            lookup.query(YOU, source, {"linked": "labs/optics"})


def test_an_archived_article_is_found_and_counted_nowhere_and_comes_back_when_admitted_again(store: Store) -> None:
    make(store, "labs/optics")
    make(store, "people/ada", linked=["labs/optics"])
    set_status(store, "people/ada", "archived")
    lookup = StoreLookup(store, FakeExtraction())
    assert pointing(store, "people", "labs/optics") == []
    assert lookup.read(YOU, ["labs/optics"]).linked_from == {}
    set_status(store, "people/ada", "active")
    assert pointing(store, "people", "labs/optics") == ["people/ada"]
    assert store.article("people/ada").properties["linked"].value == ["labs/optics"]  # type: ignore[union-attr]


def test_a_link_to_a_merged_away_article_leads_to_the_one_it_was_merged_into_and_stays_as_written(
        store: Store) -> None:
    make(store, "labs/optics")
    make(store, "labs/optical")
    make(store, "people/ada", linked=["labs/optical"])
    make(store, "people/bo", linked=["labs/optics", "labs/optical"])
    merge(store, "labs/optical", "labs/optics")
    assert pointing(store, "people", "labs/optics") == ["people/ada", "people/bo"]
    assert pointing(store, "people", "Labs/Optics") == ["people/ada", "people/bo"]
    assert StoreLookup(store, FakeExtraction()).read(YOU, ["labs/optics"]).linked_from == {
        "labs/optics": [{"count": 2, "query": {"from": "people", "linked": "labs/optics"}}]}
    assert store.article("people/ada").properties["linked"].value == ["labs/optical"]  # type: ignore[union-attr]


def test_a_chain_of_merges_leads_to_the_last_one(store: Store) -> None:
    for id in ("labs/a", "labs/b", "labs/c"):
        make(store, id)
    make(store, "people/ada", linked=["labs/a"])
    merge(store, "labs/a", "labs/b")
    merge(store, "labs/b", "labs/c")
    assert pointing(store, "people", "labs/c") == ["people/ada"]


def test_a_read_counts_what_links_to_an_article_by_collection_with_the_query_that_lists_them(store: Store) -> None:
    make(store, "labs/optics", linked=["people/ada"])
    make(store, "people/ada", linked=["labs/optics"])
    make(store, "people/bo", linked=["labs/optics"])
    make(store, "notes/bench", linked=["labs/optics", "people/ada"])
    read = StoreLookup(store, FakeExtraction()).read(YOU, ["labs/optics", "people/ada"])
    optics, _ = outside(read)["articles"]
    assert optics["properties"] == {"linked": ["people/ada"]}  # what it links to, as written, and nothing more
    assert optics["linked_from"] == [{"count": 1, "query": {"from": "notes", "linked": "labs/optics"}},
                                     {"count": 2, "query": {"from": "people", "linked": "labs/optics"}}]
    text = render_read(read)
    assert ("linked from 1 article of notes: query from: notes, linked: labs/optics\n"
            "linked from 2 articles of people: query from: people, linked: labs/optics") in text
    assert "linked from 1 article of labs: query from: labs, linked: people/ada" in text


def test_a_read_counts_an_article_that_links_to_itself_or_to_one_merged_into_it_as_its_query_finds_it(
        store: Store) -> None:
    make(store, "labs/optics", linked=["labs/optics"])
    make(store, "labs/lens", linked=["labs/prism"])
    make(store, "labs/prism")
    merge(store, "labs/prism", "labs/lens")
    read = StoreLookup(store, FakeExtraction()).read(YOU, ["labs/optics", "labs/lens"])
    for id in ("labs/optics", "labs/lens"):
        assert pointing(store, "labs", id) == [id]
        assert read.linked_from[id] == [{"count": 1, "query": {"from": "labs", "linked": id}}]


def test_a_read_of_an_article_500_others_link_to_asks_the_store_as_often_in_a_workspace_ten_times_larger(
        tmp_path: Path) -> None:
    def statements(others: int) -> int:
        with SQLiteStore(tmp_path / f"{others}.sqlite") as store:
            make(store, "labs/optics")
            for n in range(500):
                make(store, f"notes/n{n:04d}", linked=["labs/optics"])
            for n in range(others):
                make(store, f"things/t{n:05d}", linked=["labs/other"])
            lookup = StoreLookup(store, FakeExtraction())
            said: list[str] = []
            store.db.set_trace_callback(said.append)
            read = lookup.read(YOU, ["labs/optics"])
            store.db.set_trace_callback(None)
            assert read.linked_from["labs/optics"][0]["count"] == 500
            return sum(1 for sql in said if sql.lstrip().lower().startswith("select"))

    assert statements(0) == statements(5000) < 20


def test_an_agent_file_finds_what_links_to_the_article_it_writes_and_to_the_ones_it_links(
        store: Store, tmp_path: Path) -> None:
    from cerebro.agent import load_agent
    from cerebro.evidence import gather

    make(store, "labs/optics")
    make(store, "people/ada", linked=["labs/optics"])
    make(store, "briefs/optics", linked=["labs/optics"])
    lookup = StoreLookup(store, FakeExtraction())
    for rel, value, this in (("labs/article.md", "this", "labs/optics"), ("briefs/article.md", "this.linked", "briefs/optics")):
        path = tmp_path / "agents" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nevidence:\n  - query: {{ from: people, linked: {value} }}\n---\n\nWrite it.\n")
        [entry] = gather(load_agent(path, tmp_path / "agents"), lookup, {}, this=this)
        assert [line.split("]")[0] for line in entry.lines] == ["- [^people/ada"]


def test_a_list_keeps_what_links_to_any_of_its_ids_a_merged_one_too_and_an_empty_list_keeps_nothing(
        store: Store) -> None:
    for id in ("labs/optics", "labs/optical", "labs/lonely"):
        make(store, id)
    make(store, "people/ada", linked=["labs/optics"])
    make(store, "people/bo", linked=["labs/optical"], lab="optics")
    merge(store, "labs/optical", "labs/optics")
    lookup = StoreLookup(store, FakeExtraction())
    assert pointing(store, "people", ["labs/optics", "labs/lonely"]) == ["people/ada", "people/bo"]
    assert pointing(store, "people", ["labs/optical"]) == ["people/ada", "people/bo"]
    assert pointing(store, "people", [f"labs/x{n}" for n in range(999)] + ["labs/optics"]) == ["people/ada", "people/bo"]
    with pytest.raises(OperationError, match="query: filter linked lists 1001 values, and a filter takes at most 1000: "
                                             "narrow the list, or query it in parts"):
        pointing(store, "people", [f"labs/x{n}" for n in range(1001)])
    assert pointing(store, "people", []) == []
    assert [a.identity.id for a in lookup.query(YOU, "people", {"lab": ["optics", "x"]}).items] == ["people/bo"]
    assert [a.identity.id for a in lookup.query(YOU, "labs", {"id": ["labs/optics", "labs/lonely"]}).items] == [
        "labs/lonely", "labs/optics"]
