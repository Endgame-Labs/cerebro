"""What the command line says of a run that ran and wrote nothing, beside what a pass says: a run by hand that wrote nothing says so with why, also over its earlier text, and says what grounding dropped; the first
run's merge gives a run that ran again its second pass's outcome; a pass in which a run ran is no failure; and
`agents run` runs every article. Fakes only."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest
from click.testing import CliRunner

from cerebro import Cerebro, Record, cli
from cerebro.agent import Agent
from cerebro.cli import main
from cerebro.compile import Compiled
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
from cerebro.first_run import _merge
from cerebro.records import proposals
from cerebro.run import WROTE_NOTHING, Run, Usage
from cerebro.runner import PassResult

WRITER = ("---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n"
          "# Output\n\n## Summary\n")
NOTES = [Record(id=f"n{n}", source="notes", revision="1", at="2026-11-10T09:00:00Z", body=f"Note {n} on the yard.")
         for n in range(2)]
CITES = "## Summary\n\nThe yard holds a note.[^{0}]\n"


def _run(status: str, note: str = "", agent: str = "reports/weekly", target: str = "") -> Run:
    return Run(id=f"run_{status.replace(' ', '_')}", agent=agent, target=target or agent, status=status, transcript=[],
               usage=Usage(model="scripted"), note=note)


def _cites(prompt: str) -> str:
    import re

    found = re.search(r"\[\^(fact_\w+)\]", prompt.partition("<evidence>")[2])
    return CITES.format(found.group(1)) if found else ""


class Fails(ScriptedRun):
    """The scripted run, but a run on one of `failing` ends failed, as the real loop reports a model's error."""

    def __init__(self, write: Any, failing: set[str], clock: Any) -> None:
        super().__init__(write, clock=clock)
        self.failing = failing

    def run(self, agent: Agent, evidence: Any, tools: Any, budget: Any) -> Run:
        if agent.target in self.failing:
            return Run(id=f"run_on_{agent.target.split('/')[-1]}", agent=agent.id, target=agent.target,
                       status="failed", usage=Usage(model="scripted", requests=1), note="a model error")
        return super().run(agent, evidence, tools, budget)


