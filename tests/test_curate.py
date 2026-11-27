"""A curator admits, archives, merges and splits through its run, and the working set decides what is extracted."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import GTM, refusal
from test_held import PerAgent

from cerebro import Cerebro
from cerebro.agent import Agent, Authority, load_agent
from cerebro.article import Compilation, Identity, Property
from cerebro.caller import Caller
from cerebro.clock import stamp
from cerebro.connector import Record
from cerebro.curate import Curated, in_scope, run_curator
from cerebro.evidence import gather, render
from cerebro.facts import Fact, Revisions
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.identity import held_by, link, redirects
from cerebro.lookup import StoreLookup
from cerebro.records import Decision, Proposal, admit_by_hand, decide, proposals, propose, tell
from cerebro.review import held_lines
from cerebro.runner import collection_order
from cerebro.tree import Group, Row, tree

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)
YOU = Caller("you", "person")


# the authority these tests run the accounts curator under: admits and splits land, archives and merges wait. The
# template's own authority is a template test's (test_starter_authority).
ADMITS = Authority(admit=True, archive=False, merge=False, split=True)


def curator() -> Agent:
    return replace(load_agent(GTM / "accounts" / "curator.md", GTM), authority=ADMITS)


def curate(store: MemoryStore, agent: Agent, calls: list[tuple[str, dict[str, Any]]], root: Path,
           prompts: list[str] | None = None, *, at: datetime = NOW, extraction: Any = None) -> Curated:
    """Run `agent` once at `at`, making `calls`, over the facts layer `extraction`, with one source, `src`."""
    run = ScriptedRun(lambda prompt: (prompts.append(prompt) if prompts is not None else None) or {}, calls)
    return run_curator(agent, lookup=StoreLookup(store, extraction or FakeExtraction(), clock=lambda: at), store=store,
                       agent_run=run, root=root, functions={}, agent_tools={}, clock=lambda: at,
                       sources={"src": FakeConnector("src", [])})


def things(root: Path, **authority: bool) -> Agent:
    """A things curator on a file of its own under `root`, with only the authority given."""
    (root / "agents" / "things").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "things" / "curator.md").write_text(
        "---\nevidence:\n  - query: { from: things }\nheartbeat: [change]\n---\n\nKeep things.\n")
    return replace(load_agent(root / "agents" / "things" / "curator.md", root / "agents"),
                   authority=Authority(**authority))


def said(done: Curated) -> list[str]:
    assert done.run is not None
    return [e.text for e in done.run.transcript if e.role == "tool"]


def hour(n: int) -> datetime:
    return NOW + timedelta(hours=n)


def row(store: MemoryStore, id: str) -> Proposal:
    return next(p for p in proposals(store) if p.id == id)


def told(store: MemoryStore, kind: str, target: str, text: str, at: datetime, by: Caller = YOU) -> str:
    """A caller's tell of one proposal, accepted as sent: its id."""
    return tell(store, None, by, text, target=target, kind=kind, clock=lambda: at).proposals[0]


def dated(store: MemoryStore, layer: Revisions, article: str, at: datetime, tied: datetime | None = None) -> None:
    """A record dated `at`, taken in by the facts layer, its fact tied to `article` at `tied`, or at `at`."""
    record = Record(id=f"r-{article}-{stamp(at)}", source="src", revision="1", at=stamp(at), body=f"{article} news.")
    for fact in layer.extract(record).facts:
        store.link(fact.id, [article], at=stamp(tied or at))


def held(store: MemoryStore, code: str, at: datetime) -> None:
    """A record of `src`, dated `at`, held for things, that the key code `code` binds."""
    store.set_state("held:src", [*(store.state("held:src") or []), asdict(Record(
        id=f"h-{code}-{stamp(at)}", source="src", revision="1", at=stamp(at), keys={"code": [code]}, into="things"))])


def record(id: str, **keys: list[str]) -> Record:
    return Record(id=id, source="mail", revision="1", keys=dict(keys), into="accounts")


def test_a_curator_admits_an_article_and_a_record_with_its_key_is_in_scope_and_its_facts_link_to_it(
        tmp_path: Path) -> None:
    store, prompts = MemoryStore(), []
    store.append("mcp-requests", {"at": "2026-11-16T10:00:00Z", "tool": "find", "args": {"name": "Acme Corp"},
                                  "caller": "outside", "result": "miss", "signal": "admit"})
    mail = record("mail:1", domain=["ACME.example"])
    assert in_scope(store, mail) and not in_scope(store, mail, curated={"accounts"})

    done = curate(store, curator(), [("admit", {"name": "Acme Corp", "reason": "asked for 1 time this week",
                                                "keys": {"domain": ["acme.example"]}})], tmp_path, prompts)
    assert done.admitted == ["accounts/acme-corp"] and done.run is not None and done.run.status == "done"
    assert "Acme Corp" in prompts[0]  # the miss an outside agent left is the curator's evidence
    assert store.rows("agent-runs")[-1].data["agent"] == "accounts/curator"
    assert [r.data["status"] for r in store.latest("proposals")] == ["accepted"]

    assert in_scope(store, mail, curated={"accounts"})
    assert not in_scope(store, record("mail:2", domain=["globex.example"]), curated={"accounts"})
    fact = Fact(id="f1", text="Acme Corp renewed for two years.", subject="the renewal", kind="note", at="",
                source="mail", record="mail:1")
    assert link(store, fact, mail) == ["accounts/acme-corp"]
    assert store.facts_of("accounts/acme-corp") == ["f1"]

    archived = curate(store, curator(), [("archive", {"article": "Acme Corp", "reason": "no activity in 90 days"})],
                      tmp_path)
    assert archived.archived == [] and len(archived.proposed) == 1  # the template's archive: false, so it waits
    decide(store, YOU, accept=archived.proposed, clock=lambda: NOW)
    assert in_scope(store, mail, curated={"accounts"})  # accepted: its curator carries it out
    curate(store, curator(), [("archive", {"article": "Acme Corp", "reason": "asked", "proposals": archived.proposed})],
           tmp_path)
    assert not in_scope(store, mail, curated={"accounts"})


def test_a_curator_that_archives_on_its_own_can_admit_what_it_archived_back(tmp_path: Path) -> None:
    store = MemoryStore()
    mail = record("mail:1", domain=["acme.example"])
    alone = replace(curator(), authority=Authority(admit=True, archive=True))  # its file sets these two true
    curate(store, alone, [("admit", {"name": "Acme Corp", "reason": "asked for 1 time this week",
                                     "keys": {"domain": ["acme.example"]}})], tmp_path)
    archived = curate(store, alone, [("archive", {"article": "Acme Corp", "reason": "no activity in 90 days"})], tmp_path)
    assert archived.archived == ["accounts/acme-corp"] and not in_scope(store, mail, curated={"accounts"})
    back = curate(store, alone, [("admit", {"name": "acme corp", "reason": "asked again"})], tmp_path)
    assert back.admitted == ["accounts/acme-corp"] and in_scope(store, mail)


def test_a_curator_without_authority_files_proposals_and_changes_no_article(tmp_path: Path) -> None:
    store = MemoryStore()
    store.create(Identity("accounts/brackwell", "accounts", "Brackwell", {"domain": ["brackwell.example"]}))
    store.create(Identity("accounts/brackwell-inc", "accounts", "Brackwell Inc."))
    held = replace(curator(), authority=Authority(admit=False, archive=False, merge=False, split=False))
    done = curate(store, held, [
        ("admit", {"name": "Globex", "reason": "a meeting next week"}),
        ("archive", {"article": "accounts/brackwell", "reason": "closed"}),
        ("merge", {"article": "Brackwell", "other": "Brackwell Inc.", "reason": "same company"}),
    ], tmp_path)
    assert done.admitted == [] and done.archived == [] and len(done.proposed) == 3
    waiting = store.latest("proposals", where={"status": "waiting"})
    assert [r.data["kind"] for r in waiting] == ["article.admit", "article.archive", "article.merge"]
    assert store.article("accounts/globex") is None and store.article("accounts/brackwell").identity.status == "active"
    assert store.rows("merges") == []


