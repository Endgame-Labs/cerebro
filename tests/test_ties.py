"""How a fact is tied to each article it links to, on a workspace with two groups and their members: a fact that is
about another group's member reaches this group's writers labelled with whose member it is, and nothing is left out
for anyone. The accounts of the gtm template are the groups here, and their people the members."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import GTM

from cerebro.agent import load_agent
from cerebro.article import Identity, Property
from cerebro.caller import Caller
from cerebro.connector import Record
from cerebro.evidence import Evidence, gather
from cerebro.fakes import FakeExtraction
from cerebro.identity import link
from cerebro.lookup import OperationError, StoreLookup
from cerebro.mcp import render_query, render_read
from cerebro.reply import outside
from cerebro.store import SQLiteStore

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
SYNC = Caller("sync", "source")
YOU = Caller("you", "person")
KINDS = ["identity", "relationship", "decision_commitment", "goal", "timeline", "risk_objection"]
NAMES = ["Mara Quill", "Theo Brandt", "Lena Voss", "Ivo Petrak", "Pia Lund", "Acme", "Initech"]
PEOPLE = {"accounts/acme": [("people/mara-quill", "Mara Quill"), ("people/theo-brandt", "Theo Brandt")],
          "accounts/initech": [("people/lena-voss", "Lena Voss"), ("people/ivo-petrak", "Ivo Petrak")]}
KICKOFFS = [
    Record(id="calls/acme-kickoff", source="local", revision="1", at="2026-11-03T09:00:00Z",
           keys={"account": ["Acme"], "attendees": ["Mara Quill, Theo Brandt"]},
           body="Mara Quill owns the decision on the pilot.\n\nTheo Brandt is the buyer and signs the order form."),
    Record(id="calls/initech-kickoff", source="local", revision="1", at="2026-11-05T09:00:00Z",
           keys={"account": ["Initech"], "attendees": ["Lena Voss, Ivo Petrak"]},
           body="Lena Voss owns the decision on the rollout.\n\nIvo Petrak, the finance lead, holds the budget."),
]
# The three cases, each a note whose one fact is of `kind`: the gtm subagents that read that kind are handed it.
FILED_HERE = "Theo Brandt, Acme's buyer, signs Acme's order form."  # filed under Initech, about Acme's buyer
NAMES_HERE = "Theo Brandt said his own sign-off waits on Initech."  # filed under Acme, naming Initech
REFERENCE = "Theo Brandt vouched for us to Lena Voss on the Initech reference call."
ADVISES = "Pia Lund advises Initech and Acme on the decision."
WRITERS = {"decision_commitment": ["stakeholders", "overview"], "timeline": ["overview", "work-streams"]}
SUBAGENTS = ["stakeholders", "overview", "work-streams"]


def note(id: str, account: str, body: str, kind: str, attendees: str = "") -> Record:
    return Record(id=f"calls/{id}-{kind}", source="local", revision="1", at="2026-11-07T09:00:00Z",
                  keys={"account": [account], **({"attendees": [attendees]} if attendees else {})},
                  fields={"kind": kind}, body=body)


class Workspace:
    """Two accounts, two people at each and a note for each, through the real store, lookup and linking, with the
    facts layer's stand-in giving each fact a subject and mentions as Factoid does."""

    def __init__(self, root: Path, fields: bool):
        self.root = root
        self.extraction = FakeExtraction(KINDS, names=NAMES)
        self.store = SQLiteStore(root / "workspace.sqlite")
        self.store.create(Identity("accounts/acme", "accounts", "Acme", {"domain": ["acme.example"]}))
        self.store.create(Identity("accounts/initech", "accounts", "Initech", {"domain": ["initech.example"]}))
        for account, people in PEOPLE.items():
            for id, name in people:
                self.store.create(Identity(id, "people", name, {"email": [f"{name.split()[0].lower()}@x.example"]}))
                if fields:
                    self.store.set_field(id, "account", Property(account, SYNC, "2026-11-01T00:00:00Z"))
        self.store.create(Identity("people/pia-lund", "people", "Pia Lund", {"email": ["pia@lundworks.example"]}))
        for record in KICKOFFS:
            self.note(record)
        self.lookup = StoreLookup(self.store, self.extraction, clock=lambda: NOW)

    def note(self, record: Record) -> str:
        """The note's one fact, linked as a pass links it, by its id."""
        facts = self.extraction.extract(record).facts
        for fact in facts:
            link(self.store, fact, record, at=record.at)
        return facts[-1].id

    def handed(self, sub: str, this: str = "accounts/initech") -> list[Evidence]:
        agent = load_agent(GTM / "accounts" / "subagents" / f"{sub}.md", GTM)
        return gather(agent, self.lookup, {}, this=this, root=self.root, now=NOW)

    def line(self, sub: str, fact: str, this: str = "accounts/initech") -> str | None:
        return next((item.text for entry in self.handed(sub, this) for item in entry.items
                     if item.key == f"fact:{fact}"), None)


