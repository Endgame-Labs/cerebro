"""The `Store` suite. Fixtures: `store`, a new, empty store, and `reopen`, which opens it again as a workspace's
settings name it."""

from __future__ import annotations

import datetime

import pytest

from cerebro.article import Citation, Compilation, Identity, Property
from cerebro.caller import Caller
from cerebro.connector import Record
from cerebro.store import MENTIONS, RECORD, SUBJECT, PropertyError, Store, StoreError, Where

YOU = Caller("you", "person")
SYNC = Caller("crm", "source")
AGENT = Caller("projects/article", "agent")
T1, T2 = "2026-11-01T09:00:00Z", "2026-11-02T09:00:00Z"
T1_UTC = "2026-11-01T09:00:00.000000Z"  # T1 as the store keeps a time


def make(store: Store, path: str, title: str = "", **properties: object) -> None:
    store.create(Identity(path, path.rpartition("/")[0], title or path, admitted_by=YOU, admitted_at=T1))
    for name, value in properties.items():
        store.set_field(path, name, Property(value, YOU, T1))


def ids(store: Store, collection: str, *where: Where, **options: object) -> list[str]:
    return [a.identity.id for a in store.query(collection, list(where), **options)]  # type: ignore[arg-type]


def test_an_article_reads_back_as_its_identity_compilations_and_fields(store: Store) -> None:
    who = Identity("projects/apollo", "projects", "Apollo", {"email": ["apollo@example.com"]}, "active", YOU, T1)
    store.create(who)
    store.write_compilation(Compilation("projects/apollo", "Summary", "It moved [^f_1].", by="projects/article",
                                digest="sha256:1", at=T2, citations=[Citation("fact", "f_1", "memo-1", "It moved.")]))
    store.write_compilation(Compilation("projects/apollo", "Next", "Watch March.", by="projects/article"))
    store.set_field("projects/apollo", "stage", Property("open", AGENT, T1, ["f_1"]))
    article = store.article("projects/apollo")
    assert article.identity == who
    assert [(s.heading, s.text, s.digest) for s in article.compilations] == [
        ("Summary", "It moved [^f_1].", "sha256:1"), ("Next", "Watch March.", "")]
    assert article.compilations[0].citations == [Citation("fact", "f_1", "memo-1", "It moved.")]
    assert article.properties["stage"] == Property("open", AGENT, T1_UTC, ["f_1"])
    assert store.article("projects/none") is None


def test_creating_an_article_twice_or_writing_to_one_that_is_not_there_is_refused(store: Store) -> None:
    make(store, "projects/apollo")
    with pytest.raises(StoreError):
        make(store, "projects/apollo")
    with pytest.raises(StoreError):
        store.write_compilation(Compilation("projects/none", "Summary", "x", by="a"))
    with pytest.raises(StoreError):
        store.set_field("projects/none", "stage", Property("open", YOU, T1))


def test_a_compilation_written_again_keeps_its_place_and_others_stand(store: Store) -> None:
    make(store, "projects/apollo")
    for heading, text in (("Summary", "one"), ("Next", "two"), ("Summary", "three")):
        store.write_compilation(Compilation("projects/apollo", heading, text, by="a"))
    assert [(s.heading, s.text) for s in store.article("projects/apollo").compilations] == [
        ("Summary", "three"), ("Next", "two")]
    store.delete_compilation("projects/apollo", "Summary")
    store.delete_compilation("projects/apollo", "Nothing")
    assert [s.heading for s in store.article("projects/apollo").compilations] == ["Next"]


def test_a_write_of_one_field_leaves_the_others_and_the_last_write_of_it_stands(store: Store) -> None:
    make(store, "projects/apollo", stage="open")
    store.set_field("projects/apollo", "owner", Property("Vega", YOU, T1))
    assert {k: c.value for k, c in store.article("projects/apollo").properties.items()} == {
        "stage": "open", "owner": "Vega"}
    store.set_field("projects/apollo", "stage", Property("closed", AGENT, T2))
    store.set_field("projects/apollo", "stage", Property("paused", YOU, T1))  # no time orders writes: the last stands
    assert store.article("projects/apollo").properties["stage"] == Property("paused", YOU, T1_UTC)
    assert ids(store, "projects", Where("stage", "=", "paused")) == ["projects/apollo"]
    assert ids(store, "projects", Where("stage", "=", "closed")) == []


