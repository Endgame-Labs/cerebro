"""The held list a curator reads, and an act that reaches another collection: a proposal there."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import refusal

from cerebro import Cerebro, Record, cli, preview, scaffold
from cerebro.agent import Agent, AgentFileError, Budget, load_agent
from cerebro.article import Identity, Property
from cerebro.caller import Caller
from cerebro.curate import Curated, run_curator
from cerebro.evidence import Evidence, EvidenceError, digest, gather
from cerebro.fakes import FakeConnector, FakeExtraction, FakeRewriter, MemoryStore, ScriptedRun, every_item_ticked
from cerebro.first_run import Choices
from cerebro.first_run import run as first_run
from cerebro.held import LIST_MAX, held_for
from cerebro.held import entry as held_entry
from cerebro.lookup import StoreLookup
from cerebro.records import Decision, decide, proposals
from cerebro.run import AgentRun, Run

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)
SECRET = "Pricing is 40k and the buyer is unhappy."  # a body: it must never reach a curator

CURATOR = "---\n{authority}evidence:\n  - held: {{}}\n{extra}heartbeat: [change]\n---\n\n{prompt}\n"
ALL = "authority: { admit: true, archive: true, merge: true, split: true }\n"  # these tests are not about authority
YOU = Caller("you", "person")


def write_workspace(root: Path, **authority: bool) -> Path:
    """A workspace folder with curators for things, people and opportunities, each reading its held list, and a
    reports folder no curator keeps. `authority` sets a collection's admit key, as people=False."""
    agents = root / "agents"
    for collection, prompt in (("things", "Keep things."), ("people", "Keep the people of a thing."),
                               ("opportunities", "Keep every open deal.")):
        (agents / collection).mkdir(parents=True, exist_ok=True)
        said = (f"authority: {{ admit: {str(authority[collection]).lower()}, archive: true, merge: true, split: true }}\n"
                if collection in authority else ALL)
        extra = "  - query: { from: things }\n" if collection == "people" else ""
        (agents / collection / "curator.md").write_text(CURATOR.format(authority=said, extra=extra, prompt=prompt))
    (agents / "reports").mkdir(parents=True, exist_ok=True)
    (agents / "reports" / "weekly.md").write_text("---\nheartbeat: [manual]\n---\n\n# Output\n\n## Summary\n")
    (root / "settings.yaml").write_text("{}\n")
    return agents


def curator(agents: Path, collection: str) -> Agent:
    return load_agent(agents / collection / "curator.md", agents)


def deal(id: str, name: str, at: str, **fields: Any) -> Record:
    return Record(id=id, source="crm", revision="1", at=at, keys={"salesforce_id": [id], "name": [name]},
                  fields={"stage": "Proposal", **fields}, body=SECRET, into="opportunities")


def contact(id: str, name: str, email: str, at: str = "2026-11-12T09:00:00Z") -> Record:
    return Record(id=id, source="crm", revision="1", at=at, keys={"salesforce_id": [id], "email": [email]},
                  fields={"name": name, "title": "Director of Operations"}, body=SECRET, into="people")


def hold(store: MemoryStore, *records: Record, source: str = "crm") -> None:
    """Records held as the runner keeps them: listings without bodies under held:<source>."""
    store.set_state(f"held:{source}", [asdict(Record(**{**asdict(r), "body": None, "by": None})) for r in records])


class PerAgent(AgentRun):
    """Each curator's scripted calls, by its id, and every prompt it was shown."""

    def __init__(self) -> None:
        self.calls: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.prompts: dict[str, list[str]] = {}
        self.replies: dict[str, list[str]] = {}

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
        def seen(prompt: str) -> dict[str, str]:
            self.prompts.setdefault(agent.id, []).append(prompt)
            return {}

        run = ScriptedRun(seen, self.calls.pop(agent.id, [])).run(agent, evidence, tools, budget)
        self.replies.setdefault(agent.id, []).extend(e.text for e in run.transcript if e.role == "tool")
        return run


class Carries(PerAgent):
    """A curator stand-in that, on a run with no calls scripted, carries out each accepted admit of its collection
    left, by an admit that names it."""

    def __init__(self, store: MemoryStore) -> None:
        super().__init__()
        self.store = store

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
        from cerebro.curate import carried_out

        if agent.id not in self.calls:
            self.calls[agent.id] = [
                ("admit", {"name": p.payload.get("name") or p.payload.get("title"), "keys": p.payload.get("keys") or {},
                           "reason": "accepted", "proposals": [p.id]}) for p in proposals(self.store, "accepted")
                if p.kind == "article.admit" and not carried_out(p) and p.target.startswith(agent.target + "/")]
        return super().run(agent, evidence, tools, budget)


def curate(store: MemoryStore, agent: Agent, calls: list[tuple[str, dict[str, Any]]],
           sources: tuple[str, ...] = ("crm",)) -> tuple[Curated, list[str]]:
    prompts: list[str] = []
    run = ScriptedRun(lambda prompt: prompts.append(prompt) or {}, calls)
    root = agent.path.parent.parent.parent
    done = run_curator(agent, lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW, root=root), store=store,
                       agent_run=run, root=root, functions={}, agent_tools={},
                       clock=lambda: NOW, sources={name: FakeConnector(name, []) for name in sources})
    return done, prompts


def replies(done: Curated) -> list[Any]:
    assert done.run is not None
    return [e.text for e in done.run.transcript if e.role == "tool"]


def listed(store: MemoryStore, agent: Agent, sources: tuple[str, ...] = ("crm",)) -> list[dict[str, Any]]:
    """The held list's items as the curator reads them, each line parsed back to its data."""
    entry = gather(agent, StoreLookup(store, clock=lambda: NOW), {}, sources={s: FakeConnector(s, []) for s in sources},
                   now=NOW)
    held = next(e for e in entry if e.spelled.startswith("held for"))
    return [json.loads(item.text.removeprefix("- ")) for item in held.items]


# -- the held list --------------------------------------------------------------------------------------------------


