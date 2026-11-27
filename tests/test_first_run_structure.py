"""How curators coordinate on a workspace's first run: a pending admit stands in for the
article it would make, so every curator can propose at once and weigh what the others proposed. Neutral names only:
the collections `things` and `parts`, a part record that links to its parent. Fakes only."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from test_first_run import Script, every_curator_weighs  # noqa: F401  (a fixture)

from cerebro import Cerebro
from cerebro.connector import Record
from cerebro.fakes import FakeConnector, FakeExtraction
from cerebro.first_run import KEY, Choices, run
from cerebro.held import stand_ins
from cerebro.records import proposals

NOW = datetime(2026, 11, 18, 9, tzinfo=UTC)
THINGS = "---\nevidence:\n  - held: {}\nheartbeat: [daily]\n---\n\nKeep the things.\n"
PARTS = "---\nevidence:\n  - held: {}\nheartbeat: [daily]\n---\n\nKeep the parts.\n"


def thing(id: str, name: str, code: str, domain: str = "") -> Record:
    keys = {"code": [code], "name": [name]} | ({"domain": [domain]} if domain else {})
    return Record(id=id, source="src", revision="1", at="2026-11-10T09:00:00Z", keys=keys, fields={"name": name},
                  body=f"{name}.", into="things")


def part(id: str, name: str, parent: str = "", email: str = "") -> Record:
    """A part, which links to its parent thing by that thing's code under the key both carry, as a record links the
    ids it belongs to, or names a party by an address."""
    keys = {"code": [id.lower()], "name": [name]} | ({"email": [email]} if email else {})
    return Record(id=id, source="src", revision="1", at="2026-11-10T09:00:00Z", keys=keys,
                  links=[parent] if parent else [], fields={"name": name, "parent": parent},
                  body=f"{name}, part of {parent or 'nothing'}.", into="parts")


RECORDS = [thing("T1", "Alder Works", "t-alder", "alder.example"), thing("T2", "Birch Hall", "t-birch"),
           part("P1", "Alder Gear", "t-alder"), part("P2", "Loose Pin", "t-nowhere")]


def lay_out_workspace(root: Path) -> Path:
    for collection, text in (("things", THINGS), ("parts", PARTS)):
        (root / "agents" / collection).mkdir(parents=True)
        (root / "agents" / collection / "curator.md").write_text(text)
    (root / "settings.yaml").write_text("{}\n")
    return root


class Source(FakeConnector):
    """The source of these tests, whose records bind by a key one value of which several things may share."""

    IDENTITY_KEYS: ClassVar[dict[str, int]] = {"shelves": 0}


def open_it(root: Path, script: Script, records: list[Record] | None = None) -> Cerebro:
    workspace = Cerebro(root, agent_run=script, extraction=FakeExtraction(), clock=lambda: NOW,
                        connectors=[Source("src", list(RECORDS if records is None else records))])
    script.store = workspace.store
    return workspace


def by_record(id: str, reason: str = "Named on two lists this month.") -> tuple[str, dict[str, Any]]:
    return ("admit", {"record": {"source": "src", "id": id}, "reason": reason, "group": "named in your sources"})


# -- stand-ins -----------------------------------------------------------------------------------------------------


def test_an_admit_from_a_held_record_keeps_its_ref_and_stands_in_for_its_article_until_the_list_is_decided(
        tmp_path: Path) -> None:
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None)
        [standing] = stand_ins(workspace.store)
        assert (standing.collection, standing.article, standing.name) == ("things", "things/alder-works", "Alder Works")
        assert standing.record == ("src", "T1") and standing.by == "things/curator"
        assert standing.keys["code"] == ["t-alder"]
        [filed] = proposals(workspace.store, "waiting")
        assert filed.payload["record"] == {"source": "src", "id": "T1"}
        run(workspace, choose=Choices.starter)
        assert workspace.store.state(KEY)["status"] == "done" and stand_ins(workspace.store) == []  # decided: none stand in


# -- a proposal into another collection is a proposal of that collection ----------------------------------------


def parent_of(record: str, reason: str = "Alder Gear belongs to it.") -> tuple[str, dict[str, Any]]:
    return ("admit", {"record": {"source": "src", "id": record}, "collection": "things", "reason": reason,
                      "group": "parents of your parts"})


@pytest.mark.usefixtures("every_curator_weighs")  # parts is asked before the parent of its part is proposed
def test_a_curator_proposing_the_parent_of_what_it_found_files_a_row_of_that_collection_and_a_second_is_not_refused(
        tmp_path: Path) -> None:
    from cerebro.first_run import LocalApproval

    script = Script({"parts/curator": [by_record("P1"), parent_of("T1"), parent_of("T1", "Named again.")],
                     "things/curator": [by_record("T1")]})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None)
        waiting = proposals(workspace.store, "waiting")
        alder = [p for p in waiting if p.target == "things/alder-works"]
        assert sorted(p.by.name for p in alder) == ["parts/curator", "things/curator"]  # a repeat is not filed
        filed = next(p for p in alder if p.by.name == "parts/curator")
        assert filed.payload["record"] == {"source": "src", "id": "T1"}
        assert filed.payload["keys"]["code"] == ["t-alder"]
        pile = LocalApproval(workspace).pile()
        things = next(listed for listed in pile.lists if listed.collection == "things")
        assert [row.agent for row in things.proposed()] == ["things/curator", "parts/curator"]  # the person sees both
        assert script.ran.count("things/curator") == 1  # one round: the row aimed at it is the person's to tick


def test_the_same_curator_does_not_file_one_parent_twice_and_a_record_not_held_there_is_refused(tmp_path: Path) -> None:
    script = Script({"parts/curator": [], "things/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None)  # discovery read the sources and holds them; the curators filed nothing
        script.calls["parts/curator"] = [parent_of("T1"), parent_of("T1", "Named again."), parent_of("P2")]
        done = workspace.curate(workspace.agent("parts/curator"))
        assert [p.target for p in proposals(workspace.store, "waiting")] == ["things/alder-works"]
        assert done.run is not None
        said = [e.text for e in done.run.transcript if e.role == "tool"]
        assert "waits already" in said[1] and "with nothing new" in said[1]
        assert "admit: no record of src with id P2 is held for things: admit it by name and keys with " \
               "collection things" in said[2]


# -- held lines on a first run -------------------------------------------------------------------------------------


def held_lines(workspace: Cerebro, collection: str, ours: Any = None) -> dict[str, dict[str, Any]]:
    from cerebro.connector import NOBODY
    from cerebro.held import _data, held_for

    found = held_for(workspace.store, collection, ["src"], ours=ours or NOBODY)
    return {item.id: _data(item) for item in found.items}


def test_a_held_line_names_the_articles_it_would_hang_off_marks_the_proposed_ones_and_says_when_it_is_proposed(
        tmp_path: Path) -> None:
    from cerebro.article import Identity
    from cerebro.connector import Ours

    records = [*RECORDS, part("P3", "Birch Hinge", "t-birch"), part("P4", "Note from Alder", email="sam@alder.example"),
               ]
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": [by_record("T1")], "parts/curator": []}),
                 records) as workspace:
        run(workspace, choose=None)
        workspace.store.create(Identity("things/birch-hall", "things", "Birch Hall", {"code": ["t-birch"]}, "active"))
        parts = held_lines(workspace, "parts", Ours(domains=("ours.example",)))
        assert parts["P1"]["links_to"] == [{"id": "things/alder-works", "proposed_by": "agents/things/curator.md"}]
        assert parts["P3"]["links_to"] == [{"id": "things/birch-hall"}]  # in the working set: no proposer
        assert parts["P4"]["links_to"] == [  # an outside party's address meets the domain the proposal holds
            {"id": "things/alder-works", "proposed_by": "agents/things/curator.md"}]
        assert "links_to" not in parts["P2"]  # it links only to an article nobody made or proposed: shown all the same
        things = held_lines(workspace, "things")
        assert things["T1"]["proposed"] == {"id": "things/alder-works", "by": "agents/things/curator.md"}
        assert "T2" not in things  # things/birch-hall, in the working set, binds it: in scope, so not held


def test_a_held_line_carries_no_first_run_marks_once_the_list_is_decided(tmp_path: Path) -> None:
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": [], "parts/curator": []})) as workspace:
        run(workspace, choose=Choices.starter)  # nothing proposed: decided at once
        parts = held_lines(workspace, "parts")
        assert set(parts) == {"P1", "P2"}  # the first run is over: nothing waits for a proposal
        assert all("links_to" not in line and "proposed" not in line for line in parts.values())


def test_what_a_record_links_to_is_its_links_under_its_keys_and_the_domains_of_its_outside_parties_never_yours() -> None:
    from cerebro.connector import Ours
    from cerebro.held import linked

    note = part("P9", "Mixed note", "t-alder", email="kim@ours.example")
    note.keys["email"].append("lee@mail.cedar.example")
    assert linked(note, Ours(domains=("ours.example",))) == {("code", "t-alder"), ("name", "t-alder"),
                                                             ("email", "t-alder"), ("domain", "cedar.example")}


# -- what a held line shows beyond keys -----------------------------------------------------------------------------


def test_a_held_line_shows_the_outside_domains_of_its_parties_and_the_names_of_yours(tmp_path: Path) -> None:
    from cerebro.connector import Ours

    talk = part("P7", "Planning talk", "t-alder", email="lee@mail.cedar.example")
    talk.keys["email"].append("ana-example@gmail.com")  # a public mail service is no organisation
    talk.fields["ours"] = ["Kim Vale"]
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": [by_record("T1")], "parts/curator": []}),
                 [*RECORDS, talk]) as workspace:
        run(workspace, choose=None)
        line = held_lines(workspace, "parts", Ours(domains=("ours.example",)))["P7"]
        assert line["outside"] == ["cedar.example"] and line["of_this_workspaces_own_side"] == ["Kim Vale"]
        assert "ours" not in line  # a bare "ours" beside the record's other names read as all of them being ours
        assert "outside" not in held_lines(workspace, "parts")["P1"]  # a record that names no party by address



class Seen(Script):
    """A `Script` that keeps the text of each run's evidence, by curator, in the order the runs came."""

    def __init__(self, calls: dict[str, list[tuple[str, dict[str, Any]]]]) -> None:
        super().__init__(calls)
        self.read: dict[str, list[str]] = {}

    def run(self, agent: Any, evidence: list[Any], tools: list[Any], budget: Any) -> Any:
        self.read.setdefault(agent.id, []).append("\n".join(line for entry in evidence for line in entry.lines))
        return super().run(agent, evidence, tools, budget)