def test_a_query_compares_one_field_within_one_collection(store: Store) -> None:
    make(store, "projects/apollo", region="West")
    make(store, "projects/vega", owner="West")
    make(store, "teams/apollo", region="West")
    assert ids(store, "projects", Where("region", "=", "west")) == ["projects/apollo"]
    assert ids(store, "teams", Where("region", "=", "WEST")) == ["teams/apollo"]
    assert ids(store, "projects", Where("region", "=", "Wes")) == []


def test_a_query_matches_one_item_of_a_list_and_several_predicates_all(store: Store) -> None:
    make(store, "projects/apollo", tags=["Launch", "q4"], stage="open", active=True)
    make(store, "projects/vega", tags=["q4"], stage="closed", active=False)
    assert ids(store, "projects", Where("tags", "=", "launch")) == ["projects/apollo"]
    assert ids(store, "projects", Where("tags", "=", "q4"), Where("stage", "=", "closed")) == ["projects/vega"]
    assert ids(store, "projects", Where("active", "=", True)) == ["projects/apollo"]
    assert ids(store, "projects", Where("stage", "in", ["closed", "paused"])) == ["projects/vega"]
    assert ids(store, "projects", Where("stage", "in", "closed")) == ["projects/vega"]
    make(store, "projects/wren", sizes=["large", 3])
    assert ids(store, "projects", Where("sizes", ">", 2)) == ["projects/wren"]


def test_a_query_compares_numbers_as_numbers_and_dates_as_text(store: Store) -> None:
    make(store, "projects/a", size=1, due="2026-11-30")
    make(store, "projects/b", size=2.5, due="2026-12-15")
    make(store, "projects/c", size=10, due="2027-01-05")
    make(store, "projects/d", size="10", due=None)
    assert ids(store, "projects", Where("size", "=", 1.0)) == ["projects/a"]
    assert ids(store, "projects", Where("size", ">", 2), Where("size", "<=", 10)) == ["projects/b", "projects/c"]
    assert ids(store, "projects", Where("due", ">=", "2026-12-01"), Where("due", "<", "2027-01-01")) == [
        "projects/b"]
    assert ids(store, "projects", Where("size", "<", 5)) == ["projects/a", "projects/b"]
    assert ids(store, "projects", Where("size", "<", 2.5)) == ["projects/a"]
    assert ids(store, "projects", Where("size", ">", 2.5)) == ["projects/c"]
    assert ids(store, "projects", Where("size", ">=", 10)) == ["projects/c"]
    with pytest.raises(StoreError):
        store.query("projects", [Where("size", "~", 1)])


def test_a_query_orders_by_a_field_with_no_value_last_and_pages(store: Store) -> None:
    make(store, "projects/a", due="2026-12-15")
    make(store, "projects/b")
    make(store, "projects/c", due="2026-11-30")
    make(store, "projects/d", due="2027-01-05")
    assert ids(store, "projects", order_by="due") == ["projects/c", "projects/a", "projects/d", "projects/b"]
    assert ids(store, "projects", order_by="due", descending=True) == [
        "projects/d", "projects/a", "projects/c", "projects/b"]
    assert ids(store, "projects") == ["projects/a", "projects/b", "projects/c", "projects/d"]
    assert ids(store, "projects", limit=2, offset=1) == ["projects/b", "projects/c"]
    assert ids(store, "projects", offset=3) == ["projects/d"]
    assert ids(store, "nothing") == []


def test_a_nested_collection_is_its_own(store: Store) -> None:
    make(store, "notes/projects/apollo", stage="open")
    make(store, "notes/apollo", stage="open")
    assert ids(store, "notes/projects", Where("stage", "=", "open")) == ["notes/projects/apollo"]


def test_a_property_that_is_not_json_data_or_is_named_id_is_refused(store: Store) -> None:
    make(store, "projects/a")
    value = {"lead": "Vega", "size": 3, "steps": [1, "two", None, 2.5]}
    store.set_field("projects/a", "team", Property(value, YOU, T1))
    assert store.article("projects/a").properties["team"].value == value
    for name, bad in (("due", datetime.date(2026, 12, 1)), ("steps", (1, 2)), ("id", "other"),
                      ("steps", [1, (2, 3)]), ("team", {1: "Vega"}), ("team", {"lead": (1,)})):
        with pytest.raises(PropertyError):
            store.set_field("projects/a", name, Property(bad, YOU, T1))
    assert set(store.article("projects/a").properties) == {"team"}