def test_the_held_list_gives_each_record_as_data_with_no_body_newest_first(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    hold(store, deal("006A", "Alder renewal", "2026-11-10T09:00:00Z"),
         deal("006C", "Birch Yard pilot", "2026-11-14T09:00:00Z"),
         deal("006B", "Initech expansion", "2026-11-12T09:00:00Z"), contact("003P", "Pat Quill", "pat@alder.example"))
    hold(store, replace(deal("006D", "Hooli seats", "2026-11-12T09:00:00Z"), source="sheet"), source="sheet")

    items = listed(store, curator(agents, "opportunities"), sources=("crm", "sheet"))
    assert [(i["source"], i["id"]) for i in items] == [("crm", "006C"), ("crm", "006B"), ("sheet", "006D"),
                                                       ("crm", "006A")]  # newest first; one time apart, by source
    assert all(set(i) == {"source", "id", "name", "keys", "at", "listed_at"} for i in items)  # no body, no field unnamed
    assert items[0] == {"source": "crm", "id": "006C", "name": "Birch Yard pilot", "at": "2026-11-14T09:00:00Z",
                        "keys": {"salesforce_id": ["006C"], "name": ["Birch Yard pilot"]}, "listed_at": ""}
    people = listed(store, curator(agents, "people"))
    assert [(i["id"], i["name"]) for i in people] == [("003P", "Pat Quill")]  # each collection its own records

    _, prompts = curate(store, curator(agents, "opportunities"), [])
    assert "Birch Yard pilot" in prompts[0] and SECRET not in prompts[0]


def test_a_held_line_names_its_record_first_and_gives_the_fields_it_names_last_with_a_value(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    (agents / "things" / "curator.md").write_text(CURATOR.format(authority=ALL, extra="", prompt="Keep things.").replace(
        "held: {}", "held: { fields: [status, due, owner, parts, more, notes] }"))
    wide = Record(id="t1", source="files", revision="1", at="2026-11-12T09:00:00Z", into="things",
                  keys={"name": ["Thing one"], "email": [f"part{n:02d}@things.example" for n in range(80)]})
    named = Record(id="t2", source="files", revision="1", at="2026-11-11T09:00:00Z", keys={"name": ["Thing two"]},
                    fields={"status": "deferred", "due": "", "owner": None, "parts": [], "more": {}, "stage": "open"},
                    into="things")
    long = Record(id="t3", source="files", revision="1", at="2026-11-10T09:00:00Z", keys={"name": ["Thing three"]},
                  fields={"notes": "a word " * 400}, into="things")
    hold(store, wide, named, long, source="files")
    entry = gather(curator(agents, "things"), StoreLookup(store, clock=lambda: NOW), {},
                   sources={"files": FakeConnector("files", [])}, now=NOW)
    cut, short, fields_cut = next(e for e in entry if e.spelled == "held for things").items
    assert cut.text.endswith("characters)")  # cut, and what admit and leave_out name it by is still there
    assert all(said in cut.text for said in ('"source": "files"', '"id": "t1"', '"name": "Thing one"'))
    assert json.loads(short.text.removeprefix("- "))["fields"] == {"status": "deferred"}
    assert fields_cut.text.endswith("characters)") and '"keys": {"name": ["Thing three"]}' in fields_cut.text


def test_the_held_list_is_bounded_and_counts_the_rest(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    hold(store, *[deal(f"006{n:04d}", f"Deal {n}", f"2026-11-{1 + n % 28:02d}T{n % 24:02d}:00:00Z")
                  for n in range(LIST_MAX + 3)])
    found = held_for(store, "opportunities", ["crm"])
    assert len(found.items) == LIST_MAX and found.total == LIST_MAX + 3
    assert [h.at for h in found.items] == sorted((h.at for h in found.items), reverse=True)
    entry = gather(curator(agents, "opportunities"), StoreLookup(store, clock=lambda: NOW), {},
                   sources={"crm": FakeConnector("crm", [])}, now=NOW)
    held = next(e for e in entry if e.spelled == "held for opportunities")
    assert len(held.items) == LIST_MAX
    assert any(note.startswith("3 more held records are not listed") for note in held.notes)
    assert len(held_for(store, "opportunities", ["crm"], limit=2).items) == 2
    (agents / "opportunities" / "curator.md").write_text("---\nevidence:\n  - held: { limit: 2 }\n---\nx\n")
    cut = next(e for e in gather(curator(agents, "opportunities"), StoreLookup(store, clock=lambda: NOW), {},
                                 sources={"crm": FakeConnector("crm", [])}, now=NOW) if e.spelled.startswith("held"))
    assert len(cut.items) == 2 and any(n.startswith(f"{LIST_MAX + 1} more held records") for n in cut.notes)


def test_a_short_list_says_nothing_of_more_and_an_empty_one_says_it_is_empty(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    lookup, crm = StoreLookup(store, clock=lambda: NOW), {"crm": FakeConnector("crm", [])}
    empty = next(e for e in gather(curator(agents, "people"), lookup, {}, sources=crm, now=NOW)
                 if e.spelled.startswith("held"))
    assert empty.items == [] and empty.notes == ["no record is held for people"]
    hold(store, Record(id="006N", source="crm", revision="1", at="2026-11-11T09:00:00Z", keys={"salesforce_id":
                                                                                              ["006N"]},
                       into="opportunities"))
    short = next(e for e in gather(curator(agents, "opportunities"), lookup, {}, sources=crm, now=NOW)
                 if e.spelled.startswith("held"))
    assert short.notes == [] and json.loads(short.items[0].text[2:])["name"] == ""  # a record with no name gives none


def test_a_record_bound_only_to_an_archived_article_is_held_for_that_articles_collection(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    store.create(Identity("opportunities/alder-renewal", "opportunities", "Alder renewal",
                          {"salesforce_id": ["006A"]}, "archived"))
    hold(store, Record(id="00TA", source="crm", revision="1", at="2026-11-11T09:00:00Z",
                       keys={"salesforce_id": ["006A"]}), Record(id="00TB", source="crm", revision="1"))
    assert [i["id"] for i in listed(store, curator(agents, "opportunities"))] == ["00TA"]
    assert listed(store, curator(agents, "things")) == []  # nor the one bound to nothing, which lands nowhere


def test_the_curator_agents_new_writes_reads_the_records_held_for_its_collection(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    made = scaffold.new_agent(tmp_path, "things/curator")
    store = MemoryStore()
    hold(store, Record(id="T1", source="crm", revision="1", keys={"name": ["Thing one"]}, into="things"))
    entry = gather(load_agent(tmp_path / made.path, tmp_path / "agents"), StoreLookup(store, FakeExtraction()), {},
                   sources={"crm": FakeConnector("crm", [])}, now=NOW)
    shown = [json.loads(item.text.removeprefix("- ")) for e in entry for item in e.items]  # each line as it reads
    assert [(line["source"], line["id"], line["name"]) for line in shown] == [("crm", "T1", "Thing one")]


def test_a_held_line_gathers_for_a_curator_alone_and_through_a_lookup_with_a_store(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    people = curator(agents, "people")
    with pytest.raises(EvidenceError, match="agents/people/curator.md: evidence 1, held: the lookup it was gathered "
                                            "with holds no store"):
        held_entry(people, None, ["crm"], 1, people.evidence[0])
    with pytest.raises(EvidenceError, match="agents/people/article.md: evidence 1, held: it lists the records held "
                                            "for a curator's collection, and this agent curates none"):
        held_entry(replace(people, id="people/article", role="article"), store, ["crm"], 1, people.evidence[0])


def test_a_held_record_leaves_the_list_the_moment_it_is_admitted(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    hold(store, deal("006A", "Alder renewal", "2026-11-10T09:00:00Z"), deal("006B", "Initech expansion",
                                                                          "2026-11-12T09:00:00Z"))
    opportunities = curator(agents, "opportunities")
    done, _ = curate(store, opportunities, [("admit", {"name": "Alder renewal", "reason": "an open deal you own",
                                                       "keys": {"salesforce_id": ["006A"]}})])
    assert done.admitted == ["opportunities/alder-renewal"]
    assert [i["id"] for i in listed(store, opportunities)] == ["006B"]  # before any pass prunes the state


def test_a_held_record_leaves_the_list_when_its_source_removes_it(tmp_path: Path) -> None:
    agents = write_workspace(tmp_path)
    crm = FakeConnector("crm", [deal("006A", "Alder renewal", "2026-11-10T09:00:00Z"),
                                deal("006B", "Initech expansion", "2026-11-12T09:00:00Z")])
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, connectors=[crm],
                    agent_run=PerAgent())
    workspace.run_once()
    assert [i["id"] for i in listed(workspace.store, curator(agents, "opportunities"))] == ["006B", "006A"]
    crm.remove("006A")
    workspace.run_once()
    assert [i["id"] for i in listed(workspace.store, curator(agents, "opportunities"))] == ["006B"]


def test_a_domain_is_ours_only_whole_or_as_a_host_under_it_and_in_any_case() -> None:
    from cerebro.connector import Ours
    from cerebro.held import own_side

    ours = Ours(domains=("alder.example",))
    for domain, mine in (("alder.example", True), ("eu.alder.example", True), ("ALDER.example", True),
                         ("bigalder.example", False), ("alder.example.example", False)):
        assert own_side({"domain": [domain]}, ours) is mine, domain


def test_a_new_held_record_makes_its_curator_due_through_the_digest(tmp_path: Path) -> None:
    agents = write_workspace(tmp_path)
    crm = FakeConnector("crm", [deal("006A", "Alder renewal", "2026-11-10T09:00:00Z")])
    run = PerAgent()
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, connectors=[crm],
                    agent_run=run)
    workspace.run_once()  # lists and holds the deal after its curators ran
    second = workspace.run_once()
    assert "opportunities/curator" in second.due and len(run.prompts["opportunities/curator"]) == 2
    assert "Alder renewal" in run.prompts["opportunities/curator"][-1]
    quiet = workspace.run_once()
    assert quiet.skipped["opportunities/curator"] == "nothing it reads has moved since its last run"

    crm.add(deal("006B", "Initech expansion", "2026-11-12T09:00:00Z"))
    workspace.run_once()  # holds the new deal
    woken = workspace.run_once()
    assert "opportunities/curator" in woken.due and "Initech expansion" in run.prompts["opportunities/curator"][-1]
    assert woken.skipped.get("things/curator") == "nothing it reads has moved since its last run"

    def held_digest(revision: str) -> str:
        """The curator's digest with the second deal held at `revision`, all else the same."""
        revised = deal("006B", "Initech expansion", "2026-11-12T09:00:00Z")
        hold(workspace.store, deal("006A", "Alder renewal", "2026-11-10T09:00:00Z"),
             Record(**{**asdict(revised), "revision": revision}))
        return digest(curator(agents, "opportunities"), gather(curator(agents, "opportunities"), workspace.lookup, {},
                                                               sources={"crm": crm}, now=NOW))

    assert held_digest("1") == held_digest("1") != held_digest("2")  # a new revision of a held record is a change too


FACTS_AND_HELD = ("---\n{authority}evidence:\n  - query: {{ from: facts, window: last_90_days }}\n"
                  "  - held: {{}}\nheartbeat: [change]\n---\n\nKeep things.\n")


def a_part(id: str, name: str, key: str = "") -> Record:
    return Record(id=id, source="crm", revision="1", at="2026-11-10T09:00:00Z", keys={"part_id": [key or id]},
                  fields={"name": name, "colour": "blue"}, body=f"{name} is a part.", into="things")


QUIET = "nothing it reads has moved since its last run"
WAITS = "authority: { admit: false, archive: false, merge: false, split: false }\n"
OWN = "---\n{authority}evidence:\n  - query: {{ from: things }}\n  - held: {{}}\nheartbeat: [change]\n---\n\nKeep."


def acted(root: Path, file: str, crm: FakeConnector, *ids: str, run: PerAgent | None = None) -> tuple[Cerebro, PerAgent]:
    """A workspace whose things curator, on `file`, admitted the held records `ids` in its one run."""
    (write_workspace(root) / "things" / "curator.md").write_text(file)
    run = run or PerAgent()
    workspace = Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, connectors=[crm],
                        agent_run=run)
    workspace.run_once(agents=False)
    run.calls["things/curator"] = [("admit", {"record": {"source": "crm", "id": id}, "reason": "a part"}) for id in ids]
    workspace.run_once()
    return workspace, run


@pytest.mark.parametrize(("landing", "wakes"), [(None, False), ("late", False), ("reword", True), ("take back", True),
                                                (a_part("p1-note", "Alder note", key="p1"), True),
                                                (a_part("p3", "Cedar"), True)])
def test_what_a_curators_own_admit_sets_in_motion_never_wakes_it_and_a_record_landing_beside_it_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landing: Record | str | None, wakes: bool) -> None:
    agents = write_workspace(tmp_path)
    (agents / "things" / "curator.md").write_text(FACTS_AND_HELD.format(authority=ALL))
    note = Record(id="n1", source="crm", revision="1", at="2026-11-09T09:00:00Z", keys={}, fields={"subject": "Alder"},
                  body="Alder shipped on time.")  # a name no article claims until the admit
    crm, run, extraction = FakeConnector("crm", [a_part("p1", "Alder"), a_part("p2", "Birch"), note]), PerAgent(), \
        FakeExtraction()
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=extraction, clock=lambda: NOW, connectors=[crm],
                        agent_run=run)
    workspace.run_once(agents=False)  # lists and holds both parts, and reads the note
    if isinstance(landing, Record):  # listed in the same pass as the curator's admit
        crm.add(landing)
    extract = extraction.extract

    def behind(record: Record, *args: Any, **kwargs: Any) -> Any:  # Alder's record waits until its next run is over
        if record.id == "p1":
            raise RuntimeError("extraction is behind")
        return extract(record, *args, **kwargs)

    if landing == "late":
        monkeypatch.setattr(extraction, "extract", behind)
    run.calls["things/curator"] = [("admit", {"record": {"source": "crm", "id": "p1"}, "reason": "a part to keep"})]
    workspace.run_once()
    if landing == "late":
        crm.add(a_part("p3", "Cedar"))
        workspace.run_once()
        workspace.run_once()  # its run on Cedar
        monkeypatch.undo()
        workspace.run_once(agents=False)
    assert [a.identity.id for a in workspace.store.query("things")] == ["things/alder"]
    assert any(f.record == "p1" for f in extraction.facts())  # its fields and text, now in its window
    ran, alder = len(run.prompts["things/curator"]), next(f for f in extraction.facts() if f.record == "p1")
    if landing == "reword":
        extraction.by_record = {k: [replace(f, text="Alder is red now.") if f.id == alder.id else f for f in found]
                                for k, found in extraction.by_record.items()}
    elif landing == "take back":
        extraction.retract(alder.id)
    after = workspace.run_once()
    assert len(run.prompts["things/curator"]) == ran + wakes
    if not wakes:
        assert after.skipped["things/curator"] == QUIET == workspace.run_once().skipped["things/curator"]
        assert not [r for r in preview.stale(workspace).rows if r.agent == "things/curator"]  # the dry run agrees


MERGES = ("---\n" + ALL + "evidence:\n  - articles: [things]\n  - held: {}\nheartbeat: [change]\n"
          "---\n\nKeep things.\n")
KEYS_AND_FACTS = ("---\n" + ALL + "evidence:\n  - query: { from: facts, window: last_90_days }\n"
                  "  - query: { from: things }\n  - held: {}\nheartbeat: [change]\n---\n\nKeep things.\n")
OPS = Record(id="n2", source="crm", revision="1", at="2026-11-10T09:00:00Z", keys={"email": ["ops@alder.example"]},
             body="Ops at Alder asked for a quote.")  # read with no article, until a link gives Alder its address


@pytest.mark.parametrize(("file", "records", "then"), [
    (FACTS_AND_HELD.format(authority=ALL), [a_part("p1", "Alder")], None),
    (OWN.format(authority=ALL), [a_part("p1", "Alder"), a_part("p2", "B")], None),
    (MERGES, [a_part("p1", "Alder"), a_part("p2", "Birch")],  # its facts are tied to the article that survives
     ("merge", {"article": "Alder", "other": "Birch", "reason": "one part"})),
    (KEYS_AND_FACTS, [a_part("p1", "Alder"), OPS],  # its key shows, and brings n2's fact to Alder on the next pass
     ("link", {"article": "Alder", "reason": "its ops address", "keys": {"email": ["ops@alder.example"]}}))])
def test_a_curators_own_act_never_wakes_it(tmp_path: Path, file: str, records: list[Record],
                                           then: tuple[str, dict[str, Any]] | None) -> None:
    workspace, run = acted(tmp_path, file, FakeConnector("crm", records), *(r.id for r in records if r.into))
    if then is not None:
        assert workspace.run_once().skipped["things/curator"] == QUIET
        run.calls["things/curator"] = [then]
        workspace.curate(next(a for a in workspace.agents if a.id == "things/curator"))
        assert "refused" not in run.replies["things/curator"][-1], run.replies["things/curator"][-1]
    assert [workspace.run_once().skipped.get("things/curator") for _ in range(2)] == [QUIET, QUIET]


def test_a_callers_no_on_a_curators_proposal_never_wakes_it(tmp_path: Path) -> None:
    workspace, _ = acted(tmp_path, FACTS_AND_HELD.format(authority=WAITS), FakeConnector("crm", [a_part("p1", "A")]), "p1")
    assert workspace.run_once().skipped["things/curator"] == QUIET
    workspace.decide(Caller("me", "person"), reject=[Decision(p.id, "not this one")
                                                     for p in proposals(workspace.store, "waiting")])
    assert [workspace.run_once().skipped.get("things/curator") for _ in range(2)] == [QUIET, QUIET]


def test_news_listed_before_a_decision_on_a_curators_proposal_still_wakes_it(tmp_path: Path) -> None:
    crm = FakeConnector("crm", [a_part("p1", "Alder")])
    workspace, _ = acted(tmp_path, FACTS_AND_HELD.format(authority=WAITS), crm, "p1")
    crm.add(a_part("p2", "Birch"))
    assert workspace.run_once().skipped["things/curator"] == QUIET  # Birch is held after the curators' turn
    workspace.decide(Caller("me", "person"), accept=[p.id for p in proposals(workspace.store, "waiting")])
    assert "things/curator" in workspace.run_once().due


def test_a_decision_not_all_one_curators_own_is_news_to_each_curator(tmp_path: Path) -> None:
    write_workspace(tmp_path, things=False, people=False)  # the people curator reads things, never their proposals
    run = PerAgent()
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run,
                        connectors=[FakeConnector("crm", [a_part("p1", "Alder"), contact("c1", "Pat", "pat@q.example")])])
    workspace.run_once(agents=False)
    run.calls = {"things/curator": [("admit", {"record": {"source": "crm", "id": "p1"}, "reason": "a part"})],
                 "people/curator": [("admit", {"record": {"source": "crm", "id": "c1"}, "reason": "a person"})]}
    workspace.run_once()
    workspace.decide(Caller("me", "person"), accept=[p.id for p in proposals(workspace.store, "waiting")])
    assert "people/curator" in workspace.run_once().due


def test_a_first_runs_approval_of_a_curators_last_held_record_never_wakes_it(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    store = MemoryStore()
    run = Carries(store)
    workspace = Cerebro(tmp_path, store=store, extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run,
                        connectors=[FakeConnector("crm", [a_part("p1", "Alder")])])
    workspace.asker = FakeRewriter(lambda data: every_item_ticked(json.dumps(data)))
    run.calls["things/curator"] = [("admit", {"record": {"source": "crm", "id": "p1"}, "reason": "a part"})]
    first_run(workspace, choose=Choices.starter, say=lambda line: None)
    assert [a.identity.id for a in workspace.store.query("things")] == ["things/alder"]
    assert [workspace.run_once().skipped.get("things/curator") for _ in range(2)] == [QUIET, QUIET]


@pytest.mark.parametrize("how", ["new fact", "fact gone", "fact gone in a merge", "fact's words", "words in a merge",
                                 "words between acts", "name", "file", "agent file"])
def test_what_changes_outside_the_store_during_a_curators_act_still_wakes_it(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch,
                                                                           how: str) -> None:
    facts = "{ from: facts, record: p1 }" if how == "name" else "{ from: facts }"  # the name alone shows its fact
    merging = how in ("fact gone in a merge", "words in a merge", "words between acts")
    file = ("---\n" + ALL + f"evidence:\n  - files: [notes/plan.md]\n  - query: {facts}\n  - held: {{}}\n"
            "heartbeat: [change]\n---\n\nKeep things.\n")
    agents = write_workspace(tmp_path)
    (agents / "things" / "curator.md").write_text(file)
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "plan.md").write_text("Keep every blue part.\n")
    note = Record(id="n1", source="crm", revision="1", at="2026-11-09T09:00:00Z", keys={}, fields={"subject": "Birch"},
                  body="Birch shipped on time.")
    extraction, run = FakeExtraction(), PerAgent()
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=extraction, clock=lambda: NOW, agent_run=run,
                        connectors=[FakeConnector("crm", [a_part("p1", "Alder"), a_part("p2", "Birch"),
                                                                  a_part("p3", "Cedar"), note])])
    workspace.run_once(agents=False)
    module = sys.modules["cerebro.curate"]  # the package's `curate` names a function
    admits = [("admit", {"record": {"source": "crm", "id": id}, "reason": "a part"}) for id in ("p1", "p2", "p3")]
    target, act, calls = module, "apply", admits[:1]
    if merging:  # the merge ties Birch's facts to Alder too, so their store side moves
        run.calls["things/curator"] = admits[:2]
        workspace.run_once()
        act, calls = "_merge", [("merge", {"article": "Alder", "other": "Birch", "reason": "one part"})]
    if how == "words between acts":  # after the admit's write, before the merge's
        target, act, calls = module._Measure, "__call__", admits[2:] + calls
    changes = {"new fact": lambda: extraction.extract(replace(note, id="n2", fields={"subject": "Cedar"}, body="Cedar.")),
               "fact gone": lambda: extraction.retract(extraction.facts(record="n1")[0].id),
               "fact's words": lambda: extraction.by_record.update(
                   {key: [replace(f, text="Birch shipped late.") for f in found]
                    for key, found in extraction.by_record.items() if any(f.record == "n1" for f in found)}),
               "file": lambda: (tmp_path / "notes" / "plan.md").write_text("Keep every red part.\n"),
               "agent file": lambda: (agents / "things" / "curator.md").write_text(file.replace("Keep things.", "Keep."))}
    changes["name"] = changes["fact gone in a merge"] = changes["fact gone"]
    changes["words in a merge"] = changes["words between acts"] = changes["fact's words"]
    land = getattr(target, act)

    def meanwhile(*args: Any, **kwargs: Any) -> Any:  # a change outside the store's write, during the act
        out = land(*args, **kwargs)
        changes[how]()
        return out

    monkeypatch.setattr(target, act, meanwhile)
    run.calls["things/curator"] = calls
    if act == "apply":
        workspace.run_once()
    else:
        workspace.curate(next(a for a in workspace.agents if a.id == "things/curator"))
    monkeypatch.undo()
    assert "things/curator" in workspace.run_once().due


def a_writer_sets(workspace: Cerebro, article: str, value: str) -> None:
    """A field set on an article in one write, as a writer in another process sets it."""
    with workspace.store.exclusive():
        workspace.store.set_field(article, "stage", Property(value, Caller("things/article", "agent"), "2026-11-17T09:00:00Z"))


def test_a_writers_change_while_a_curator_runs_still_wakes_it(tmp_path: Path) -> None:
    run = Meanwhile()
    workspace, _ = acted(tmp_path, OWN.format(authority=ALL), FakeConnector("crm", [a_part("p1", "A"), a_part("p2", "B")]),
                         "p1", run=run)
    run.during = lambda: a_writer_sets(workspace, "things/a", "Won")
    run.calls["things/curator"] = [("admit", {"record": {"source": "crm", "id": "p2"}, "reason": "a part"})]
    workspace.connectors["crm"].add(a_part("p3", "C"))  # news, so the curator runs again
    workspace.run_once()
    workspace.run_once()
    assert "things/curator" in workspace.run_once().due


@pytest.mark.parametrize("answer", ["reject", "own act"])
def test_a_curators_answers_wake_neither_it_nor_the_proposer(tmp_path: Path, answer: str) -> None:
    write_workspace(tmp_path)
    (tmp_path / "agents" / "people" / "curator.md").write_text(CURATOR.format(  # it reads no things: no news there
        authority=ALL, extra="", prompt="Keep the people."))
    run = PerAgent()
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run,
                        connectors=[FakeConnector("crm", [a_part("p1", "Alder"), contact("c1", "Pat", "pat@q.example")])])
    workspace.run_once(agents=False)
    run.calls["people/curator"] = [("admit", {"name": "Alder", "collection": "things", "reason": "a thing",
                                              "record": {"source": "crm", "id": "p1"}})]
    workspace.run_once()
    [asked] = proposals(workspace.store, "waiting")
    run.calls["things/curator"] = [("reject", {"proposals": [{"id": asked.id, "reason": "not a thing we keep"}]})
                                   if answer == "reject" else
                                   ("admit", {"record": {"source": "crm", "id": "p1"}, "reason": "a thing we keep",
                                              "proposals": [asked.id]})]
    workspace.curate(workspace.agent("things/curator"))
    assert next(p for p in proposals(workspace.store) if p.id == asked.id).status in ("rejected", "accepted")
    skipped = workspace.run_once().skipped
    assert (skipped["things/curator"], skipped["people/curator"]) == (QUIET, QUIET)