def test_a_merge_keeps_the_first_article_named_and_a_merge_after_a_callers_split_is_refused_naming_it(
        tmp_path: Path) -> None:
    store = MemoryStore()
    store.create(Identity("people/j-doe", "people", "J. Doe", {"phone": ["555"]}))
    store.create(Identity("people/jane-doe", "people", "Jane Doe", {"email": ["jane@alder.example"]}))
    people = replace(curator(), id="people/curator", target="people", path=Path("agents/people/curator.md"),
                     authority=Authority(admit=True, merge=True, split=True))  # its file sets these true
    done = curate(store, people, [("merge", {"article": "Jane Doe", "other": "J. Doe", "reason": "one person"})],
                  tmp_path)
    assert done.merged == ["people/j-doe"]
    jane = store.article("people/jane-doe").identity
    assert jane.keys == {"email": ["jane@alder.example"], "phone": ["555"], "alias": ["J. Doe"]}
    assert in_scope(store, record("call:1", phone=["555"]))
    fact = Fact(id="f1", text="J. Doe asked for a demo.", subject="J. Doe", kind="note", at="", source="mail",
                record="call:1")
    assert link(store, fact) == ["people/jane-doe"]

    split = told(store, "article.split", "people/j-doe", "two people", hour(1))
    back = curate(store, people, [("split", {"article": "people/j-doe", "reason": "two people", "proposals": [split]}),
                                  ("merge", {"article": "Jane Doe", "other": "J. Doe", "reason": "again"})], tmp_path,
                  at=hour(2))
    assert back.split == ["people/j-doe"] and back.merged == []
    assert store.article("people/jane-doe").identity.keys == {"email": ["jane@alder.example"]}
    assert store.article("people/j-doe").identity.keys == {"phone": ["555"]}
    assert f"a caller split people/j-doe from people/jane-doe in {split}" in said(back)[-1]


def test_a_merge_carries_each_tie_to_the_survivor_and_a_split_takes_back_only_what_it_carried(tmp_path: Path) -> None:
    store = MemoryStore()
    store.create(Identity("people/j-doe", "people", "J. Doe", {"phone": ["555"]}))
    store.create(Identity("people/jane-doe", "people", "Jane Doe", {"email": ["jane@alder.example"]}))
    store.link("f_moved", ["people/j-doe"], how="mentions")
    store.link("f_shared", ["people/j-doe"], how="subject")
    store.link("f_shared", ["people/jane-doe"], how="record")
    store.link("f_hers", ["people/jane-doe"], how="mentions")
    people = replace(curator(), id="people/curator", target="people", path=Path("agents/people/curator.md"),
                     authority=Authority(admit=True, merge=True, split=True))  # its file sets these true
    curate(store, people, [("merge", {"article": "Jane Doe", "other": "J. Doe", "reason": "one person"})], tmp_path)
    assert store.ties_of("f_moved")["people/jane-doe"] == ["mentions"]
    assert store.ties_of("f_shared")["people/jane-doe"] == ["subject", "record"]
    signed = Fact(id="f_told", text="J. Doe signed.", subject="J. Doe", kind="note", at="", source="mail",
                  record="call:1")
    link(store, signed)  # by the merged article's name, followed to the survivor
    assert store.ties_of("f_told") == {"people/jane-doe": ["subject"]}

    curate(store, people, [("split", {"article": "people/j-doe", "reason": "two people"})], tmp_path)
    assert store.ties_of("f_moved") == {"people/j-doe": ["mentions"]}
    assert store.ties_of("f_shared") == {"people/j-doe": ["subject"], "people/jane-doe": ["record"]}
    assert store.ties_of("f_hers") == {"people/jane-doe": ["mentions"]}
    assert store.ties_of("f_told") == {"people/j-doe": ["subject"]}


def test_with_no_curator_every_record_is_extracted() -> None:
    store = MemoryStore()
    assert in_scope(store, Record(id="notes/1.md", source="local", revision="1"))
    assert in_scope(store, record("mail:1", domain=["alder.example"]), curated={"people"})


def test_the_working_set_a_connector_is_bounded_by_is_the_one_a_pass_extracts_by() -> None:
    from cerebro.curate import working_set

    store = MemoryStore()
    store.create(Identity("things/alder", "things", "Alder", {"domain": ["alder.example"]}))
    store.create(Identity("things/birch-yard", "things", "Birch Yard", {"domain": ["birch.example"]}, "archived"))
    store.create(Identity("people/ann", "people", "Ann", {"domain": ["ann.example"]}, "merged"))
    from cerebro.connector import NOBODY, address_keys
    from cerebro.curate import address_form

    said = address_keys(["Ada@Mail.Alder.example"], NOBODY)  # what a connector lists, and what an admit keeps
    assert address_form({"email": ["Ada@Mail.Alder.example"], "domain": ["www.Alder.example"]}) == said
    bounds = {value for who in working_set(store) for value in who.keys.get("domain", [])}
    assert bounds == {"alder.example"}
    for value in ("alder.example", "birch.example", "ann.example"):
        assert in_scope(store, record("mail:1", domain=[value]), curated={"things"}) == (value in bounds)


def test_a_key_named_in_another_case_or_spacing_is_read_as_the_name_its_source_writes(tmp_path: Path) -> None:
    from cerebro.identity import bound_to

    store, agent = MemoryStore(), things(tmp_path, admit=True)
    listed = Record(id="h-1", source="src", revision="1", keys={"thing_code": ["T-1"]}, into="things")
    store.set_state("held:src", [asdict(listed)])
    curate(store, agent, [("admit", {"name": "Alder", "keys": {"Thing Code": ["T-1"]}, "reason": "a maker"})], tmp_path)
    assert store.article("things/alder").identity.keys == {"thing_code": ["T-1"]}
    assert bound_to(store, listed) == ["things/alder"]
    curate(store, agent, [("link", {"article": "Alder", "wrong": {"THING_CODE": ["T-1"]}, "reason": "not it"})],
           tmp_path)
    assert store.article("things/alder").identity.keys == {}


def test_a_key_named_exactly_is_itself_and_one_a_link_adds_in_another_spelling_is_the_sources_name(
        tmp_path: Path) -> None:
    store, agent = MemoryStore(), things(tmp_path, admit=True)
    curate(store, agent, [("admit", {"name": "Alder", "keys": {"Thing Code": ["T-1"]}, "reason": "a maker"})], tmp_path)
    store.set_state("held:src", [asdict(Record(id=f"h-{n}", source="src", revision="1", keys=keys, into="things"))
                                 for n, keys in enumerate([{"Code": ["X"]}, {"code": ["Y"]}, {"thing_code": ["T-2"]}])])
    curate(store, agent, [("admit", {"name": "Birch", "keys": {"code": ["Y"]}, "reason": "a maker"}),
                          ("link", {"article": "Alder", "keys": {"THING CODE": ["T-2"]}, "reason": "its code"})],
           tmp_path)
    assert store.article("things/birch").identity.keys == {"code": ["Y"]}
    assert store.article("things/alder").identity.keys == {"Thing Code": ["T-1"], "thing_code": ["T-2"]}


def test_one_key_given_in_two_spellings_lands_as_one_key_with_the_values_of_both(tmp_path: Path) -> None:
    store, agent = MemoryStore(), things(tmp_path, admit=True)
    curate(store, agent, [("admit", {"name": "Cedar", "keys": {"tag": ["c-1"], "Tag ": ["c-2"]}, "reason": "a maker"})],
           tmp_path)
    assert store.article("things/cedar").identity.keys == {"tag": ["c-1", "c-2"]}


def test_what_a_template_file_asks_for_and_the_workspace_skips_gets_a_line_that_names_the_file() -> None:
    from cerebro.agent import load_agents
    from cerebro.curate import unbuilt

    lines = unbuilt(load_agents(GTM)[0])
    assert not [line for line in lines if "declares no evidence" in line]  # each reads its held list
    bare = replace(curator(), evidence=[])
    said = (f"agents/{bare.id}.md declares no evidence, so it reads no held record and runs on its "
            f"tools alone: add - held: {{}} under evidence: to read what its sources hold for {bare.target}")
    assert [line for line in unbuilt([bare]) if "declares" in line] == [said]
    assert not [line for line in lines if "leave_out" in line]  # built: a curator calls it
    assert not any("record: document" in line for line in lines)  # built: it names the kind


def test_two_alike_proposals_filed_at_one_instant_keep_two_ids_and_a_second_ask_is_filed_beside_the_first(
        tmp_path: Path) -> None:
    held = replace(curator(), authority=Authority(admit=False, archive=False, merge=False, split=False))
    done = curate(MemoryStore(), held, [("admit", {"name": "Globex", "reason": "a meeting"}),
                                        ("admit", {"name": "Initech", "reason": "a meeting"})], tmp_path)
    assert len(set(done.proposed)) == 2
    store = MemoryStore()
    first = curate(store, held, [("admit", {"name": "Globex", "reason": "a meeting"})], tmp_path)
    again = curate(store, held, [("admit", {"name": "Globex", "keys": {"domain": ["globex.example"]},
                                            "reason": "a second meeting"})], tmp_path)
    assert first.proposed and again.proposed and first.proposed != again.proposed
    assert [(p.target, p.reason) for p in proposals(store, "waiting")] == [
        ("accounts/globex", "a meeting"), ("accounts/globex", "a second meeting")]