def test_keys_and_titles_find_their_articles(store: Store) -> None:
    store.create(Identity("projects/apollo", "projects", "Apollo", {"email": ["Team@Apollo.example"]}))
    store.create(Identity("teams/apollo", "teams", "Apollo"))
    assert store.bound("email", "team@apollo.example") == ["projects/apollo"]
    assert store.bound("domain", "apollo.example") == []
    assert store.named("apollo") == ["projects/apollo", "teams/apollo"]


def test_the_articles_whose_field_holds_an_id_are_found_by_it_in_any_case(store: Store) -> None:
    make(store, "people/ada", lab="Labs/Optics", linked=["projects/atlas", "projects/birch"])
    make(store, "notes/bench", lab="labs/optics", linked="labs/optics")
    make(store, "labs/optics")
    assert [who.id for who in store.linking_to("linked", ["labs/optics", "projects/birch"])] == [
        "notes/bench", "people/ada"]
    assert [who.id for who in store.linking_to("lab", ["labs/optics"])] == ["notes/bench", "people/ada"]
    assert store.linking_to("linked", ["labs/none"]) == [] and store.linking_to("linked", []) == []


def test_a_first_party_record_is_kept_whole_and_by_copy(store: Store) -> None:
    told = Record(id="r_1", source="note", revision="1", at=T1, keys={"email": ["a@example.com"]},
                  fields={"collection": "notes"}, body="The launch moved to March.", by=YOU)
    store.keep(told)
    told.fields["collection"] = "changed"
    got = store.record("r_1")
    assert got == Record(id="r_1", source="note", revision="1", at=T1, keys={"email": ["a@example.com"]},
                         fields={"collection": "notes"}, body="The launch moved to March.", by=YOU)
    got.body = "changed again"
    assert store.record("r_1").body == "The launch moved to March."
    store.keep(Record(id="r_2", source="note", revision="1", body="two"))
    store.keep(Record(id="r_3", source="other", revision="1", body="three"))
    assert [r.id for r in store.records("note")] == ["r_1", "r_2"] and store.record("r_9") is None
    store.keep(Record(id="r_1", source="note", revision="2", body="kept again"))
    assert [(r.id, r.revision) for r in store.records("note")] == [("r_2", "1"), ("r_1", "2")]


def test_a_record_kept_with_unique_fields_takes_the_least_suffix_that_frees_them_all_and_a_revision_its_own(
        store: Store) -> None:
    def kept(id: str, name: str, slug: str, source: str = "note") -> Record:
        record = Record(id=id, source=source, revision="1", fields={"name": f"d-{name}", "slug": slug}, body=id)
        assert store.keep(record, unique=("name", "slug")) is record
        return record

    assert kept("r_1", "plan", "plan").fields == {"name": "d-plan", "slug": "plan"}
    assert kept("r_2", "plan", "plan").fields == {"name": "d-plan-2", "slug": "plan-2"}
    assert kept("r_3", "other", "plan").fields == {"name": "d-other-3", "slug": "plan-3"}  # one taken: both move
    assert kept("r_4", "plan", "plan", source="task").fields == {"name": "d-plan", "slug": "plan"}  # another kind
    assert kept("r_1", "plan", "plan").fields == {"name": "d-plan", "slug": "plan"}  # its own name is no clash
    assert store.record("r_2").fields == {"name": "d-plan-2", "slug": "plan-2"}
    plain = Record(id="r_5", source="note", revision="1", fields={"name": "d-plan"}, body="x")
    store.keep(plain)
    assert store.record("r_5").fields == {"name": "d-plan"}  # nothing unique asked, nothing changed


def test_logs_append_and_read_back_with_filters_and_from_a_point(store: Store) -> None:
    first = store.append("proposals", {"kind": "admit", "target": "projects/apollo"})
    store.append("agent-runs", {"agent": "projects/article"})
    second = store.append("proposals", {"kind": "archive", "target": "projects/vega"})
    assert second.seq > first.seq
    assert [r.data["kind"] for r in store.rows("proposals")] == ["admit", "archive"]
    assert [r.data["kind"] for r in store.rows("proposals", {"kind": "ARCHIVE"})] == ["archive"]
    assert [r.data["kind"] for r in store.rows("proposals", since=first.seq)] == ["archive"]
    assert store.rows("proposals", limit=1)[0].seq == first.seq


