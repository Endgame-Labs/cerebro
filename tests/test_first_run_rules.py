"""The first run's rules. Neutral names only: the collections `things`, `parts` and `members`.
Fakes only."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_first_run import Script, carried, every_curator_weighs, review_rest, settled  # noqa: F401  (a fixture)
from test_first_run_structure import by_record, lay_out_workspace, loose_and_its_holder, open_it

from cerebro.first_run import KEY, Choices, run

MEMBERS = "---\nevidence:\n  - held: {}\nheartbeat: [daily]\n---\n\nKeep the members.\n"


# -- a curator with nothing to weigh, and no source that answers questions, is left out with no model call


def test_a_curator_that_fetches_nothing_and_has_nothing_to_weigh_is_not_asked_and_is_off_the_screen(
        tmp_path: Path) -> None:
    from cerebro.first_run import LocalApproval, lines

    root = lay_out_workspace(tmp_path)
    (root / "agents" / "members").mkdir()
    (root / "agents" / "members" / "curator.md").write_text(MEMBERS)  # no record goes into members
    said: list[str] = []
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(root, script) as workspace:
        run(workspace, choose=None, say=said.append)
        assert workspace.store.state(KEY)["lists"]["members/curator"]["state"] == "empty"
        shown = lines(LocalApproval(workspace).pile())
        run(workspace, choose=Choices.starter, say=said.append)
    assert "members/curator" not in script.ran  # neither at discovery nor after the build
    assert {"things/curator", "parts/curator"} <= set(script.ran)
    assert not any("members" in line for line in [*said, *shown])  # never shown as reviewing, nor on the list


def test_a_declined_row_is_nothing_to_weigh(tmp_path: Path) -> None:
    import uuid

    from cerebro.caller import Caller
    from cerebro.records import Proposal, propose

    no_held = "---\nevidence:\n  - query: { from: facts }\nheartbeat: [daily]\n---\n\nKeep the members.\n"
    root = lay_out_workspace(tmp_path)
    (root / "agents" / "members").mkdir()
    (root / "agents" / "members" / "curator.md").write_text(no_held)
    script = Script({"things/curator": [], "parts/curator": []})
    with open_it(root, script) as workspace:
        declined = Proposal(id=f"p_{uuid.uuid4().hex[:12]}", kind="article.admit", target="members/kim-vale",
                            by=Caller("members/curator", "agent"), at="2026-11-01T09:00:00Z",
                            payload={"name": "Kim Vale", "collection": "members"}, reason="Named.",
                            status="rejected", decided_by=Caller("you", "person"), decided_at="2026-11-01T10:00:00Z")
        propose(workspace.store, declined)
        assert workspace.weighs_nothing(workspace.agent("members/curator"))  # what a person declined is no candidate


def test_a_curator_is_asked_with_nothing_held_when_a_source_answers_questions(tmp_path: Path) -> None:
    from typing import ClassVar

    from test_first_run_structure import NOW, RECORDS, Source

    from cerebro import Cerebro
    from cerebro.fakes import FakeExtraction

    class Asked(Source):
        questions: ClassVar[dict[str, str]] = {"members": "every member; params: none"}

    root = lay_out_workspace(tmp_path)
    (root / "agents" / "members").mkdir()
    (root / "agents" / "members" / "curator.md").write_text(MEMBERS)
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with Cerebro(root, agent_run=script, extraction=FakeExtraction(), clock=lambda: NOW,
                 connectors=[Asked("src", list(RECORDS))]) as workspace:
        run(workspace, choose=Choices.starter, say=lambda line: None)
    assert "members/curator" in script.ran


# -- a declined list starts fresh on the next run


def test_a_list_declined_whole_builds_nothing_and_the_next_run_asks_the_curators_again(tmp_path: Path) -> None:
    from cerebro.first_run import in_first_run

    root = lay_out_workspace(tmp_path)
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(root, script) as workspace:
        first = run(workspace, choose=review_rest(workspace), say=lambda line: None)
        assert first is not None and first.result is None  # nothing is built
        # the hand admit names a collection the list's rows were in, from the curators that proposed them, not the
        # first curator of all by id
        assert "Nothing approved: the wiki starts empty; add with cerebro admit things/<name>." in first.lines
        assert first.next == "cerebro admit things/<name>"
        assert in_first_run(workspace) and workspace.store.article("things/alder-works") is None
        asked = script.ran.count("things/curator")
        script.calls["things/curator"] = [by_record("T2")]
        again = run(workspace, choose=Choices.starter, say=lambda line: None)
        assert again is not None and script.ran.count("things/curator") > asked  # its curators are asked again
        assert [row.name for row in again.pile.rows() if not row.left_out] == ["Birch Hall"]
        assert not in_first_run(workspace)


def test_a_first_run_that_proposes_nothing_says_so_names_the_hand_admit_and_no_older_records(tmp_path: Path) -> None:
    from dataclasses import replace

    from test_first_run_structure import RECORDS, thing

    root = lay_out_workspace(tmp_path)
    script = Script({"things/curator": [], "parts/curator": []})
    older = replace(thing("N1", "Old Note", "t-old"), at="2026-10-01T09:00:00Z", into="")  # bound to no collection
    with open_it(root, script, [*RECORDS, older]) as workspace:
        out = run(workspace, choose=Choices.starter, say=lambda line: None)
    assert out is not None
    assert "Nothing to propose: the wiki starts empty; add with cerebro admit parts/<name>." in out.lines
    assert not any(line.startswith("Nothing approved") for line in out.lines)
    assert out.result.older and not any("older record" in line for line in out.result.notices)  # no article to rewrite


# -- the curators of a round are asked side by side


def test_the_curators_of_a_round_are_asked_side_by_side_and_land_in_the_order_of_their_ids(tmp_path: Path) -> None:
    import threading

    both = threading.Barrier(2, timeout=5)  # passes only when the two runs are under way at the same time

    class Together(Script):
        def run(self, agent, evidence, tools, budget):  # type: ignore[no-untyped-def]
            if len(self.ran) < 2:
                self.ran.append("waiting")
                both.wait()
                self.ran.remove("waiting")
            return super().run(agent, evidence, tools, budget)

    said: list[str] = []
    script = Together({"things/curator": [by_record("T1")], "parts/curator": [by_record("P5")]})
    with open_it(lay_out_workspace(tmp_path), script, loose_and_its_holder()) as workspace:
        run(workspace, choose=None, say=said.append)
        lists = workspace.store.state(KEY)["lists"]
    assert {lists[id]["state"] for id in ("things/curator", "parts/curator")} == {"proposed"}
    assert [line.split(", by agents/")[1] for line in said if line.startswith("proposed ")][:2] == [
        "parts/curator.md", "things/curator.md"]


# -- under the selection count, the curator's own last line, which counts what it left out by reason, in its
#    words, with every domain and address taken out; it takes the place of a count the code would make


def test_a_left_out_line_names_no_domain_and_is_otherwise_the_curators_own() -> None:
    from cerebro.first_run import without_domains

    for given, want in [  # a left-out line names no domain
        ("Left out 14: 9 single inbound messages, 3 vendors, 2 machine-sent invites",
         "Left out 14: 9 single inbound messages, 3 vendors, 2 machine-sent invites"),
        ("Left out 3: 2 cold pitches (plovarix.example, oneway.example), 1 invoice (billing@ledgerline.example).",
         "Left out 3: 2 cold pitches, 1 invoice."),
        ("Left out 1: a calendar machine at mail.provider.example [1 invite]", "Left out 1: a calendar machine at [1 invite]"),
        ("Left out 4: e.g. 4 newsletters", "Left out 4: e.g. 4 newsletters"),
        ("Left out 1: a draft kept in notes/acme/brief.md", "Left out 1: a draft kept in notes/acme/brief.md"),
        ("Left out 1: a page at https://mail.provider.example/x", "Left out 1: a page at"),
        ("Left out 2: globex.example/initech.example share a domain", "Left out 2: share a domain"),
        ("Left out 1: the owner is jane/jane.doe@globex.example", "Left out 1: the owner is jane/"),
        ("Left out 1 (acme.example/globex.example)", "Left out 1"),
        ("Left out 1: see https://globex.example/about", "Left out 1: see"),
        ("Left out 1: notes/acme/brief.md", "Left out 1: notes/acme/brief.md"),
        ("Left out 1: a rule in agents/things/curator.md, 2 in sources/globex.example",
         "Left out 1: a rule in agents/things/curator.md, 2 in sources/"),
        ("Left out 1: x/globex.example.json", "Left out 1: x/"),
        ("Left out 1: data/acme.example.yaml", "Left out 1: data/"),
        ("Left out 1: agents/acme.example/curator.md", "Left out 1: agents/curator.md"),
        ("Left out 1: notes/a.md/b.md", "Left out 1: notes/b.md"),
        ("Left out 1 / 2 of them, one at oneway.example", "Left out 1 / 2 of them, one at"),
    ]:
        assert without_domains(given) == want, given


def test_under_its_collection_is_each_curators_own_last_line_and_no_count_the_code_made(tmp_path: Path) -> None:
    from cerebro.first_run import LocalApproval, lines
    from cerebro.first_run_list import checklist
    from cerebro.run import Entry

    class Says(Script):
        def run(self, agent, evidence, tools, budget):  # type: ignore[no-untyped-def]
            done = super().run(agent, evidence, tools, budget)
            if agent.id == "things/curator":
                done.transcript.append(Entry("model", "Proposed 1.\n\nLeft out 2:  1 cold pitch (oneway.example),\t"
                                                      "1 machine-sent invite (noreply@initech.example)\n\n"))
            return done

    script = Says({"things/curator": [by_record("T1"), ("leave_out", {"name": "Birch Hall",
                                                                       "reason": "A cold pitch."})],
                   "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        pile = LocalApproval(workspace).pile()
    said = lines(pile)
    own = said.index("  Left out 2: 1 cold pitch, 1 machine-sent invite")
    assert any(" selected" in line for line in said[:own])  # under the selection line, as the curator's own line
    screen, _ = checklist(pile)
    # on the screen, under the selection
    assert "Left out 2: 1 cold pitch, 1 machine-sent invite" in screen.after
    assert not any("Left out 2" in line for line in screen.note)


def test_a_curators_last_line_with_a_terminal_escape_is_printed_clean() -> None:
    from cerebro.first_run import Listed, Pile, lines

    said = lines(Pile(lists=[Listed("things/curator", "things", said="Left out 1: a\x1b[2J forged\x07 line")]))
    assert not any("\x1b" in line or "\x07" in line for line in said)
    assert any(line.strip().startswith("Left out 1: a") for line in said)  # under its collection's menu row


# -- a filed deferral names its record


def test_a_checked_left_out_row_of_a_held_record_is_filed_with_that_record(tmp_path: Path) -> None:
    from cerebro.records import proposals

    script = Script({"things/curator": [by_record("T1"), ("leave_out", {"record": {"source": "src", "id": "T2"},
                                                                       "reason": "Named once."})],
                     "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=lambda pile: settled(pile, {row.id for row in pile.rows()
                                                              if row.reason != "not weighed"}), say=lambda line: None)
        [filed] = [p for p in proposals(workspace.store) if p.reason.startswith("left out: ")]
    assert filed.status == "accepted" and filed.payload["record"] == {"source": "src", "id": "T2"}


# -- a record on no list. A held record its curator neither proposed nor left out is a row of its Left out
#    group, `not weighed`, for every collection


def test_a_held_record_its_curator_neither_proposed_nor_left_out_is_listed_as_not_weighed(tmp_path: Path) -> None:
    from test_first_run_structure import RECORDS, thing

    from cerebro.first_run import LocalApproval, lines

    left = ("leave_out", {"record": {"source": "src", "id": "T3"}, "reason": "not weighed"})  # a curator's own words
    script = Script({"things/curator": [by_record("T1"), left], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script, [*RECORDS, thing("T3", "Cedar Row", "t-cedar")]) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        pile = LocalApproval(workspace).pile()
        things = next(listed for listed in pile.lists if listed.collection == "things")
        assert ("Birch Hall", "not weighed") in [(row.name, row.reason) for row in things.left()]
        assert things.said == "Left out 2: 1 not weighed, 1 not weighed"  # Cerebro's row and the curator's, apart
        assert [row.name for row in things.proposed()] == ["Alder Works"]  # what it proposed is not listed again
        assert "    [ ] Birch Hall  things" in lines(pile)
        LocalApproval(workspace).apply(settled(pile, {row.id for row in pile.rows()}))  # admitted if ticked
        assert "things/birch-hall" in carried(workspace)


# -- a curator that proposed nothing at discovery is not asked again after the build. It is
#    asked at its own next heartbeat, still in proposal mode, since a person has decided nothing of it

LEAVES_OUT = ("---\nevidence:\n  - held: {}\n  - query: { from: facts }\nheartbeat: [daily]\n"
              "---\n\nKeep the members.\n")


@pytest.mark.usefixtures("every_curator_weighs")
def test_a_curator_that_proposed_nothing_waits_for_its_own_heartbeat_and_proposes_there(tmp_path: Path) -> None:
    from datetime import timedelta

    from test_first_run_structure import NOW, RECORDS

    from cerebro import Cerebro
    from cerebro.fakes import FakeConnector, FakeExtraction
    from cerebro.records import proposals

    root = lay_out_workspace(tmp_path)
    (root / "agents" / "members").mkdir()
    (root / "agents" / "members" / "curator.md").write_text(LEAVES_OUT)
    said: list[str] = []
    script = Script({"things/curator": [by_record("T1")], "parts/curator": [],
                     "members/curator": [("leave_out", {"name": "Kim Vale", "reason": "No fact places them yet."})]})
    now = [NOW]
    with Cerebro(root, agent_run=script, extraction=FakeExtraction(), clock=lambda: now[0],
                 connectors=[FakeConnector("src", list(RECORDS))]) as workspace:
        run(workspace, choose=Choices.starter, say=said.append)
        assert script.ran.count("members/curator") == 1  # not asked again in the pass that built
        assert not any("agents/members/curator.md" in line for line in said if line.startswith("stage     late"))
        script.calls["members/curator"] = [("admit", {"name": "Kim Vale", "reason": "Placed now.", "group": "met"})]
        now[0] = NOW + timedelta(days=1, hours=1)
        workspace.run_once()
        assert script.ran.count("members/curator") == 2  # at its own next heartbeat
        [waits] = [p for p in proposals(workspace.store, "waiting") if p.by.name == "members/curator"]
        assert waits.target == "members/kim-vale"  # still in proposal mode: it waits for a person


# -- filing the checked left-out rows and deciding every row is one write, so a stop between them leaves nothing filed and nothing decided


def test_a_stop_while_a_list_is_decided_leaves_nothing_filed_or_decided(tmp_path: Path) -> None:
    import pytest

    from cerebro.first_run import LocalApproval
    from cerebro.records import proposals

    script = Script({"things/curator": [by_record("T1"), ("leave_out", {"record": {"source": "src", "id": "T2"},
                                                                       "reason": "Named once."})],
                     "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        before = sorted((p.id, p.status) for p in proposals(workspace.store))
        pile = LocalApproval(workspace).pile()
        decide = workspace.decide

        def stops(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("the process stopped")

        workspace.decide = stops  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            LocalApproval(workspace).apply(settled(pile, {row.id for row in pile.rows()
                                                             if row.reason != "not weighed"}))
        workspace.decide = decide  # type: ignore[method-assign]
        assert sorted((p.id, p.status) for p in proposals(workspace.store)) == before


# -- `claimed` is fed the keys that identify a thing, which
#    are the keys a connector declares a weight for and the engine's address keys (`curate.key_weights`), and the
#    record. A header value two records share, such as a parent they name, makes them no same thing


def test_a_held_record_sharing_a_header_value_with_a_proposed_one_is_still_on_a_list(tmp_path: Path) -> None:
    from test_first_run_structure import RECORDS

    from cerebro.connector import Record
    from cerebro.first_run import LocalApproval

    def call(id: str, name: str) -> Record:
        return Record(id=id, source="src", revision="1", at="2026-11-10T09:00:00Z",
                      keys={"name": [name], "parent": ["Alder Works"]}, fields={"name": name}, body=f"{name}.",
                      into="things")

    records = [*RECORDS, call("C1", "Alder call one"), call("C2", "Alder call two")]
    script = Script({"things/curator": [by_record("C1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script, records) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        pile = LocalApproval(workspace).pile()
        things = next(listed for listed in pile.lists if listed.collection == "things")
        assert [row.name for row in things.proposed()] == ["Alder call one"]
        assert "Alder call two" in [row.name for row in things.left()]  # on a list, not hidden by the shared parent


# -- a held record no row speaks for is listed under the reason an earlier run gave it, else `not weighed`; one person's
#    records, one name and one address, as one row; each reason under a heading of its own,
#    `Left out: <reason> (n)`; and named by its record, never by its file path


def test_a_held_record_left_out_by_an_earlier_run_keeps_that_reason_before_not_weighed(tmp_path: Path) -> None:
    from cerebro.curate import LEFT_OUT
    from cerebro.first_run import LocalApproval

    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        workspace.store.append(LEFT_OUT, {"at": "2026-10-01T09:00:00Z", "agent": "things/curator", "collection": "things",
                                      "name": "Birch Hall", "source": "src", "id": "T2",
                                      "reason": "Named once, on one list."})
        run(workspace, choose=None, say=lambda line: None)
        things = next(listed for listed in LocalApproval(workspace).pile().lists if listed.collection == "things")
        assert [(row.name, row.reason) for row in things.left()] == [("Birch Hall", "Named once, on one list.")]


def test_every_held_record_is_a_row_of_its_own_whatever_name_and_address_it_shares(tmp_path: Path) -> None:
    from test_first_run_structure import RECORDS, part

    from cerebro.first_run import LocalApproval

    records = [*RECORDS, part("P7", "Kim Vale", email="kim@vale.example"), part("P8", "Kim Vale", email="kim@vale.example"),
               part("P9", "Kim Vale", email="kim@other.example")]
    script = Script({"things/curator": [], "parts/curator": [by_record("P1")]})
    with open_it(lay_out_workspace(tmp_path), script, records) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        parts = next(listed for listed in LocalApproval(workspace).pile().lists if listed.collection == "parts")
        kims = [row for row in parts.left() if row.name == "Kim Vale"]  # the engine folds none into one (rule 1)
        assert sorted(row.record["id"] for row in kims) == ["P7", "P8", "P9"]


def test_left_out_rows_show_each_with_its_reason_and_a_file_record_by_its_name_not_its_path(
        tmp_path: Path) -> None:
    from test_first_run_structure import RECORDS

    from cerebro.connector import Record
    from cerebro.first_run import LocalApproval, lines

    note = Record(id="local/calls/2026-11-10-cedar-check-in.md", source="src", revision="1",
                  at="2026-11-10T09:00:00Z", keys={"parent": ["Alder Works"]}, body="A check-in.", into="things")
    script = Script({"things/curator": [by_record("T1"), ("leave_out", {"record": {"source": "src", "id": "T2"},
                                                                       "reason": "Named once."})],
                     "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script, [*RECORDS, note]) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        pile = LocalApproval(workspace).pile()
        things = next(listed for listed in pile.lists if listed.collection == "things")
        assert [(row.reason, row.name) for row in things.left()] == [
            ("Named once.", "Birch Hall"), ("not weighed", "2026-11-10-cedar-check-in")]
        shown = lines(pile)
        assert "    [ ] 2026-11-10-cedar-check-in  things" in shown and "          not weighed" in shown
        assert not any("local/calls/" in line for line in shown)


# -- a list declined whole starts afresh however it was declined: its session
#    is cleared whenever nothing waits and nothing of the session was approved


def _session(workspace):  # type: ignore[no-untyped-def]
    return workspace.store.state(KEY) or {}


def test_a_list_declined_with_decide_between_runs_starts_afresh_and_builds_nothing(tmp_path: Path) -> None:
    from cerebro.caller import Caller
    from cerebro.records import proposals

    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        waiting = [{"id": p.id, "reason": "not these"} for p in proposals(workspace.store, "waiting")]
        workspace.call(Caller("you", "person"), "decide", {"reject": waiting})
        out = run(workspace, choose=None, say=lambda line: None)
        assert out is not None and out.result is None and workspace.store.article("things/alder-works") is None
        assert "Nothing approved: the wiki starts empty; add with cerebro admit things/<name>." in out.lines
        assert _session(workspace).get("status") == "declined"
        asked = script.ran.count("things/curator")
        run(workspace, choose=None, say=lambda line: None)
        assert script.ran.count("things/curator") > asked  # the next run asks the curators again


def test_an_admit_accepted_by_hand_before_the_first_run_is_no_approval_of_its_list(tmp_path: Path) -> None:
    import uuid

    from cerebro.caller import Caller
    from cerebro.records import Proposal, propose

    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        propose(workspace.store, Proposal(id=f"p_{uuid.uuid4().hex[:12]}", kind="article.admit", target="things/elm-yard",
                                      by=Caller("you", "person"), at="2026-10-01T09:00:00Z", payload={"name": "Elm Yard"},
                                      reason="By hand.", status="accepted", decided_by=Caller("you", "person"),
                                      decided_at="2026-10-01T09:00:00Z"))
        out = run(workspace, choose=review_rest(workspace), say=lambda line: None)
        assert out is not None and out.result is None and _session(workspace).get("status") == "declined"


def test_a_list_cut_at_the_cap_and_declined_whole_starts_afresh_and_builds_nothing(tmp_path: Path) -> None:
    from test_first_run import ADMITS
    from test_first_run import Script as Spending
    from test_first_run import lay_out_workspace as capped
    from test_first_run import open_it as open_capped

    root = capped(tmp_path, budget="  first_run_usd: 0.001\n", knowledge=True)
    with open_capped(root, Spending({"things/curator": list(ADMITS)}, spend=200_000)) as workspace:
        out = run(workspace, choose=review_rest(workspace), say=lambda line: None)
        assert out is not None and out.result is None and _session(workspace).get("status") == "declined"
        assert any(line.startswith("Nothing approved: the wiki starts empty") for line in out.lines)


def test_serve_prints_a_declined_list_plain_with_a_next_line(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    from cerebro import cli
    from cerebro.caller import Caller
    from cerebro.cli import main
    from cerebro.records import proposals

    root = lay_out_workspace(tmp_path)
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(root, script) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        workspace.call(Caller("you", "person"), "decide",
                   {"reject": [{"id": p.id, "reason": "no"} for p in proposals(workspace.store, "waiting")]})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    done = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    out = done.output.splitlines()
    assert done.exit_code == 0, done.output
    assert "Nothing approved: the wiki starts empty; add with cerebro admit things/<name>." in out
    assert "next      cerebro admit things/<name>" in out and not any(line.startswith("waiting") for line in out)


# -- a round's curators side by side under one cap: every model call is checked against the cap, the runs under way counted
#    as each reply comes, so the cap cuts them at their next call; a paused budget, a provider's refusal or an interrupt
#    stops the round, which does not count; a line is said as each curator starts


def test_a_run_under_a_meter_is_cut_at_its_next_call_and_keeps_what_its_turns_spent() -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from suites.agent_run import AGENT, EVIDENCE, lookup_tool

    from cerebro.agent import Budget
    from cerebro.budget import METER, Meter
    from cerebro.run import PydanticRun

    meter = Meter(1.0, lambda: 0.0)

    def reply(messages: object, info: object) -> ModelResponse:
        meter.stop("another run of the round spent the rest of the cap")  # the cap is reached while this one runs
        return ModelResponse(parts=[ToolCallPart("lookup_tool", {"id": "n"})])

    turn = meter.turn(AGENT.id)
    held = METER.set(turn)
    try:
        run = PydanticRun(FunctionModel(reply)).run(AGENT, EVIDENCE, [lookup_tool], Budget())
    finally:
        METER.reset(held)
    assert run.status == "cut" and turn.cut and "spent the rest of the cap" in run.note
    assert run.usage.requests == 1 and run.usage.input_tokens > 0  # the turn it made counts


def test_an_interrupt_keeps_the_spend_of_the_turns_a_run_made(tmp_path: Path) -> None:
    import pytest
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from suites.agent_run import AGENT, EVIDENCE, lookup_tool

    from cerebro.budget import Ledger, read_caps
    from cerebro.compile import run_agent
    from cerebro.models import ModelSpec
    from cerebro.run import PydanticRun
    from cerebro.store import SQLiteStore

    calls = []

    def reply(messages: object, info: object) -> ModelResponse:
        calls.append(1)
        if len(calls) > 1:
            raise KeyboardInterrupt
        return ModelResponse(parts=[ToolCallPart("lookup_tool", {"id": "n"})])

    store = SQLiteStore(tmp_path / "store.db")
    ledger = Ledger(store, read_caps({}))
    with pytest.raises(KeyboardInterrupt):
        run_agent(AGENT, EVIDENCE, [lookup_tool], agent_run=PydanticRun(FunctionModel(reply)), store=store,
                  ledger=ledger, model=ModelSpec("fireworks", "glm-5p3"))
    [row] = store.rows("agent-runs")
    assert row.data["status"] == "stopped" and row.data["requests"] == 1  # the turn made before the interrupt
    assert [r.data["run"] for r in store.rows("spend")] == [row.data["run"]] and store.rows("spend")[0].data["usd"] > 0


class Turns:
    """A curator's run of several model calls, each checked against the round's meter before it starts and counted on
    it as it ends, as a run on Pydantic AI is: cut, it stops there, and its turns made count."""

    def __init__(self, turns: int, tokens: int, usd: float) -> None:
        import threading

        self.turns, self.tokens, self.usd, self.ran, self.lock = turns, tokens, usd, [], threading.Lock()

    def run(self, agent, evidence, tools, budget):  # type: ignore[no-untyped-def]
        import time
        import uuid

        from cerebro.budget import METER, RoundCut
        from cerebro.run import Run, Usage

        with self.lock:
            self.ran.append(agent.id)
        made, status, note, turn = 0, "done", "", METER.get()
        for _ in range(self.turns):
            try:
                if turn is not None:
                    turn.before()
            except RoundCut as error:
                status, note = "cut", str(error)
                break
            time.sleep(0.005)
            made += 1
            if turn is not None:
                turn.add(self.usd)
        return Run(id=f"run_{uuid.uuid4().hex[:8]}", agent=agent.id, target=agent.target, status=status, note=note,
                   usage=Usage("glm-5p3", made, made * self.tokens, 0, 0, 0, 0))


EIGHT = ("ash", "beech", "cedar", "elm", "fir", "hazel", "larch", "oak")


def _eight(root: Path, cap: float) -> Path:
    for name in EIGHT:
        (root / "agents" / name).mkdir(parents=True)
        (root / "agents" / name / "curator.md").write_text(MEMBERS)
    (root / "settings.yaml").write_text(f"budget:\n  first_run_usd: {cap}\ncompile:\n  concurrency: 8\n")
    return root


@pytest.mark.usefixtures("every_curator_weighs")
def test_eight_curators_at_once_are_held_to_the_cap_within_one_reply_each(tmp_path: Path) -> None:
    from cerebro import Cerebro
    from cerebro.budget import spent_by_runs
    from cerebro.fakes import FakeConnector, FakeExtraction
    from cerebro.first_run import discover
    from cerebro.models import TokenCount, cost

    tokens = 100_000
    probe = _eight(tmp_path / "probe", 1.0)
    with Cerebro(probe, extraction=FakeExtraction(), connectors=[FakeConnector("src", [])]) as workspace:
        spec = workspace.model_of(workspace.agents[0])
    usd = cost(spec.provider, spec.model, TokenCount(input_tokens=tokens))
    assert usd > 0
    cap = 6 * usd  # eight curators of ten turns each would spend 80
    run = Turns(10, tokens, usd)
    said: list[str] = []
    with Cerebro(_eight(tmp_path / "w", cap), agent_run=run, extraction=FakeExtraction(),
                 connectors=[FakeConnector("src", [])]) as workspace:
        pile = discover(workspace, say=said.append)
        session = workspace.store.state(KEY)
        spent = spent_by_runs(workspace.store, [row.data["run"] for row in workspace.store.rows("agent-runs")])
        assert spent >= cap and spent <= cap + 8 * usd + 1e-9, (spent / usd)  # cut the moment it is reached
        assert pile is not None and pile.stopped.startswith("Discovery stopped at its budget")
        assert session["status"] == "stopped" and session["unfinished"]  # the cut ones are asked again first
    starts = [n for n, line in enumerate(said) if line.startswith("stage     discover: reviewing sources for ")]
    assert len(starts) >= 2 and starts[1] < min(n for n, line in enumerate(said) if line.startswith("stopped  "))


@pytest.mark.usefixtures("every_curator_weighs")
def test_a_paused_budget_stops_the_round_which_does_not_count_and_its_curators_are_asked_again(
        tmp_path: Path) -> None:
    import pytest

    from cerebro.budget import BudgetSpent
    from cerebro.first_run import RoundStopped

    class Paused(Script):
        def run(self, agent, evidence, tools, budget):  # type: ignore[no-untyped-def]
            if agent.id == "parts/curator" and self.paused:
                self.ran.append(agent.id)
                raise BudgetSpent("compilation spent $5.00 of its $5 cap on 2026-11-18 (UTC)")
            return super().run(agent, evidence, tools, budget)

    script = Paused({"things/curator": [by_record("T1")], "parts/curator": []})
    script.paused = True
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        with pytest.raises(RoundStopped, match="discovery stopped: BudgetSpent: compilation spent"):
            run(workspace, choose=None, say=lambda line: None)
        session = workspace.store.state(KEY)
        assert "parts/curator" in session["todo"]  # the round does not count
        script.paused = False
        out = run(workspace, choose=None, say=lambda line: None)
        assert out is not None and script.ran.count("parts/curator") >= 2  # asked again on the next run


# -- the curator's own line, as the list takes it


def test_the_own_line_is_the_last_turns_text_only_and_none_when_that_turn_said_nothing() -> None:
    from cerebro.first_run import left_out_line
    from cerebro.run import Entry, Run

    def ran(*entries: Entry) -> Run:
        return Run(id="r", agent="things/curator", target="things", status="done", transcript=list(entries))

    remark = Entry("model", "Looking at the held list first.")
    assert left_out_line(ran(Entry("user", "x"), remark, Entry("call", tool="admit"), Entry("tool", "ok"),
                             Entry("call", tool="admit"))) == ""  # its last turn only called a tool
    assert left_out_line(ran(Entry("user", "x"), remark, Entry("call", tool="admit"), Entry("tool", "ok"),
                             Entry("model", "Admitted 2.\nLeft out 1: 1 named once."))) == "Left out 1: 1 named once."


def test_a_failed_or_cut_run_keeps_no_own_line() -> None:
    from cerebro.first_run import _keep_said
    from cerebro.run import Entry, Run
    from cerebro.runner import PassResult

    class Masking:
        def mask(self, text: str) -> str:
            return text

    for status, kept in (("done", "Left out 1: 1 named once."), ("failed", ""), ("cut", ""), ("budget spent", "")):
        result = PassResult()
        result.runs.append(Run(id="r", agent="things/curator", target="things", status=status,
                               transcript=[Entry("model", "Left out 1: 1 named once.")]))
        state: dict[str, str] = {}
        _keep_said(Masking(), state, "things/curator", result)  # type: ignore[arg-type]
        assert state.get("said", "") == kept, status


def test_each_own_line_shows_once_and_cut_to_its_cap() -> None:
    from cerebro.first_run import Listed, Pile, Row, lines

    long = "Left out 9: " + ", ".join(f"{n} named once on note {n}" for n in range(1, 60)) + "."
    pile = Pile([Listed("things/curator", "things", [Row("p_1", "things/curator", "things", "Alder Works", "Named.")],
                        said="Left out 1: 1 named once."),
                 Listed("parts/curator", "parts", [Row("p_2", "parts/curator", "parts", "Alder Gear", "Named.")],
                        said="Left out 1: 1 named once."),
                 Listed("members/curator", "members", [], said=long)])
    shown = lines(pile)
    assert shown.count("  Left out 1: 1 named once.") == 1  # a repeat is dropped
    [members] = [line for line in shown if line.strip().startswith("Left out 9")]
    assert members.strip().startswith("Left out 9: 1 named once") and "… (" in members  # cut, saying how long
    assert not any(line.startswith("  things: ") for line in shown)  # none heaped under the footer


def test_a_late_list_never_repeats_a_line_said_at_discovery(tmp_path: Path) -> None:
    from cerebro.first_run import LocalApproval

    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        session = workspace.store.state(KEY)
        session["lists"]["things/curator"]["said"] = "Left out 0."
        workspace.store.set_state(KEY, session)
        late = LocalApproval(workspace, agents=["things/curator"], late=True).pile()
        assert all(listed.said != "Left out 0." for listed in late.lists)  # its own, or a count of its own rows


def test_an_address_inside_brackets_goes_and_the_brackets_other_words_stay() -> None:
    from cerebro.first_run import without_domains

    # only the address goes
    assert without_domains("Fixing the source's command (install from https://tools.example/cli) would let the next "
                           "run find these.") == "Fixing the source's command (install from would let the next run " \
                                                 "find these."


# -- the one write: the deferrals filed and the list decided at once. A batch that would make an
#    article while a compile of its collection runs decides nothing; rejects alone decide whatever runs


def test_a_list_that_would_make_an_article_while_its_collection_compiles_decides_nothing(tmp_path: Path) -> None:
    import pytest

    from cerebro.first_run import FirstRunError, LocalApproval
    from cerebro.records import proposals

    root = lay_out_workspace(tmp_path)
    (root / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - facts: { window: last_90_days }\n---\n\n# Instructions\n\nWrite it.\n")
    script = Script({"things/curator": [by_record("T1"), by_record("T2")], "parts/curator": []})
    with open_it(root, script) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        workspace.store.set_state("lease:things/article", {"by": "another pass", "until": "2099-01-01T00:00:00Z"})
        pile = LocalApproval(workspace).pile()
        before = sorted((p.id, p.status) for p in proposals(workspace.store))
        [alder, birch] = [row.id for row in pile.rows() if not row.left_out]
        with pytest.raises(FirstRunError, match="a compile of things collection runs now"):
            LocalApproval(workspace).apply(Choices(accept=[[alder]], leave=[birch]))
        assert sorted((p.id, p.status) for p in proposals(workspace.store)) == before  # nothing decided