def test_an_exact_repeat_of_a_curators_own_waiting_ask_is_not_filed_and_another_askers_is(tmp_path: Path) -> None:
    held = replace(curator(), authority=Authority(admit=False, archive=False, merge=False, split=False))
    store = MemoryStore()
    curate(store, held, [("admit", {"name": "Globex", "reason": "a meeting"})], tmp_path)
    again = curate(store, held, [("admit", {"name": "Globex", "reason": "the meeting again"})], tmp_path)
    assert again.proposed == [] and "waits already" in again.run.transcript[-1].text
    other = replace(held, id="people/curator")
    assert curate(store, other, [("admit", {"name": "Globex", "reason": "a meeting"})], tmp_path).proposed
    assert len(proposals(store, "waiting")) == 2


def test_a_curator_that_may_admit_alone_files_into_what_waits_for_the_person(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose

    store = MemoryStore()
    propose(store, Proposal("p_acme", "article.admit", "accounts/acme", Caller("accounts/curator", "agent"),
                            "2026-11-16T09:00:00Z", payload={"name": "Acme", "keys": {"domain": ["acme.example"]}}))
    done = curate(store, curator(), [("admit", {"name": "Acme Corporation", "keys": {"domain": ["acme.example"]},
                                                "reason": "a third call"})], tmp_path)
    assert done.admitted == [] and store.article("accounts/acme-corporation") is None  # it names what waits


def test_an_ask_rejected_beside_an_accept_is_told_as_no_deferral_and_one_not_taken_keeps_proposal_mode(
        tmp_path: Path) -> None:
    from cerebro.curate import proposing
    from cerebro.held import FIRST_RUN_KEY
    from cerebro.records import Proposal, propose

    store, you = MemoryStore(), Caller("you", "person")
    store.set_state(FIRST_RUN_KEY, {"status": "done", "at": "2026-11-10T09:00:00Z"})
    for id, by in (("p_a", "accounts/curator"), ("p_b", "accounts/curator"), ("p_c", "people/curator")):
        propose(store, Proposal(id, "article.admit", "accounts/globex", Caller(by, "agent"), "2026-11-11T09:00:00Z",
                                payload={"name": "Globex"}, reason="a meeting"))
    decide(store, you, accept=["p_a"], reject=["p_b"], clock=lambda: NOW)
    prompts: list[str] = []
    curate(store, curator(), [], tmp_path, prompts)
    assert "accounts/globex), deferred" not in prompts[0]
    assert proposing(store, "people/curator")  # p_c closed as not taken: no person decided an admit of its


def test_carrying_out_a_persons_own_ask_ends_proposal_mode_for_neither_the_curator_nor_a_row_it_folds_in(
        tmp_path: Path) -> None:
    from cerebro.curate import proposing
    from cerebro.held import FIRST_RUN_KEY

    store = MemoryStore()
    store.set_state(FIRST_RUN_KEY, {"status": "done", "at": stamp(NOW)})
    asked = told(store, "article.admit", "things/alder", "keep Alder", hour(1))
    propose(store, Proposal("p_waits", "article.admit", "things/alder", Caller("people/curator", "agent"),
                            stamp(hour(1)), payload={"name": "Alder"}))
    curate(store, things(tmp_path, admit=True), [("admit", {"name": "Alder", "reason": "asked",
                                                            "proposals": [asked, "p_waits"]})], tmp_path, at=hour(2))
    assert store.article("things/alder") is not None
    assert proposing(store, "things/curator") and proposing(store, "people/curator")  # no person decided their admit


def test_two_things_that_share_a_name_are_never_made_one_article(tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose

    store, you = MemoryStore(), Caller("you", "person")
    for id, day, ref in (("p_one", "01", "c1"), ("p_two", "08", "c2")):
        propose(store, Proposal(id, "article.admit", "accounts/weekly-sync", Caller("accounts/curator", "agent"),
                                f"2026-11-{day}T09:00:00Z", payload={"name": "Weekly sync",
                                                                     "record": {"source": "notes", "id": ref}}))
    for id, mail in (("p_sam", "sam@acme.example"), ("p_lee", "sam.quennell@acme.example")):  # one domain, two addresses
        propose(store, Proposal(id, "article.admit", "accounts/sam-quennell", Caller("accounts/curator", "agent"),
                                "2026-11-02T09:00:00Z", payload={"name": "Sam Quennell", "keys": {
                                    "email": [mail], "domain": ["acme.example"]}}))
    decide(store, you, accept=["p_one", "p_two", "p_sam", "p_lee"], clock=lambda: NOW)
    curate(store, curator(), [("admit", {"name": p.payload["name"], "keys": p.payload.get("keys", {}), "reason": "asked",
                                         "proposals": [p.id]}) for p in proposals(store)], tmp_path)
    assert sorted(a.identity.id for a in store.query("accounts")) == [
        "accounts/sam-quennell", "accounts/sam-quennell-2", "accounts/weekly-sync", "accounts/weekly-sync-2"]
    curate(store, curator(), [("admit", {"name": "Globex", "keys": {"domain": ["globex.example"]}, "reason": "a call"}),
                              ("admit", {"name": "Globex", "keys": {"domain": ["globex.test"]}, "reason": "another"})],
           tmp_path)
    assert {a.identity.id: a.identity.keys["domain"] for a in store.query("accounts") if "globex" in a.identity.id} == {
        "accounts/globex": ["globex.example"], "accounts/globex-2": ["globex.test"]}


def test_a_person_admitted_by_a_line_of_several_names_binds_by_their_own_name_alone(tmp_path: Path) -> None:
    store = MemoryStore()
    people = load_agent(GTM / "people" / "curator.md", GTM)
    calls = [("admit", {"name": "Nessa Torvik", "reason": "on three calls, Nov 3 to 14",
                        "keys": {"attendees": ["Nessa Torvik, Sam Okoye", "Nessa Torvik, Priya Nair, Sam Okoye"]}}),
             ("admit", {"name": "Brackwell, Inc.", "reason": "named whole on a line", "keys": {"account": ["Brackwell, Inc."]}}),
             ("admit", {"name": "Grace Liu", "reason": "on two calls", "keys": {"attendees": ["Omar Haddad, Lina"]}})]
    assert curate(store, people, calls, tmp_path).admitted == ["people/nessa-torvik", "people/brackwell-inc", "people/grace-liu"]
    assert store.article("people/nessa-torvik").identity.keys == {"attendees": ["Nessa Torvik"]}
    assert store.article("people/brackwell-inc").identity.keys == {"account": ["Brackwell, Inc."]}
    assert store.article("people/grace-liu").identity.keys == {}  # a list that names others binds nothing of hers
    call = Record(id="local/call.md", source="local", revision="1", keys={"attendees": ["Nessa Torvik, Priya Nair"]})
    fact = Fact(id="f1", text="Priya Nair asked for SSO.", subject="Priya Nair", kind="note", at="", source="local",
                record="local/call.md")
    assert link(store, fact, call) == []  # a fact about Priya does not reach Nessa by the line they share


def test_the_engine_neither_refuses_nor_hides_the_workspaces_own_side(tmp_path: Path) -> None:
    """A collection about the workspace's own members: a member admitted by name or by record lands with the own
    address it is keyed by, and a note that names the member by name binds to that article and ties it its fact."""
    from test_held import ALL, CURATOR

    from cerebro.identity import bound_to

    (tmp_path / "agents" / "members").mkdir(parents=True)
    (tmp_path / "agents" / "members" / "curator.md").write_text(
        CURATOR.format(authority=ALL, extra="", prompt="Keep the workspace's own members."))
    (tmp_path / "workspace.md").write_text("---\nidentity: { domains: [ourco.example] }\n---\n\nWho we are.\n")
    (tmp_path / "settings.yaml").write_text("{}\n")
    user = Record(id="u1", source="notes", revision="1", at="2026-11-15T09:00:00Z", into="members",
                  keys={"email": ["bo@ourco.example"]}, fields={"name": "Bo Lin"})
    note = Record(id="n1", source="notes", revision="1", at="2026-11-16T09:00:00Z", into="calls",
                  keys={"attendees": ["Ann Ross, Kim Vale"]}, fields={"ours": ["Kim Vale"]},
                  body="Kim Vale will send Ann Ross the plan.")
    run = PerAgent()
    run.calls["members/curator"] = [
        ("admit", {"name": "Kim Vale", "keys": {"email": ["kim@ourco.example"]}, "reason": "A member."}),
        ("admit", {"record": {"source": "notes", "id": "u1"}, "reason": "A member, by their own record."})]
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(names=["Kim Vale", "Ann Ross"]),
                 clock=lambda: NOW, connectors=[FakeConnector("notes", [user, note])], agent_run=run) as workspace:
        workspace.run_once(agents=False)
        workspace.curate(next(a for a in workspace.agents if a.id == "members/curator"))
        assert '"of_this_workspaces_own_side": ["Bo Lin"]' in run.prompts["members/curator"][0]
        assert {a.identity.id: a.identity.keys for a in workspace.store.query("members")} == {
            "members/kim-vale": {"email": ["kim@ourco.example"]}, "members/bo-lin": {"email": ["bo@ourco.example"]}}
        workspace.run_once()
        assert bound_to(workspace.store, note) == ["members/kim-vale"]
        assert workspace.store.facts_of("members/kim-vale")
        from cerebro.curate import apply

        store = workspace.store  # after a merge, the name reaches the article that holds the member's address now
        store.create(Identity("members/kimberly-vale", "members", "Kimberly Vale"))
        apply(store, _accepted("article.merge", "members/kimberly-vale", {"with": "members/kim-vale"}), stamp(NOW))
        assert bound_to(store, note) == ["members/kimberly-vale"]


def _accepted(kind: str, target: str, payload: dict[str, Any]) -> Any:
    from cerebro.records import Proposal

    return Proposal("p_1", kind, target, Caller("you", "person"), "2026-11-18T09:00:00Z", payload)


def test_an_accepted_admit_takes_its_given_name_over_its_slug() -> None:
    from cerebro.curate import apply

    store = MemoryStore()
    apply(store, _accepted("article.admit", "things/alder-works", {"name": "Alder Works Ltd"}), "2026-11-18T09:00:00Z")
    assert store.article("things/alder-works").identity.title == "Alder Works Ltd"
    apply(store, _accepted("article.admit", "things/vega-labs", {}), "2026-11-18T09:00:00Z")
    assert store.article("things/vega-labs").identity.title == "Vega labs"


def test_merging_an_article_already_merged_away_is_refused() -> None:
    import pytest

    from cerebro.curate import Gone, apply

    store = MemoryStore()
    for id, title in (("things/alder", "Alder"), ("things/alder-inc", "Alder Inc"), ("things/vega", "Vega")):
        store.create(Identity(id, "things", title))
    apply(store, _accepted("article.merge", "things/alder", {"with": "things/alder-inc"}), "2026-11-18T09:00:00Z")
    merged = next(id for id in ("things/alder", "things/alder-inc")
                  if store.article(id).identity.status == "merged")
    with pytest.raises(Gone, match=f"{merged} was merged away already"):
        apply(store, _accepted("article.merge", merged, {"with": "things/vega"}), "2026-11-18T09:01:00Z")


def test_splitting_an_article_split_already_is_refused_and_changes_nothing() -> None:
    import pytest

    from cerebro.curate import Gone, apply

    store = MemoryStore()
    for id, title in (("things/alder", "Alder"), ("things/alder-inc", "Alder Inc")):
        store.create(Identity(id, "things", title, {"code": [title]}))
    apply(store, _accepted("article.merge", "things/alder", {"with": "things/alder-inc"}), "2026-11-18T09:00:00Z")
    merged = next(id for id in ("things/alder", "things/alder-inc") if store.article(id).identity.status == "merged")
    apply(store, _accepted("article.split", merged, {}), "2026-11-18T09:01:00Z")
    was = [(store.article(id).identity, len(store.rows("merges"))) for id in ("things/alder", "things/alder-inc")]
    with pytest.raises(Gone, match=f"{merged} is not merged into another article"):
        apply(store, _accepted("article.split", merged, {}), "2026-11-18T09:02:00Z")
    assert [(store.article(id).identity, len(store.rows("merges"))) for id in ("things/alder", "things/alder-inc")] == was


def test_each_key_weighs_what_the_connector_that_issues_it_declares() -> None:
    from cerebro.connectors.salesforce import SalesforceConnector
    from cerebro.curate import ADDRESS_WEIGHTS, key_weights

    weights = key_weights()
    assert weights["salesforce_id"] == 1000 and SalesforceConnector.IDENTITY_KEYS == {"salesforce_id": 1000}
    assert weights["email"] == 100 and weights["domain"] == 10 and ADDRESS_WEIGHTS == {"email": 100, "domain": 10}


def test_a_key_no_connector_declares_weighs_the_default_and_one_a_listed_connector_declares_counts() -> None:
    from cerebro.connector import Connector
    from cerebro.connectors import CONNECTORS
    from cerebro.curate import key_weights

    assert "part_code" not in key_weights()

    class PartsConnector(Connector):
        IDENTITY_KEYS: ClassVar[dict[str, int]] = {"part_code": 500}

    assert "part_code" not in key_weights()  # defined, and so loaded, but no kind builds it: it counts nothing
    assert key_weights({**CONNECTORS, "parts": PartsConnector})["part_code"] == 500
    assert "salesforce_id" not in key_weights({"files": CONNECTORS["files"]})  # the table, not what is imported


def test_two_connectors_that_weigh_one_key_differently_are_refused_with_both_named() -> None:
    from cerebro.connector import Connector
    from cerebro.curate import KeyWeightError, key_weights

    class FirstConnector(Connector):
        IDENTITY_KEYS: ClassVar[dict[str, int]] = {"part_code": 500}

    class SecondConnector(Connector):
        IDENTITY_KEYS: ClassVar[dict[str, int]] = {"part_code": 50}

    with pytest.raises(KeyWeightError) as refused:
        key_weights({"first": FirstConnector, "second": SecondConnector})
    assert str(refused.value) == ("the key part_code weighs 500 by FirstConnector (kind first) and 50 by "
                                  "SecondConnector (kind second): declare one weight for it, on the connector that "
                                  "issues it")
    FirstConnector.IDENTITY_KEYS = {"email": 1}  # nor may a connector weigh an address key otherwise than the engine
    with pytest.raises(KeyWeightError, match="weighs 100 by the engine's address keys and 1 by FirstConnector"):
        key_weights({"first": FirstConnector})


def test_a_fresh_process_weighs_a_source_key_from_the_connectors_table_with_nothing_imported_before() -> None:
    import subprocess
    import sys

    code = "from cerebro.curate import key_weights\nprint(key_weights()['salesforce_id'])\n"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["1000"]


def test_a_curators_admit_sets_the_fields_it_gives_under_the_guard_an_article_agent_writes_under(
        tmp_path: Path) -> None:
    store = MemoryStore()
    done = curate(store, curator(), [
        ("admit", {"name": "Acme Corp", "reason": "a renewal", "keys": {"domain": ["acme.example"]},
                   "fields": {"tier": "gold", "seats": 40}}),
        ("admit", {"name": "Globex", "reason": "a renewal", "keys": {"domain": ["globex.example"]},
                   "fields": {"status": "closed"}})], tmp_path)
    acme = store.article("accounts/acme-corp")
    by = Caller("accounts/curator", "agent")
    assert {k: (p.value, p.by) for k, p in acme.properties.items()} == {"tier": ("gold", by), "seats": (40, by)}
    assert done.admitted == ["accounts/acme-corp"] and store.article("accounts/globex") is None  # refused whole
    said = [e.text for e in done.run.transcript if e.role == "tool"]
    assert any("status is the system's, not yours to set" in text for text in said), said


ARCHIVE = ("archive", {"article": "things/alder", "reason": "no longer bought from"})
BY_THINGS = Caller("things/curator", "agent")
TWO = [("admit", {"name": "Alder", "keys": {"code": ["a-1"]}, "reason": "a maker"}),
       ("admit", {"name": "Alder Works", "keys": {"code": ["w-1"]}, "reason": "a maker"})]


def test_a_curator_does_not_undo_what_a_caller_decided_until_something_new_arrives(tmp_path: Path) -> None:
    agent, other = things(tmp_path, admit=True, archive=True, merge=True, split=True), Caller("laptop", "person")
    lift = "until a new fact or record about it arrives"

    def run(store: MemoryStore, *calls: tuple[str, dict[str, Any]], at: datetime, layer: Any = None,
            by: Agent = agent) -> list[str]:
        return said(curate(store, by, list(calls), tmp_path, at=at, extraction=layer))

    # a no on a curator's act is a no on what it names: the archive is refused, never left to wait
    store = MemoryStore()
    run(store, TWO[0], at=hour(0))
    propose(store, Proposal("p_theirs", "article.archive", "things/alder", Caller("people/curator", "agent"),
                            stamp(hour(0)), reason="quiet"))
    run(store, (ARCHIVE[0], {**ARCHIVE[1], "proposals": ["p_theirs"]}), at=hour(1), by=replace(agent, authority=Authority()))
    [act] = [p for p in proposals(store, "waiting") if p.by.name == "things/curator"]
    decide(store, YOU, reject=[Decision(act.id, "we still buy spares")], clock=lambda: hour(2))
    assert row(store, "p_theirs").status == "rejected"
    assert run(store, ARCHIVE, at=hour(3)) == [(
        refusal(f"a caller rejected archive of things/alder in p_theirs: we still buy spares: it is not done "
                f"again {lift}"))]
    assert proposals(store, "waiting") == [] and store.article("things/alder").identity.status == "active"
    mine = told(store, "article.archive", "things/alder", "archive it after all", hour(4), by=other)  # never refused
    assert "archived" in run(store, (ARCHIVE[0], {**ARCHIVE[1], "proposals": [mine]}), at=hour(4))[0]

    # a rejected merge, asked again in the other order, until a newer held record binds one of the pair
    store = MemoryStore()
    run(store, *TWO, at=hour(0))
    run(store, ("merge", {"article": "things/alder", "other": "things/alder-works", "reason": "one maker"}), at=hour(1),
        by=replace(agent, authority=Authority()))
    [no] = proposals(store, "waiting")
    decide(store, YOU, reject=[Decision(no.id, "two makers")], clock=lambda: hour(2))
    again = ("merge", {"article": "things/alder-works", "other": "things/alder", "reason": "one maker"})
    assert f"a caller rejected merge of things/alder and things/alder-works in {no.id}: two makers" in run(
        store, again, at=hour(3))[0]
    held(store, "w-1", hour(1))  # dated before the no: nothing new
    assert "refused" in run(store, again, at=hour(4))[0]
    held(store, "w-1", hour(5))
    assert "merged" in run(store, again, at=hour(6))[0]

    # a caller's admit, carried out: neither the old records it brings in, read late, nor the curator's own re-key is
    # anything new
    store = MemoryStore()
    layer = Revisions(FakeExtraction(), store)
    asked = told(store, "article.admit", "things/alder", "keep Alder", hour(0))
    run(store, ("admit", {"name": "Alder", "keys": {"code": ["a-1"]}, "reason": "asked", "proposals": [asked]}),
        at=hour(1))
    held(store, "a-1", hour(-1))
    dated(store, layer, "things/alder", hour(-1), tied=hour(2))
    run(store, ("link", {"article": "things/alder", "keys": {"code": ["a-2"]}, "reason": "its new code"}), at=hour(2))
    dated(store, layer, "things/alder", hour(-2), tied=hour(3))
    assert run(store, ARCHIVE, at=hour(3), layer=layer) == [
        refusal(f"a caller admitted things/alder in {asked}: keep Alder: an archive of it is not done {lift}")]
    dated(store, layer, "things/alder", hour(4))
    assert run(store, ARCHIVE, at=hour(5), layer=layer)[0].startswith("{'archived': 'things/alder'")

    # a caller's admit carried out under another path than the one asked holds on the article it made
    store = MemoryStore()
    bolt = told(store, "article.admit", "things/bolt", "keep Bolt", hour(0))
    run(store, ("admit", {"name": "Bolt Co", "reason": "asked", "proposals": [bolt]}), at=hour(1))
    assert run(store, ("archive", {"article": "things/bolt-co", "reason": "quiet"}), at=hour(2)) == [
        refusal(f"a caller admitted things/bolt-co in {bolt}: keep Bolt: an archive of it is not done {lift}")]

    # a caller's archive of an article a caller made refuses an admit back
    store = MemoryStore()
    made = told(store, "article.admit", "things/alder", "keep Alder", hour(0))
    run(store, ("admit", {"name": "Alder", "reason": "asked", "proposals": [made]}), at=hour(0))
    gone = told(store, "article.archive", "things/alder", "we stopped buying", hour(1))
    run(store, (ARCHIVE[0], {**ARCHIVE[1], "proposals": [gone]}), at=hour(1))
    assert f"a caller archived things/alder in {gone}: we stopped buying: an admit of it" in run(
        store, TWO[0], at=hour(2))[0]

    # a caller's archive asked on a merged-away path holds on the article it landed on, once the path is split out
    store = MemoryStore()
    run(store, *TWO, at=hour(0))
    run(store, ("merge", {"article": "things/alder", "other": "things/alder-works", "reason": "one"}), at=hour(1))
    gone = told(store, "article.archive", "things/alder-works", "we stopped buying", hour(2))
    run(store, ("archive", {"article": "things/alder-works", "reason": "asked", "proposals": [gone]}), at=hour(2))
    run(store, ("split", {"article": "things/alder-works", "reason": "two makers"}), at=hour(3))
    assert f"a caller archived things/alder in {gone}" in run(store, TWO[0], at=hour(4))[0]

    # a caller's merge, carried out, moves old facts: the split is refused until news of either article
    store = MemoryStore()
    layer = Revisions(FakeExtraction(), store)
    run(store, *TWO, at=hour(0))
    dated(store, layer, "things/alder-works", hour(0))
    merge = told(store, "article.merge", "things/alder", "merge it with things/alder-works", hour(1))
    run(store, ("merge", {"article": "things/alder", "other": "things/alder-works", "reason": "asked",
                          "proposals": [merge]}), at=hour(2))
    split = ("split", {"article": "things/alder-works", "reason": "two makers"})
    assert f"a caller merged things/alder and things/alder-works in {merge}" in run(store, split, at=hour(3),
                                                                                   layer=layer)[0]
    dated(store, layer, "things/alder", hour(4))
    assert "'split'" in run(store, split, at=hour(5), layer=layer)[0]

    # two that land: a merge of two articles a caller made
    store = MemoryStore()
    for name, key in (("Alder", "a-1"), ("Alder Works", "w-1")):
        made = told(store, "article.admit", f"things/{name.lower().replace(' ', '-')}", name, hour(0))
        run(store, ("admit", {"name": name, "keys": {"code": [key]}, "reason": "asked", "proposals": [made]}),
            at=hour(0))
    assert "merged" in run(store, ("merge", {"article": "things/alder", "other": "things/alder-works",
                                             "reason": "one maker"}), at=hour(1))[0]

    # a value a caller added: the curator's link does not take it off, nor what carrying out another caller's link
    # would take off beyond what that caller asked
    store = MemoryStore()
    run(store, TWO[0], at=hour(0))
    added = told(store, "article.link", "things/alder", "its domain is alder.example and its code a-9", hour(1))
    run(store, ("link", {"article": "things/alder", "keys": {"domain": ["alder.example"], "code": ["a-9"]},
                         "reason": "asked", "proposals": [added]}), at=hour(1))
    off = ("link", {"article": "things/alder", "wrong": {"domain": ["alder.example"]}, "reason": "not theirs"})
    assert run(store, off, at=hour(2)) == [(
        refusal(f"a caller added domain alder.example to things/alder in {added}: its domain is alder.example "
                f"and its code a-9: taking it off is not done {lift}"))]
    longer = told(store, "article.link", "things/alder", "add alder.example.org too", hour(3), by=other)
    assert f"a caller added domain alder.example to things/alder in {added}" in run(store, (off[0], {
        **off[1], "keys": {"domain": ["alder.example.org"]}, "proposals": [longer]}), at=hour(3))[0]  # a whole value only
    wrong = told(store, "article.link", "things/alder", "alder.example is not theirs", hour(3), by=other)
    beyond = {"domain": ["alder.example"], "code": ["a-9"]}
    assert f"a caller added code a-9 to things/alder in {added}" in run(
        store, (off[0], {**off[1], "wrong": beyond, "proposals": [wrong]}), at=hour(3))[0]
    assert "'removed'" in run(store, (off[0], {**off[1], "proposals": [wrong]}), at=hour(3))[0]  # its own word
    assert store.article("things/alder").identity.keys == {"code": ["a-1", "a-9"]}

    # a value a caller took off or turned down stays so once a source writes its key in another spelling, and one it
    # added stays on against a take-off or a null in the article's own spelling
    store = MemoryStore()
    run(store, ("admit", {"name": "Alder", "keys": {"Thing Code": ["T-1"]}, "reason": "a maker"}), at=hour(0))
    propose(store, Proposal("p_t2", "article.link", "things/alder", Caller("people/curator", "agent"), stamp(hour(0)),
                            payload={"keys": {"Thing Code": ["T-2"]}}))
    decide(store, YOU, reject=[Decision("p_t2", "not its code")], clock=lambda: hour(1))
    store.set_state("held:src", [asdict(Record(id="h-1", source="src", revision="1", at=stamp(hour(-1)),
                                               keys={"thing_code": ["T-1"]}, into="things"))])
    no = told(store, "article.link", "things/alder", "T-1 is not its code", hour(1))
    run(store, ("link", {"article": "Alder", "wrong": {"Thing Code": ["T-1"]}, "reason": "asked", "proposals": [no]}),
        at=hour(1))
    again = run(store, *(("link", {"article": "Alder", "keys": {"Thing Code": [code]}, "reason": "its code"})
                         for code in ("T-1", "T-2")), at=hour(2))
    assert f"a caller took off thing_code t-1 from things/alder in {no}" in again[0]
    assert "a caller rejected link of things/alder in p_t2: not its code" in again[1]
    assert store.article("things/alder").identity.keys == {}
    added = told(store, "article.link", "things/alder", "its code is C-5", hour(3))
    run(store, ("link", {"article": "Alder", "keys": {"Code": ["C-5"]}, "reason": "asked", "proposals": [added]}),
        at=hour(3))
    kept = run(store, *(("link", {"article": "Alder", "reason": "not its code", **call})
                        for call in ({"wrong": {"Code": ["C-5"]}}, {"keys": {"Code": None}})), at=hour(4))
    assert all(f"a caller added code c-5 to things/alder in {added}" in reply for reply in kept), kept
    assert store.article("things/alder").identity.keys == {"Code": ["C-5"]}

    # an admit carrying out an ask is checked on the article it lands on, but for the ask's own path
    store = MemoryStore()
    run(store, TWO[0], at=hour(0))
    gone = told(store, "article.archive", "things/alder", "we stopped buying", hour(1))
    run(store, (ARCHIVE[0], {**ARCHIVE[1], "proposals": [gone]}), at=hour(1))
    bolt = told(store, "article.admit", "things/bolt", "keep Bolt", hour(2))
    assert run(store, ("admit", {"name": "Alder", "reason": "the one meant", "proposals": [bolt]}), at=hour(2)) == [(
        refusal(f"a caller archived things/alder in {gone}: we stopped buying: an admit of it is not done {lift}. "
                f"The ask this act carries out, {bolt}, stays accepted until then, or until a caller asks for "
                "things/alder itself"))]
    assert row(store, bolt).status == "accepted"
    back = told(store, "article.admit", "things/alder", "bring Alder back", hour(3))
    run(store, ("admit", {"name": "Alder", "reason": "asked", "proposals": [back]}), at=hour(3))
    assert store.article("things/alder").identity.status == "active"

    # a yes the curator leaves is shown as not carried out yet, and wakes it once, not on every pass
    (tmp_path / "settings.yaml").write_text("{}\n")
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW,
                 agent_run=PerAgent()) as workspace:
        workspace.store.create(Identity("things/alder", "things", "Alder", {}, "active"))
        workspace.run_once()
        left = told(workspace.store, "article.archive", "things/alder", "archive it", NOW)
        assert held_lines(workspace) == [(f"{left} archive of things/alder: accepted, not carried out yet: its "
                                          "curator carries it out on its next run")]
        assert "things/curator" in workspace.run_once().due
        assert "things/curator" not in workspace.run_once().due