def test_a_log_row_is_written_through_the_stores_scrub(store: Store) -> None:
    store.scrub = lambda text: text.replace("sk-123456", "[secret]")
    row = store.append("mcp-requests", {"args": {"text": "key sk-123456"}, "lines": ["sk-123456"], "n": 1})
    assert row.data == store.rows("mcp-requests")[0].data == {"args": {"text": "key [secret]"}, "lines": ["[secret]"],
                                                              "n": 1}


def test_state_keeps_a_value_by_key(store: Store) -> None:
    assert store.state("cursor:notes") is None
    store.set_state("cursor:notes", "7")
    store.set_state("cursor:notes", {"page": 2})
    assert store.state("cursor:notes") == {"page": 2}


def test_a_fact_links_to_many_articles_once_each(store: Store) -> None:
    make(store, "projects/apollo")
    make(store, "teams/apollo")
    store.link("f_1", ["projects/apollo", "teams/apollo"])
    store.link("f_1", ["projects/apollo"])
    store.link("f_2", ["projects/apollo"])
    assert store.facts_of("projects/apollo") == ["f_1", "f_2"]
    assert store.facts_of("teams/apollo") == ["f_1"]


def test_what_changed_after_a_cursor_names_each_kind_and_id_once(store: Store) -> None:
    make(store, "projects/old")
    mark = store.cursor()
    assert store.changed_since(mark) == ([], mark)
    make(store, "projects/a")
    store.set_field("projects/a", "stage", Property("open", YOU, T1))
    store.set_field("projects/a", "stage", Property("closed", YOU, T2))
    store.keep(Record(id="projects/a", source="note", revision="1", body="x"))
    store.append("proposals", {"kind": "admit"})
    store.set_state("cursor:notes", "3")
    changed, later = store.changed_since(mark)
    assert changed == [("article", "projects/a"), ("record", "projects/a"), ("log", "proposals"),
                       ("state", "cursor:notes")]
    assert later == store.cursor() > mark
    assert store.changed_since(later) == ([], later)
    store.link("f_1", ["projects/old"])
    assert store.changed_since(later)[0] == [("article", "projects/old")]


def test_a_fields_time_is_kept_in_utc_and_a_time_with_no_offset_is_refused(store: Store) -> None:
    make(store, "projects/apollo")
    store.set_field("projects/apollo", "stage", Property("set", YOU, "2026-11-01T12:00:00+01:00"))
    assert store.article("projects/apollo").properties["stage"].at == "2026-11-01T11:00:00.000000Z"
    for bad in ("2026-11-01T09:00:00", "yesterday", ""):
        with pytest.raises(StoreError):
            store.set_field("projects/apollo", "stage", Property("x", YOU, bad))


def test_an_identity_can_change_after_birth_and_a_link_can_be_taken_back(store: Store) -> None:
    store.create(Identity("projects/apollo", "projects", "Apollo", {"email": ["old@apollo.example"]}))
    store.set_identity(Identity("projects/apollo", "projects", "Apollo Launch", {"domain": ["apollo.example"]},
                                "archived", YOU, T2))
    assert store.article("projects/apollo").identity.status == "archived"
    assert store.bound("email", "old@apollo.example") == [] and store.named("apollo") == []
    assert store.bound("domain", "apollo.example") == store.named("apollo launch") == ["projects/apollo"]
    with pytest.raises(StoreError):
        store.set_identity(Identity("projects/apollo", "teams"))
    store.link("f_1", ["projects/apollo"])
    store.unlink("f_1", ["projects/apollo", "projects/none"])
    assert store.facts_of("projects/apollo") == []