class Meanwhile(PerAgent):
    """Calls `during` once, while the things curator's run is under way."""

    during: Any = None

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
        out = super().run(agent, evidence, tools, budget)
        if agent.id == "things/curator" and self.during is not None:
            self.during, during = None, self.during
            during()
        return out


def test_a_callers_admit_during_a_curators_run_wakes_it_next_pass(tmp_path: Path) -> None:
    (write_workspace(tmp_path) / "things" / "curator.md").write_text(OWN.format(authority=ALL))
    run = Meanwhile()
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run,
                        connectors=[FakeConnector("crm", [a_part("p1", "Alder"), a_part("p3", "Cedar")])])
    workspace.run_once(agents=False)
    run.during = lambda: list(cli._admit_one(workspace, "things/Cedar"))
    run.calls["things/curator"] = [("admit", {"record": {"source": "crm", "id": "p1"}, "reason": "a part"})]
    workspace.run_once()
    assert "things/curator" in workspace.run_once().due


def test_the_opportunities_curator_admits_two_of_three_held_deals_and_the_third_is_held_next_run(
        tmp_path: Path) -> None:
    write_workspace(tmp_path)
    crm = FakeConnector("crm", [deal("006A", "Alder renewal", "2026-11-10T09:00:00Z"),
                                deal("006B", "Initech expansion", "2026-11-12T09:00:00Z"),
                                deal("006C", "Birch Yard pilot", "2026-11-14T09:00:00Z")])
    run = PerAgent()
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, connectors=[crm],
                    agent_run=run)
    workspace.run_once(agents=False)  # lists and holds the deals; a pass in which the curator ran would make it late
    run.calls["opportunities/curator"] = [
        ("admit", {"name": name, "reason": "an open deal closing in the next 90 days", "keys": {"salesforce_id": [id]}})
        for id, name in (("006C", "Birch Yard pilot"), ("006A", "Alder renewal"))]
    admitted = workspace.run_once()
    shown = run.prompts["opportunities/curator"][-1]
    assert all(name in shown for name in ("Alder renewal", "Initech expansion", "Birch Yard pilot"))
    assert shown.index("Birch Yard pilot") < shown.index("Initech expansion") < shown.index("Alder renewal")
    assert "opportunities/curator" in admitted.due and SECRET not in shown
    assert {a.identity.id for a in workspace.store.query("opportunities")} == {"opportunities/birch-yard-pilot",
                                                                           "opportunities/alder-renewal"}

    crm.add(deal("006D", "Hooli seats", "2026-11-15T09:00:00Z"))
    workspace.run_once()
    workspace.run_once()
    again = run.prompts["opportunities/curator"][-1]
    assert "Initech expansion" in again and "Hooli seats" in again
    assert "Birch Yard pilot" not in again and "Alder renewal" not in again
    assert [r["id"] for r in workspace.store.state("held:crm")] == ["006B", "006D"]


