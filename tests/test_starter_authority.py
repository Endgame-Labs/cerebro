"""What a new workspace's curators may do on their own: in the sales template the calls and people curators admit on
their own and nothing else, and every other curator does none of the four, each on an explicit authority line. These
are template tests: they read every bundled template's curator files from disk, so a curator added later cannot be
missed."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from cerebro import Cerebro, cli
from cerebro.agent import Agent, Budget, load_agent
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.cli import main
from cerebro.evidence import Evidence
from cerebro.fakes import FakeExtraction, ScriptedRun
from cerebro.identity import redirects
from cerebro.records import proposals
from cerebro.run import Run

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "cerebro" / "templates"
CURATORS = sorted(TEMPLATES.glob("**/curator.md"))


def _agents_folder(path: Path) -> Path:
    return next(parent for parent in path.parents if parent.name == "agents")


def test_the_templates_lay_down_curators() -> None:
    assert CURATORS, f"no curator.md under {TEMPLATES}"


OPEN = {"gtm/agents/calls/curator.md", "gtm/agents/people/curator.md"}  # the curators that admit on their own


@pytest.mark.parametrize("path", CURATORS, ids=[str(p.relative_to(TEMPLATES)) for p in CURATORS])
def test_only_the_open_template_curators_admit_alone_and_nothing_else_lands_alone(path: Path) -> None:
    agent = load_agent(path, _agents_folder(path))
    rel = str(path.relative_to(TEMPLATES))
    authority = agent.authority
    assert authority is not None, f"{rel} carries no authority: line"
    line = next(line for line in path.read_text().splitlines() if line.startswith("authority:"))
    assert all(f"{key}:" in line for key in ("admit", "archive", "merge", "split")), f"{rel}: {line}"  # all shown
    assert authority.admit is (rel in OPEN), f"{rel}: admit is {authority.admit}"
    assert (authority.archive, authority.merge, authority.split) == (False, False, False), f"{rel}: {authority}"


# -- the path a person walks when a curator's archive or merge waits ------------------------------------------------

CURATOR = """\
---
authority: {{ admit: true, archive: {archive}, merge: {merge}, split: true }}
evidence:
  - query: {{ from: things }}
heartbeat: [manual]
---

Decide which things the workspace keeps.
"""
HELD = {"archive": "false", "merge": "false"}  # archives and merges wait, as in every curator of the starter
ALONE = {"archive": "true", "merge": "true"}  # the authority every key true


class Recording(ScriptedRun):
    """The scripted model, keeping each run it makes, so a test reads the tool replies and the prompt."""

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]):
        self.prompts: list[str] = []
        super().__init__(lambda prompt: self.prompts.append(prompt) or {})
        self.calls = calls  # the list the test fills before each run
        self.runs: list[Run] = []

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
        made = super().run(agent, evidence, tools, budget)
        self.runs.append(made)
        return made


class Workspace:
    """A workspace folder with one curator of things, under the authority given, and the commands run on it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, authority: dict[str, str]):
        self.root, self.calls = root, []
        (root / "agents" / "things").mkdir(parents=True)
        (root / "settings.yaml").write_text("{}\n")
        (root / "agents" / "things" / "curator.md").write_text(CURATOR.format(**authority))
        self.model = Recording(self.calls)
        extraction = FakeExtraction()
        self.ahead = timedelta(0)  # how far `later` moved the workspace's clock past the real one
        monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(folder, agent_run=self.model,
                                                                      extraction=extraction, clock=self.clock))
        monkeypatch.setattr(cli, "_asking", lambda: True)
        monkeypatch.setattr(Cerebro, "weighs_nothing", lambda self, agent: False)  # its facts are not what is tested

    def clock(self) -> datetime:
        return datetime.now(UTC) + self.ahead

    def later(self) -> None:
        """Move the clock a minute on, so what comes next is after what came before, to the second."""
        self.ahead += timedelta(minutes=1)

    def run(self, *args: str, input: str | None = None) -> Result:
        return CliRunner().invoke(main, [*args, "--workspace", str(self.root)], input=input)

    def curate(self, *calls: tuple[str, dict[str, Any]]) -> Result:
        self.calls[:] = list(calls)
        done = self.run("agents", "run", "things/curator")
        assert done.exit_code == 0, done.output
        return done

    def replies(self, tool: str) -> list[str]:
        """What the tool answered on the latest run."""
        return [entry.text for entry in self.model.runs[-1].transcript if entry.role == "tool" and entry.tool == tool]

    def waiting(self) -> list[dict[str, Any]]:
        listed = self.run("review", "--json")
        assert listed.exit_code == 0, listed.output
        return json.loads(listed.output)

    def state(self) -> dict[str, Any]:
        """What landed: each article's status and keys, the redirects, and each accepted row's change, its times
        and ids left out, so two workspaces compare."""
        with Cerebro(self.root, extraction=FakeExtraction()) as opened:
            articles = {a.identity.id: (a.identity.status, a.identity.keys)
                        for a in (opened.store.article(id) for id in ("things/alder", "things/alder-works",
                                                                      "things/birch"))
                        if a is not None}
            applied: list[dict[str, Any]] = []  # a row an act carried out holds a copy of the act's change
            for r in proposals(opened.store, "accepted"):
                change = {k: v for k, v in (r.applied or {}).items() if k != "at"}
                if r.kind != "article.admit" and change and change not in applied:
                    applied.append(change)
            return {"articles": articles, "redirects": redirects(opened.store), "applied": applied}