@pytest.fixture(params=["with account fields", "notes alone"])
def workspace(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Workspace]:
    made = Workspace(tmp_path, fields=request.param == "with account fields")
    try:
        yield made
    finally:
        made.store.close()


def about(workspace: Workspace, fact: str) -> dict[str, list[str]]:
    return workspace.store.ties_of(fact)


@pytest.mark.parametrize("kind", sorted(WRITERS))
def test_a_note_filed_here_about_the_other_accounts_buyer_is_handed_with_whose_buyer_he_is(
        workspace: Workspace, kind: str) -> None:
    fact = workspace.note(note("initech-order", "Initech", FILED_HERE, kind))
    assert about(workspace, fact) == {"accounts/initech": ["record"], "people/theo-brandt": ["subject"],
                                      "accounts/acme": ["mentions"]}
    for sub in SUBAGENTS:
        line = workspace.line(sub, fact)
        assert (line is not None) is (sub in WRITERS[kind])
        assert line is None or line.endswith(
            ' [from this article\'s record; subject: "Theo Brandt", of "Acme"]')
    assert workspace.line("stakeholders" if kind == "decision_commitment" else "work-streams", fact,
                          "accounts/acme").endswith('[mentions; subject: "Theo Brandt"]')


@pytest.mark.parametrize("kind", sorted(WRITERS))
def test_a_note_filed_under_the_other_account_that_names_this_one_is_handed_as_a_mention(
        workspace: Workspace, kind: str) -> None:
    fact = workspace.note(note("acme-signoff", "Acme", NAMES_HERE, kind))
    assert about(workspace, fact) == {"accounts/acme": ["record"], "people/theo-brandt": ["subject"],
                                      "accounts/initech": ["mentions"]}
    for sub in SUBAGENTS:
        line = workspace.line(sub, fact)
        assert (line is not None) is (sub in WRITERS[kind])
        assert line is None or line.endswith(' [mentions; subject: "Theo Brandt", of "Acme"]')


@pytest.mark.parametrize("kind", sorted(WRITERS))
def test_a_reference_call_where_the_other_accounts_person_vouches_to_this_ones_is_handed_to_both(
        workspace: Workspace, kind: str) -> None:
    fact = workspace.note(note("acme-reference", "Acme", REFERENCE, kind, "Theo Brandt, Lena Voss"))
    assert about(workspace, fact) == {"accounts/acme": ["record"], "people/theo-brandt": ["subject"],
                                      "people/lena-voss": ["mentions"], "accounts/initech": ["mentions"]}
    for sub in SUBAGENTS:
        line = workspace.line(sub, fact)
        assert (line is not None) is (sub in WRITERS[kind])
        assert line is None or line.endswith(' [mentions; subject: "Theo Brandt", of "Acme"]')
        acme = workspace.line(sub, fact, "accounts/acme")
        assert (acme is not None) is (sub in WRITERS[kind])
        assert acme is None or acme.endswith(" [from this article's record; subject: \"Theo Brandt\"]")


@pytest.mark.parametrize(("account", "of"), [
    ("accounts/acme", ', of "Acme"'), (["accounts/acme", "accounts/initech"], ""), (None, "")])