def test_a_held_line_loads_on_a_curator_alone_and_takes_a_limit_and_fields_alone(tmp_path: Path) -> None:
    agents = write_workspace(tmp_path)
    assert curator(agents, "people").evidence[0].args == {}
    (agents / "people" / "curator.md").write_text("---\nevidence:\n  - held: { limit: 50 }\n---\nx\n")
    assert curator(agents, "people").evidence[0].args == {"limit": 50}
    for line, loads in (("", {}), ("{ limit: 1 }", {"limit": 1}), (f"{{ limit: {LIST_MAX} }}", {"limit": LIST_MAX}),
                        ("{ fields: [status, due] }", {"fields": ["status", "due"]})):
        (agents / "people" / "curator.md").write_text(f"---\nevidence:\n  - held: {line}\n---\nx\n")
        assert curator(agents, "people").evidence[0].args == loads
    for line, problem in (("{ limit: 0 }", "limit 0 is not a whole number from 1 to 500"),
                          ("{ limit: 501 }", "limit 501 is not"), ("{ limit: true }", "limit True is not"),
                          ("{ limit: '20' }", "limit '20' is not"),
                          ("{ fields: status }", "fields 'status' is not a list of field names"),
                          ("{ fields: [1] }", "fields \\[1\\] is not a list of field names"),
                          ("{ from: crm }", "line 3: evidence 1, held: it takes limit and fields, or nothing"),
                          ("[crm]", "it takes limit and fields, or nothing")):
        (agents / "people" / "curator.md").write_text(f"---\nevidence:\n  - held: {line}\n---\nx\n")
        said = problem if problem.startswith("line") else f"line 3: evidence 1, held: {problem}"
        with pytest.raises(AgentFileError, match=f"agents/people/curator.md: {said}"):
            curator(agents, "people")
    (agents / "people" / "article.md").write_text("---\nevidence:\n  - held: {}\n---\n\n# Output\n\n## Overview\n")
    with pytest.raises(AgentFileError, match="agents/people/article.md: line 3: evidence 1, held: it lists the records held "
                                             "for a curator's collection, and this file is no curator.md: move the "
                                             "line to the collection's curator.md, or delete it"):
        load_agent(agents / "people" / "article.md", agents)


# -- admitting into another collection ------------------------------------------------------------------------------


def a_thing(store: MemoryStore) -> None:
    store.create(Identity("things/alder", "things", "Alder", {"domain": ["alder.example"]}, "active"))


