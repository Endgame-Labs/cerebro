"""What a run that ran and wrote nothing leaves where a person and a caller read it:
a pass's closing lines name it, a run by hand that wrote nothing is no failure and moves the article's clock, the first
run's merged result keeps it, it comes masked, and a live check prints it. Fakes only: a scripted run, a fake
connector, a fake extraction."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from cerebro import Cerebro, Record, cli
from cerebro.cli import main
from cerebro.compile import Compiled
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
from cerebro.run import CUT_NOTHING, HANDED_NOTHING, SPENT_NOTHING, WROTE_NOTHING, Run, Usage
from cerebro.runner import PassResult

WEEKLY = "---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n"
NOTES = [Record(id=f"n{n}", source="notes", revision="1", at="2026-11-10T09:00:00Z", body=f"Note {n}.")
         for n in range(2)]
DECLINED = "it said there is nothing to write: nothing new"


def _run(status: str, note: str = "", agent: str = "reports/weekly") -> Run:
    return Run(id=f"run_{status.replace(' ', '_')}", agent=agent, target=agent, status=status, transcript=[],
               usage=Usage(model="scripted"), note=note)


# -- 1. a pass's closing lines

def test_a_pass_names_each_run_that_wrote_nothing_and_its_closing_line_counts_them(
        capsys: pytest.CaptureFixture[str]) -> None:
    result = PassResult(compiled=["things/first"], ran={"things/article on things/second": DECLINED},
                        failed={"things/article on things/third": "run r3: failed: a model error"})
    cli._print(result)
    out = capsys.readouterr().out.splitlines()
    assert f"ran       things/article on things/second: wrote nothing: {DECLINED}" in out
    assert out[-1] == "Done: 1 written, 1 ran and wrote nothing, 1 failed."


def test_a_pass_with_no_run_that_wrote_nothing_keeps_its_closing_line_without_that_part(
        capsys: pytest.CaptureFixture[str]) -> None:
    cli._print(PassResult(compiled=["things/first"], held={"things/article": "held for room"}))
    assert capsys.readouterr().out.splitlines()[-1] == "Done: 1 written, 1 held for the next pass."


def test_a_pass_whose_only_run_wrote_nothing_does_not_say_no_agent_ran(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print(PassResult(ran={"reports/weekly": DECLINED}))
    out = capsys.readouterr().out
    assert "agents    none" not in out and out.splitlines()[-1] == "Done: 1 ran and wrote nothing."


# -- 2. a run by hand

def test_a_run_by_hand_that_wrote_nothing_is_no_failure_and_says_why(capsys: pytest.CaptureFixture[str]) -> None:
    run = _run(WROTE_NOTHING, DECLINED)
    cli._print_compiled("reports/weekly", "reports/weekly", Compiled(run=run, article=None, runs=[run]))
    assert capsys.readouterr().out == f"ran      reports/weekly: wrote nothing: {DECLINED}\n"
    cli._print_compiled("things/article", "things/first", Compiled(run=run, article=None, runs=[run]))
    assert capsys.readouterr().out == f"ran      things/article on things/first: wrote nothing: {DECLINED}\n"


@pytest.mark.parametrize("failed", [_run("failed", "a model error"), _run("failed", SPENT_NOTHING),
                                    _run("failed", CUT_NOTHING)])
def test_a_run_by_hand_that_failed_still_exits_1(failed: Run) -> None:
    why = cli._print_compiled("reports/weekly", "reports/weekly", Compiled(run=failed, article=None, runs=[failed]))
    assert why.startswith("run run_failed: failed"), why


def test_a_run_by_hand_whose_own_run_wrote_nothing_while_a_subagent_failed_exits_1() -> None:
    own = _run(WROTE_NOTHING, "it added nothing, so its subagents' content stands", "things/article")
    sub = _run("failed", "a model error", "things/subagents/overview")
    why = cli._print_compiled("things/article", "things/first", Compiled(run=own, article=None, runs=[sub, own]))
    assert "of things/subagents/overview: failed" in why, why


class Weekly:
    """One fixed agent, reports/weekly, over a notes source, whose scripted run hands in `said[0]`."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, said: list[Any]) -> None:
        (root / "agents" / "reports").mkdir(parents=True)
        (root / "settings.yaml").write_text("{}\n")
        (root / "agents" / "reports" / "weekly.md").write_text(WEEKLY)
        self.root, self.said = root, said
        self.at = [datetime(2026, 11, 17, 9, tzinfo=UTC)]
        self.notes, self.extraction = FakeConnector("notes", list(NOTES)), FakeExtraction()
        monkeypatch.setattr(cli, "open_workspace", lambda folder: self.open())

    def open(self) -> Cerebro:
        return Cerebro(self.root, extraction=self.extraction, connectors=[self.notes], clock=lambda: self.at[0],
                       agent_run=ScriptedRun(lambda prompt: self.said[0], clock=lambda: self.at[0]))