def test_a_consultant_who_advises_both_is_held_by_the_account_their_front_matter_names(
        workspace: Workspace, account: str | list[str] | None, of: str) -> None:
    if account is not None:
        workspace.store.set_field("people/pia-lund", "account", Property(account, SYNC, "2026-11-01T00:00:00Z"))
    fact = workspace.note(note("joint-review", "Initech, Acme", ADVISES, "decision_commitment", "Pia Lund"))
    assert about(workspace, fact) == {"people/pia-lund": ["subject", "record"], "accounts/initech": ["mentions"],
                                      "accounts/acme": ["mentions"]}
    for sub in WRITERS["decision_commitment"]:
        assert workspace.line(sub, fact).endswith(f' [mentions; subject: "Pia Lund"{of}]')


def test_this_accounts_own_facts_carry_no_label_and_a_fact_from_its_record_about_its_own_person_names_no_other(
        workspace: Workspace) -> None:
    lena = workspace.store.facts_of("people/lena-voss")
    line = workspace.line("stakeholders", lena[0])
    assert line.endswith(' [from this article\'s record; subject: "Lena Voss"]')
    plain = workspace.note(note("initech-plain", "Initech", "Initech set a timeline for March.", "timeline"))
    assert workspace.line("work-streams", plain).endswith("calls/initech-plain-timeline)")


def everything(workspace: Workspace) -> dict[str, str]:
    return {id: workspace.note(note(id, account, body, "decision_commitment", attendees))
            for id, account, body, attendees in [("filed-here", "Initech", FILED_HERE, ""),
                                                 ("names-here", "Acme", NAMES_HERE, ""),
                                                 ("reference", "Acme", REFERENCE, "Theo Brandt, Lena Voss"),
                                                 ("advises", "Initech, Acme", ADVISES, "Pia Lund")]}


def test_a_person_who_queries_the_account_sees_every_case_marked_and_a_relation_filter_narrows_it(
        workspace: Workspace) -> None:
    cases = everything(workspace)
    every = workspace.lookup.query(YOU, "facts", {"articles": "accounts/initech"})
    assert set(cases.values()) <= {f.id for f in every.items}
    assert every.ties == {cases["filed-here"]: "record", cases["names-here"]: "mentions",
                          cases["reference"]: "mentions", cases["advises"]: "mentions",
                          **{f.id: "record" for f in workspace.extraction.facts(record="calls/initech-kickoff")}}
    reply = {item["id"]: item for item in outside(every)["items"]}
    assert reply[cases["filed-here"]]["via"] == "record" and "relation" not in reply[cases["filed-here"]]
    assert reply[cases["reference"]]["relation"] == "mentions" and "via" not in reply[cases["reference"]]
    text = render_query(every)
    assert f"{REFERENCE} [relation: mentions]" in text and f"{FILED_HERE} [via: record]" in text

    named = workspace.lookup.query(YOU, "facts", {"articles": "accounts/initech", "relation": ["subject", "mentions"]})
    assert {f.id for f in named.items} == {cases["names-here"], cases["reference"], cases["advises"]}
    assert workspace.lookup.query(YOU, "facts", {"articles": "accounts/initech", "relation": "subject"}).items == []
    theo = workspace.lookup.query(YOU, "facts", {"articles": "people/theo-brandt", "relation": "subject"})
    assert {cases["filed-here"], cases["names-here"], cases["reference"]} <= {f.id for f in theo.items}
    assert theo.ties == {} and all("relation" not in i and "via" not in i for i in outside(theo)["items"])
    with pytest.raises(OperationError, match="relation takes subject or mentions"):
        workspace.lookup.query(YOU, "facts", {"articles": "accounts/initech", "relation": "record"})
    with pytest.raises(OperationError, match="with no articles"):
        workspace.lookup.query(YOU, "facts", {"relation": "subject"})


def test_a_read_of_the_account_marks_each_fact_not_about_it(workspace: Workspace) -> None:
    cases = everything(workspace)
    read = workspace.lookup.read(YOU, ["accounts/initech"])
    assert set(cases.values()) <= {f.id for f in read.facts}
    assert read.ties[cases["reference"]] == "mentions" and read.ties[cases["filed-here"]] == "record"
    facts = {f["id"]: f for f in outside(read)["facts"]}
    assert facts[cases["advises"]]["relation"] == "mentions" and facts[cases["filed-here"]]["via"] == "record"
    assert f"{ADVISES} [relation: mentions]" in render_read(read)


