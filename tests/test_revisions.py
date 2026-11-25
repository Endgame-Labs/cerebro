"""Earlier saves of a record: the facts a revision gave and the next one no longer does are kept as superseded, out
of every read unless a query of facts says `latest: false`, and an article cites them only as history."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from test_mcp import over_mcp
from test_tokens import no_network  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro import Caller, Cerebro, Record
from cerebro.article import Identity, passages
from cerebro.compile import _history_only
from cerebro.facts import Fact, Revisions, revisions_kept
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.lookup import StoreLookup
from cerebro.store import SQLiteStore, Store

AGENT = Caller("knowledge/article", "agent")
YOU = Caller("you", "person")
DOC = "local/knowledge/discounts.md"
V1 = Record(id=DOC, source="local", revision="sha256:aaa1", at="2026-09-01",
            body="The discount limit without approval is 10%.\n\nA deal over the limit needs the sales lead.",
            fields={"subject": "Discounts"})
V2 = Record(id=DOC, source="local", revision="sha256:bbb2", at="2026-11-10",
            body="The discount limit without approval is 15%.\n\nA deal over the limit needs the sales lead.",
            fields={"subject": "Discounts"})
V3 = Record(id=DOC, source="local", revision="sha256:ccc3", at="2026-11-12",
            body="The discount limit without approval is 20%.\n\nA deal over the limit needs the sales lead.",
            fields={"subject": "Discounts"})
NOW = datetime(2026, 11, 18, 9, tzinfo=UTC)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    made = MemoryStore() if request.param == "memory" else SQLiteStore()
    yield made
    made.close()


def facts_layer(store: Store, keep: int = 10) -> Revisions:
    return Revisions(FakeExtraction(), store, keep=keep, clock=lambda: NOW)


def texts(facts: list[Fact]) -> list[str]:
    return [f.text for f in facts]


def test_a_query_of_facts_gives_the_latest_save_alone_by_default(store: Store) -> None:
    layer = facts_layer(store)
    layer.extract(V1)
    layer.extract(V2)
    result = StoreLookup(store, layer).query(AGENT, "facts", {"record": DOC})
    assert texts(result.items) == ["The discount limit without approval is 15%.",
                                   "A deal over the limit needs the sales lead."]
    assert all(f.status == "current" and not f.revision for f in result.items) and result.count == 2


def test_latest_false_adds_the_earlier_saves_each_marked_with_its_revision_and_date_and_no_warning(
        store: Store) -> None:
    layer = facts_layer(store)
    layer.extract(V1)
    layer.extract(V2)
    result = StoreLookup(store, layer).query(AGENT, "facts", {"record": DOC, "latest": False})
    assert result.warnings == [] and result.count == 3
    old = [f for f in result.items if f.status == "superseded"]
    assert [(f.text, f.revision, f.superseded_at, f.at) for f in old] == [
        ("The discount limit without approval is 10%.", "sha256:aaa1", "2026-11-10", "2026-09-01")]
    assert texts(result.items)[0] == "The discount limit without approval is 15%."  # newest first by its own time
    assert StoreLookup(store, layer).query(AGENT, "facts", {"latest": "false", "revision": "sha256:aaa1"}).count == 1


def test_a_fact_the_new_save_gives_again_in_the_same_words_is_not_superseded(store: Store) -> None:
    layer = facts_layer(store)
    first = layer.extract(V1).facts
    lead = next(f for f in first if "sales lead" in f.text)
    moved = Record(id=DOC, source="local", revision="sha256:ddd4", at="2026-11-10",
                   body="A deal over the limit needs the sales lead.\n\nThe discount limit without approval is 15%.")
    layer.extract(moved)  # the same words at another place: a new id from this layer, as Factoid gives one
    assert lead.id not in {f.id for f in layer.facts(record=DOC)}
    assert texts(layer.facts(record=DOC, latest=False)).count("A deal over the limit needs the sales lead.") == 1


def test_a_record_taken_in_again_at_the_revision_it_holds_supersedes_nothing(store: Store) -> None:
    layer = facts_layer(store)
    layer.extract(V1)
    layer.extract(Record(id=DOC, source="local", revision=V1.revision, at=V1.at, body=V2.body))  # other words, as a
    assert store.superseded() == []  # model's may differ on a retry of one revision


def test_every_read_but_one_that_asks_leaves_a_superseded_fact_out(store: Store) -> None:
    layer = facts_layer(store)
    store.create(Identity("knowledge/discounts", "knowledge", "Discounts"))
    first = layer.extract(V1).facts
    store.link(first[0].id, ["knowledge/discounts"])
    layer.extract(V2)
    lookup = StoreLookup(store, layer)
    old = first[0].id
    read = lookup.read(AGENT, ["knowledge/discounts"])
    assert old not in {f.id for f in read.facts} and old not in read.linked["knowledge/discounts"]
    assert old not in {f.id for f in lookup.read(AGENT, [DOC]).facts}  # a record reads at its latest save
    assert old not in {f.id for f in layer.facts()} and old not in {h.id for h in layer.search("discount limit 10%")}
    assert old not in {f.id for f in lookup.query(AGENT, "facts", {"articles": "knowledge/discounts"}).items}
    assert lookup.query(AGENT, "facts", {}).count == 2
    linked = lookup.query(AGENT, "facts", {"articles": "knowledge/discounts", "latest": False})
    assert [f.id for f in linked.items] == [old]  # its link stays, for a query that asks
    by_id = lookup.read(AGENT, [old]).facts
    assert [(f.id, f.status, f.revision) for f in by_id] == [(old, "superseded", "sha256:aaa1")]


def test_a_fact_retracted_before_a_new_save_never_comes_back_as_superseded(store: Store) -> None:
    layer = facts_layer(store)
    gone = layer.extract(V1).facts[0]
    StoreLookup(store, layer).retract(YOU, gone.id)
    layer.extract(V2)
    assert gone.id not in {f.id for f in layer.facts(latest=False)}
    assert [f.status for f in layer.facts(ids=[gone.id])] == ["retracted"]


def test_a_superseded_fact_a_person_retracts_stays_retracted_through_later_saves(store: Store) -> None:
    layer = facts_layer(store)
    old = layer.extract(V1).facts[0]
    layer.extract(V2)
    lookup = StoreLookup(store, layer)
    assert lookup.retract(YOU, old.id).withdrew == [old.id]
    layer.extract(V3)
    assert old.id not in {f.id for f in layer.facts(latest=False)}
    assert [f.status for f in layer.facts(ids=[old.id])] == ["retracted"]


def test_past_the_bound_the_oldest_saves_go_and_the_store_grows_by_the_bound_alone(store: Store) -> None:
    layer = facts_layer(store, keep=2)
    for n in range(50):
        layer.extract(Record(id=DOC, source="local", revision=f"r{n}", at=f"2026-09-{1 + n % 28:02d}",
                             body=f"The discount limit without approval is {n}%.\n\nA deal over the limit needs the "
                                  "sales lead."))
    kept = layer.facts(record=DOC, latest=False)
    assert sorted(f.revision for f in kept if f.status == "superseded") == ["r47", "r48"]
    assert len(store.superseded()) <= 2 * 2  # two saves of two facts at most, one the latest save repeats


def test_the_workspace_takes_the_bound_from_its_settings_and_refuses_one_that_is_no_whole_number(tmp_path: Path) -> None:
    from cerebro.project import WorkspaceError, check_settings

    assert revisions_kept({}) == 10 and revisions_kept({"facts": {"revisions_kept": 3}}) == 3
    for bad in ({"facts": {"revisions_kept": -1}}, {"facts": {"revisions_kept": "ten"}}, {"facts": 3}):
        with pytest.raises(WorkspaceError, match="facts"):
            check_settings(bad)
    (tmp_path / "settings.yaml").write_text("facts:\n  revisions_kept: 3\n")
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction()) as workspace:
        assert isinstance(workspace.extraction, Revisions) and workspace.extraction.keep == 3


def test_the_workspaces_mask_covers_a_superseded_fact_on_its_way_out(store: Store) -> None:
    layer = facts_layer(store)
    layer.extract(Record(id=DOC, source="local", revision="1", at="2026-09-01", body="The code is QX7-PLUM-81."))
    layer.extract(Record(id=DOC, source="local", revision="2", at="2026-11-01", body="The code was rotated."))
    store.scrub = lambda text: text.replace("QX7-PLUM-81", "[masked]")  # a secret read after the fact was kept
    assert texts(layer.facts(latest=False))[-1] == "The code is [masked]."


# -- through a pass, MCP and the JSON API ---------------------------------------------------------------------------

def workspace_with_a_note(root: Path, run: ScriptedRun | None = None) -> tuple[Cerebro, FakeConnector]:
    (root / "agents" / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "settings.yaml").write_text("schedule: { articles: 0 }\n")  # a changed article is rewritten at once
    (root / "agents" / "knowledge" / "discounts.md").write_text(
        "---\nevidence:\n  - facts: { mentions: true }\n  - facts: { mentions: true, latest: false }\n"
        "heartbeat: [change]\n---\n\n# Output\n\n## Current\n\n## Versions\n")
    notes = FakeConnector("local", [V1])
    workspace = Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), connectors=[notes], clock=lambda: NOW,
                    agent_run=run or ScriptedRun(lambda prompt: {}, clock=lambda: NOW))
    return workspace, notes


def test_a_record_removed_at_its_source_takes_its_superseded_facts_with_it(tmp_path: Path) -> None:
    workspace, notes = workspace_with_a_note(tmp_path)
    workspace.run_once()
    notes.add(V2)
    workspace.run_once()
    assert [f.status for f in workspace.extraction.facts(record=DOC, latest=False)].count("superseded") == 1
    notes.remove(DOC)
    result = workspace.run_once()
    assert result.retracted == 3 and workspace.extraction.facts(record=DOC, latest=False) == []
    assert {r["status"] for r in workspace.store.superseded()} == {"retracted"}


def test_a_persons_correction_stands_through_a_new_save(tmp_path: Path) -> None:
    from cerebro.article import Property

    workspace, notes = workspace_with_a_note(tmp_path)
    workspace.run_once()
    workspace.store.set_field("knowledge/discounts", "discount_without_approval", Property("12%", YOU, "2026-11-11T09:00:00Z"))
    notes.add(V2)
    workspace.run_once()
    held = workspace.store.article("knowledge/discounts").properties["discount_without_approval"]
    assert held.value == "12%" and held.by == YOU


def write_both(prompt: str) -> dict[str, str]:
    """An article that states the old limit as current on the earlier save alone, and as history beside the new."""
    evidence = prompt.split("<evidence>")[-1]
    old = re.search(r"\[\^([^\]\s]+)\] The discount limit without approval is 10%.*an earlier save", evidence)
    new = re.search(r"\[\^([^\]\s]+)\] The discount limit without approval is 15%", evidence)
    if not (old and new):
        return {"Current": f"The discount limit without approval is 10%. [^{re.findall(r'f[a-z]+_[0-9a-z]+', evidence)[0]}]"}
    return {"Current": f"- The discount limit without approval is 10%. [^{old.group(1)}]\n"
                       f"- The discount limit without approval is 15%. [^{new.group(1)}]",
            "Versions": "| Version | Status |\n| --- | --- |\n"
                        f"| 2026-11-10 | current [^{new.group(1)}] |\n"
                        f"| 2026-09-01 | superseded [^{old.group(1)}] [^{new.group(1)}] |\n"
                        f"| 2026-09-01 | current [^{old.group(1)}] |"}


def test_an_article_never_states_an_earlier_saves_value_as_current_and_keeps_it_as_history(tmp_path: Path) -> None:
    seen: list[str] = []
    workspace, notes = workspace_with_a_note(tmp_path, ScriptedRun(lambda p: seen.append(p) or write_both(p),
                                                            clock=lambda: NOW))
    workspace.run_once()
    notes.add(V2)
    workspace.run_once()
    assert "Facts, mentions True, every revision of each record" in seen[-1]
    assert "an earlier save (revision aaa1), stated on 2026-09-01, superseded on 2026-11-10 by a later save of " + DOC in seen[-1]
    article = {s.heading: s.text for s in passages(workspace.store.article("knowledge/discounts"))}
    assert "is 10%" not in article["Current"] and "The discount limit without approval is 15%." in article["Current"]
    rows = article["Versions"].splitlines()
    assert any("superseded" in row for row in rows) and not any("| 2026-09-01 | current" in row for row in rows)


def test_the_history_rule_lets_an_earlier_save_stand_only_beside_a_later_save_of_the_same_record() -> None:
    old = Fact("f_old", "The limit is 10%.", "", "fact", "2026-09-01", "local", DOC, status="superseded",
               revision="r1", superseded_at="2026-11-10T00:00:00Z")
    older = Fact("f_older", "The limit is 5%.", "", "fact", "2026-06-01", "local", DOC, status="superseded",
                 revision="r0", superseded_at="2026-09-01T00:00:00Z")
    new = Fact("f_new", "The limit is 15%.", "", "fact", "2026-11-10", "local", DOC)
    other = Fact("f_other", "The limit is 15%.", "", "fact", "2026-11-10", "local", "local/other.md")
    parts = {"Current": "The limit is 10%. [^f_old] The limit is 15%. [^f_new]\n- Ten. [^f_old] [^f_other]",
                "History": "It was 5% until 1 September. [^f_older] [^f_old] It was 10% until 10 November. "
                           "[^f_old] [^f_new]"}
    kept, dropped = _history_only(parts, {f.id: f for f in (old, older, new, other)})
    assert kept == {"Current": "The limit is 15%. [^f_new]", "History": parts["History"]}
    assert len(dropped) == 2 and all("no later save of the same thing" in line for line in dropped)


def test_an_earlier_save_beside_a_later_fact_of_its_record_on_another_matter_is_still_alone() -> None:
    old = Fact("f_old", "The discount limit without approval is 10%.", "", "fact", "2026-09-01", "local", DOC,
               status="superseded", revision="r1", superseded_at="2026-11-10T00:00:00Z")
    lead = Fact("f_lead", "A deal over the limit needs the sales lead.", "", "fact", "2026-11-10", "local", DOC)
    new = Fact("f_new", "The discount limit without approval is 15%.", "", "fact", "2026-11-10", "local", DOC)
    parts = {"Current": "The discount limit without approval is 10%. [^f_old] [^f_lead]",
                "History": "The limit was 10% until 10 November, and is 15% now. [^f_old] [^f_new]"}
    kept, dropped = _history_only(parts, {f.id: f for f in (old, lead, new)})
    assert kept == {"Current": "", "History": parts["History"]} and len(dropped) == 1


def test_latest_false_reaches_an_outside_agent_over_mcp_and_the_json_api_alike(tmp_path: Path) -> None:
    from test_api import call
    from test_tokens import serving

    workspace, notes = workspace_with_a_note(tmp_path)
    workspace.run_once()
    notes.add(V2)
    workspace.run_once()

    async def calls(client: Client) -> list[Any]:
        return [await client.call_tool("query", {"from": "facts", "record": DOC, "latest": False}),
                await client.call_tool("query", {"from": "facts", "record": DOC})]

    every, latest = over_mcp(workspace, calls)
    assert every.structured_content["count"] == 3 and latest.structured_content["count"] == 2
    old = [f for f in every.structured_content["items"] if f.get("status") == "superseded"]
    assert [(f["revision"], f["superseded_at"]) for f in old] == [("sha256:aaa1", "2026-11-10")]
    assert "[an earlier save (revision aaa1), stated on 2026-09-01, superseded on 2026-11-10" in every.content[0].text
    assert "earlier save" not in latest.content[0].text and "warnings" not in every.structured_content
    token = workspace.make_token("desk-agent").token
    with serving(workspace) as client:
        answered = call(client, "query", {"from": "facts", "record": DOC, "latest": False}, token).json()
        refused = call(client, "query", {"from": "facts", "record": DOC, "latest": False}, None)
    assert answered["count"] == 3 and [f["revision"] for f in answered["items"] if f.get("status") == "superseded"] == [
        "sha256:aaa1"]
    assert refused.status_code == 401


def test_the_pass_says_why_it_dropped_a_sentence_that_gave_an_earlier_saves_value_as_current(tmp_path: Path) -> None:
    workspace, notes = workspace_with_a_note(tmp_path, ScriptedRun(write_both, clock=lambda: NOW))
    workspace.run_once()
    notes.add(V2)
    second = workspace.run_once()
    said = [line for line in second.warnings if "an earlier save of " + DOC in line]
    assert said and all("so it would give an old value as the one that holds now" in line for line in said)


def test_a_retracted_fact_of_the_record_is_no_later_save_beside_an_earlier_one() -> None:
    old = Fact("f_old", "The limit is 10%.", "", "fact", "2026-09-01", "local", DOC, status="superseded",
               revision="r1", superseded_at="2026-11-10T00:00:00Z")
    gone = Fact("f_gone", "The limit is 15%.", "", "fact", "2026-11-10", "local", DOC, status="retracted")
    kept, dropped = _history_only({"History": "It was 10% until 10 November. [^f_old] [^f_gone]"},
                                  {f.id: f for f in (old, gone)})
    assert kept == {"History": ""} and len(dropped) == 1