def test_one_property_name_holds_a_date_in_one_collection_and_text_or_a_number_in_another(store: Store) -> None:
    make(store, "projects/a", due="2026-12-01")
    make(store, "projects/b", due="2027-02-01")
    make(store, "notes/a", due=5)
    make(store, "notes/b", due="soon")
    assert ids(store, "projects", Where("due", ">=", "2026-11-01"), Where("due", "<", "2027-01-01")) == [
        "projects/a"]
    assert ids(store, "notes", Where("due", ">", 3)) == ["notes/a"]
    assert ids(store, "notes", Where("due", "=", "SOON")) == ["notes/b"]
    assert ids(store, "notes", Where("due", "in", [5, "soon"])) == ["notes/a", "notes/b"]
    assert ids(store, "projects", Where("due", "=", 5)) == []
    assert ids(store, "notes", order_by="due", descending=True) == ["notes/b", "notes/a"]


def test_a_list_orders_by_its_least_item_numbers_before_text_and_ties_by_id(store: Store) -> None:
    make(store, "projects/a", rank=[5, "x"])
    make(store, "projects/b", rank="abc")
    make(store, "projects/c", rank=3)
    make(store, "projects/d", rank=[7, 1])
    make(store, "projects/e", rank=3)
    make(store, "projects/f", rank={"not": "comparable"})
    assert ids(store, "projects", order_by="rank") == [
        "projects/d", "projects/c", "projects/e", "projects/a", "projects/b", "projects/f"]
    assert ids(store, "projects", order_by="rank", descending=True) == [
        "projects/b", "projects/a", "projects/c", "projects/e", "projects/d", "projects/f"]
    assert ids(store, "projects", order_by="rank", limit=2, offset=2) == ["projects/e", "projects/a"]
    assert ids(store, "projects", Where("rank", ">", 4), order_by="rank") == ["projects/d", "projects/a"]


def test_a_value_that_does_not_compare_matches_nothing(store: Store) -> None:
    make(store, "projects/a", team={"lead": "Vega"}, stage=None, size=2)
    for where in (Where("team", "=", {"lead": "Vega"}), Where("stage", "=", None), Where("size", "in", []),
                  Where("size", "=", [2]),
                  Where("size", "<", None), Where("missing", "=", 2)):
        assert ids(store, "projects", where) == [], where


def test_a_log_reads_from_a_point_with_a_filter_and_a_limit_past_other_logs(store: Store) -> None:
    for n in range(6):
        store.append("agent-runs", {"n": n, "agent": "a" if n % 2 else "b"})
        store.append("mcp-requests", {"n": n})
    runs = store.rows("agent-runs")
    assert [r.data["n"] for r in runs] == [0, 1, 2, 3, 4, 5] and all(r.log == "agent-runs" for r in runs)
    assert [r.data["n"] for r in store.rows("agent-runs", {"agent": "a"}, since=runs[1].seq, limit=1)] == [3]
    assert [r.data["n"] for r in store.rows("agent-runs", since=runs[-1].seq)] == []
    assert store.rows("agent-runs", limit=0) == [] and store.rows("nothing") == []
    assert [r.data["n"] for r in store.rows("mcp-requests", {"n": 4})] == [4]


def test_state_keeps_any_json_value_and_refuses_one_that_is_not(store: Store) -> None:
    for value in ("text", 3, 2.5, True, ["a", 1], {"page": [1, {"x": None}]}, None):
        store.set_state("k", value)
        assert store.state("k") == value
    value = {"page": [1]}
    store.set_state("k", value)
    value["page"].append(2)
    assert store.state("k") == {"page": [1]}
    with pytest.raises(PropertyError):
        store.set_state("k", (1, 2))
    assert store.state("k") == {"page": [1]}


def test_a_link_taken_back_and_made_again_goes_last_and_only_a_real_change_is_recorded(store: Store) -> None:
    make(store, "projects/apollo")
    store.link("f_1", ["projects/apollo"])
    store.link("f_2", ["projects/apollo"])
    mark = store.cursor()
    store.unlink("f_9", ["projects/apollo"])
    store.link("f_2", ["projects/apollo"])
    assert store.changed_since(mark) == ([], mark)
    store.unlink("f_1", ["projects/apollo"])
    store.link("f_1", ["projects/apollo"])
    assert store.facts_of("projects/apollo") == ["f_2", "f_1"] and store.facts_of("projects/none") == []
    assert store.changed_since(mark)[0] == [("article", "projects/apollo")]