def test_the_first_run_lists_the_held_records_of_the_last_two_weeks_and_a_later_run_lists_the_older(
        tmp_path: Path) -> None:
    old = Record(id="T9", source="src", revision="1", at="2026-10-01T09:00:00Z",
                 keys={"code": ["t-oak"], "name": ["Old Oak"]}, fields={"name": "Old Oak"}, body="Old Oak.",
                 into="things")
    script = Seen({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script, [*RECORDS, old]) as workspace:
        run(workspace, choose=Choices.starter)
        first, build = script.read["things/curator"]  # discovery, then the run that makes what was approved
        assert "Birch Hall" in first and "Old Oak" not in first
        assert "2026-11-04" in first  # the older one is counted, from the day the window opens
        assert "Old Oak" in build


# -- every curator reads the queue as discovery began -----------------------------------------------------------


def loose_and_its_holder() -> list[Record]:
    """A part with no parent, and a thing that links to that part's code."""
    holder = Record(id="T3", source="src", revision="1", at="2026-11-10T09:00:00Z",
                    keys={"code": ["t-cedar"], "name": ["Cedar Rack"]}, links=["p5"], fields={"name": "Cedar Rack"},
                    body="Cedar Rack, which holds the free bolt.", into="things")
    return [*RECORDS, part("P5", "Free Bolt"), holder]


def test_a_curator_does_not_weigh_what_another_proposes_beside_it(tmp_path: Path) -> None:
    script = Seen({"parts/curator": [by_record("P5")], "things/curator": [by_record("T1")]})
    with open_it(lay_out_workspace(tmp_path), script, loose_and_its_holder()) as workspace:
        run(workspace, choose=None)
        # parts proposes parts/free-bolt; things reads the queue as discovery began, so the thing that links to it
        # is shown linked to nothing
        assert sorted(script.ran) == ["parts/curator", "things/curator"]
        [first] = script.read["things/curator"]
        assert "Cedar Rack" in first and "parts/free-bolt" not in first
        assert sorted(p.by.name for p in proposals(workspace.store, "waiting")) == ["parts/curator", "things/curator"]
        assert workspace.store.state(KEY)["pinned"] is None


def test_discovery_keeps_the_curators_in_the_order_of_their_ids_whatever_one_reads_of_another(tmp_path: Path,
                                                                                               monkeypatch: Any) -> None:
    monkeypatch.setattr(Cerebro, "weighs_nothing", lambda self, agent: False)
    root = lay_out_workspace(tmp_path)
    (root / "agents" / "parts" / "curator.md").write_text(PARTS.replace("  - held: {}\n",
                                                                        "  - held: {}\n  - query: {from: things}\n"))
    script = Script({"parts/curator": [], "things/curator": []})
    with open_it(root, script) as workspace:
        from cerebro.curate import curator_order

        curators = [a for a in workspace.agents if a.role == "curator"]
        assert [a.id for a in curator_order(curators)] == ["things/curator", "parts/curator"]  # a pass's order
        said: list[str] = []
        run(workspace, choose=None, say=said.append)
        assert sorted(script.ran[:2]) == ["parts/curator", "things/curator"]  # side by side
        assert [line.split(", by agents/")[1].split(".md")[0] for line in said if line.startswith("proposed ")][:2] == [
            "parts/curator", "things/curator"]  # said and kept by id, whichever finished first


# -- a curator run by hand, a left-out record, a run's guess -------------------------------------------------------


def test_a_curator_run_by_hand_on_a_new_workspace_reads_the_sources_first_and_discovery_reads_them_once(
        tmp_path: Path) -> None:
    from cerebro.first_run import by_hand

    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        said: list[str] = []
        agent = workspace.agent("things/curator")
        with by_hand(workspace, agent, said.append) as count, workspace.hold(agent):  # as cerebro agents run does
            count(workspace.curate(agent).run)
        assert said == ["read      4 records from 1 source, held until you decide the first run's list"]
        assert [p.target for p in proposals(workspace.store, "waiting")] == ["things/alder-works"]  # it had the records
        assert workspace.store.state(KEY)["read"] is True
        told: list[str] = []
        run(workspace, choose=None, say=told.append)
        assert not any(line.startswith(("listed", "read ")) for line in told)  # read once, by the run by hand


def test_a_left_out_record_with_no_name_is_listed_by_its_file_name(tmp_path: Path) -> None:
    from cerebro.first_run import LocalApproval

    nameless = Record(id="notes/standup.md", source="src", revision="1", at="2026-11-10T09:00:00Z",
                      keys={"code": ["t-standup"]}, body="A standup.", into="things")
    leave = ("leave_out", {"record": {"source": "src", "id": "notes/standup.md"}, "reason": "Only a standup."})
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": [leave], "parts/curator": []}),
                 [*RECORDS, nameless]) as workspace:
        run(workspace, choose=None)
        things = next(listed for listed in LocalApproval(workspace).pile().lists if listed.collection == "things")
        assert [row.name for row in things.left() if row.reason != "not weighed"] == ["standup"]