def admitted(space: Workspace, *names: str) -> None:
    """The things in the working set, admitted by the curator on its own, so no caller decided them."""
    with Cerebro(space.root, extraction=FakeExtraction()) as opened:
        for name in names:
            opened.store.create(Identity(f"things/{name.lower().replace(' ', '-')}", "things", name, {}, "active",
                                         Caller("things/curator", "agent"), "2026-11-01T09:00:00Z",
                                         f"{name} came up in two notes"))


def carried(act: tuple[str, dict[str, Any]], id: str) -> tuple[str, dict[str, Any]]:
    """The curator's act that carries out the accepted proposal `id`."""
    return act[0], {**act[1], "proposals": [id]}


ARCHIVE = ("archive", {"article": "Alder", "reason": "no activity since spring"})
MERGE = ("merge", {"article": "Alder", "other": "Alder Works", "reason": "one thing under two names"})


@pytest.mark.parametrize("act", [ARCHIVE, MERGE], ids=["archive", "merge"])
def test_a_starter_curators_act_waits_is_counted_shown_and_once_approved_lands_as_it_would_have_alone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, act: tuple[str, dict[str, Any]]) -> None:
    alone = Workspace(tmp_path / "alone", monkeypatch, ALONE)
    admitted(alone, "Alder", "Alder Works", "Birch")
    alone.curate(act)
    landed = alone.state()

    space = Workspace(tmp_path / "held", monkeypatch, HELD)
    admitted(space, "Alder", "Alder Works", "Birch")
    before = space.state()
    space.curate(act)
    (reply,) = space.replies(act[0])
    assert "'note': 'waits for a caller, in p_" in reply  # the curator is told it waits, and for whom
    (row,) = space.waiting()
    assert row["kind"] == f"article.{act[0]}" and row["target"] == "things/alder"
    assert row["by"] == {"name": "things/curator", "kind": "agent"} and row["reason"] == act[1]["reason"]
    assert space.state() == before  # nothing changed while it waits

    status = space.run("status")
    assert "review    1 proposal is awaiting review: decide it with cerebro review" in status.output

    walked = space.run("review", input="a\n")
    assert walked.exit_code == 0, walked.output
    assert f"{row['id']}  1 of 1, awaiting review" in walked.output
    assert f"  change   {row['change']}" in walked.output and row["change"]
    assert f"  why      {act[1]['reason']}" in walked.output
    assert f"Accepted {row['id']}: its curator carries it out on its next run." in walked.output
    assert space.state() == before and space.waiting() == []
    space.curate(carried(act, row["id"]))
    assert space.state() == landed and landed != before  # carried out, it lands as it would have on its own


def rejected(space: Workspace, act: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    space.curate(act)
    (row,) = space.waiting()
    assert space.run("review", "--reject", row["id"]).exit_code == 0
    return row


@pytest.mark.parametrize("act", [ARCHIVE, MERGE], ids=["archive", "merge"])
def test_a_rejected_archive_or_merge_is_in_its_curators_next_evidence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, act: tuple[str, dict[str, Any]]) -> None:
    space = Workspace(tmp_path / "held", monkeypatch, HELD)
    admitted(space, "Alder", "Alder Works", "Birch")
    space.curate(act)
    (row,) = space.waiting()
    walked = space.run("review", input="r: still in use this quarter\n")
    assert "Your reason is on the row, for the proposer to read on its next run." in walked.output
    space.curate()  # its next run, which files nothing
    told = space.model.prompts[-1]
    assert "## Decisions on your proposals" in told
    assert f"- {row['id']}, {act[0]} of things/alder: rejected by a caller: still in use this quarter" in told
    space.curate()  # and the run after it is not told again
    assert row["id"] not in space.model.prompts[-1]


