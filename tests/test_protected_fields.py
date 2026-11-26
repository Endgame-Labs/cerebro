"""The fields the system relies on are protected: the system's value always wins over what a model writes there,
in the copy's front matter, in evidence and in the store, and every other field stays the model's to set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cerebro.article import PROTECTED, Article, Identity, Property, fields, markdown, protected
from cerebro.caller import Caller
from cerebro.compile import _Writes
from cerebro.evidence import _front_text
from cerebro.fakes import MemoryStore

WRITER = Caller("things/writer", "agent")
AT = "2026-11-19T09:00:00Z"


def _override(store: MemoryStore, article: str, field: str, value: object) -> str:
    """What the override tool a run is handed says to one call, with what it set landed after."""
    writes = _Writes(article, store, WRITER.name)
    [override] = writes.tools()
    said = override(field, value)
    writes.land(AT)
    return said["refused"] if isinstance(said, dict) else said  # a refusal answers as a refused call does


def _front_matter(store: MemoryStore, article: str) -> str:
    return markdown(store.article(article)).split("\n---\n")[0]


# -- a model's claim of identity or keys ------------------------------------------------------------------------------

def test_a_models_claim_of_identity_or_keys_never_shows_where_the_identity_goes() -> None:
    store = MemoryStore()
    store.create(Identity("things/b", "things", "Beta"))
    for name in ("identity", "keys"):  # values written straight in, past the tool
        store.set_field("things/b", name, Property(["domain: alpha.test"], WRITER, AT))
    front = _front_matter(store, "things/b")
    assert "identity" not in front and "keys" not in front and "alpha.test" not in front
    store.set_identity(Identity("things/b", "things", "Beta", keys={"domain": ["beta.test"]}))
    front = _front_matter(store, "things/b")
    assert "identity:\n  domain:\n    - beta.test" in front and "alpha.test" not in front


# -- one protected field of each kind ----------------------------------------------------------------------------------

@pytest.mark.parametrize(("field", "kind"), [("title", "the identity's"), ("compiled_at", "compile's"),
                                             ("in_source", "a source's")])
def test_a_model_is_told_a_protected_field_is_the_systems_and_its_value_never_stands(field: str, kind: str) -> None:
    store = MemoryStore()
    store.create(Identity("things/b", "things", "Beta"))
    said = _override(store, "things/b", field, "things/elsewhere")
    assert said.startswith(f"override: {field} is the system's, not yours to set: "), said
    assert PROTECTED[field] in said  # the reply says what the system keeps it for
    assert field not in store.article("things/b").properties
    assert "things/elsewhere" not in _front_matter(store, "things/b")


def test_a_field_that_reads_as_a_protected_one_is_the_systems_too() -> None:
    store = MemoryStore()
    store.create(Identity("things/b", "things", "Beta"))
    for name in ("ID", "_id", "Ｔｉｔｌｅ", "tіtle", " Status", "in_source_"):  # case, edges, fullwidth, a Cyrillic i
        assert protected(name), name
        assert "is the system's, not yours to set" in _override(store, "things/b", name, "x"), name
    assert not store.article("things/b").properties
    assert not protected("stage") and not protected("domain") and not protected("source")


def test_the_systems_in_source_reads_from_the_identity_and_no_writer_moves_it() -> None:
    store = MemoryStore()
    store.create(Identity("things/b", "things", "Beta", in_source=False))  # its source removed its record
    assert "in_source: false" in _front_matter(store, "things/b")
    assert "is the system's" in _override(store, "things/b", "in_source", True)
    assert store.article("things/b").identity.in_source is False and "in_source: false" in _front_matter(store, "things/b")


# -- an empty protected field, and every other field ------------------------------------------------------------------

def test_every_other_field_lands_as_the_model_set_it() -> None:
    store = MemoryStore()
    store.create(Identity("things/b", "things", "Beta", keys={"domain": ["beta.test"]}))
    assert _override(store, "things/b", "stage", "won") == "set stage"
    assert _override(store, "things/b", "domain", "gamma.test") == "set domain"  # a field, not the identity's key
    article = store.article("things/b")
    assert article.properties["stage"].value == "won" and article.properties["stage"].by == WRITER
    front = _front_matter(store, "things/b")
    assert "stage: won" in front and "domain: gamma.test" in front and "- beta.test" in front
    assert json.loads(_front_text(article)) == {"domain": "gamma.test", "stage": "won", "title": "Beta"}
    assert store.bound("domain", "gamma.test") == []  # it binds nothing


def test_linked_lands_as_a_list_of_article_ids_and_any_other_shape_is_refused_with_the_right_one() -> None:
    store = MemoryStore()
    store.create(Identity("things/b", "things", "Beta"))
    assert _override(store, "things/b", "linked", ["things/a", "parts/never-made"]) == "set linked"
    for wrong in ("[things/a]", "things/a", 3, [["things/a"]], ["[things/a]"], ["../things/a.md"], ["../things/a"], ["things"],
                  ["articles/things/a"]):
        said = _override(store, "things/b", "linked", wrong)
        assert said.startswith('override: linked is a list of article ids, such as ["projects/atlas"], and '), said
        assert said.endswith("is not: give each id alone, with no brackets, link, articles/ or .md"), said
    assert store.article("things/b").properties["linked"].value == ["things/a", "parts/never-made"]
    assert _override(store, "things/b", "linked", None) == "dropped linked"


# -- every other reader of an article's fields ------------------------------------------------------------------------

def _claimed() -> Article:
    """An article its curator admitted, on which a model claimed protected fields straight in, past the tool, and set
    one field of its own."""
    article = Article(Identity("things/b", "things", "Beta"))
    for name, value in (("id", "things/x"), ("title", "Forged"), ("in_source", False), ("stage", "won")):
        article.properties[name] = Property(value, WRITER, AT)
    return article


def test_a_query_reads_the_systems_value_of_a_protected_field_and_the_models_of_its_own() -> None:
    from cerebro.lookup import _values

    values = _values(_claimed())
    assert "title" not in values and "in_source" not in values
    assert values["stage"] == "won" and values["id"] == "things/b"


def test_the_mcp_lines_of_an_article_show_no_models_claim_of_a_protected_field() -> None:
    from cerebro.lookup import ReadResult
    from cerebro.mcp import _article_line, _article_text

    article = _claimed()
    for text in (_article_line(article), _article_text(article, ReadResult([article]))):
        assert "Forged" not in text and "things/x" not in text and "in_source" not in text, text
        assert "stage: won" in text and "Beta" in text


def test_an_article_as_a_reply_gives_it_holds_no_models_claim_of_a_protected_field() -> None:
    from cerebro.reply import _article

    shown = _article(_claimed())
    assert shown["properties"] == {"stage": "won"} and shown["title"] == "Beta"


def test_a_left_line_reads_why_an_article_left_by_the_systems_values() -> None:
    from cerebro.evidence import _Gatherer, _values_of

    article = _claimed()
    assert _values_of(article, "in_source") == [] and _values_of(article, "title") == []
    assert _values_of(article, "stage") == ["won"]
    unmet = _Gatherer.unmet(None, 0, None, article, {"id": "things/x", "stage": "won"}, None)  # type: ignore[arg-type]
    assert unmet == ["id"]  # a model's id does not meet a query for it, and its own field does


# -- the readers of raw values, outside callers, the refused count ----------------------------------------------------

OUTSIDE = Caller("an-assistant", "outside")


def test_a_status_filter_reads_the_systems_status_whoever_wrote_one() -> None:
    from cerebro.lookup import _status

    article = Article(Identity("things/b", "things", "Beta"))
    for by in (WRITER, Caller("things/curator", "agent")):
        article.properties["status"] = Property("past", by, AT)  # straight in, as no guard lets a writer
        assert _status(article) == "active"


def test_what_holds_an_article_reads_no_models_claim_of_a_protected_field() -> None:
    from cerebro.identity import held_by

    store = MemoryStore()
    for id in ("groups/a", "groups/x"):
        store.create(Identity(id, "groups", id))
    store.create(Identity("things/b", "things", "Beta"))
    store.set_field("things/b", "linked", Property(["groups/a"], WRITER, AT))
    store.set_field("things/b", "title", Property("groups/x", WRITER, AT))
    assert held_by(store, "things/b", "groups") == ["groups/a"]


def test_what_an_eval_shows_its_judge_holds_no_models_claim_of_a_protected_field() -> None:
    from cerebro.evals import Probe, _rules, _shown, _text

    article = _claimed()
    assert "Forged" not in _text(article) and "things/x" not in _text(article) and "stage: won" in _text(article)
    assert _shown(article)["properties"] == {"stage": "won"}
    probe = Probe("p", "What is it called?", "read", "things/b", {"fields": {"title": "Forged"}})
    [check] = [c for c in _rules(probe, [article], "") if c.name == "field title"]
    assert not check.passed  # a model's title is no answer the article holds


def test_a_tell_is_sorted_against_the_fields_the_system_reads() -> None:
    from cerebro.records import _material

    assert _material("rules", _claimed(), "things", {})["fields"] == ["stage"]


def test_no_writers_value_of_a_protected_field_stands_whoever_it_is() -> None:
    store = MemoryStore()
    store.create(Identity("things/b", "things", "Beta"))
    for by in (OUTSIDE, Caller("ann", "person"), Caller("things/curator", "agent")):
        store.set_field("things/b", "status", Property("past", by, AT))
        store.set_field("things/b", "in_source", Property(False, by, AT))
        assert "status" not in _front_matter(store, "things/b") and "in_source" not in _front_matter(store, "things/b")


def test_an_override_refusal_counts_as_a_refused_tool_call() -> None:
    store = MemoryStore()
    store.create(Identity("things/b", "things", "Beta"))
    writes = _Writes("things/b", store, WRITER.name)
    [override] = writes.tools()
    said = override("title", "Forged")
    assert isinstance(said, dict) and set(said) == {"refused"} and "is the system's" in said["refused"]
    assert '"refused"' in json.dumps(said)  # what a check counts refused tool calls by


def test_a_store_query_on_a_protected_field_reads_the_systems_value(tmp_path: Path) -> None:
    from cerebro.store import SQLiteStore, Where

    store = SQLiteStore(tmp_path / "store.db")
    store.create(Identity("things/b", "things", "Beta"))
    store.set_field("things/b", "in_source", Property(False, WRITER, AT))
    store.set_field("things/b", "stage", Property("won", WRITER, AT))
    assert store.query("things", [Where("in_source", "=", False)]) == []
    assert [a.identity.id for a in store.query("things", [Where("stage", "=", "won")])] == ["things/b"]


# -- why a query matched nothing, and the stand-in store -------------------------------------------------------------

def test_why_a_query_matched_nothing_names_no_models_claim_of_a_protected_field() -> None:
    from cerebro.lookup import StoreLookup

    lines = StoreLookup._why_empty("things", [_claimed()], {"id": "things/q", "stage": "lost"}, {})
    assert not any("things/x" in line for line in lines), lines  # a model's id is no value the article holds
    assert any("stage: lost, which holds won" in line for line in lines), lines


def test_the_stand_in_store_queries_and_orders_by_the_systems_value_as_the_real_one_does() -> None:
    from cerebro.store import Where

    store = MemoryStore()
    for id in ("things/a", "things/b"):
        store.create(Identity(id, "things", id))
    store.set_field("things/b", "in_source", Property(False, WRITER, AT))
    store.set_field("things/b", "title", Property("Aaa", WRITER, AT))
    store.set_field("things/b", "stage", Property("won", WRITER, AT))
    assert store.query("things", [Where("in_source", "=", False)]) == []
    assert [a.identity.id for a in store.query("things", order_by="title")] == ["things/a", "things/b"]
    assert [a.identity.id for a in store.query("things", [Where("stage", "=", "won")])] == ["things/b"]


# -- a writer's value of a protected field never stands ---------------------------------------------------------------

@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_a_writers_value_of_a_protected_field_is_never_indexed_or_read(kind: str, tmp_path: Path) -> None:
    from cerebro.lookup import _status
    from cerebro.store import SQLiteStore, Where

    store = MemoryStore() if kind == "memory" else SQLiteStore(tmp_path / "store.db")
    store.create(Identity("things/b", "things", "Beta"))
    store.set_field("things/b", "status", Property("gone", WRITER, "2026-11-19T10:00:00Z"))  # straight in, past the tool
    article = store.article("things/b")
    assert "status" not in fields(article) and _status(article) == "active"
    assert store.query("things", [Where("status", "=", "gone")]) == []


# -- a run counts its refusals where it makes them --------------------------------------------------------------------

def test_a_run_counts_each_refusal_it_makes_from_the_reply_and_its_row_in_the_log_names_the_tool() -> None:
    from suites.agent_run import AGENT, EVIDENCE
    from test_run import _asks, _talker

    from cerebro.compile import run_agent
    from cerebro.run import PydanticRun

    store = MemoryStore()
    store.create(Identity(AGENT.target, "reports", "Weekly"))
    writes = _Writes(AGENT.target, store, AGENT.id)
    turns = [[("override", {"field": "title", "value": "Forged"}), ("_asks", {"about": "this"})],
             [("override", {"field": "stage", "value": "won"})],
             [("write_article", {"text": "The review moved [^f_1]."})]]
    run = run_agent(AGENT, EVIDENCE, [*writes.tools(), _asks], agent_run=PydanticRun(_talker(turns, [])), store=store)
    replies = [e for e in run.transcript if e.role == "tool" and e.tool in ("override", "_asks")]
    assert [e.text.startswith("{'refused'") for e in replies] == [True, True, False]  # as the transcript holds them
    assert run.refused == ["override", "_asks"]
    [row] = [r.data for r in store.rows("agent-runs")]
    assert row["refused"] == 2 and row["refused_tools"] == ["override", "_asks"]


def test_a_null_override_drops_a_free_field_and_is_refused_on_a_protected_one_as_a_set_is() -> None:
    store = MemoryStore()
    store.create(Identity("things/b", "things", "Beta"))
    assert _override(store, "things/b", "level", "high") == "set level"
    assert _override(store, "things/b", "level", None) == "dropped level"
    assert "level" not in store.article("things/b").properties
    assert "is the system's" in _override(store, "things/b", "status", None)