def test_an_identity_change_is_a_change_and_one_to_an_article_that_is_not_there_is_refused(store: Store) -> None:
    make(store, "projects/apollo")
    mark = store.cursor()
    store.set_identity(Identity("projects/apollo", "projects", "Apollo", {"email": ["A@x.example"]}))
    assert store.changed_since(mark)[0] == [("article", "projects/apollo")]
    assert store.bound("email", "a@X.example") == ["projects/apollo"]
    with pytest.raises(StoreError):
        store.set_identity(Identity("projects/none", "projects"))
    assert store.article("projects/none") is None


def test_changes_page_by_cursor_each_page_holding_only_what_came_after_the_last(store: Store) -> None:
    make(store, "projects/a")
    make(store, "projects/b")
    start = store.cursor()
    store.write_compilation(Compilation("projects/a", "Summary", "one", by="a"))
    store.keep(Record(id="r_1", source="note", revision="1", body="x"))
    store.write_compilation(Compilation("projects/a", "Next", "two", by="a"))
    first, mark = store.changed_since(start)
    assert first == [("record", "r_1"), ("article", "projects/a")]
    store.set_field("projects/b", "stage", Property("open", YOU, T1))
    store.delete_compilation("projects/a", "Next")
    store.delete_compilation("projects/a", "Nothing")
    second, later = store.changed_since(mark)
    assert second == [("article", "projects/b"), ("article", "projects/a")] and later > mark
    assert store.changed_since(start)[0] == [("record", "r_1"), ("article", "projects/b"), ("article", "projects/a")]
    assert store.changed_since(later) == ([], later)


def test_links_keep_when_a_fact_was_learned_and_keys_and_collections_read_across(store: Store) -> None:
    store.create(Identity("projects/apollo", "projects", "Apollo", {"email": ["team@apollo.example"],
                                                                     "domain": ["apollo.example"]}))
    store.create(Identity("teams/apollo", "teams", "Apollo", {"slack": ["APOLLO.example"]}))
    store.create(Identity("notes/deep/one", "notes/deep", "One"))
    assert store.keyed("apollo.example") == ["projects/apollo", "teams/apollo"]
    assert store.collections() == ["notes/deep", "projects", "teams"]
    store.link("f_1", ["projects/apollo", "teams/apollo"], at="2026-11-01T10:00:00+01:00")
    store.link("f_2", ["projects/apollo"], at="2026-11-01T09:30:00Z")
    store.link("f_3", ["projects/apollo"])
    assert store.articles_of("f_1") == ["projects/apollo", "teams/apollo"] and store.articles_of("f_9") == []
    assert store.facts_of("projects/apollo") == ["f_1", "f_2", "f_3"]
    assert store.facts_of("projects/apollo", since="2026-11-01T09:00:00Z") == ["f_1", "f_2"]
    assert store.facts_of("projects/apollo", since="2026-11-01T09:15:00Z") == ["f_2"]
    store.write_compilation(Compilation("notes/deep/one", "Body", "x", by="a", at=T2, started=T1))
    assert store.article("notes/deep/one").compilations[0].started == T1


def test_a_link_says_how_its_fact_is_tied_and_reads_each_fact_and_article_once(store: Store) -> None:
    store.link("f_1", ["people/ada", "projects/apollo"], at="2026-11-01T09:00:00Z", how=MENTIONS)
    store.link("f_1", ["projects/apollo"], at="2026-11-05T09:00:00Z", how=RECORD)
    store.link("f_1", ["projects/apollo"], how=SUBJECT)
    store.link("f_2", ["projects/apollo"], at="2026-11-03T09:00:00Z")
    assert store.ties_of("f_1") == {"people/ada": [MENTIONS], "projects/apollo": [SUBJECT, MENTIONS, RECORD]}
    assert store.ties_of("f_2") == {"projects/apollo": [SUBJECT]} and store.ties_of("f_9") == {}
    assert store.articles_of("f_1") == ["people/ada", "projects/apollo"]
    assert store.facts_of("projects/apollo") == ["f_1", "f_2"]
    assert store.facts_of("projects/apollo", since="2026-11-02T00:00:00Z") == ["f_2"]  # f_1 keeps its first time
    store.unlink("f_1", ["projects/apollo"], MENTIONS)
    assert store.ties_of("f_1")["projects/apollo"] == [SUBJECT, RECORD]
    store.unlink("f_1", ["projects/apollo"])
    assert store.ties_of("f_1") == {"people/ada": [MENTIONS]} and store.facts_of("projects/apollo") == ["f_2"]
    with pytest.raises(StoreError, match="one of subject, mentions, record, and got 'filed'"):
        store.link("f_1", ["projects/apollo"], how="filed")


