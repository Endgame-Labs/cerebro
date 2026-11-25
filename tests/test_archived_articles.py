"""No agent runs on an article a caller archived, a fixed agent's own article included, and a curator that admits
an archived article by its path finds it. Fakes only: a scripted run, a fake
connector, a fake extraction."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from test_archive_command import CURATOR, carry
from test_runner import NOTES, cited

from cerebro import Cerebro, Record, cli
from cerebro.cli import main
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun

WEEKLY = "---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n"


class Weekly:
    """A workspace with one fixed agent, reports/weekly, over a notes source, and a curator of reports run by hand,
    opened afresh for each pass or command, as the command line opens it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (root / "agents" / "reports").mkdir(parents=True)
        (root / "settings.yaml").write_text("{}\n")
        (root / "agents" / "reports" / "weekly.md").write_text(WEEKLY)
        (root / "agents" / "reports" / "curator.md").write_text(CURATOR.replace("{0}", "reports"))
        self.root, self.notes, self.extraction = root, FakeConnector("notes", list(NOTES)), FakeExtraction()
        self.run = ScriptedRun(lambda prompt: cited(prompt))
        monkeypatch.setattr(cli, "open_workspace", lambda folder: self.open())

    def open(self) -> Cerebro:
        return Cerebro(self.root, extraction=self.extraction, connectors=[self.notes], agent_run=self.run)

    def pass_(self):  # type: ignore[no-untyped-def]
        with self.open() as workspace:
            return workspace.run_once()

    def command(self, *args: str):  # type: ignore[no-untyped-def]
        return CliRunner().invoke(main, [*args, "--workspace", str(self.root)])

    def carried(self, *args: str) -> None:
        """A caller's command, then the curator's next run, which carries it out."""
        done = self.command(*args)
        assert done.exit_code == 0, done.output
        with self.open() as workspace:
            carry(workspace)

    def note(self, n: int) -> None:
        self.notes.add(Record(id=f"n{n}", source="notes", revision="1", at=f"2026-11-1{n % 10}T09:00:00Z",
                              body=f"A new note {n}."))


def test_no_pass_runs_the_fixed_agent_of_an_archived_article_until_a_person_admits_it_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weekly = Weekly(tmp_path / "w", monkeypatch)
    assert weekly.pass_().compiled == ["reports/weekly"]
    weekly.carried("archive", "reports/weekly")
    weekly.note(7)
    left = weekly.pass_()
    assert left.compiled == [] and left.extracted == 1  # the new note is read, and no agent writes the article
    assert left.skipped["reports/weekly"] == "reports/weekly is archived, left alone"
    assert not left.failed and not left.warnings
    with weekly.open() as workspace:
        assert workspace.store.article("reports/weekly").identity.status == "archived"
    weekly.carried("admit", "reports/weekly")
    weekly.note(8)
    assert weekly.pass_().compiled == ["reports/weekly"]  # back in the working set: its agent runs on it again


def test_agents_run_by_hand_on_an_archived_article_runs_nothing_and_says_so(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    weekly = Weekly(tmp_path / "w", monkeypatch)
    weekly.pass_()
    weekly.carried("archive", "reports/weekly")
    ran = len(weekly.run.prompts) if hasattr(weekly.run, "prompts") else None
    told = weekly.command("agents", "run", "reports/weekly")
    assert "reports/weekly is archived, left alone" in told.output, told.output
    assert ran is None or len(weekly.run.prompts) == ran
    with weekly.open() as workspace:
        assert workspace.store.article("reports/weekly").identity.status == "archived"


def test_targets_leave_out_an_archived_or_merged_fixed_article_and_keep_one_not_yet_born(tmp_path: Path,
                                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from cerebro.compile import targets

    weekly = Weekly(tmp_path / "w", monkeypatch)
    with weekly.open() as workspace:
        agent = workspace.agent("reports/weekly")
        assert targets(agent, workspace.lookup) == ["reports/weekly"]  # not born yet: the pass births it
    weekly.pass_()
    with weekly.open() as workspace:
        assert targets(agent, workspace.lookup) == ["reports/weekly"]
        found = workspace.store.article("reports/weekly").identity
        for status in ("archived", "merged"):
            workspace.store.set_identity(replace(found, status=status))
            assert targets(agent, workspace.lookup) == [], status


# -- 2. a curator that admits an archived article by its path finds it, and its admit waits for a person

def _curator_admits(w, name: str) -> list[str]:  # type: ignore[no-untyped-def]
    from test_archive_command import CURATOR_OF

    from cerebro.agent import load_agent
    from cerebro.caller import Caller
    from cerebro.curate import run_curator
    from cerebro.records import Proposal, propose

    with w.open() as workspace:  # the curator's own admit was decided by a person, so this is no first run
        propose(workspace.store, Proposal(id="p_in", kind="article.admit", target="things/blue-widget", by=CURATOR_OF,
                                      at="2026-11-10T09:00:00Z", status="accepted",
                                      decided_by=Caller("you", "person"), decided_at="2026-11-10T09:00:00Z"))
    curator = load_agent(w.root / "agents" / "things" / "curator.md", w.root / "agents")
    with w.open() as workspace:
        done = run_curator(curator, lookup=workspace.lookup, store=workspace.store,
                           agent_run=ScriptedRun(lambda p: {}, [("admit", {"name": name, "reason": "seen again"})]),
                           root=w.root, functions={}, agent_tools={}, clock=workspace.clock)
        assert done.admitted == []
        return [e.text for e in done.run.transcript if e.role == "tool"]  # type: ignore[union-attr]


def test_a_curators_admit_by_the_path_of_a_merged_article_finds_the_one_it_was_merged_into(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from test_archive_command import Workspace

    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget")
    w.make("things/widget", "Widget")
    with w.open() as workspace:
        gone = workspace.store.article("things/blue-widget").identity
        workspace.store.set_identity(replace(gone, status="merged"))
        workspace.store.append("merges", {"redirect": "things/blue-widget", "to": "things/widget",
                                      "at": "2026-11-10T09:00:00Z"})
    replies = _curator_admits(w, "things/blue-widget")
    assert "admit: things/widget is in the working set already" in replies[0], replies
    with w.open() as workspace:
        assert sorted(a.identity.id for a in workspace.store.query("things")) == ["things/blue-widget", "things/widget"]