def test_a_record_admitted_under_an_articles_name_is_refused_unless_their_event_ids_differ(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()

    def meeting(id: str, keys: dict[str, list[str]]) -> Record:
        return Record(id=id, source="crm", revision="1", at="2026-11-03T09:00:00Z", keys=keys,
                      fields={"name": "Weekly sync"}, into="things")

    weeks = [meeting(f"evt-1#{day}", {"calendar_event_id": [f"evt-1#{day}"]}) for day in ("20261103", "20261110")]
    early = meeting("rec-2", {"calendar_event_id": ["evt-1#20261102"]})  # its key took the day before, in UTC
    hold(store, *weeks, meeting("rec-1", {}), early)
    things = curator(agents, "things")
    said = [replies(curate(store, things, [("admit", {"record": {"source": "crm", "id": id}, "name": "Weekly sync",
                                                      "reason": "a meeting"})])[0])[0]
            for id in ("evt-1#20261103", "rec-1", "rec-2", "evt-1#20261110")]
    assert all("things/weekly-sync is in the working set already" in said[n] for n in (1, 2)), said
    assert {a.identity.id: a.identity.keys.get("calendar_event_id") for a in store.query("things")} == {
        "things/weekly-sync": ["evt-1#20261103"], "things/weekly-sync-2": ["evt-1#20261110"]}


def test_the_people_curator_reading_an_account_admits_a_person_into_people(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    a_thing(store)
    hold(store, contact("003P", "Pat Quill", "pat@alder.example"))
    people = curator(agents, "people")
    done, prompts = curate(store, people, [("admit", {
        "name": "Pat Quill", "collection": "people", "keys": {"email": ["pat@alder.example"]},
        "reason": "Director of Operations at Alder, the lead on its project"})])
    assert "Alder" in prompts[0] and "Pat Quill" in prompts[0]  # it read the thing and the held contact
    assert done.admitted == ["people/pat-quill"]
    assert store.article("people/pat-quill").identity.keys == {"email": ["pat@alder.example"]}
    assert listed(store, people) == []


def test_a_curator_without_authority_may_reject_another_curators_proposal_and_wakes_for_it(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose

    write_workspace(tmp_path, people=False)
    run = PerAgent()
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run,
                        connectors=[FakeConnector("crm", [a_part("p1", "Birch"), contact("c1", "Pat", "pat@q.example")])])
    workspace.run_once(agents=False)
    workspace.run_once()
    for curator_id in ("things/curator", "people/curator", "opportunities/curator"):  # each out of its own first run
        propose(workspace.store, Proposal(f"p_{curator_id[:5]}", "article.archive", "x/y", Caller(curator_id, "agent"),
                                          NOW.isoformat(), status="rejected", decided_by=YOU, decided_at=NOW.isoformat()))
    workspace.run_once()
    run.calls["opportunities/curator"] = [
        ("admit", {"name": "Alder", "collection": "things", "reason": "a thing", "keys": {"domain": ["alder.example"]}}),
        ("admit", {"name": "Pat Quill", "collection": "people", "reason": "a lead", "keys": {"email": ["p@q.example"]}})]
    workspace.curate(workspace.agent("opportunities/curator"))
    passed = workspace.run_once()
    assert "things/curator" not in passed.skipped and "people/curator" not in passed.skipped  # people holds admit false
    assert "Pat Quill" in run.prompts["people/curator"][-1]
    pat = next(p for p in proposals(workspace.store, "waiting") if p.target.startswith("people/"))
    curate(workspace.store, workspace.agent("people/curator"), [
        ("reject", {"proposals": [{"id": pat.id, "reason": "not one of ours"}]})])
    assert next(p for p in proposals(workspace.store) if p.id == pat.id).status == "rejected"


def test_a_keep_apart_is_filed_under_merge_authority_and_a_no_to_it_keeps_nothing_apart(tmp_path: Path) -> None:
    from cerebro.agent import Authority
    from cerebro.records import review

    agents, store = write_workspace(tmp_path), MemoryStore()
    for name in ("Bolt", "Nut", "Washer", "Pin"):
        store.create(Identity(f"things/{name.lower()}", "things", name, {}, "active"))
    things = curator(agents, "things")
    curate(store, things, [("merge", {"article": "Bolt", "other": "Washer", "reason": "two parts", "distinct": True})])
    assert [r.data["distinct_from"] for r in store.rows("merges") if "distinct_from" in r.data] == [
        ["things/bolt", "things/washer"]]
    asks = replace(things, authority=Authority(merge=False))
    done, _ = curate(store, asks, [("merge", {"article": "Bolt", "other": "Nut", "reason": "two", "distinct": True})])
    [row] = proposals(store, "waiting")
    assert "'note': 'waits for a caller, in p_" in replies(done)[0]
    assert row.payload == {"with": "things/nut", "distinct": True}
    _, prompts = curate(store, asks, [])
    assert f"- {row.id} keep things/bolt apart from things/nut" in prompts[0]
    assert "kept apart" in review(store, YOU)[0].change
    decide(store, YOU, reject=[Decision(row.id, "they are one part")], clock=lambda: NOW)
    assert len([r for r in store.rows("merges") if "distinct_from" in r.data]) == 1
    merged, _ = curate(store, asks, [("merge", {"article": "Bolt", "other": "Nut", "reason": "one part"})])
    assert "'proposed'" in replies(merged)[0]  # the no to keeping them apart is no no to merging them
    decide(store, YOU, reject=[p.id for p in proposals(store, "waiting")], clock=lambda: NOW)
    curate(store, asks, [("merge", {"article": "Bolt", "other": "Pin", "reason": "two", "distinct": True}),
                         ("merge", {"article": "Bolt", "other": "Pin", "reason": "one part"}),
                         ("merge", {"article": "Nut", "other": "Washer", "reason": "two", "distinct": True})])
    accepted = proposals(store, "waiting")
    decide(store, YOU, accept=[p.id for p in accepted], clock=lambda: NOW)
    curate(store, asks, [("merge", {"article": p.target, "other": p.payload["with"], "reason": "asked",
                                    "distinct": bool(p.payload.get("distinct")), "proposals": [p.id]}) for p in accepted])
    assert ["things/bolt", "things/pin"] in [r.data.get("distinct_from") for r in store.rows("merges")]
    assert "merged" in (store.article("things/pin").identity.status, store.article("things/bolt").identity.status)
    done, _ = curate(store, replace(things, authority=Authority(archive=True)), [
        ("archive", {"article": "Nut", "reason": "gone"})])  # a person's keep-apart is no merge of theirs
    assert "'archived'" in replies(done)[0]


def test_a_persons_no_to_a_link_bars_that_change_alone_and_review_says_what_a_link_changes(tmp_path: Path) -> None:
    from cerebro.records import review
    from cerebro.review import lines

    agents, store = write_workspace(tmp_path), MemoryStore()
    store.create(Identity("people/pat", "people", "Pat", {"code": ["B7"]}, "active"))
    things, people = curator(agents, "things"), curator(agents, "people")

    def link(by: Agent, keys: dict[str, Any], collection: str = "people") -> str:
        return replies(curate(store, by, [("link", {"article": "pat", "keys": keys, "reason": "its code",
                                                    "collection": collection})])[0])[0]

    link(things, {"code": None})
    [row] = proposals(store, "waiting")
    shown = lines(review(store, YOU)[0], 1, 1)
    assert "  change   people/pat: code taken off" in shown, shown
    decide(store, YOU, reject=[Decision(row.id, "B7 is its code")], clock=lambda: NOW)
    assert "rejected link of people/pat" in link(things, {"code": None})  # the change the person said no to
    assert "'proposed'" in link(things, {"code": ["B8"]})
    assert "'linked'" in link(people, {"code": ["B9"]}, "")


def test_a_proposal_waiting_elsewhere_leaves_its_curator_awake_to_news_there(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose

    write_workspace(tmp_path)  # the people curator reads things
    run = PerAgent()
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run,
                        connectors=[FakeConnector("crm", [a_part("p1", "Alder")])])
    workspace.run_once(agents=False)
    for curator_id in ("things/curator", "people/curator"):  # each out of its own first run
        propose(workspace.store, Proposal(f"p_{curator_id[:5]}", "article.archive", "x/y", Caller(curator_id, "agent"),
                                          NOW.isoformat(), status="rejected", decided_by=YOU, decided_at=NOW.isoformat()))
    run.calls["people/curator"] = [("admit", {"name": "Alder", "collection": "things", "reason": "a thing"})]
    workspace.run_once()
    workspace.run_once()
    run.calls["things/curator"] = [("admit", {"name": "Birch", "reason": "a thing we keep"})]
    workspace.curate(workspace.agent("things/curator"))
    assert "people/curator" not in workspace.run_once().skipped  # a new thing is news to it


def test_another_curators_split_is_on_the_list_of_a_curator_without_authority(tmp_path: Path) -> None:
    from cerebro.agent import Authority
    from cerebro.records import Proposal, propose

    agents, store = write_workspace(tmp_path), MemoryStore()
    propose(store, Proposal("p_split", "article.split", "people/ann", Caller("things/curator", "agent"),
                            NOW.isoformat(), payload={"from": "people/pat"}, reason="two people"))
    _, prompts = curate(store, replace(curator(agents, "people"), authority=Authority()), [])
    assert "- p_split split people/ann, by things/curator: two people" in prompts[0].split(
        "## Proposals waiting on your collection")[1]


def test_a_person_is_not_asked_about_a_proposal_its_collections_curator_holds_and_decides_it_by_id(
        tmp_path: Path) -> None:
    write_workspace(tmp_path)
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW,
                 agent_run=ScriptedRun(lambda p: {})) as workspace:
        done, _ = curate(workspace.store, workspace.agent("things/curator"), [("admit", {
            "name": "Pat Quill", "collection": "people", "keys": {"email": ["pat@alder.example"]},
            "reason": "the lead on the Alder project"})])
        [asked] = proposals(workspace.store, "waiting")
        assert "'note': 'waits for the curator of people, in p_" in replies(done)[0]
        assert workspace.status()["waiting"] == 0 and workspace.review() == []
        assert [r.proposal.id for r in workspace.review(Caller("people/curator", "agent"))] == [asked.id]
        assert workspace.decide(YOU, accept=[asked.id]).answers == {asked.id: "decided"}
    assert next(p.status for p in proposals(workspace.store) if p.id == asked.id) == "accepted"


@pytest.mark.parametrize("act", [
    ("admit", {"name": "Weekly"}), ("archive", {"article": "weekly"}), ("link", {"article": "weekly", "keys": {}}),
    ("merge", {"article": "weekly", "other": "daily"}), ("split", {"article": "weekly"})])
def test_an_act_on_another_collection_waits_there_and_a_collection_with_no_curator_refuses_it(
        tmp_path: Path, act: tuple[str, dict[str, Any]]) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    for id in ("reports/weekly", "reports/daily", "people/weekly", "people/daily"):
        store.create(Identity(id, id.split("/")[0], id.split("/")[1].capitalize(), {}, "active"))
    store.append("merges", {"at": NOW.isoformat(), "redirect": "people/weekly", "to": "people/daily"})
    name, args = act
    asked = {**args, "reason": "a report people read", "collection": "reports"}
    refused, _ = curate(store, curator(agents, "things"), [(name, asked)])
    assert replies(refused) == [refusal("articles/reports has no curator, so it takes no proposal: act in things, or "
                                        "name a collection a curator keeps")]
    if name == "split":
        store.set_identity(Identity("people/weekly", "people", "Weekly", {}, "merged"))
    elif name == "admit":
        asked = {**asked, "name": "Pat Quill"}
    done, _ = curate(store, curator(agents, "things"), [(name, {**asked, "collection": "people"})])
    [row] = proposals(store, "waiting")
    assert (row.kind, row.target.split("/")[0], row.reason) == (f"article.{name}", "people", "a report people read")
    assert "'note': 'waits for the curator of people, in p_" in replies(done)[0]
    noted, _ = curate(store, curator(agents, "things"), [("admit", {"name": "Ann", "collection": "people",
                                                                    "instructions": "look", "reason": "x"})])
    assert "a proposal for people takes none" in replies(noted)[0]


def test_an_admit_into_a_collection_the_workspace_does_not_have_is_refused_in_one_line(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    done, _ = curate(store, curator(agents, "things"), [
        ("admit", {"name": "Pat Quill", "collection": "contacts", "reason": "the lead on a project"}),
        ("admit", {"name": "Pat Quill", "collection": "../people", "reason": "the lead on a project"})])
    assert replies(done) == [
        refusal("articles/contacts is no collection of this workspace: name one it has, or leave collection out to act "
                "in things"),
        refusal("collection '../people' is not the name of a folder under articles/: give the name of another "
                "collection's folder, or leave collection out to act in things")]
    assert proposals(store) == []


# -- when a record was first held, deciding another curator's proposal at once, and declining it


def test_a_held_record_shows_its_own_time_and_when_a_pass_first_held_it(tmp_path: Path) -> None:
    agents = write_workspace(tmp_path)
    clock = [datetime(2026, 11, 17, 9, tzinfo=UTC)]
    renewal = replace(deal("006A", "Alder renewal", "2026-11-10T09:00:00Z"), body=None)
    crm = FakeConnector("crm", [renewal])
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), connectors=[crm],
                 agent_run=ScriptedRun(lambda p: {}), clock=lambda: clock[0]) as workspace:
        workspace.run_once()
        assert workspace.store.state("held:crm")[0]["listed_at"] == "2026-11-17T09:00:00Z"
        clock[0] = datetime(2026, 11, 18, 9, tzinfo=UTC)
        crm.records = [replace(renewal, revision="2")]  # listed again, revised: still the time it was first held
        workspace.run_once()
        item = listed(workspace.store, curator(agents, "opportunities"))[0]
        assert (item["at"], item["listed_at"]) == ("2026-11-10T09:00:00Z", "2026-11-17T09:00:00Z")