def test_a_decision_with_no_reason_is_not_told(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = held(tmp_path, monkeypatch)
    row = rejected(space, ARCHIVE)
    space.curate()
    assert row["id"] not in space.model.prompts[-1]
    assert "## Decisions on your proposals" not in space.model.prompts[-1]


# -- one act on one article waits once, and a caller's decision of it holds for every proposer -----------------------

ALDER_WITH_BIRCH = ("merge", {"article": "Alder", "other": "Birch", "reason": "one thing under two names"})
WORKS_WITH_ALDER = ("merge", {"article": "Alder Works", "other": "Alder", "reason": "one thing under two names"})


def held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    space = Workspace(tmp_path / "held", monkeypatch, HELD)
    admitted(space, "Alder", "Alder Works", "Birch")
    return space


def person_tells(space: Workspace, kind: str, target: str, text: str) -> str:
    """A caller's tell of `kind` on `target`: the id of the proposal it files, accepted and not carried out yet."""
    told = space.run("tools", "tell", "--json", json.dumps({"text": text, "target": target, "kind": kind}))
    assert told.exit_code == 0, told.output
    (id,) = json.loads(told.output)["proposals"]
    assert decided_row(space, id).status == "accepted" and not decided_row(space, id).applied
    return str(id)


def test_a_rejected_merge_keeps_its_two_apart_and_bars_no_other_pair(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    space = held(tmp_path, monkeypatch)
    space.curate(MERGE)
    (row,) = space.waiting()
    rejected = space.run("review", "--reject", row["id"])
    assert f"Rejected {row['id']}. The proposer will not raise it again until something new about them arrives." \
        in rejected.output, rejected.output
    space.curate(WORKS_WITH_ALDER)
    assert f"a caller rejected merge of things/alder and things/alder-works in {row['id']}" in space.replies("merge")[0]
    space.curate(ALDER_WITH_BIRCH)
    assert "proposed" in space.replies("merge")[0]


def test_a_person_merges_by_hand_two_things_a_rejection_kept_apart(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    space = held(tmp_path, monkeypatch)
    space.curate(MERGE)
    (row,) = space.waiting()
    space.run("review", "--reject", row["id"])
    id = person_tells(space, "merge", "things/alder", "merge this with things/alder-works")
    space.curate(carried(MERGE, id))
    assert redirects_of(space) == {"things/alder-works": "things/alder"}


def redirects_of(space: Workspace) -> dict[str, str]:
    return space.state()["redirects"]


# -- an act that waited is checked against the article as it stands when its curator carries it out ------------------

def one_state(space: Workspace) -> None:
    """No article is in two states at once: each one merged away is marked merged, and no other is."""
    st = space.state()
    merged = {id for id, (status, _) in st["articles"].items() if status == "merged"}
    assert merged == set(st["redirects"]), st


def decided_row(space: Workspace, id: str) -> Any:
    with Cerebro(space.root, extraction=FakeExtraction()) as opened:
        return next(p for p in proposals(opened.store) if p.id == id)


def test_a_waiting_archive_of_a_thing_a_person_merged_away_since_is_closed_when_approved(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = held(tmp_path, monkeypatch)
    space.curate(("archive", {"article": "Alder Works", "reason": "no activity since spring"}))
    (row,) = space.waiting()
    space.curate(carried(MERGE, person_tells(space, "merge", "things/alder", "merge this with things/alder-works")))
    assert redirects_of(space) == {"things/alder-works": "things/alder"}
    shown = [r for r in space.waiting() if r["id"] == row["id"]]
    assert not shown or "nothing" in shown[0]["change"], shown
    assert space.run("review", "--accept", row["id"]).exit_code == 0
    space.calls[:] = []
    passed = space.run("serve", "--once")
    assert f"{row['id']} archive of things/alder-works: withdrawn, as" in passed.output, passed.output
    assert decided_row(space, row["id"]).status == "withdrawn"  # closed before its curator's run is paid for
    one_state(space)
    assert space.state()["articles"]["things/alder-works"][0] == "merged"


def test_a_waiting_archive_of_a_thing_archived_since_says_accepting_it_changes_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = held(tmp_path, monkeypatch)
    space.curate(ARCHIVE)
    with Cerebro(space.root, extraction=FakeExtraction()) as opened:  # taken out by another road since
        article = opened.store.article("things/alder").identity
        article.status = "archived"
        opened.store.set_identity(article)
    (shown,) = space.waiting()
    assert shown["change"] == "nothing: things/alder is archived already, so accepting closes this row"


def test_a_waiting_merge_that_would_bring_back_a_thing_a_person_archived_since_says_so_and_does_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = held(tmp_path, monkeypatch)
    space.curate(MERGE)
    (row,) = space.waiting()
    space.curate(carried(("archive", {"article": "Alder Works", "reason": "done with it"}),
                         person_tells(space, "archive", "things/alder-works", "done with it")))
    (shown,) = space.waiting()
    assert "things/alder-works is archived" in shown["change"] and "back into the working set" in shown["change"], \
        shown["change"]
    walked = space.run("review", input="a\n")
    assert f"  change   {shown['change']}" in walked.output
    space.curate(carried(MERGE, row["id"]))
    one_state(space)
    assert space.state()["articles"]["things/alder"][0] == "active"


def person_says(space: Workspace, target: str, text: str) -> None:
    told = space.run("tools", "tell", "--json", json.dumps({"text": text, "target": target, "as": "fact"}))
    assert told.exit_code == 0, told.output


# -- a rejection lifted, a link, and a decision made while its curator runs -----------------------------------------

def test_a_second_rejection_of_an_archive_holds_after_a_first_was_lifted(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    space = held(tmp_path, monkeypatch)
    rejected(space, ARCHIVE)
    space.later()
    person_says(space, "things/alder", "Alder closed its last office and stopped trading.")
    again = rejected(space, ARCHIVE)
    space.curate(ARCHIVE)
    assert f"rejected archive of things/alder in {again['id']}" in space.replies("archive")[0], space.replies("archive")
    assert space.waiting() == []


def test_a_link_that_adds_a_key_to_a_thing_in_the_working_set_does_not_close_its_waiting_archive(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = held(tmp_path, monkeypatch)
    space.curate(ARCHIVE)
    (row,) = space.waiting()
    space.curate(("link", {"article": "Alder", "reason": "Alder came up in two notes",
                           "keys": {"alias": ["Alder Co"]}}))
    assert "alias Alder Co added" in space.replies("link")[0], space.replies("link")
    (shown,) = space.waiting()
    assert shown["change"].startswith("things/alder leaves the working set"), shown["change"]
    assert space.run("review", "--accept", row["id"]).exit_code == 0
    space.curate(carried(ARCHIVE, row["id"]))
    assert space.state()["articles"]["things/alder"][0] == "archived"


def test_a_decision_made_while_its_curator_runs_is_told_on_the_next_run(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.records import Decision

    space = held(tmp_path, monkeypatch)
    space.curate(ARCHIVE)
    (row,) = space.waiting()
    real = space.model.run

    def deciding(agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
        with Cerebro(space.root, extraction=FakeExtraction()) as opened:
            opened.decide(Caller("you", "person"), reject=[Decision(row["id"], "still in use this quarter")])
        return real(agent, evidence, tools, budget)

    space.model.run = deciding  # type: ignore[method-assign]
    space.curate()
    space.model.run = real  # type: ignore[method-assign]
    space.curate()
    assert f"- {row['id']}, archive of things/alder: rejected by a caller" in space.model.prompts[-1]


def test_a_decision_is_told_once_though_its_row_is_written_again(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    space = held(tmp_path, monkeypatch)
    space.curate(ARCHIVE)
    (row,) = space.waiting()
    space.run("review", input="r: still in use this quarter\n")
    space.curate()
    assert row["id"] in space.model.prompts[-1]
    with Cerebro(space.root, extraction=FakeExtraction()) as opened:  # the decided row written again
        decided = next(p for p in proposals(opened.store) if p.id == row["id"])
        from cerebro.records import propose
        propose(opened.store, replace(decided, reason="no activity since the spring"))
    space.curate()
    assert row["id"] not in space.model.prompts[-1]


def test_a_merge_of_a_thing_merged_away_since_says_on_its_change_line_that_it_changes_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = held(tmp_path, monkeypatch)
    space.curate(MERGE, ("merge", {"article": "Alder Works", "other": "Birch", "reason": "one thing"}))
    first, _ = space.waiting()
    assert space.run("review", "--accept", first["id"]).exit_code == 0
    space.curate(carried(MERGE, first["id"]))
    (second,) = space.waiting()
    assert second["change"].startswith("nothing: things/alder-works was merged into things/alder since"), second
    one_state(space)


def test_a_curator_written_by_agents_new_holds_all_four_kinds_for_a_person(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "space"
    (root / "agents").mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    made = CliRunner().invoke(main, ["agents", "new", "things/curator", "--workspace", str(root)])
    assert made.exit_code == 0, made.output
    path = root / "agents" / "things" / "curator.md"
    authority = load_agent(path, root / "agents").authority
    assert authority is not None and (authority.admit, authority.archive, authority.merge, authority.split) == (
        False, False, False, False)