class Workspace:
    """A fixed agent reports/weekly and a writer of things, over a notes source, whose scripted run hands in what
    `said[0]` gives for a prompt, and fails on each article in `failing`."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for folder in ("reports", "things"):
            (root / "agents" / folder).mkdir(parents=True)
        (root / "settings.yaml").write_text("{}\n")
        (root / "agents" / "reports" / "weekly.md").write_text(WRITER)
        (root / "agents" / "things" / "article.md").write_text(WRITER)
        (root / "agents" / "things" / "curator.md").write_text("---\nheartbeat: [manual]\n---\n\n"
                                                               "Keep them.\n")
        self.root = root
        self.said: list[Any] = [_cites]
        self.failing: set[str] = set()
        self.at = [datetime(2026, 11, 17, 9, tzinfo=UTC)]
        self.notes, self.extraction = FakeConnector("notes", list(NOTES)), FakeExtraction()
        monkeypatch.setattr(cli, "open_workspace", lambda folder: self.open())
        with self.open() as opened:
            opened.run_once(agents=False)  # the notes become facts, and no agent runs

    def open(self) -> Cerebro:
        return Cerebro(self.root, extraction=self.extraction, connectors=[self.notes], clock=lambda: self.at[0],
                       agent_run=Fails(lambda prompt: self.said[0](prompt), self.failing, clock=lambda: self.at[0]))

    def cli(self, *args: str) -> Any:
        return CliRunner().invoke(main, [*args, "--workspace", str(self.root)])


# -- a run by hand that wrote nothing, over the agent's earlier text

@pytest.mark.parametrize("command", [["agents", "run", "reports/weekly"], ["compile", "--article", "reports/weekly"]])
def test_a_run_by_hand_that_wrote_nothing_over_its_earlier_text_says_so_with_why_and_not_wrote(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]) -> None:
    w = Workspace(tmp_path / "w", monkeypatch)
    first = w.cli(*command)
    assert first.exit_code == 0 and first.output.startswith("wrote    reports/weekly"), first.output
    w.said[0] = lambda prompt: ""
    again = w.cli(*command)
    assert again.exit_code == 0, again.output
    lines = again.output.splitlines()
    assert "ran      reports/weekly: wrote nothing: it said there is nothing to write: the stand-in wrote nothing" \
        in lines, lines
    assert not any(line.startswith("wrote") for line in lines), lines


def test_a_run_by_hand_that_wrote_nothing_once_its_evidence_moved_says_what_of_its_earlier_text_went(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch)
    assert w.cli("agents", "run", "reports/weekly").exit_code == 0
    for record in NOTES:
        w.notes.remove(record.id)
    w.notes.add(Record(id="n9", source="notes", revision="1", at="2026-11-12T09:00:00Z",
                               body="A crane moved to the east quay."))
    with w.open() as opened:
        opened.run_once(agents=False)
    w.said[0] = lambda prompt: ""
    told = w.cli("agents", "run", "reports/weekly")
    assert told.exit_code == 0, told.output
    lines = told.output.splitlines()
    assert lines[0].startswith("ran      reports/weekly: wrote nothing: "), lines
    assert "The yard holds a note." not in (w.root / "articles" / "reports" / "weekly.md").read_text()
    assert any(line.startswith("warning  reports/weekly: ") and "its earlier text" in line for line in lines), lines


def test_a_run_by_hand_prints_what_grounding_dropped_and_its_notes_as_a_pass_does(
        capsys: pytest.CaptureFixture[str]) -> None:
    run = _run(WROTE_NOTHING, "it said there is nothing to write: nothing new")
    article = SimpleNamespace(identity=SimpleNamespace(id="reports/weekly"), compilations=[])
    compiled = Compiled(run=run, article=article, runs=[run],  # type: ignore[arg-type]
                        dropped=['reports/weekly, its earlier text: Summary: dropped "Orders rose."'],
                        notes=["its run wrote nothing (it said there is nothing to write: nothing new)"])
    cli._print_compiled("reports/weekly", "reports/weekly", compiled)
    assert capsys.readouterr().out.splitlines() == [
        "ran      reports/weekly: wrote nothing: it said there is nothing to write: nothing new",
        'warning  reports/weekly: reports/weekly, its earlier text: Summary: dropped "Orders rose."',
        "warning  reports/weekly: its run wrote nothing (it said there is nothing to write: nothing new)"]
    done = _run("done")
    cli._print_compiled("things/article", "things/alpha", Compiled(
        run=done, article=SimpleNamespace(identity=SimpleNamespace(id="things/alpha")),  # type: ignore[arg-type]
        runs=[done], dropped=["Summary: dropped \"x\": it cites nothing"]))
    assert capsys.readouterr().out.splitlines() == [
        "wrote    things/alpha  articles/things/alpha.md",
        'warning  things/article on things/alpha: Summary: dropped "x": it cites nothing']


# -- the first run's merge

def test_a_run_that_ran_again_in_the_second_pass_is_told_by_that_pass_alone() -> None:
    wrote = PassResult(compiled=["reports/weekly"], runs=[_run("done")])
    into = PassResult(ran={"reports/weekly": "nothing new in pass 1"})
    _merge(into, wrote)
    assert into.ran == {} and into.compiled == ["reports/weekly"]
    into = PassResult(failed={"reports/weekly": "run r1 wrote nothing: failed: in pass 1"})
    _merge(into, PassResult(ran={"reports/weekly": "nothing new in pass 2"}, runs=[_run(WROTE_NOTHING)]))
    assert into.failed == {} and into.ran == {"reports/weekly": "nothing new in pass 2"}
    into = PassResult(ran={"reports/weekly": "nothing new in pass 1"})
    _merge(into, PassResult(failed={"reports/weekly": "run r2 wrote nothing: failed: in pass 2"}))
    assert into.ran == {} and into.failed == {"reports/weekly": "run r2 wrote nothing: failed: in pass 2"}
    into = PassResult(held={"reports/weekly": "held for room in pass 1"}, failed={"sources/notes/n1": "a read error"})
    _merge(into, PassResult(compiled=["reports/weekly"], runs=[_run("done")]))
    assert into.held == {} and into.failed == {"sources/notes/n1": "a read error"}
    key = "things/article on things/alpha"
    into = PassResult(failed={key: "failed in pass 1"}, held={"things/article on things/beta": "held"})
    _merge(into, PassResult(compiled=["things/alpha"], runs=[_run("done", agent="things/article",
                                                                   target="things/alpha")]))
    assert into.failed == {} and into.held == {"things/article on things/beta": "held"}


def test_a_first_run_whose_every_failure_ran_again_and_wrote_nothing_exits_0(
        capsys: pytest.CaptureFixture[str]) -> None:
    into = PassResult(failed={"reports/weekly": "run r1 wrote nothing: failed: e1"})
    _merge(into, PassResult(ran={"reports/weekly": "n2"}, runs=[_run(WROTE_NOTHING)]))
    cli._print_first_run(SimpleNamespace(applied=[], quit=False, result=into, lines=[], failed=[]))
    out = capsys.readouterr().out.splitlines()
    assert "ran       reports/weekly: wrote nothing: n2" in out and not any(line.startswith("failed") for line in out)
    assert out[-1] == "Done: 1 ran and wrote nothing."


# -- a pass in which a run ran is no failure

def test_a_pass_that_ran_one_run_and_failed_another_writing_nothing_exits_0(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import first_run

    class Opened(SimpleNamespace):
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    result = PassResult(ran={"reports/b": "it said there is nothing to write: nothing new"}, failed={"reports/c": "run r wrote nothing: failed: e"})
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Opened(root=tmp_path, agents=[], files_read=lambda: None,
                                                                  mask_every=lambda: lambda text: text))
    monkeypatch.setattr(first_run, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_pass_once", lambda opened, plain: result)
    told = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(tmp_path)])
    assert told.exit_code == 0, told.output
    assert "failed    reports/c: run r wrote nothing: failed: e" in told.output.splitlines()
    result.ran = {}
    assert CliRunner().invoke(main, ["serve", "--once", "--workspace", str(tmp_path)]).exit_code == 1


# -- agents run on a writer of many articles

def test_agents_run_runs_every_article_names_each_that_failed_and_counts_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch)
    for name in ("alpha", "beta", "gamma"):
        assert w.cli("admit", f"things/{name}").exit_code == 0
    with w.open() as opened:  # the curator carries out each ask
        opened.agent_run.calls = [("admit", {"name": p.target.split("/")[1], "keys": {}, "reason": "Asked for.",
                                             "proposals": [p.id]}) for p in proposals(opened.store, "accepted")]
        opened.curate(next(agent for agent in opened.agents if agent.id == "things/curator"))
    w.failing.add("things/alpha")
    told = w.cli("agents", "run", "things/article")
    assert told.exit_code == 1, told.output
    lines = told.stdout.splitlines()
    assert "failed   things/article on things/alpha: run run_on_alpha wrote nothing: failed: a model error" in lines
    assert {"wrote    things/beta  articles/things/beta.md", "wrote    things/gamma  articles/things/gamma.md"} \
        <= set(lines), lines
    assert told.stderr.strip() == "Error: things/article: 1 of 3 runs failed", told.stderr


def test_a_writer_of_one_article_whose_run_failed_still_says_why_in_its_one_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch)
    w.failing.add("reports/weekly")
    told = w.cli("agents", "run", "reports/weekly")
    assert told.exit_code == 1
    assert told.stderr.strip() == "Error: reports/weekly: run run_on_weekly wrote nothing: failed: a model error"


def test_a_first_pass_row_stays_when_a_different_agent_wrote_the_same_article_in_the_second_pass() -> None:
    key = "things/article on things/alpha"
    other = _run("done", agent="things/brief", target="things/alpha")
    for rows in ("failed", "ran", "held"):
        into = PassResult(**{rows: {key: "in pass 1"}})
        _merge(into, PassResult(compiled=["things/alpha"], runs=[other]))
        assert getattr(into, rows) == {key: "in pass 1"}, (rows, getattr(into, rows))