def admit_elsewhere(store: MemoryStore, agents: Path, name: str = "Pat Quill") -> str:
    curate(store, curator(agents, "things"), [("admit", {
        "name": name, "collection": "people", "keys": {"email": [f"{name.split()[0].lower()}@alder.example"]},
        "reason": "the lead on the Alder project"})])
    return proposals(store, "waiting")[-1].id


def test_a_late_curator_is_not_held_back_for_a_proposal_it_filed_for_another_collection(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose
    from cerebro.runner import PassResult, curators_due

    agents = write_workspace(tmp_path)
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW,
                 agent_run=ScriptedRun(lambda p: {}), connectors=[FakeConnector("crm", [])]) as workspace:
        for c in ("things", "people"):  # past their first runs: opportunities is late, in proposal mode
            propose(workspace.store, Proposal(f"p_{c}", "article.link", "things/x", Caller(f"{c}/curator", "agent"),
                                              NOW.isoformat(), status="accepted", decided_by=YOU,
                                              decided_at=NOW.isoformat()))
        curate(workspace.store, curator(agents, "opportunities"), [
            ("admit", {"name": "Alder", "collection": "things", "reason": "a thing"})])
        result = PassResult()
        curators_due(workspace, result)
    assert "first list" not in result.skipped.get("opportunities/curator", "")


def test_a_curator_whose_only_work_is_another_curators_proposal_has_something_to_weigh(tmp_path: Path) -> None:
    agents = write_workspace(tmp_path)
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW,
                 agent_run=ScriptedRun(lambda p: {}), connectors=[FakeConnector("crm", [])]) as workspace:
        assert workspace.weighs_nothing(workspace.agent("people/curator"))
        admit_elsewhere(workspace.store, agents)
        assert not workspace.weighs_nothing(workspace.agent("people/curator"))


def test_a_curators_reject_of_another_curators_proposal_leaves_its_own_first_run_open() -> None:
    from cerebro.curate import in_own_first_run
    from cerebro.records import Proposal, propose

    store = MemoryStore()
    propose(store, Proposal("p_x", "article.admit", "people/ann", Caller("things/curator", "agent"), NOW.isoformat(),
                            status="rejected", decided_by=Caller("people/curator", "agent"), decided_at=NOW.isoformat()))
    assert in_own_first_run(store, "people/curator")


def test_a_first_run_round_shows_a_curator_only_the_proposals_waiting_when_the_round_began(tmp_path: Path) -> None:
    from cerebro.held import FIRST_RUN_KEY

    agents, store = write_workspace(tmp_path), MemoryStore()
    id = admit_elsewhere(store, agents)
    store.set_state(FIRST_RUN_KEY, {"status": "discovering", "at": NOW.isoformat(), "pinned": []})
    _, prompts = curate(store, curator(agents, "people"), [])
    assert id not in prompts[0]


NAMED = {"name": "Patricia Quill", "keys": {"email": ["pat@alder.example"]}, "reason": "a lead"}


@pytest.mark.parametrize("lands", [True, False])
def test_a_curators_act_takes_the_proposals_it_names_accepted_with_it_or_waiting_with_it(
        tmp_path: Path, lands: bool) -> None:
    from cerebro.curate import cited, may_answer

    agents = write_workspace(tmp_path, people=lands)
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW,
                 agent_run=ScriptedRun(lambda p: {})) as workspace:
        store = workspace.store
        id = admit_elsewhere(store, agents)
        done, _ = curate(store, curator(agents, "people"), [("admit", {**NAMED, "proposals": [id]})])
        act = next(p for p in proposals(store) if p.by.name == "people/curator")
        assert f"'{'carried_out' if lands else 'waits_with_it'}': ['{id}']" in replies(done)[0]
        if not lands:
            named = next(p for p in proposals(store) if p.id == id)
            assert act.cites == [id] and not may_answer(curator(agents, "people"), named, cited(proposals(store)))
            assert [item.proposal.id for item in workspace.review()] == [act.id]  # it waits with the act
            assert workspace.status()["waiting"] == 1
            workspace.decide(YOU, accept=[act.id])
            curate(store, curator(agents, "people"), [("admit", {**NAMED, "proposals": [act.id]})])
            act = next(p for p in proposals(store) if p.id == act.id)
        row = next(p for p in proposals(store) if p.id == id)
        assert (row.status, row.decided_by, row.applied) == ("accepted", act.decided_by, act.applied)
        said = workspace.decide(YOU, accept=[id])  # a person who still had it on screen
        assert (said.answers, said.notes) == ({id: "already decided"}, {id: f"{id} is already accepted"})
        assert [a.identity.id for a in store.query("people")] == ["people/patricia-quill"]


