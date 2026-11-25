"""A curator's `link` and `instruct` on an article of its collection that exists, the admit they replace there, and
what a pass does with the records and facts a changed key binds."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import refusal
from test_held import PerAgent

from cerebro import Cerebro, Record
from cerebro.agent import load_agent
from cerebro.article import Identity, Property
from cerebro.caller import Caller
from cerebro.curate import holder, run_curator
from cerebro.evidence import gather
from cerebro.facts import Mention
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.lookup import StoreLookup
from cerebro.records import proposals
from cerebro.reply import outside

NOW = datetime(2026, 11, 24, 9, tzinfo=UTC)
CURATOR = ("---\nauthority: {{ admit: {admit}, archive: {archive}, merge: true, split: true }}\n"
           "evidence:\n  - held: {{}}\n  - query: {{ from: things }}\nheartbeat: [change]\n---\n\nKeep the things.\n")
ALDER = "things/alder-works"


def agents(root: Path, admit: bool = True, archive: bool = True) -> Path:
    (root / "agents" / "things").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "things" / "curator.md").write_text(CURATOR.format(admit=str(admit).lower(),
                                                                         archive=str(archive).lower()))
    return root / "agents"


def curate(store: MemoryStore, root: Path, calls: list[tuple[str, dict[str, Any]]], admit: bool = True) -> list[str]:
    folder = agents(root, admit)
    done = run_curator(load_agent(folder / "things" / "curator.md", folder), store=store, root=root,
                       lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW), functions={}, agent_tools={},
                       agent_run=ScriptedRun(lambda prompt: {}, calls), clock=lambda: NOW,
                       sources={"src": FakeConnector("src", [])})
    assert done.run is not None
    return [e.text for e in done.run.transcript if e.role == "tool"]


def things(store: MemoryStore, **keys: dict[str, list[str]]) -> None:
    for name, held in keys.items():
        store.create(Identity(f"things/{name}", "things", name.capitalize(), held, "active"))


def thing(id: str, name: str, at: str = "2026-11-20T09:00:00Z", **keys: list[str]) -> Record:
    return Record(id=id, source="src", revision="1", at=at, keys=keys, fields={"name": name}, body=f"{name} news.",
                  into="things")


def workspace(root: Path, *records: Record, clock: list[datetime] | None = None,
              archive: bool = True) -> tuple[Cerebro, PerAgent]:
    agents(root, archive=archive)
    (root / "settings.yaml").write_text("{}\n")
    run, now = PerAgent(), clock or [NOW]
    return Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: now[0], agent_run=run,
                   connectors=[FakeConnector("src", list(records))], trusted=True), run


def act(ws: Cerebro, run: PerAgent, *calls: tuple[str, dict[str, Any]]) -> list[str]:
    """The things curator run once by hand with these calls, and its replies."""
    run.calls["things/curator"] = list(calls)
    ws.curate(next(a for a in ws.agents if a.id == "things/curator"))
    return run.replies["things/curator"][-len(calls):]


def on(ws: Cerebro, article: str) -> set[str]:
    """The records whose facts are tied to the article."""
    ids = set(ws.store.facts_of(article))
    return {f.record for f in ws.extraction.facts() if f.id in ids}  # type: ignore[union-attr]


def held(ws: Cerebro) -> list[str]:
    return [row["id"] for row in ws.store.state("held:src") or []]


# -- the act ------------------------------------------------------------------------------------------------------

def test_link_refuses_an_article_outside_its_collection(tmp_path: Path) -> None:
    store = MemoryStore()
    things(store, alder={"code": ["t-alder"]})
    store.create(Identity("others/birch", "others", "Birch", {}, "active"))
    replies = curate(store, tmp_path, [
        ("link", {"article": "others/birch", "reason": "its code", "keys": {"code": ["t-birch"]}})])
    assert "others/birch is no article of things" in replies[0], replies
    assert proposals(store) == []


def test_link_adds_fixes_and_takes_off_keys_and_replaces_a_writers_link_and_says_each(tmp_path: Path) -> None:
    store = MemoryStore()
    things(store, alder={"domain": ["alder.example"], "code": ["t-1", "t-9"], "alias": ["Alder Co"]})
    store.set_field("things/alder", "linked", Property(["parts/ash"], Caller("things/alder/writer", "agent"),
                                                  "2026-11-20T09:00:00Z"))
    [reply] = curate(store, tmp_path, [("link", {
        "article": "Alder", "reason": "its new address, and a code fixed", "keys": {
            "email": ["ops@alder.example"], "code": ["t-2"], "alias": None, "Domain": None},
        "wrong": {"code": ["t-9"]}, "linked": ["parts/birch"], "unlinked": ["parts/ash"]})])
    assert store.article("things/alder").identity.keys == {"domain": ["alder.example"], "code": ["t-1", "t-2"],
                                                           "email": ["ops@alder.example"]}
    assert store.article("things/alder").properties["linked"].value == ["parts/birch"]
    for said in ("email ops@alder.example added", "code t-2 added", "code t-9 taken off", "alias Alder Co taken off",
                 "link parts/birch added", "link parts/ash taken off", "Domain was not on it"):
        assert said in reply, reply
    assert [(p.kind, p.status) for p in proposals(store)] == [("article.link", "accepted")]


def test_an_admit_of_an_article_in_the_working_set_is_refused_and_names_link(tmp_path: Path) -> None:
    store = MemoryStore()
    things(store, alder={"code": ["t-alder"]})
    replies = curate(store, tmp_path, [("admit", {"name": "Alder", "reason": "seen again"})], admit=False)
    assert replies == [refusal("admit: things/alder is in the working set already: to change its keys or links, use "
                               "link, and to change your note to its writers, use instruct")]
    assert proposals(store) == []


def test_instruct_refuses_another_collection_and_given_no_words_changes_nothing(tmp_path: Path) -> None:
    store = MemoryStore()
    things(store, alder={})
    store.create(Identity("others/birch", "others", "Birch", {}, "active"))
    replies = curate(store, tmp_path, [("instruct", {"article": "others/birch", "text": "Weigh its venue."}),
                                       ("instruct", {"article": "Alder", "text": "Weigh its new plant."}),
                                       ("instruct", {"article": "Alder", "text": " "})])
    assert "others/birch is no article of things" in replies[0], replies
    assert "it gives no words, so nothing changed" in replies[2], replies
    assert [(p.target, p.payload["text"]) for p in proposals(store, "accepted")] == [
        ("things/alder", "Weigh its new plant.")]


# -- one value, one article, where its key names the thing ---------------------------------------------------------

def test_a_shared_key_finds_no_article_for_an_admit_and_stays_on_a_second_one(tmp_path: Path) -> None:
    store = MemoryStore()
    things(store, alder={"code": ["t-alder"], "slack_channels": ["C1"]})
    curate(store, tmp_path, [("admit", {"name": "Birch", "keys": {"code": ["t-birch"], "slack_channels": ["C1"]},
                                        "reason": "a venue we hire"})])
    assert store.article("things/birch").identity.keys == {"code": ["t-birch"], "slack_channels": ["C1"]}
    assert store.article("things/alder").identity.keys == {"code": ["t-alder"], "slack_channels": ["C1"]}


def test_link_adds_a_shared_value_another_article_holds_and_leaves_off_one_that_names_a_thing(tmp_path: Path) -> None:
    store = MemoryStore()
    things(store, alder={"code": ["t-alder"], "slack_channels": ["C1"]}, birch={"code": ["t-birch"]})
    [reply] = curate(store, tmp_path, [("link", {"article": "Birch", "reason": "its channel",
                                                 "keys": {"slack_channels": ["C1"], "code": ["t-alder"]}})])
    assert store.article("things/birch").identity.keys == {"code": ["t-birch"], "slack_channels": ["C1"]}
    assert "The key code t-alder stays on things/alder, which holds it, so it is not added." in reply, reply


def test_a_key_the_workspaces_own_connector_issues_as_shared_may_sit_on_many_articles(tmp_path: Path) -> None:
    class Rooms(FakeConnector):
        IDENTITY_KEYS: ClassVar[dict[str, int]] = {"room": 0}

    agents(tmp_path)
    (tmp_path / "settings.yaml").write_text("{}\n")
    run = PerAgent()
    ws = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run,
                 connectors=[Rooms("src", [])], trusted=True)
    things(ws.store, alder={"code": ["t-alder"], "room": ["R1"]}, birch={"code": ["t-birch"]})
    act(ws, run, ("link", {"article": "Birch", "reason": "its room", "keys": {"room": ["R1"]}}))
    assert ws.store.article("things/birch").identity.keys["room"] == ["R1"]


def test_a_built_in_connectors_weight_stands_where_a_workspaces_own_connector_declares_its_key_otherwise(
        tmp_path: Path) -> None:
    class Channels(FakeConnector):
        IDENTITY_KEYS: ClassVar[dict[str, int]] = {"slack_channels": 5}

    for name in ("slack", "my-slack"):
        root = tmp_path / name
        agents(root)
        (root / "settings.yaml").write_text("{}\n")
        ws = Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, agent_run=PerAgent(),
                     connectors=[Channels(name, [])], trusted=True)
        things(ws.store, alder={"code": ["t-alder"], "slack_channels": ["C1"]})
        assert holder(ws.store, "things", "slack_channels", "C1") is None, name


# -- what a pass does with the records and facts a changed key binds -------------------------------------------------

def test_facts_kept_before_an_article_gained_a_key_tie_to_it_on_the_next_pass(tmp_path: Path) -> None:
    loose = Record(id="L1", source="src", revision="1", at="2026-11-20T09:00:00Z",
                   keys={"email": ["ops@alder.example"]}, body="Ops at Alder asked for a quote.")
    ws, run = workspace(tmp_path, thing("T1", "Alder Works", code=["t-alder"]), loose)
    ws.run_once(agents=False)
    run.calls["things/curator"] = [("admit", {"record": {"source": "src", "id": "T1"}, "reason": "a maker"})]
    ws.run_once()
    act(ws, run, ("link", {"article": ALDER, "reason": "its ops address", "keys": {"email": ["ops@alder.example"]}}))
    ws.run_once(agents=False)
    assert on(ws, ALDER) == {"T1", "L1"}


def test_facts_a_wrong_key_tied_come_off_when_the_key_does(tmp_path: Path) -> None:
    ws, run = workspace(tmp_path, thing("T1", "Alder Works", code=["t-alder"]),
                        thing("B1", "Birch Hall", code=["t-birch"]))
    ws.run_once(agents=False)
    run.calls["things/curator"] = [("admit", {"record": {"source": "src", "id": "T1"}, "keys": {"code": ["t-birch"]},
                                              "reason": "a maker"})]
    ws.run_once()
    ws.run_once(agents=False)
    assert on(ws, ALDER) == {"T1", "B1"}
    act(ws, run, ("link", {"article": ALDER, "reason": "Birch Hall's code", "wrong": {"code": ["t-birch"]}}))
    ws.run_once(agents=False)
    assert on(ws, ALDER) == {"T1"}


def test_a_fact_a_wrong_key_tied_by_its_own_subject_comes_off_with_the_key(tmp_path: Path) -> None:
    birch = thing("B1", "Birch Hall", code=["t-birch"])
    ws, run = workspace(tmp_path, thing("T1", "Alder Works", code=["t-alder"]),
                        replace(birch, fields={**birch.fields, "subject": "Birch Hall"}))
    ws.run_once(agents=False)
    run.calls["things/curator"] = [("admit", {"record": {"source": "src", "id": "T1"}, "keys": {"code": ["t-birch"]},
                                              "reason": "a maker"})]
    ws.run_once()
    ws.run_once(agents=False)
    assert on(ws, ALDER) == {"T1", "B1"}
    act(ws, run, ("link", {"article": ALDER, "reason": "Birch Hall's code", "wrong": {"code": ["t-birch"]}}))
    ws.run_once(agents=False)
    assert on(ws, ALDER) == {"T1"}


def test_a_fact_whose_subject_also_carries_a_right_key_keeps_its_tie_and_first_time_when_a_wrong_one_comes_off(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mentions = FakeExtraction._mentions  # the subject carries an address its record does not, as a model reads it
    monkeypatch.setattr(FakeExtraction, "_mentions", lambda self, record, paragraph: (
        [Mention("Birch Hall", "subject", {"code": ["t-birch"], "email": ["ops@alder.example"]})]
        if record.id == "B1" else mentions(self, record, paragraph)))
    clock = [NOW]
    ws, run = workspace(tmp_path, thing("T1", "Alder Works", code=["t-alder"]), thing("B1", "Birch order",
                        code=["t-birch"]), clock=clock)
    ws.run_once(agents=False)
    act(ws, run, ("admit", {"record": {"source": "src", "id": "T1"}, "reason": "a maker",
                            "keys": {"code": ["t-birch"], "email": ["ops@alder.example"]}}))
    ws.run_once(agents=False)
    act(ws, run, ("link", {"article": ALDER, "reason": "Birch Hall's code", "wrong": {"code": ["t-birch"]}}))
    clock[0] = NOW + timedelta(hours=2)
    ws.run_once(agents=False)
    fact = next(f.id for f in ws.extraction.facts() if f.record == "B1")  # type: ignore[union-attr]
    assert "subject" in ws.store.ties_of(fact)[ALDER]
    assert fact not in ws.store.facts_of(ALDER, since=(NOW + timedelta(hours=1)).isoformat())


def test_a_told_fact_stays_on_its_article_when_the_articles_keys_change(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    mentions = FakeExtraction._mentions  # a real tell need not name its article, as the stand-in's subject does
    monkeypatch.setattr(FakeExtraction, "_mentions", lambda self, record, paragraph: (
        [] if record.source == "tell" else mentions(self, record, paragraph)))
    ws, run = workspace(tmp_path, thing("T1", "Alder Works", code=["t-alder"]))
    ws.run_once(agents=False)
    act(ws, run, ("admit", {"record": {"source": "src", "id": "T1"}, "reason": "a maker"}))
    ws.run_once(agents=False)
    ws.tell(Caller("you", "person"), "The plant moved to Leeds in October.", ALDER, "fact")
    act(ws, run, ("link", {"article": ALDER, "reason": "its sales address", "keys": {"domain": ["alder.example"]}}))
    ws.run_once(agents=False)
    tied = set(ws.store.facts_of(ALDER))
    assert any("Leeds" in f.text for f in ws.extraction.facts() if f.id in tied)  # type: ignore[union-attr]


def test_a_key_change_brings_nothing_new_that_lifts_a_rejected_archive(tmp_path: Path) -> None:
    clock = [NOW]
    ws, run = workspace(tmp_path, thing("T1", "Alder Works", code=["t-alder"]), clock=clock, archive=False)
    ws.run_once(agents=False)
    act(ws, run, ("admit", {"record": {"source": "src", "id": "T1"}, "reason": "a maker"}))
    ws.run_once(agents=False)
    act(ws, run, ("archive", {"article": ALDER, "reason": "quiet for a year"}))
    clock[0] = NOW + timedelta(hours=1)
    ws.decide(Caller("you", "person"), reject=[p.id for p in proposals(ws.store, "waiting")])
    clock[0] = NOW + timedelta(hours=2)
    act(ws, run, ("link", {"article": ALDER, "reason": "its sales address", "keys": {"domain": ["alder.example"]}}))
    ws.run_once(agents=False)
    [again] = act(ws, run, ("archive", {"article": ALDER, "reason": "quiet for a year"}))
    assert "rejected archive" in again, again


def test_a_recording_joins_its_meetings_article_by_a_link_of_its_own_id(tmp_path: Path) -> None:
    meeting = thing("E1", "Weekly sync", calendar_event_id=["evt-1#20261120"])
    recording = Record(id="G1", source="src", revision="1", at="2026-11-20T09:02:00Z",
                       keys={"gong_call_id": ["g-1"], "email": ["dana@alder.example"]}, carried=["email"],
                       fields={"name": "Weekly sync"}, body="Dana asked for the price list.", into="things")
    ws, run = workspace(tmp_path, meeting, recording)
    ws.run_once(agents=False)
    run.calls["things/curator"] = [("admit", {"record": {"source": "src", "id": "E1"}, "reason": "a weekly meeting"})]
    ws.run_once()
    assert "G1" in held(ws)
    ws.store.create(Identity("things/standup", "things", "Standup", {}, "active"))
    run.calls["things/curator"] = [(
        "link", {"article": article, "reason": "the recording of this meeting", "keys": {"gong_call_id": ["g-1"]}})
        for article in ("things/weekly-sync", "things/standup")]
    ws.run_once()
    ws.run_once(agents=False)
    assert "G1" not in held(ws) and "G1" in on(ws, "things/weekly-sync")
    assert ws.store.article("things/standup").identity.keys == {}  # a recording's own id binds one article


# -- what is shown ----------------------------------------------------------------------------------------------

def test_read_and_query_and_evidence_show_an_articles_keys(tmp_path: Path) -> None:
    store = MemoryStore()
    things(store, alder={"code": ["t-alder"]})
    folder = agents(tmp_path)
    (folder / "things" / "alder.md").write_text("---\nevidence:\n  - query: { from: things }\n---\n\n# Output\n")
    lookup = StoreLookup(store, FakeExtraction(), clock=lambda: NOW)
    reader = Caller("things/curator", "agent")
    assert outside(lookup.read(reader, ["things/alder"]))["articles"][0]["keys"] == {"code": ["t-alder"]}
    assert outside(lookup.query(reader, "things", {}))["items"][0]["keys"] == {"code": ["t-alder"]}
    shown = {}
    for agent in ("curator", "alder"):
        evidence = gather(load_agent(folder / "things" / f"{agent}.md", folder), lookup, {}, root=tmp_path)
        shown[agent] = "t-alder" in "\n".join(line for entry in evidence for line in entry.lines)
    assert shown == {"curator": True, "alder": False}