def test_the_latest_row_per_id_reads_a_log_of_states(store: Store) -> None:
    store.append("proposals", {"id": "p_1", "kind": "admit", "status": "waiting"})
    store.append("proposals", {"id": "p_2", "kind": "archive", "status": "waiting"})
    store.append("proposals", {"note": "no id"})
    store.append("proposals", {"id": "p_1", "kind": "admit", "status": "accepted"})
    store.append("merges", {"id": "p_2", "status": "other log"})
    assert [r.data["status"] for r in store.latest("proposals")] == ["waiting", "accepted"]
    assert [r.data["id"] for r in store.latest("proposals", where={"status": "waiting"})] == ["p_2"]
    assert len(store.rows("proposals")) == 4


def test_a_total_sums_one_field_over_the_rows_that_match(store: Store) -> None:
    store.append("spend", {"day": "2026-11-17", "layer": "Compilation", "usd": 0.25})
    store.append("spend", {"day": "2026-11-17", "layer": "extraction", "usd": 0.5})
    store.append("spend", {"day": "2026-11-18", "layer": "compilation", "usd": 2})
    store.append("spend", {"day": "2026-11-17", "layer": "compilation", "usd": "lots"})
    assert store.total("spend", "usd", {"day": "2026-11-17"}) == 0.75
    assert store.total("spend", "usd", {"day": "2026-11-17", "layer": "compilation"}) == 0.25
    assert store.total("spend", "usd") == 2.75 and store.total("merges", "usd") == 0


@pytest.mark.synced
def test_a_store_opened_again_from_settings_keeps_what_was_written(store: Store, reopen) -> None:
    make(store, "projects/apollo", stage="open")
    store.append("proposals", {"id": "p_1"})
    again = reopen()
    try:
        assert again.article("projects/apollo").properties["stage"].value == "open"
        assert [r.data["id"] for r in again.rows("proposals")] == ["p_1"]
    finally:
        if again is not store:
            again.close()


def test_a_nested_block_that_raises_is_undone_even_when_the_outer_block_catches_it(store: Store) -> None:
    with store.exclusive():
        store.set_state("kept", 1)
        try:
            with store.exclusive():
                store.set_state("dropped", 1)
                store.set_state("kept", 2)
                raise ValueError("inner")
        except ValueError:
            pass
    assert store.state("kept") == 1 and store.state("dropped") is None