@pytest.mark.parametrize("lands", [True, False])
def test_a_proposer_reads_its_named_proposal_as_its_decider_decided_it(tmp_path: Path, lands: bool) -> None:
    agents, store = write_workspace(tmp_path, people=lands), MemoryStore()
    id = admit_elsewhere(store, agents)
    curate(store, curator(agents, "people"), [("admit", {**NAMED, "proposals": [id]})])
    if not lands:
        act = next(p.id for p in proposals(store, "waiting") if p.by.name == "people/curator")
        decide(store, YOU, reject=[Decision(act, "not one of ours")], clock=lambda: NOW)
    _, told = curate(store, curator(agents, "things"), [])
    said = "- your proposal for people/pat-quill was made as people/patricia-quill" if lands else \
        f"- {id}, admit of people/pat-quill: rejected by a caller: not one of ours"
    assert said in told[0]


def archive_named(tmp_path: Path, store: MemoryStore) -> str:
    """things/curator's waiting archive of people/ann, and people/curator's, without authority, that names it."""
    from cerebro.agent import Authority
    from cerebro.records import Proposal, propose

    agents = write_workspace(tmp_path)
    store.create(Identity("people/ann", "people", "Ann", {"email": ["ann@x.example"]}, "active"))
    propose(store, Proposal("p_theirs", "article.archive", "people/ann", Caller("things/curator", "agent"),
                            NOW.isoformat(), reason="gone quiet"))
    curate(store, replace(curator(agents, "people"), authority=Authority()),
           [("archive", {"article": "people/ann", "reason": "gone quiet", "proposals": ["p_theirs"]})])
    return next(p.id for p in proposals(store, "waiting") if p.by.name == "people/curator")