def test_a_curator_does_not_undo_the_name_a_caller_gave_an_article(tmp_path: Path) -> None:
    agent, store = things(tmp_path), MemoryStore()
    store.create(Identity("things/week", "things", "Week", {"code": ["w-1"]}))
    named = told(store, "article.admit", "things/Weekly briefing", "the weekly one", hour(0))
    curate(store, agent, [("admit", {"name": "Week", "reason": "asked", "proposals": [named]})], tmp_path, at=hour(0))
    assert store.article("things/week").identity.title == "Weekly briefing"  # over the name the curator gave
    gone = told(store, "article.archive", "things/week", "not this month", hour(1))
    curate(store, agent, [("archive", {"article": "things/week", "reason": "asked", "proposals": [gone]})], tmp_path,
           at=hour(1))
    back = told(store, "article.admit", "things/week", "bring it back", hour(2))  # an ask that names nothing
    curate(store, agent, [("admit", {"name": "Week", "keys": {"code": ["w-1"]}, "reason": "asked", "proposals": [back]})],
           tmp_path, at=hour(2))
    found = store.article("things/week").identity
    assert (found.status, found.title) == ("active", "Weekly briefing")


def test_a_yes_on_any_proposal_is_carried_out_by_its_curator_not_the_engine(tmp_path: Path) -> None:
    store, agent = MemoryStore(), things(tmp_path)
    for id, name in (("things/alder", "Alder"), ("things/alder-works", "Alder Works"), ("things/cedar", "Cedar")):
        store.create(Identity(id, "things", name, {}, "active"))
    curate(store, agent, [("admit", {"name": "Birch", "reason": "a maker"}),
                          ("merge", {"article": "things/alder", "other": "things/alder-works", "reason": "one"})],
           tmp_path)
    propose(store, Proposal("p_theirs", "article.archive", "things/cedar", Caller("people/curator", "agent"),
                            stamp(NOW), reason="quiet"))
    asked = [p.id for p in proposals(store, "waiting")]
    decide(store, YOU, accept=asked, clock=lambda: NOW)
    asked.append(told(store, "article.admit", "things/dune", "keep Dune", NOW))
    assert [(row(store, id).status, row(store, id).decided_by, row(store, id).applied) for id in asked] == [
        ("accepted", YOU, {})] * 4
    assert [(a.identity.id, a.identity.status) for a in store.query("things")] == [
        ("things/alder", "active"), ("things/alder-works", "active"), ("things/cedar", "active")]

    birch, merge, cedar, dune = asked
    curate(store, agent, [("admit", {"name": "Birch", "reason": "asked", "proposals": [birch]}),
                          ("merge", {"article": "things/alder", "other": "things/alder-works", "reason": "asked",
                                     "proposals": [merge]}),
                          ("archive", {"article": "things/cedar", "reason": "asked", "proposals": [cedar]}),
                          ("admit", {"name": "Dune", "reason": "asked", "proposals": [dune]})], tmp_path)
    assert [(a.identity.id, a.identity.status) for a in store.query("things")] == [
        ("things/alder", "active"), ("things/alder-works", "merged"), ("things/birch", "active"),
        ("things/cedar", "archived"), ("things/dune", "active")]
    assert all(row(store, id).applied for id in asked)