def test_agents_run_by_hand_that_wrote_nothing_exits_0_and_moves_the_articles_clock(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weekly = Weekly(tmp_path / "w", monkeypatch, [""])
    with weekly.open() as workspace:
        workspace.run_once(agents=False)  # the notes become facts, and no agent runs
    told = CliRunner().invoke(main, ["agents", "run", "reports/weekly", "--workspace", str(weekly.root)])
    assert told.exit_code == 0, told.output
    assert "ran      reports/weekly: wrote nothing: it said there is nothing to write" in told.output
    with weekly.open() as workspace:
        beat = workspace.store.state("heartbeat:reports/weekly") or {}
    assert (beat.get("articles") or {}).get("reports/weekly", {}).get("ran") == "2026-11-17T09:00:00Z", beat


def test_what_a_run_by_hand_that_wrote_nothing_says_comes_masked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weekly = Weekly(tmp_path / "w", monkeypatch, [""])
    with weekly.open() as workspace:
        workspace.run_once(agents=False)
        workspace.mask = lambda text: text.replace("stand-in", "[masked]")  # type: ignore[method-assign]
        compiled = workspace.compile(workspace.agent("reports/weekly"), this="reports/weekly", scheduled=True)
    assert compiled.run is not None and "[masked]" in compiled.run.note and "stand-in" not in compiled.run.note


# -- 3. the first run's merged and masked result

def _every(n: int) -> PassResult:
    """A result with every field a pass fills set to a value of its own, so a merge that leaves one out shows."""
    values: dict[str, Any] = {}
    for f in dataclasses.fields(PassResult):
        if not f.init or f.name in INFRA:
            continue
        kind = type(f.default_factory()) if f.default_factory is not dataclasses.MISSING else type(f.default)
        values[f.name] = ({f"{f.name}-{n}": [n] if "list" in str(f.type) else n} if kind is dict else [f"{f.name}-{n}"] if kind is list
                          else {f"{f.name}-{n}"} if kind is set else n if kind is int else None)
        assert values[f.name] is not None, f"{f.name} is of a kind this test does not know: say how a merge takes it"
    values["runs"] = [_run("done", agent=f"runs-{n}")]
    return PassResult(**values)


INFRA = {"token", "say", "on", "_said"}  # whose leases, and where lines and events go: the pass's own, never merged
# `next` is set only on the pass of a first run that failed under serve, which no build merges
INFRA |= {"next"}


def test_the_first_runs_merge_keeps_every_field_of_both_passes() -> None:
    from cerebro.first_run import _merge

    into, more = _every(1), _every(2)
    _merge(into, more)
    for f in dataclasses.fields(PassResult):
        if f.name in INFRA or not f.init:
            continue
        got = getattr(into, f.name)
        if isinstance(got, int):
            assert got == 3, f.name
        elif f.name == "runs":
            assert [r.agent for r in got] == ["runs-1", "runs-2"]
        else:
            assert f"{f.name}-1" in got and f"{f.name}-2" in got, (f.name, got)


def test_an_agent_due_in_either_merged_pass_is_not_said_skipped() -> None:
    from cerebro.first_run import _merge

    into = PassResult(due={"things/article": "its evidence moved"})
    _merge(into, PassResult(skipped={"things/article": "nothing it reads has moved", "parts/article": "manual"}))
    assert into.due == {"things/article": "its evidence moved"} and into.skipped == {"parts/article": "manual"}


def test_the_first_runs_build_masks_what_ran_and_wrote_nothing_as_it_masks_failures() -> None:
    from cerebro.first_run import _masked

    mask = lambda text: text.replace("secret", "[masked]")
    workspace = SimpleNamespace(mask=mask, mask_every=lambda: mask)
    result = _masked(workspace, PassResult(ran={"a": "a secret"}, failed={"b": "a secret"}))  # type: ignore[arg-type]
    assert result.ran == {"a": "a [masked]"} and result.failed == {"b": "a [masked]"}


def test_a_pass_masks_what_ran_and_wrote_nothing_after_it_ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_once` masks `ran` as it masks `failed`, as the last step, with the mask the pass ends with."""
    weekly = Weekly(tmp_path / "w", monkeypatch, [""])
    with weekly.open() as workspace:
        from cerebro import app

        real = app.run_pass

        def after(*args: Any, **kwargs: Any) -> PassResult:
            done = real(*args, **kwargs)
            done.ran = {key: why + " sk-planted" for key, why in done.ran.items()}
            return done

        monkeypatch.setattr(app, "run_pass", after)
        workspace.mask = lambda text: text.replace("sk-planted", "[masked]")  # type: ignore[method-assign]
        result = workspace.run_once()
    assert result.ran and all(why.endswith("[masked]") for why in result.ran.values()), result.ran


# -- 4. the live checks

def test_the_live_checks_flag_a_run_that_spent_its_room_or_was_cut_with_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "checks"))
    from _paths import paths

    assert paths(_run("failed", SPENT_NOTHING)).endswith("spent its room or calls and handed in nothing")
    assert paths(_run("failed", CUT_NOTHING)).endswith("was cut at its output limit and handed in nothing")
    assert paths(_run("failed", HANDED_NOTHING)).endswith(": handed in nothing")


def test_every_live_check_that_prints_a_passs_failures_prints_what_ran_and_wrote_nothing_beside_them() -> None:
    import re

    checks = Path(__file__).resolve().parents[1] / "checks"
    printing = [p for p in sorted(checks.glob("*.py")) if re.search(r"result\.failed\b", p.read_text())]
    assert printing
    missing = [p.name for p in printing if not re.search(r"result\.ran\b|\bran_lines\(", p.read_text())]
    assert not missing, missing
