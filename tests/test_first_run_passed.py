"""The record that a run went past the workspace's first run: a case for each way it is made or read, and a session
row removed by hand as a case of its own. Once the record is made, a curator still in its own first run is late: its own admits wait for a person, and everything else
goes on. Neutral collections only: `things` and `parts`. Fakes and stand-in models only."""

from __future__ import annotations

from pathlib import Path

from test_first_run import Script
from test_first_run_nobody_decides import _articles, _landed_alone, _own_authority
from test_first_run_structure import by_record, lay_out_workspace, open_it

from cerebro import Cerebro
from cerebro.caller import Caller
from cerebro.curate import proposing
from cerebro.first_run import KEY, Choices, in_first_run, run, served
from cerebro.held import FIRST_RUN_PASSED, pass_first_run, passed_first_run
from cerebro.records import Proposal, proposals, propose
from cerebro.runner import first_list


def test_a_curator_whose_first_list_comes_late_waits_alone(tmp_path: Path) -> None:
    script = Script({"things/curator": [], "parts/curator": [by_record("P1")]})
    with open_it(_own_authority(lay_out_workspace(tmp_path)), script) as workspace:
        workspace.run_once(agents=False)  # the sources are listed, and no agent runs: no run went past the first run
        assert in_first_run(workspace) and not passed_first_run(workspace.store)
        pass_first_run(workspace.store, "2026-11-18T09:00:00Z")
        first = workspace.store.state(FIRST_RUN_PASSED)
        pass_first_run(workspace.store, "2026-11-18T10:00:00Z")
        assert workspace.store.state(FIRST_RUN_PASSED) == first  # the record is made once
        assert not in_first_run(workspace)  # none holds the workspace
        assert proposing(workspace.store, "things/curator") and proposing(workspace.store, "parts/curator")  # both late
        workspace.curate(workspace.agent("parts/curator"))
        assert workspace.store.article("parts/alder-gear") is None  # nothing born before a person approves it
        assert len(first_list(workspace, "parts/curator")) == 1 and _landed_alone(workspace) == []


def _reviewed(workspace: Cerebro) -> None:
    """The things curator's first list, decided with review off a terminal: one admit accepted by you, one rejected,
    and one it filed as left out, rejected; no session kept."""
    you, curator = Caller("you", "person"), Caller("things/curator", "agent")
    for n, (target, status, group) in enumerate([("things/alder-works", "accepted", ""), ("things/birch-hall",
                                                 "rejected", ""), ("things/loose-pin", "rejected", "left out")]):
        propose(workspace.store, Proposal(f"p_000{n}", "article.admit", target, curator, "2026-11-11T00:00:00Z",
                                      payload={"collection": "things", **({"group": group} if group else {})},
                                      status=status, decided_by=you, decided_at="2026-11-11T00:00:00Z"))


def test_a_first_list_decided_with_review_opens_no_session_where_none_follows_past_the_first_run(
        tmp_path: Path) -> None:
    script = Script({"things/curator": [], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        _reviewed(workspace)
        pass_first_run(workspace.store, "2026-11-18T09:00:00Z")
        assert run(workspace, choose=Choices.starter) is None and workspace.store.state(KEY) is None
        assert script.ran == []  # no curator is asked: nothing of a first run follows


def test_a_tick_opens_the_session_of_a_first_list_approved_with_review_past_the_first_run(tmp_path: Path) -> None:
    script = Script({"things/curator": [], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        _reviewed(workspace)
        pass_first_run(workspace.store, "2026-11-18T09:00:00Z")
        assert served(workspace) is None and workspace.store.state(KEY) is None  # past the first run the tick opens none
        assert script.ran == []


def test_no_record_is_made_while_an_admit_of_a_curator_in_its_own_first_run_waits(tmp_path: Path) -> None:
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        assert run(workspace, choose=None) is not None  # the first list waits for a person
        workspace.store.set_state(KEY, None)  # the session row, removed by hand
        workspace.run_once()
        assert any(p.status == "waiting" for p in proposals(workspace.store)) and not passed_first_run(workspace.store)


def test_a_pass_records_the_first_run_passed_once_a_curators_collection_left_it(tmp_path: Path) -> None:
    script = Script({"things/curator": [], "parts/curator": []})
    with open_it(_own_authority(lay_out_workspace(tmp_path)), script) as workspace:
        _reviewed(workspace)  # the things collection left its first run, and no record was kept
        workspace.run_once(agents=False)
        assert not passed_first_run(workspace.store)  # a pass that runs no agent goes past nothing
        script.calls = {"parts/curator": [by_record("P1")], "things/curator": []}
        workspace.run_once()
        assert passed_first_run(workspace.store) and _landed_alone(workspace) == []  # the parts curator is late
        assert len(first_list(workspace, "parts/curator")) == 1


def test_a_workspace_a_run_went_past_is_never_put_back_into_a_whole_first_run(tmp_path: Path) -> None:
    script = Script({"things/curator": [], "parts/curator": []})
    with open_it(_own_authority(lay_out_workspace(tmp_path)), script) as workspace:
        workspace.run_once()  # used as a library: the curators run and propose nothing, and the pass goes on
        assert passed_first_run(workspace.store) and not in_first_run(workspace)
        assert served(workspace) is None and workspace.store.state(KEY) is None
        script.calls = {"things/curator": [by_record("T1")], "parts/curator": []}
        workspace.curate(workspace.agent("things/curator"))  # its first list comes later
        assert _articles(workspace) == [] and _landed_alone(workspace) == []  # late: its admit waits for a person
        assert len(first_list(workspace, "things/curator")) == 1


def test_a_curator_that_proposed_nothing_at_the_first_run_stays_late_once_its_session_row_is_removed(
        tmp_path: Path) -> None:
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(_own_authority(lay_out_workspace(tmp_path)), script) as workspace:
        out = run(workspace, choose=Choices.starter)
        assert out is not None and _articles(workspace) == ["things/alder-works"]
        workspace.store.set_state(KEY, None)  # the session row, removed by hand
        script.calls = {"parts/curator": [by_record("P1")]}
        workspace.curate(workspace.agent("parts/curator"))
        assert _articles(workspace) == ["things/alder-works"] and _landed_alone(workspace) == []
        assert len(first_list(workspace, "parts/curator")) == 1


def test_a_curator_that_proposed_nothing_at_the_first_run_stays_late_once_both_its_rows_are_removed(
        tmp_path: Path) -> None:
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(_own_authority(lay_out_workspace(tmp_path)), script) as workspace:
        out = run(workspace, choose=Choices.starter)
        assert out is not None and _articles(workspace) == ["things/alder-works"]
        workspace.store.set_state(KEY, None)  # the session row and the record, removed by hand
        workspace.store.set_state(FIRST_RUN_PASSED, None)
        assert proposing(workspace.store, "parts/curator") and not proposing(workspace.store, "things/curator")
        script.calls = {"parts/curator": [by_record("P1")]}
        workspace.curate(workspace.agent("parts/curator"))  # as agents run does: no pass goes first to make the record
        assert _articles(workspace) == ["things/alder-works"] and _landed_alone(workspace) == []
        assert len(first_list(workspace, "parts/curator")) == 1