def test_a_curators_act_that_carries_out_a_yes_lands_as_its_own_whatever_its_authority_and_the_yes_stays_one(
        tmp_path: Path) -> None:
    store, agent = MemoryStore(), things(tmp_path)
    store.create(Identity("things/dune", "things", "Dune", {}, "active"))
    asked = told(store, "article.admit", "things/alder", "keep Alder", NOW)
    propose(store, Proposal("p_waits", "article.admit", "things/alder", Caller("people/curator", "agent"), stamp(NOW),
                            payload={"name": "Alder"}))
    done = curate(store, agent, [("admit", {"name": "Alder", "reason": "asked", "proposals": [asked, "p_waits"]})],
                  tmp_path, at=hour(1))
    act = next(p for p in proposals(store) if p.by == BY_THINGS)
    assert done.admitted == ["things/alder"] and (act.status, act.decided_by, act.cites) == (
        "accepted", BY_THINGS, [asked, "p_waits"])
    assert [(p.id, p.decided_at) for p in proposals(store) if p.decided_by == YOU] == [(asked, stamp(NOW))]
    assert [(row(store, id).status, row(store, id).decided_by, row(store, id).applied) for id in (asked, "p_waits")] == [
        ("accepted", YOU, act.applied), ("accepted", BY_THINGS, act.applied)]
    assert store.article("things/alder").identity.admitted_by == YOU

    # an act that waited, accepted with the proposal it named, is carried out with it
    propose(store, Proposal("p_theirs", "article.archive", "things/dune", Caller("people/curator", "agent"),
                            stamp(NOW), reason="quiet"))
    curate(store, agent, [("archive", {"article": "things/dune", "reason": "quiet", "proposals": ["p_theirs"]})],
           tmp_path)
    [waits] = [p for p in proposals(store, "waiting") if p.by == BY_THINGS]
    decide(store, YOU, accept=[waits.id], clock=lambda: NOW)
    assert curate(store, agent, [("archive", {"article": "things/dune", "reason": "asked", "proposals": [waits.id]})],
                  tmp_path).archived == ["things/dune"]
    assert row(store, "p_theirs").applied == row(store, waits.id).applied != {}

    # a merge asked in one order and carried out in the other keeps the article the caller named first
    store.create(Identity("things/alder-works", "things", "Alder Works", {}, "active"))
    merge = told(store, "article.merge", "things/alder", "merge it with things/alder-works", NOW)
    done = curate(store, agent, [("merge", {"article": "things/alder-works", "other": "things/alder", "reason": "asked",
                                            "proposals": [merge]})], tmp_path)
    assert "'merged': 'things/alder-works'" in said(done)[0] and redirects(store) == {"things/alder-works": "things/alder"}

    # a first run's group: each other row's path leads to the lead's, and one admit makes them one article
    for id, path, name in (("p_lead", "things/cedar", "Cedar"), ("p_also", "things/cedar-ltd", "Cedar Ltd")):
        propose(store, Proposal(id, "article.admit", path, BY_THINGS, stamp(NOW), payload={"name": name}))
    store.append("merges", {"at": stamp(NOW), "redirect": "things/cedar-ltd", "to": "things/cedar",
                            "path": "things/cedar-ltd"})
    decide(store, YOU, accept=["p_lead", "p_also"], clock=lambda: NOW)
    curate(store, agent, [("admit", {"name": "Cedar", "reason": "asked", "proposals": ["p_lead", "p_also"]})], tmp_path)
    assert [a.identity.title for a in store.query("things") if a.identity.id.startswith("things/cedar")] == ["Cedar"]
    assert row(store, "p_lead").applied == row(store, "p_also").applied != {}

    # a yes is never left to wait behind an act on another collection
    (tmp_path / "agents" / "parts").mkdir()
    (tmp_path / "agents" / "parts" / "curator.md").write_text(
        "---\nevidence:\n  - query: { from: parts }\nheartbeat: [change]\n---\n\nKeep parts.\n")
    ask = told(store, "article.admit", "things/elm", "keep Elm", NOW)
    done = curate(store, agent, [("admit", {"name": "Elm", "collection": "parts", "reason": "a part", "proposals": [ask]})],
                  tmp_path)
    assert "is accepted by a caller" in said(done)[0] and not [p for p in proposals(store, "waiting") if ask in p.cites]

    # a yes on a proposal its own waiting act names is carried out all the same
    propose(store, Proposal("p_fir", "article.admit", "things/fir", Caller("people/curator", "agent"), stamp(NOW),
                            payload={"name": "Fir", "keys": {}}))
    fir = ("admit", {"name": "Fir", "keys": {}, "reason": "agreed", "proposals": ["p_fir"]})
    curate(store, agent, [fir], tmp_path)
    decide(store, YOU, accept=["p_fir"], clock=lambda: NOW)
    curate(store, agent, [fir], tmp_path)
    assert store.article("things/fir") is not None and row(store, "p_fir").applied != {}