def test_a_curators_reject_of_an_act_leaves_the_proposals_it_names_waiting(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose

    agents, store = write_workspace(tmp_path), MemoryStore()
    propose(store, Proposal("p_aimed", "article.admit", "things/alder", Caller("opportunities/curator", "agent"),
                            NOW.isoformat(), payload={"name": "Alder"}))
    curate(store, curator(agents, "things"), [("admit", {**NAMED, "collection": "people", "proposals": ["p_aimed"]})])
    act = next(p for p in proposals(store, "waiting") if p.by.name == "things/curator")
    curate(store, curator(agents, "people"), [("reject", {"proposals": [{"id": act.id, "reason": "not ours"}]})])
    assert next(p for p in proposals(store) if p.id == "p_aimed").status == "waiting"  # a person's no alone carries


def test_a_curators_act_closes_no_proposal_it_does_not_name(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    id = admit_elsewhere(store, agents)
    curate(store, curator(agents, "people"), [("admit", {"name": "Pat Quill", "keys": {"email": ["pat@alder.example"]},
                                                         "reason": "a lead"})])
    assert store.article("people/pat-quill") is not None
    assert next(p for p in proposals(store) if p.id == id).status == "waiting"


def test_an_act_its_filing_refuses_leaves_the_proposals_it_names_waiting(tmp_path: Path) -> None:
    from cerebro.agent import Authority
    from cerebro.records import Proposal, propose

    agents, store = write_workspace(tmp_path), MemoryStore()
    store.create(Identity("people/ann", "people", "Ann", {"email": ["ann@x.example"]}, "active"))
    propose(store, Proposal("p_theirs", "article.archive", "people/ann", Caller("things/curator", "agent"),
                            NOW.isoformat(), reason="gone quiet"))
    people = replace(curator(agents, "people"), authority=Authority())
    done, _ = curate(store, people, [("archive", {"article": "people/ann", "reason": "gone quiet"}),
                                     ("archive", {"article": "people/ann", "reason": "gone quiet",
                                                  "proposals": ["p_theirs"]})])  # the same act again is refused
    assert "refused" in replies(done)[1]
    assert next(p for p in proposals(store) if p.id == "p_theirs").status == "waiting"
    store.create(Identity("people/bo", "people", "Bo", {"email": ["bo@x.example"]}, "archived"))
    propose(store, Proposal("p_bo", "article.archive", "people/bo", Caller("things/curator", "agent"), NOW.isoformat()))
    done, _ = curate(store, people, [("archive", {"article": "people/bo", "reason": "x", "proposals": ["p_bo"]})])
    assert "carried_out" not in replies(done)[0] and "waits_with_it" not in replies(done)[0]  # it files nothing


def test_an_act_that_names_a_proposal_of_another_kind_is_refused_whole(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose, tell

    agents, store = write_workspace(tmp_path), MemoryStore()
    store.create(Identity("people/ann", "people", "Ann", {"email": ["ann@x.example"]}, "active"))
    id = admit_elsewhere(store, agents)
    done, _ = curate(store, curator(agents, "people"), [("link", {
        "article": "people/ann", "reason": "the same lead", "keys": {"alias": ["Pat"]}, "proposals": [id]})])
    assert f"{id} asks for another act than link: turn it down with reject" in replies(done)[0]
    assert next(p for p in proposals(store) if p.id == id).status == "waiting"
    assert "alias" not in store.article("people/ann").identity.keys
    store.create(Identity("people/bo", "people", "Bo", {"email": ["bo@x.example"]}, "active"))
    propose(store, Proposal("p_merge", "article.merge", "people/ann", Caller("things/curator", "agent"), NOW.isoformat(),
                            payload={"with": "people/bo"}))
    done, _ = curate(store, curator(agents, "people"), [("merge", {
        "article": "people/ann", "other": "people/bo", "reason": "two people", "distinct": True,
        "proposals": ["p_merge"]})])  # a keep-apart does not do what a merge asks
    assert "p_merge asks for another act than keeping two apart" in replies(done)[0]
    yes = tell(store, None, YOU, "archive Bo", target="people/bo", kind="article.archive", clock=lambda: NOW).proposals[0]
    done, _ = curate(store, curator(agents, "people"), [  # a caller's yes is named only by its own act on its own article
        ("link", {"article": "people/bo", "reason": "x", "keys": {"alias": ["B"]}, "proposals": [yes]}),
        ("archive", {"article": "people/ann", "reason": "gone quiet", "proposals": [yes]})])
    assert [line.split(": turn")[0] for line in replies(done)] == [
        f"{{'refused': '{yes} asks for another act than link",
        f"{{'refused': '{yes} asks about another article than people/ann"]
    assert next((p.status, p.applied) for p in proposals(store) if p.id == yes) == ("accepted", {})
    assert store.article("people/ann").identity.status == "active" and "alias" not in store.article(
        "people/bo").identity.keys


def test_an_act_that_names_a_proposal_about_another_article_is_refused_whole(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose

    agents, store = write_workspace(tmp_path), MemoryStore()
    for id, name in (("people/ann", "Ann"), ("people/bo", "Bo")):
        store.create(Identity(id, "people", name, {"email": [f"{name.lower()}@x.example"]}, "active"))
    propose(store, Proposal("p_bo", "article.archive", "people/bo", Caller("things/curator", "agent"), NOW.isoformat()))
    propose(store, Proposal("p_pair", "article.merge", "people/bo", Caller("things/curator", "agent"), NOW.isoformat(),
                            payload={"with": "people/ann"}))
    done, _ = curate(store, curator(agents, "people"), [
        ("archive", {"article": "people/ann", "reason": "gone quiet", "proposals": ["p_bo"]}),
        ("merge", {"article": "people/ann", "other": "people/bo", "reason": "one person", "proposals": ["p_pair"]})])
    assert "p_bo asks about another article than people/ann: turn it down with reject" in replies(done)[0]
    assert store.article("people/ann").identity.status == "active"
    assert {p.id: p.status for p in proposals(store) if p.id in ("p_bo", "p_pair")} == {"p_bo": "waiting",
                                                                                        "p_pair": "accepted"}


def test_an_act_that_names_a_proposal_it_may_not_answer_files_nothing(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose

    agents, store = write_workspace(tmp_path), MemoryStore()
    propose(store, Proposal("p_you", "article.admit", "people/ann", YOU, NOW.isoformat(), payload={"name": "Ann"}))
    done, _ = curate(store, curator(agents, "people"), [("admit", {"name": "Bo", "reason": "a lead",
                                                                    "proposals": ["p_you", "p_none"]})])
    assert "p_you and p_none are not ones you decide" in replies(done)[0]
    assert [(p.id, p.status) for p in proposals(store)] == [("p_you", "waiting")] and store.article("people/bo") is None


def test_a_proposal_for_another_collection_carries_the_fields_an_admit_may_set(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path, people=False), MemoryStore()
    a_thing(store)
    asked = {"name": "Pat Quill", "collection": "people", "keys": {"email": ["pat@alder.example"]},
             "reason": "the lead on the Alder project"}
    refused, _ = curate(store, curator(agents, "things"), [("admit", {**asked, "fields": {"status": "archived"}})])
    assert "refused" in replies(refused)[0] and proposals(store, "waiting") == []  # a protected field, as on any admit
    curate(store, curator(agents, "things"), [("admit", {**asked, "fields": {"linked": ["things/alder"]}})])
    [row] = proposals(store, "waiting")
    assert row.payload["fields"] == {"linked": ["things/alder"]}


def test_a_curator_rejects_only_another_curators_proposals_on_its_collection(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose

    agents, store = write_workspace(tmp_path), MemoryStore()
    id = admit_elsewhere(store, agents)
    people = curator(agents, "people")
    for row in (Proposal("p_you", "article.admit", "people/ann", YOU, NOW.isoformat()),
                Proposal("p_own", "article.admit", "people/bo", Caller("people/curator", "agent"), NOW.isoformat()),
                Proposal("p_writer", "article.archive", "people/cy", Caller("things/subagents/curator", "agent"),
                         NOW.isoformat())):
        propose(store, row)
    _, prompts = curate(store, people, [])
    assert f"- {id} admit Pat Quill" in prompts[0] and "p_you" not in prompts[0]
    blank, _ = curate(store, people, [("reject", {"proposals": [{"id": id, "reason": " "}]})])
    assert "give each a reason: its proposer reads it" in replies(blank)[0]
    for other in ("p_you", "p_own", "p_writer"):
        no, _ = curate(store, people, [("reject", {"proposals": [{"id": other, "reason": "no"}]})])
        assert f"{other} is not one you decide: answer the proposals your opening lists" in replies(no)[0]
    assert decide(store, Caller("people/curator", "agent"), accept=[id], clock=lambda: NOW,
                  root=tmp_path).answers == {id: "not yours to decide"}  # a curator answers by reject or its own act
    elsewhere, _ = curate(store, curator(agents, "opportunities"), [("reject", {"proposals": [{"id": id, "reason": "x"}]})])
    assert "is not one you decide" in replies(elsewhere)[0]
    other = admit_elsewhere(store, agents, "Ann Birch")
    done, _ = curate(store, people, [("reject", {"proposals": [{"id": id, "reason": "a supplier, not a maker"}]}),
                                     ("reject", {"proposals": [{"id": other, "reason": "met once"},
                                                               {"id": id, "reason": "a supplier, not a maker"}]})])
    assert replies(done) == [f"{{'rejected': ['{id}']}}",
                             f"{{'rejected': ['{other}'], 'refused': {{'{id}': '{id} is rejected already'}}}}"]
    row = next(p for p in proposals(store) if p.id == id)
    assert (row.status, row.decided_by, row.decision_reason) == ("rejected", Caller("people/curator", "agent"),
                                                                 "a supplier, not a maker")
    again, _ = curate(store, curator(agents, "things"), [("admit", {
        "name": "Pat Quill", "collection": "people", "reason": "the lead on the Alder project, met twice"})])
    assert "proposed" in replies(again)[0]  # a curator's no bars nothing


def test_a_group_given_as_a_key_binds_nothing_and_is_kept_with_the_reason(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    done, _ = curate(store, curator(agents, "opportunities"), [
        ("admit", {"name": "Alder renewal", "keys": {"salesforce_id": ["006A"], "group": ["closing soon"]},
                   "reason": "an open deal"}),
        ("admit", {"name": "Birch Yard pilot", "keys": {"salesforce_id": ["006G"]}, "group": "closing soon",
                   "reason": "an open deal"})])
    assert done.admitted == ["opportunities/alder-renewal", "opportunities/birch-yard-pilot"]  # two articles, not one
    assert store.article("opportunities/alder-renewal").identity.keys == {"salesforce_id": ["006A"]}
    assert [p.reason for p in proposals(store)] == ["an open deal (closing soon)"] * 2


def test_a_proposal_of_an_article_the_other_collection_holds_already_is_refused_toward_link(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    store.create(Identity("people/pat-quill", "people", "Pat Quill", {"email": ["pat@alder.example"]}, "active"))
    done, _ = curate(store, curator(agents, "things"), [
        ("admit", {"name": "Pat Quill", "collection": "people", "reason": "the lead on the Alder project"}),
        ("admit", {"name": "P. Quill", "collection": "people", "keys": {"email": ["pat@alder.example"]},
                   "reason": "the lead on the Alder project"})])
    said = refusal("admit: people/pat-quill is in the working set already: to change its keys or links, use link with "
                   "collection people")
    assert replies(done) == [said, said]
    assert proposals(store) == [] and done.proposed == []


def test_a_held_list_and_a_proposal_survive_a_pass_that_lets_go_of_a_gone_source_and_agent(tmp_path: Path) -> None:
    agents = write_workspace(tmp_path)
    renewal = replace(deal("006A", "Alder renewal", "2026-11-10T09:00:00Z"), body=None)
    store = MemoryStore()
    store.set_state("cerebro:known", {"agents": ["gone/curator"], "sources": ["crm", "old"]})
    store.set_state("held:old", [asdict(replace(renewal, source="old"))])
    admit_elsewhere(store, agents)
    with Cerebro(tmp_path, store=store, extraction=FakeExtraction(), connectors=[FakeConnector("crm", [renewal])],
                 agent_run=ScriptedRun(lambda p: {}), clock=lambda: NOW, trusted=True) as workspace:
        workspace.run_once()
        assert store.state("held:old") is None  # the gone source's held records are let go
        assert [i["id"] for i in listed(store, curator(agents, "opportunities"))] == ["006A"]
        assert [p.target for p in proposals(store, "waiting")] == ["people/pat-quill"]  # the proposal still waits


def test_someone_on_the_workspaces_own_side_is_held_after_the_others_with_the_mark(tmp_path: Path) -> None:
    agents, store = write_workspace(tmp_path), MemoryStore()
    (tmp_path / "workspace.md").write_text("---\nidentity: { domains: [ourco.example] }\n---\n\nWho we are.\n")
    colleague = Record(id="005U", source="crm", revision="1", at="2026-11-14T09:00:00Z",  # a CRM user: our own,
                       keys={"salesforce_id": ["005U"], "name": ["Sam Okoye"], "email": ["Sam@OurCo.example"]},
                       into="people")  # and the newest
    namesake = replace(contact("003S", "Sam Okoye", "sam.okoye@home.example"), keys={"name": ["Sam Okoye"]})
    hold(store, colleague, namesake, contact("003P", "Pat Quill", "pat@alder.example"))
    shown = gather(curator(agents, "people"), StoreLookup(store, clock=lambda: NOW), {}, root=tmp_path, now=NOW,
                   sources={"crm": FakeConnector("crm", [])})
    held = next(e for e in shown if e.spelled.startswith("held for"))
    lines = [json.loads(i.text.removeprefix("- ")) for i in held.items]
    assert [line["id"] for line in lines] == ["003P", "003S", "005U"]  # the newest first, the own side last
    assert [line.get("of_this_workspaces_own_side") for line in lines] == [None, None, ["Sam Okoye"]]


def test_only_a_record_about_one_of_our_people_lends_a_name_and_a_name_alone_marks_no_record(
        ) -> None:
    from cerebro.connector import Ours
    from cerebro.held import own_names

    ours, store = Ours(addresses=("me@ourco.example",), domains=("ourco.example",)), MemoryStore()
    dana = contact("003D", "Dana Lee", "dana@alder.example")
    meeting = Record(id="evt9", source="calendar", revision="1", at="2026-11-11T00:00:00Z", into="meetings",
                     keys={"email": ["me@ourco.example", "dana@alder.example"]}, fields={"title": "Dana Lee"})
    user = Record(id="005U", source="crm", revision="1", at="2026-11-12T09:00:00Z", into="people",
                  keys={"name": ["Sam Okoye"], "email": ["sam@ourco.example"]})
    bare = replace(contact("003S", "Sam Okoye", "x@x.example"), keys={"name": ["Sam Okoye"]})
    outside = contact("003T", "Sam Okoye", "sam.okoye@birch.example")
    hold(store, dana, meeting, user, bare, outside)
    assert own_names(store, ["crm", "calendar"], ours) == {"sam okoye"}  # the meeting lends no name
    shown = held_for(store, "people", ["crm", "calendar"], ours=ours)
    assert [h.id for h in shown.items if h.own] == ["005U"]  # neither namesake is marked, nor Dana by the meeting


@pytest.mark.parametrize("order", [[0, 1], [1, 0]])
def test_what_a_held_record_hangs_off_is_listed_in_one_order_whichever_curator_proposed_first(order: list[int]) -> None:
    from cerebro.curate import FIRST_RUN_KEY
    from cerebro.records import Proposal, propose

    proposed = [("p_a", "alpha/the-firm", "alpha/curator"), ("p_b", "beta/firm-group", "beta/curator")]
    with MemoryStore() as store:
        store.set_state(FIRST_RUN_KEY, {"status": "waiting", "at": "2026-11-17T09:00:00Z"})
        for n in order:
            id, target, by = proposed[n]
            propose(store, Proposal(id, "article.admit", target, Caller(by, "agent"), "2026-11-17T09:00:00Z",
                                    {"name": target, "keys": {"domain": ["firm.example"]},
                                     "collection": target.split("/")[0]}))
        store.set_state("held:crm", [{"id": "r1", "source": "crm", "revision": "1", "at": "2026-11-10T09:00:00Z",
                                      "keys": {"email": ["ann@firm.example"]}, "fields": {}, "body": None,
                                      "into": "parts"}])
        [found] = held_for(store, "parts", ["crm"]).items
    assert [link["id"] for link in found.links_to] == ["alpha/the-firm", "beta/firm-group"]