def test_an_own_budget_lowers_the_guess_of_a_run_and_never_raises_it() -> None:
    from cerebro.budget import DEFAULT_RUN, guessed_run

    assert guessed_run(None) == guessed_run(1_000_000) == DEFAULT_RUN  # a budget is a ceiling, not a spend
    small = guessed_run(3_300)
    assert (small.input_tokens, small.output_tokens) == (3_000, 300)


# -- after the build, only a curator that failed or had nothing to read at discovery runs late --------------------


class Fails(Script):
    """A `Script` whose runs of `agent` fail the first `times`, as a model call can."""

    def __init__(self, calls: dict[str, list[tuple[str, dict[str, Any]]]], agent: str, times: int) -> None:
        super().__init__(calls)
        self.failing, self.times = agent, times

    def run(self, agent: Any, evidence: list[Any], tools: list[Any], budget: Any) -> Any:
        if agent.id == self.failing and self.times > 0:
            self.times -= 1
            self.ran.append(agent.id)
            raise RuntimeError("the model did not answer")
        return super().run(agent, evidence, tools, budget)


def test_after_the_build_a_curator_that_took_part_in_discovery_is_not_asked_late(tmp_path: Path) -> None:
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        out = run(workspace, choose=Choices.starter)
        assert workspace.store.state(KEY)["lists"]["parts/curator"]["state"] == "nothing"  # it read held parts
        assert script.ran.count("parts/curator") == 1 and out is not None and out.late is None


# -- a query of a collection on a first run gives its proposed articles, marked -------------------------------------


def test_a_query_of_a_collection_on_a_first_run_gives_its_pending_admits_as_marked_stand_ins(tmp_path: Path) -> None:
    from cerebro.caller import Caller

    curator = Caller("parts/curator", "agent")
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None)
        [admit] = proposals(workspace.store, "waiting")
        found = workspace.lookup.query(curator, "things")
        assert found.items == [{"id": "things/alder-works", "title": "Alder Works", "proposed": True,
                                "proposal": admit.id, "proposed_by": "agents/things/curator.md",
                                "keys": admit.payload["keys"], "record": {"source": "src", "id": "T1"}}]
        assert found.count == 1 and "proposed on the workspace's first run and not made yet" in found.warnings[0]
        assert workspace.lookup.query(curator, "things", {"title": "Alder Works"}).items == []  # a filtered query: none
        run(workspace, choose=Choices.starter)
        after = workspace.lookup.query(curator, "things")
        assert [getattr(item, "identity", None) and item.identity.id for item in after.items] == ["things/alder-works"]
        assert not any(isinstance(item, dict) for item in after.items)  # decided: the article, and no stand-in