def test_an_admit_of_an_existing_article_carries_out_an_accepted_ask_it_names_with_no_new_article(
        tmp_path: Path) -> None:
    store = MemoryStore()
    store.create(Identity("things/patricia-quill", "things", "Patricia Quill", {}, "active"))
    asked = told(store, "article.admit", "things/pat-quill", "Pat Quill", NOW)
    done = curate(store, things(tmp_path), [("admit", {"name": "Patricia Quill", "reason": "the one meant",
                                                       "proposals": [asked]})], tmp_path)
    assert done.admitted == ["things/patricia-quill"] and store.article("things/pat-quill") is None
    assert row(store, asked).applied == {"at": stamp(NOW), "article": "things/patricia-quill", "created": False}


def test_an_approved_proposal_the_curator_filed_is_made_at_its_path_and_name(tmp_path: Path) -> None:
    store, agent = MemoryStore(), things(tmp_path)
    curate(store, agent, [("admit", {"name": "Alder Works", "reason": "a maker"})], tmp_path)
    [asked] = proposals(store, "waiting")
    decide(store, YOU, accept=[asked.id], clock=lambda: NOW)
    done = curate(store, agent, [("admit", {"name": "Alder", "reason": "asked", "proposals": [asked.id]}),
                                 ("admit", {"name": "things/alder-works", "reason": "asked", "proposals": [asked.id]})],
                  tmp_path)
    assert said(done)[0] == refusal(f"{asked.id} was approved as Alder Works at things/alder-works: admit it there")
    assert [(a.identity.id, a.identity.title) for a in store.query("things")] == [("things/alder-works", "Alder Works")]
    curate(store, agent, [("admit", {"name": "Ann", "reason": "a maker"}), ("admit", {"name": "Bob", "reason": "a maker"})],
           tmp_path)
    ann, bob = proposals(store, "waiting")
    decide(store, YOU, accept=[ann.id, bob.id], clock=lambda: NOW)
    done = curate(store, agent, [("admit", {"name": "Ann", "reason": "asked", "proposals": [ann.id, bob.id]})],
                  tmp_path)  # one act never carries out an approved admit of another path
    assert said(done)[0] == refusal(f"{bob.id} was approved as Bob at things/bob: admit it there")
    assert row(store, bob.id).applied == {} and store.article("things/ann") is None
    store.create(Identity("things/elm", "things", "Elm", {}, "archived"))
    curate(store, agent, [("admit", {"name": "Elm", "reason": "back again"})], tmp_path)
    [back] = proposals(store, "waiting")
    decide(store, YOU, accept=[back.id], clock=lambda: NOW)
    curate(store, agent, [("admit", {"name": "things/elm", "reason": "asked", "proposals": [back.id]})], tmp_path)
    assert store.article("things/elm").identity.status == "active"  # by its path, an archived one comes back