def test_an_exclusive_block_runs_as_one_write_and_leaves_nothing_when_it_raises(store: Store) -> None:
    import threading
    import time

    store.set_state("n", 0)

    def bump() -> None:
        with store.exclusive():  # a read, then a write: no other block lands between them
            seen = store.state("n")
            time.sleep(0.01)
            store.set_state("n", seen + 1)

    threads = [threading.Thread(target=bump) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.state("n") == 3
    with pytest.raises(ValueError, match="undone"), store.exclusive():
        store.append("proposals", {"id": "p_1", "status": "accepted"})
        with store.exclusive():
            store.set_state("n", 9)
        raise ValueError("undone")
    assert store.state("n") == 3 and store.rows("proposals") == []


def test_a_write_from_another_thread_waits_until_an_exclusive_block_ends(store: Store) -> None:
    import threading

    started, landed = threading.Event(), threading.Event()

    def write() -> None:
        started.set()
        store.set_state("other", 1)
        landed.set()

    other = threading.Thread(target=write)
    try:
        with store.exclusive():
            store.set_state("mine", 1)
            other.start()
            assert started.wait(5)
            assert not landed.wait(0.2) and store.state("other") is None
    finally:
        other.join(5)
    assert landed.is_set() and (store.state("mine"), store.state("other")) == (1, 1)


def test_rows_and_fields_written_from_threads_side_by_side_each_land(store: Store) -> None:
    import threading

    make(store, "projects/apollo")
    start = threading.Barrier(4)
    rows = []

    def write(n: int) -> None:
        start.wait(5)
        rows.append(store.append("spend", {"n": n}))
        store.set_field("projects/apollo", f"f{n}", Property(n, YOU, T1))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    seqs = sorted(r.seq for r in rows)
    assert len(set(seqs)) == 4
    assert {k: p.value for k, p in store.article("projects/apollo").properties.items()} == {f"f{n}": n for n in range(4)}
    for n, seq in enumerate(seqs):
        assert sorted(r.seq for r in store.rows("spend", since=seq)) == seqs[n + 1:]


def fact(id: str, text: str) -> dict[str, object]:
    return {"id": id, "text": text, "record": "local/discounts.md"}


def test_the_facts_of_an_earlier_save_are_kept_as_superseded_the_latest_save_first(store: Store) -> None:
    assert store.supersede("local", "local/discounts.md", "r1", T1, [fact("f_1", "The limit is 10%.")], 10) == 0
    store.supersede("local", "local/discounts.md", "r2", T2, [fact("f_2", "The limit is 15%.")], 10)
    store.supersede("local", "local/other.md", "r1", T2, [fact("f_9", "Another.")], 10)
    rows = store.superseded(source="local", record="local/discounts.md")
    assert [(r["fact"]["id"], r["revision"], r["at"], r["status"]) for r in rows] == [
        ("f_2", "r2", T2, "superseded"), ("f_1", "r1", T1, "superseded")]
    assert rows[1]["fact"] == fact("f_1", "The limit is 10%.") and rows[1]["source"] == "local"
    assert [r["fact"]["id"] for r in store.superseded(ids=["f_9", "f_x"])] == ["f_9"]
    assert len(store.superseded()) == 3 and store.superseded(ids=[]) == []


def test_a_fact_kept_already_keeps_its_first_row(store: Store) -> None:
    store.supersede("local", "a.md", "r1", T1, [fact("f_1", "First.")], 10)
    store.supersede("local", "a.md", "r2", T2, [fact("f_1", "Again.")], 10)
    assert [(r["revision"], r["fact"]["text"]) for r in store.superseded()] == [("r1", "First.")]


def test_past_the_bound_the_oldest_saves_of_a_record_are_dropped_and_no_other_records(store: Store) -> None:
    store.supersede("local", "b.md", "r1", T1, [fact("f_b", "Other record.")], 1)
    for n in range(1, 6):
        dropped = store.supersede("local", "a.md", f"r{n}", T2, [fact(f"f_{n}a", "x"), fact(f"f_{n}b", "y")], 2)
        assert dropped == (2 if n > 2 else 0)
    assert [r["revision"] for r in store.superseded(record="a.md")] == ["r5", "r5", "r4", "r4"]
    assert [r["fact"]["id"] for r in store.superseded(record="b.md")] == ["f_b"]
    assert store.supersede("local", "c.md", "r1", T1, [fact("f_c", "None kept.")], 0) == 1
    assert store.superseded(record="c.md") == []


def test_a_superseded_fact_retracted_stays_readable_marked_retracted(store: Store) -> None:
    store.supersede("local", "a.md", "r1", T1, [fact("f_1", "One."), fact("f_2", "Two.")], 10)
    assert store.retract_superseded(["f_1", "f_none"]) == 1 and store.retract_superseded(["f_1"]) == 0
    assert {r["fact"]["id"]: r["status"] for r in store.superseded()} == {"f_1": "retracted", "f_2": "superseded"}


def test_a_superseded_fact_is_written_through_the_stores_scrub(store: Store) -> None:
    store.scrub = lambda text: text.replace("sk-123456", "[secret]")
    store.supersede("local", "a.md", "r1", T1, [fact("f_1", "The key is sk-123456.")], 10)
    assert store.superseded()[0]["fact"]["text"] == "The key is [secret]."


def test_a_dropped_field_leaves_the_article_and_its_index_and_dropping_one_not_held_changes_nothing(
        store: Store) -> None:
    make(store, "projects/apollo", stage="open", owner="Vega")
    mark = store.cursor()
    store.drop_field("projects/apollo", "stage")
    assert set(store.article("projects/apollo").properties) == {"owner"}
    assert ids(store, "projects", Where("stage", "=", "open")) == []
    assert store.changed_since(mark)[0] == [("article", "projects/apollo")]
    mark = store.cursor()
    store.drop_field("projects/apollo", "stage")
    assert store.changed_since(mark)[0] == []