def test_what_a_mark_costs_a_caller_in_characters(workspace: Workspace) -> None:
    cases = everything(workspace)
    every = outside(workspace.lookup.query(YOU, "facts", {"articles": "accounts/initech"}))
    sizes = {item["id"]: len(json.dumps({k: v for k, v in item.items() if k in ("relation", "via")})) - 2
             for item in every["items"]}
    assert sizes[cases["reference"]] == len('"relation": "mentions"')
    assert sizes[cases["filed-here"]] == len('"via": "record"')


def test_a_title_in_a_label_is_quoted_so_it_can_neither_end_the_label_nor_start_a_line(workspace: Workspace) -> None:
    fact = workspace.note(note("acme-signoff", "Acme", NAMES_HERE, "decision_commitment"))
    workspace.store.set_identity(Identity("accounts/acme", "accounts", 'Acme "Labs"]\n[mentions; subject: "Ivo Petrak"',
                                          {"domain": ["acme.example"]}))
    line = workspace.line("stakeholders", fact)
    assert "\n" not in line
    assert line.endswith(r' [mentions; subject: "Theo Brandt", of "Acme \"Labs\"]\\n[mentions; subject: \"Ivo '
                         r'Petrak\""]')


def test_a_facts_line_may_keep_one_relation(workspace: Workspace, tmp_path: Path) -> None:
    cases = everything(workspace)
    path = tmp_path / "agents" / "accounts" / "subagents" / "about-it.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nevidence:\n  - facts: { relation: [subject, mentions] }\n---\n\n# Instructions\n\nWrite.\n")
    (facts,) = gather(load_agent(path, tmp_path / "agents"), workspace.lookup, {}, this="accounts/initech",
                      root=workspace.root, now=NOW)
    handed = {item.key.removeprefix("fact:") for item in facts.items}
    assert handed == {cases["names-here"], cases["reference"], cases["advises"]}
    path.write_text("---\nevidence:\n  - facts: { relation: record }\n---\n\n# Instructions\n\nWrite.\n")
    with pytest.raises(Exception, match="facts relation is 'record': write subject or mentions"):
        load_agent(path, tmp_path / "agents")


def test_the_labels_of_a_writers_hundred_facts_on_a_large_wiki_take_under_a_tenth_of_a_second(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import random
    import sys
    import time

    from cerebro.facts import Fact, Mention

    evidence = sys.modules["cerebro.evidence"]

    pick = random.Random(7)
    store = SQLiteStore(tmp_path / "large.sqlite")
    groups = [f"accounts/a{n}" for n in range(200)]
    members = [f"people/p{n}" for n in range(1800)]
    with store.exclusive():  # the large wiki is written in one transaction: what is timed is the read below
        for id in groups + members:
            store.create(Identity(id, id.split("/")[0], id.split("/")[1]))
        for member in members:
            store.set_field(member, "account", Property(pick.choice(groups), SYNC, "2026-11-01T00:00:00Z"))
        for n in range(5000):
            store.link(f"f{n}", [pick.choice(members)], "2026-11-01T00:00:00Z", "subject")
            store.link(f"f{n}", [pick.choice(groups)], "2026-11-01T00:00:00Z", "record")
            store.link(f"f{n}", [pick.choice(groups), pick.choice(members)], "2026-11-01T00:00:00Z", "mentions")
    extraction = FakeExtraction()
    extraction.by_record["calls/large"] = [
        Fact(id=f"g{n}", text=f"p{n % 10} said something.", subject=f"p{n % 10}", kind="note", at="2026-11-10T09:00:00Z",
             source="local", record="calls/large", mentions=[Mention(f"p{n % 10}", "subject")]) for n in range(100)]
    for n in range(100):
        store.link(f"g{n}", ["accounts/a0"], "2026-11-10T09:00:00Z", "record")
        store.link(f"g{n}", [f"people/p{n % 10}"], "2026-11-10T09:00:00Z", "subject")
    assert len(store.facts_of("accounts/a0")) >= 100
    spent = [0.0]
    tied = evidence._Gatherer.tied

    def timed(self: Any, *args: Any) -> Any:
        start = time.perf_counter()
        try:
            return tied(self, *args)
        finally:
            spent[0] += time.perf_counter() - start

    monkeypatch.setattr(evidence._Gatherer, "tied", timed)
    path = tmp_path / "agents" / "accounts" / "subagents" / "facts.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nevidence:\n  - facts: {}\n---\n\n# Instructions\n\nWrite.\n")
    lookup = StoreLookup(store, extraction, clock=lambda: NOW)
    (facts,) = gather(load_agent(path, tmp_path / "agents"), lookup, {}, this="accounts/a0", root=tmp_path, now=NOW)
    labelled = [item for item in facts.items if "; subject: " in item.text]
    store.close()
    assert len(labelled) == 100 and spent[0] < 0.1