def test_an_accepted_proposal_whose_target_is_gone_or_done_closes_with_no_model(tmp_path: Path) -> None:
    things(tmp_path)
    (tmp_path / "agents" / "things" / "curator.md").write_text(  # it reads no article: nothing it reads moves
        "---\nevidence:\n  - held: {}\nheartbeat: [change]\n---\n\nKeep things.\n")
    (tmp_path / "settings.yaml").write_text("{}\n")
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW,
                 agent_run=PerAgent()) as workspace:
        store = workspace.store
        for id, status in (("things/alder", "active"), ("things/birch", "active"), ("things/dune", "active")):
            store.create(Identity(id, "things", id[7:].title(), {}, status))
        workspace.run_once()
        propose(store, Proposal("p_theirs", "article.archive", "things/dune", Caller("people/curator", "agent"),
                                stamp(NOW), reason="quiet"))
        curate(store, replace(workspace.agent("things/curator"), authority=Authority()), [
            ("archive", {"article": "things/dune", "reason": "quiet", "proposals": ["p_theirs"]})], tmp_path)
        [act] = [p.id for p in proposals(store, "waiting") if p.by.name == "things/curator"]
        decide(store, YOU, accept=[act], clock=lambda: NOW)
        rows = [told(store, "article.admit", "things/alder", "keep Alder", NOW),
                told(store, "article.archive", "things/birch", "archive it", NOW), act, "p_theirs"]
        for id in ("things/birch", "things/dune"):  # taken out by another road since
            gone = store.article(id).identity
            gone.status = "archived"
            store.set_identity(gone)
        result = workspace.run_once()
        assert "things/curator" not in result.due
        assert [(row(store, id).status, row(store, id).decision_reason) for id in rows] == [
            ("withdrawn", "things/alder is in the working set already, so nothing is left to do"),
            ("withdrawn", "things/birch is archived already, so nothing is left to do"),
            ("withdrawn", "things/dune is archived already, so nothing is left to do"),
            ("withdrawn", "things/dune is archived already, so nothing is left to do")]
        for id, mail in (("p_pat", "pat@one.example"), ("p_pat2", "pat@two.example")):  # one name, two things
            propose(store, Proposal(id, "article.admit", "things/pat", BY_THINGS, stamp(NOW),
                                    payload={"name": "Pat", "keys": {"email": [mail]}}))
        decide(store, YOU, accept=["p_pat", "p_pat2"], clock=lambda: NOW)
        curate(store, workspace.agent("things/curator"), [("admit", {"name": "Pat", "keys": {"email": [
            "pat@one.example"]}, "reason": "asked", "proposals": ["p_pat"]})], tmp_path)
        workspace.run_once()
        assert row(store, "p_pat2").status == "accepted"  # the other Pat is made at a free path, not closed


def test_an_admit_by_name_is_made_from_its_told_record_and_a_dropped_record_keeps_only_a_callers_own_admit(
        tmp_path: Path) -> None:
    agent = things(tmp_path)
    (tmp_path / "settings.yaml").write_text("{}\n")
    src = FakeConnector("src", [Record(id=id, source="src", revision="1", at=stamp(NOW), keys={"code": [id]},
                                       fields={"name": name}, body=f"{name} is listed.", into="things")
                                for id, name in (("P1", "Alder"), ("P2", "Birch"))])
    run = PerAgent()
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run,
                 connectors=[src]) as workspace:
        store = workspace.store
        workspace.run_once()
        mine = admit_by_hand(store, None, YOU, "things", "Alder", clock=lambda: NOW)
        run.calls[agent.id] = [("admit", {"record": {"source": "tell", "id": mine.record}, "keys": {"code": ["P1"]},
                                          "reason": "asked", "proposals": mine.proposals}),
                               ("admit", {"record": {"source": "src", "id": "P2"}, "reason": "a maker"})]
        workspace.curate(workspace.agent(agent.id))
        [theirs] = proposals(store, "waiting")
        decide(store, YOU, accept=[theirs.id], clock=lambda: NOW)
        run.calls[agent.id] = [("admit", {"record": {"source": "src", "id": "P2"}, "reason": "asked",
                                          "proposals": [theirs.id]})]
        workspace.curate(workspace.agent(agent.id))
        assert [(a.identity.id, a.identity.admitted_by) for a in store.query("things")] == [
            ("things/alder", YOU), ("things/birch", BY_THINGS)]
        workspace.run_once()  # reads both records into their articles
        src.remove("P1")
        src.remove("P2")
        workspace.run_once()
        assert [(a.identity.id, a.identity.status) for a in store.query("things")] == [
            ("things/alder", "active"), ("things/birch", "archived")]


# -- what a curator reads: its replies and its opening -----------------------------------------------------------------

def test_a_reply_says_done_waits_or_refused_and_which_proposals_it_carried_out(tmp_path: Path) -> None:
    import ast

    from cerebro.run import refused

    store, agent = MemoryStore(), things(tmp_path, admit=True)
    yes = told(store, "article.admit", "things/birch", "keep Birch", hour(0))
    done = curate(store, agent, [("admit", {"name": "Alder", "reason": "a maker"}),
                                 ("archive", {"article": "things/alder", "reason": "gone quiet"}),
                                 ("archive", {"article": "things/oak", "reason": "gone quiet"}),
                                 ("admit", {"name": "Birch", "reason": "asked", "proposals": [yes]})], tmp_path)
    made, waits, no, carried = map(ast.literal_eval, said(done))
    assert made["note"] == "things/alder is a new article in the working set."
    assert waits["note"] == f"waits for a caller, in {waits['proposed']}" and "waits_for" not in waits
    assert no["note"] == f"refused: {no['refused']}" and refused(no) and not refused(made)
    assert not refused({"rejected": [yes], "refused": {"p_x": "p_x is accepted already"}})  # reject's, in part
    assert carried["carried_out"] == [yes] and "answers" not in carried


def test_the_opening_marks_what_is_new_since_the_last_run_and_names_an_accepted_wake(tmp_path: Path) -> None:
    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things" / "curator.md").write_text(
        "---\nauthority: { admit: true }\nevidence:\n  - query: { from: things }\nheartbeat: [change]\n---\n\nKeep.\n")
    run = PerAgent()
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run) as ws:
        run.calls["things/curator"] = [("admit", {"name": "Alder", "reason": "a maker"})]
        ws.curate(ws.agent("things/curator"))
        assert "[new]" not in run.prompts["things/curator"][-1]  # a first run marks nothing
        yes = told(ws.store, "article.admit", "things/birch", "keep Birch", NOW)
        run.calls["things/curator"] = [("admit", {"name": "Birch", "reason": "asked", "proposals": [yes]})]
        ws.run_once()
        woken = run.prompts["things/curator"][-1]
        assert f"What woke this run: a proposal was accepted for your collection: {yes}" in woken
        assert "What woke this run: a change wake" not in woken  # the accepted proposal alone woke it
        assert "## Accepted proposals for your collection" in woken
        ws.store.create(Identity("things/cedar", "things", "Cedar"))
        ws.curate(ws.agent("things/curator"))
        marked = [line for line in run.prompts["things/curator"][-1].splitlines() if "[new]" in line]
        assert len(marked) == 1 and "Cedar" in marked[0]  # what its own acts made is not news to it
        admit_by_hand(ws.store, None, YOU, "things", "Fir Group")  # a bare name: no path to make it at is shown
        run.calls["things/curator"] = []
        ws.curate(ws.agent("things/curator"))
        assert "admit what a caller named Fir Group," in run.prompts["things/curator"][-1]
        assert "at things/fir-group" not in run.prompts["things/curator"][-1]


def test_a_curator_left_out_by_name_is_shown_again(tmp_path: Path) -> None:
    store, agent, prompts = MemoryStore(), things(tmp_path), []
    curate(store, agent, [("leave_out", {"name": "Cedar", "keys": {"domain": ["cedar.example"]},
                                         "reason": "named once"})], tmp_path, at=hour(0))
    curate(store, agent, [], tmp_path, prompts, at=hour(1))
    assert "## Candidates you left out" in prompts[0]
    assert "- Cedar, domain cedar.example: named once, 2026-11-17" in prompts[0]


def test_a_merged_path_is_followed_by_every_reader_and_a_proposer_is_shown_where_its_proposal_was_made(
        tmp_path: Path) -> None:
    store = MemoryStore()
    layer = Revisions(FakeExtraction(), store)
    store.create(Identity("things/alder", "things", "Alder"))
    store.create(Identity("things/alder-works", "things", "Alder Works"))
    store.create(Identity("parts/bolt", "parts", "Bolt"))
    store.set_field("parts/bolt", "linked", Property(["things/alder-works"], Caller("parts/curator", "agent"),
                                                     stamp(NOW)))
    store.write_compilation(Compilation("things/alder", "Overview", "Alder is a thing.", by="things/article"))
    keeper = things(tmp_path, merge=True)
    curate(store, keeper, [("merge", {"article": "things/alder", "other": "things/alder-works", "reason": "one"})],
           tmp_path)
    dated(store, layer, "things/alder", hour(1))  # a fact of the article the merged path leads to, and of it alone
    lookup, writer = StoreLookup(store, layer, clock=lambda: hour(2)), Caller("parts/article", "agent")
    (tmp_path / "agents" / "parts").mkdir(parents=True)
    (tmp_path / "agents" / "parts" / "article.md").write_text(
        "---\nevidence:\n  - articles: [this.linked]\n  - articles: [things/alder-works]\n---\n\nWrite it.\n")

    shown = render(gather(load_agent(tmp_path / "agents" / "parts" / "article.md", tmp_path / "agents"), lookup, {},
                          this="parts/bolt", now=hour(2)))
    assert "articles/things/alder.md" in shown and "does not exist yet" not in shown
    assert [f.text for f in lookup.query(writer, "facts", {"articles": "things/alder-works"}).items] == [
        "things/alder news."]
    assert [a.identity.id for a in lookup.query(writer, "things", {"id": "things/alder-works"}).items] == [
        "things/alder"]
    assert lookup.find(writer, "things/alder-works").matches == ["things/alder"]
    curate(store, keeper, [("instruct", {"article": "things/alder-works", "text": "Say what it is."})], tmp_path,
           at=hour(3))
    assert [p.target for p in proposals(store) if p.kind == "article.instruct"] == ["things/alder"]
    assert held_by(store, "parts/bolt", "things") == ["things/alder"]  # the label on what holds it
    bolt = Group([Row("p_1", "parts/curator", "parts", "Bolt", path="parts/bolt", linked=["things/alder-works"])])
    [top] = tree([bolt], {"things/alder": "Alder"}, redirects(store))  # the first-run tree
    assert top.article == ("things/alder", "Alder") and top.under == [bolt]
    store.append("merges", {"at": stamp(NOW), "redirect": "things/pegs", "to": "things/alder", "path": "things/pegs"})
    store.set_field("parts/bolt", "linked", Property(["things/pegs"], Caller("parts/curator", "agent"), stamp(NOW)))
    order = tmp_path / "order"
    for collection in ("parts", "things"):  # a path no article holds, which the first run led to another
        (order / collection).mkdir(parents=True)
        (order / collection / "article.md").write_text("---\nevidence:\n  - articles: [this.linked]\n---\n\nWrite.\n")
    agents = [load_agent(order / c / "article.md", order) for c in ("parts", "things")]
    assert collection_order(store, agents, ["parts", "things"]).groups == [["things"], ["parts"]]

    (tmp_path / "agents" / "parts" / "curator.md").write_text("---\nheartbeat: [change]\n---\n\nKeep parts.\n")
    proposer = load_agent(tmp_path / "agents" / "parts" / "curator.md", tmp_path / "agents")
    curate(store, proposer, [("admit", {"name": "Pat Quill", "reason": "a maker", "collection": "things"})], tmp_path,
           at=hour(4))
    [asked] = proposals(store, "waiting")
    curate(store, things(tmp_path, admit=True), [("admit", {"name": "Patricia Quill", "reason": "one maker",
                                                            "proposals": [asked.id]})], tmp_path, at=hour(5))
    quiet = run_curator(proposer, lookup=lookup, store=store, agent_run=ScriptedRun(lambda p: {}, []), root=tmp_path,
                        functions={}, agent_tools={}, clock=lambda: hour(6), changed_only=True)
    assert quiet.skipped  # the decision does not wake the proposer
    prompts: list[str] = []
    curate(store, proposer, [], tmp_path, prompts, at=hour(7))
    assert "- your proposal for things/pat-quill was made as things/patricia-quill" in prompts[0]


def test_an_item_the_budget_cut_wakes_no_quiet_pass_and_reads_new_when_first_shown(tmp_path: Path) -> None:
    store = MemoryStore()
    for n in range(40):
        store.create(Identity(f"things/t{n:02d}", "things", f"Thing {n:02d} " + "word " * 40))
    (tmp_path / "agents" / "things").mkdir(parents=True)
    file = tmp_path / "agents" / "things" / "curator.md"

    def agent(tokens: int) -> Agent:
        file.write_text(f"---\nevidence:\n  - query: {{ from: things }}\nbudget: {{ tokens_per_run: {tokens} }}\n"
                        "heartbeat: [change]\n---\n\nKeep things.\n")
        return load_agent(file, tmp_path / "agents")

    small, prompts = agent(3000), []
    curate(store, small, [], tmp_path, prompts)
    shown = {line for line in prompts[0].splitlines() if line.startswith("- [^things/")}
    assert 0 < len(shown) < 40  # the budget cut some
    quiet = run_curator(small, lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW), store=store,
                        agent_run=ScriptedRun(lambda p: {}, []), root=tmp_path, functions={}, agent_tools={},
                        clock=lambda: NOW, changed_only=True)
    assert quiet.skipped  # what was cut is no news
    curate(store, agent(400000), [], tmp_path, prompts)
    marked = [line for line in prompts[1].splitlines() if line.startswith("- [new] [^things/")]
    assert len(marked) == 40 - len(shown)  # each cut item reads new once shown, and nothing read before does