def test_the_grounding_judge_is_given_a_labelled_fact_byte_for_byte_as_with_no_label(
        workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from cerebro.compile import _Grounding
    from cerebro.fakes import FakeDecider
    from cerebro.ground import ground

    evidence = sys.modules["cerebro.evidence"]
    cases = everything(workspace)
    agent = load_agent(GTM / "accounts" / "subagents" / "stakeholders.md", GTM)
    parts = {"Stakeholders": f"Theo Brandt vouched for us to Lena Voss [^{cases['reference']}]. Theo Brandt signs "
                                f"Acme's order form [^{cases['filed-here']}]. Pia Lund advises both [^{cases['advises']}]."}

    def judged() -> tuple[str, str]:
        handed = gather(agent, workspace.lookup, {}, this="accounts/initech", root=workspace.root, now=NOW)
        citable = {c.id: c for entry in handed for item in entry.items for c in item.citations}
        decider = FakeDecider()
        grounding = _Grounding(ground, "accounts/initech", lookup=workspace.lookup, root=workspace.root, ledger=None,
                               clock=lambda: NOW, decider=decider, decision=None, zone=UTC)
        grounding(agent, parts, citable, handed)
        lines = "\n".join(item.text for entry in handed for item in entry.items)
        return json.dumps([state for state, _ in decider.asked], sort_keys=True, default=str), lines

    labelled, read = judged()
    assert '[mentions; subject: "Theo Brandt", of "Acme"]' in read
    monkeypatch.setattr(evidence._Gatherer, "tied", lambda self, item, *rest: item)
    plain, unread = judged()
    assert "subject: " not in unread
    assert REFERENCE in labelled and "mentions;" not in labelled and labelled == plain


# -- a subject of the workspace's own side, as the fact's record says --------------------------------------------------

OURS = "Sam Reed from our side owns the pilot plan for Initech."


class OwnSide(Workspace):
    """The workspace above, whose facts layer also knows a colleague of ours by name, as a note names one."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, fields=False)
        self.extraction = FakeExtraction(KINDS, names=[*NAMES, "Sam Reed"])
        self.lookup = StoreLookup(self.store, self.extraction, clock=lambda: NOW)


def test_a_fact_whose_subject_its_record_names_as_ours_is_labelled_of_this_workspaces_own_side(tmp_path: Path) -> None:
    workspace = OwnSide(tmp_path)
    try:
        record = note("initech-plan", "Initech", OURS, "decision_commitment", "Ana Diaz, Sam Reed")
        fact = workspace.note(record)
        assert workspace.line("stakeholders", fact).endswith(' [mentions; subject: "Sam Reed"]')  # no ours yet
        workspace.store.set_state("held:local", [{"id": record.id, "fields": {"ours": ["Sam Reed"]}}])
        held = workspace.line("stakeholders", fact)  # while its held row stands, the row says who is ours
        assert held.endswith(' [mentions; subject: "Sam Reed", of this workspace\'s own side]')
        workspace.store.set_state("held:local", [])
        workspace.store.set_state("ours:local", {record.id: ["sam reed"]})  # as extraction keeps it, any case
        assert workspace.line("stakeholders", fact) == held
    finally:
        workspace.store.close()


def test_a_record_of_names_alone_marks_no_subject_as_ours(tmp_path: Path) -> None:
    workspace = OwnSide(tmp_path)
    try:
        record = note("initech-names", "Initech", OURS, "decision_commitment", "Ana Diaz, Sam Reed")
        fact = workspace.note(record)
        workspace.store.set_state("ours:local", {"calls/another-note": ["Sam Reed"]})  # another record's, not this
        assert "own side" not in workspace.line("stakeholders", fact)
    finally:
        workspace.store.close()
