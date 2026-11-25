"""Runs that went and did not finish: a part that failed gets one more try in its
period and no part that finished runs again, a part the cap cut runs once room is back, `agents run` runs every
article and says each failure, waits at a cap reached mid-run, tells a run's own budget from a daily cap and says
truly when a held part runs, and `serve` reads a raised cap at its next wake. Stand-in store, fake extraction and
scripted runs: no model is called and no key is real."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_pass_at_cap import DAY, Logged, Workspace, collection, composed, opener

import cerebro.compile as compile_module
from cerebro import cli
from cerebro.budget import HeldForRoom
from cerebro.run import Run
from cerebro.runner import run_forever

RISKS, PEOPLE, COMPOSER = "things/subagents/risks", "things/subagents/people", "things/article"
RAISED = "budget: { daily_usd: 5000 }\ncompile: { concurrency: 1 }\n"


@pytest.fixture(autouse=True)
def no_decision_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


class Unfinished(Logged):
    """Logged, with one agent on one article whose run does not finish `times` times: it fails, or with `cut` the
    budget cuts it before it hands in text, crossing the daily cap first, or with `own` it spends its own budget."""

    def __init__(self, space: Workspace, who: str, article: str, times: int = 1, how: str = "failed") -> None:
        super().__init__(space)
        self.who, self.page, self.times, self.how, self.tries = who, article, times, how, Counter[str]()

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        self.tries[f"{agent.id} on {agent.target}"] += 1
        if (agent.id, agent.target) == (self.who, self.page) and self.times:
            self.times -= 1
            if self.how == "cut":
                self.space.spent(self.space.clock.at.isoformat().replace("+00:00", "Z"), "compilation", 60.0)
            if self.how in ("cut", "own"):
                return Run("run_cut", agent.id, agent.target, "budget spent", note="the run holds 41000 tokens, its "
                           "budget of 40000, and it handed in nothing before then, so nothing lands")
            return Run("run_x", agent.id, agent.target, "failed", note="the model broke")
        return super().run(agent, evidence, tools, budget)


def passes(space: Workspace, run: Logged, n: int, raise_at: int | None = None) -> list[Any]:
    opened, results = opener(space, run), []
    for k in range(n):
        space.clock.at = DAY + timedelta(minutes=10 * k)
        if k == raise_at:
            space.settings(RAISED)
        results.append(opened().run_once(scheduled=True))
    return results


def test_a_part_that_failed_fails_its_article_and_the_one_more_try_runs_every_part(tmp_path: Path) -> None:
    """Nothing of the article lands, and its one more try in the period runs it whole."""
    space = composed(tmp_path)
    run = Unfinished(space, RISKS, "things/b")
    first, second, *_ = passes(space, run, 6)
    assert list(first.failed) == [f"{COMPOSER} on things/b"] and first.compiled == ["things/a"]
    assert not [line for line in first.warnings if "runs again" in line]
    assert second.compiled == ["things/b"]
    assert run.tries == {f"{who} on {article}": 1 for who in (RISKS, PEOPLE, COMPOSER) for article in ("things/a", "things/b")
                         } | {f"{RISKS} on things/b": 2, f"{PEOPLE} on things/b": 2}


def test_a_part_that_fails_all_day_fails_its_article_twice_and_no_note_says_it_runs_again(tmp_path: Path) -> None:
    space = composed(tmp_path)
    run = Unfinished(space, RISKS, "things/b", times=99)
    first, second, *rest = passes(space, run, 6)
    assert run.tries[f"{RISKS} on things/b"] == 2 and run.tries[f"{PEOPLE} on things/b"] == 2
    assert run.tries[f"{COMPOSER} on things/b"] == 0  # a part failed, so its own run does not start
    assert f"{COMPOSER} on things/b" in first.failed and f"{COMPOSER} on things/b" in second.failed
    assert not [line for result in (first, second) for line in result.warnings if "runs again" in line]
    assert not any(result.compiled or result.failed or result.warnings for result in rest)


def test_a_held_part_beside_a_failed_one_fails_its_article(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = composed(tmp_path)
    space.clock.at = DAY
    real = compile_module.run_agent

    def held(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if (agent.id, agent.target) == (PEOPLE, "things/b"):
            raise HeldForRoom("held for room under the cap, runs next pass: runs under way hold it")
        return real(agent, *args, **kwargs)

    monkeypatch.setattr(compile_module, "run_agent", held)
    result = opener(space, Unfinished(space, RISKS, "things/b"))().run_once(scheduled=True)
    assert f"{COMPOSER} on things/b" in result.failed, result


def test_a_part_the_cap_cut_waits_and_runs_once_room_is_back_in_its_period(tmp_path: Path) -> None:
    """The cut crosses the cap before the other part starts, so that one is refused and waits too."""
    import threading

    class Early(Unfinished):
        def __init__(self, space: Workspace) -> None:
            super().__init__(space, RISKS, "things/a", how="cut")
            self.cut = threading.Event()

        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            if (agent.id, agent.target) == (RISKS, "things/a"):
                try:
                    return super().run(agent, evidence, tools, budget)
                finally:
                    self.cut.set()
            return super().run(agent, evidence, tools, budget)

    space = composed(tmp_path)
    run = Early(space)
    real = compile_module.run_agent

    def after_the_cut(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if (agent.id, agent.target) == (PEOPLE, "things/a"):
            assert run.cut.wait(5)
        return real(agent, *args, **kwargs)

    compile_module.run_agent = after_the_cut  # type: ignore[assignment]
    try:
        first, *_ = passes(space, run, 6, raise_at=3)
    finally:
        compile_module.run_agent = real  # type: ignore[assignment]
    assert f"{RISKS} on things/a" in first.waiting_runs and not first.failed
    assert run.tries[f"{RISKS} on things/a"] == 2 and run.tries[f"{PEOPLE} on things/a"] == 1


def test_a_part_the_cap_cut_after_its_sibling_finished_waits_and_its_article_runs_whole_next(tmp_path: Path) -> None:
    """Parts run side by side: the cut lands here once the other part has finished, so none was refused. Nothing of
    the article landed, so what the finished part returned was not kept, and the next run takes every part."""
    import threading

    class Late(Unfinished):
        def __init__(self, space: Workspace) -> None:
            super().__init__(space, RISKS, "things/a", how="cut")
            self.finished = threading.Event()

        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            if (agent.id, agent.target) == (RISKS, "things/a"):
                assert self.finished.wait(5)
            done = super().run(agent, evidence, tools, budget)
            if (agent.id, agent.target) == (PEOPLE, "things/a"):
                self.finished.set()
            return done

    space = composed(tmp_path)
    run = Late(space)
    first, *_ = passes(space, run, 6, raise_at=3)
    assert f"{RISKS} on things/a" in first.waiting_runs and not first.failed, first
    assert run.tries[f"{RISKS} on things/a"] == 2 and run.tries[f"{PEOPLE} on things/a"] == 2


def test_a_part_that_spent_its_own_budget_gets_one_more_try_as_a_failure(tmp_path: Path) -> None:
    space = composed(tmp_path)
    run = Unfinished(space, RISKS, "things/a", times=99, how="own")
    first, *_ = passes(space, run, 6)
    assert not first.waiting_runs and not first.stops
    assert run.tries[f"{RISKS} on things/a"] == 2


def test_agents_run_runs_every_article_says_each_failure_and_exits_1_with_the_count(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = collection(tmp_path, ("a", "b", "c"))
    run = Unfinished(space, COMPOSER, "things/a")
    monkeypatch.setattr(space, "open", opener(space, run))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER)
    assert ran.exit_code == 1
    assert ran.output.splitlines() == [
        "failed   things/article on things/a: run run_x wrote nothing: failed: the model broke",
        "wrote    things/b  articles/things/b.md", "wrote    things/c  articles/things/c.md",
        "Error: things/article: 1 of 3 runs failed"]


def test_agents_run_goes_on_past_an_article_whose_run_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = collection(tmp_path, ("a", "b"))
    run = Logged(space, breaks={"things/a": 1})
    monkeypatch.setattr(space, "open", opener(space, run))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER)
    assert ran.exit_code == 1 and run.day() == {"things/b": 1}
    assert ran.output.splitlines()[0] == "failed   things/article on things/a: RuntimeError: the model broke"
    assert ran.output.splitlines()[-1] == "Error: things/article: 1 of 2 runs failed"


def test_agents_run_cut_by_the_cap_mid_run_waits_says_the_stop_and_exits_0(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = collection(tmp_path, ("a", "b", "c"))
    space.clock.at = DAY + timedelta(hours=1)
    monkeypatch.setattr(space, "open", opener(space, Unfinished(space, COMPOSER, "things/a", how="cut")))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER)
    lines = ran.output.splitlines()
    assert ran.exit_code == 0, ran.output
    assert "waiting  things/b: the daily budget is spent" in lines
    assert "waiting  things/article on things/a: the daily budget is spent" in lines
    assert lines[-1] == "Daily budget: $60.00 of $50.00 spent at 11:00 UTC. 3 agent runs wait."
    assert space.cli(monkeypatch, "status").output.splitlines()[0] == lines[-1]


@pytest.mark.parametrize("text", [False, True])
def test_agents_run_tells_a_runs_own_budget_from_a_daily_cap(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: bool) -> None:
    space = composed(tmp_path)
    space.clock.at = DAY + timedelta(hours=1)

    class Own(Logged):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            done = super().run(agent, evidence, tools, budget)
            if (agent.id, agent.target) == (RISKS, "things/b"):
                done.status, done.note = "budget spent", "the run holds 41000 tokens, its budget of 40000"
                done.result = done.result if text else None
            return done

    monkeypatch.setattr(space, "open", opener(space, Own(space)))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER)
    assert "daily budget" not in ran.output and not space.open().ledger.reached(space.clock())
    if text:  # what it handed in before its budget ran out landed
        assert ran.exit_code == 0 and "failed" not in ran.output, ran.output
        return
    assert ran.exit_code == 1  # a part that failed fails its article
    assert (f"failed   {COMPOSER} on things/b: run run_scripted spent its own budget, not a daily cap, before it "
            f"handed in text: budget: {{ tokens_per_run, tool_calls }} in agents/{RISKS}.md sets it: the run holds "
            "41000 tokens, its budget of 40000") in ran.output.splitlines()


def test_a_part_agents_run_left_held_after_its_periods_run_is_said_once_and_runs_at_the_next_scheduled_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = composed(tmp_path)
    passes(space, Logged(space), 1)  # the day's scheduled run
    space.clock.at = DAY + timedelta(hours=1)
    real = compile_module.run_agent

    def held(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if (agent.id, agent.target) == (RISKS, "things/b"):
            raise HeldForRoom("held for room under the cap, runs next pass: runs under way hold it")
        return real(agent, *args, **kwargs)

    monkeypatch.setattr(compile_module, "run_agent", held)
    run = Logged(space)
    monkeypatch.setattr(space, "open", opener(space, run))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER)
    monkeypatch.setattr(compile_module, "run_agent", real)
    said = (f"held     {RISKS} on things/b: held for room under the cap, runs at its writer's next scheduled run, "
            "from 2026-11-20 00:00")
    assert [line for line in ran.output.splitlines() if RISKS in line] == [said], ran.output
    for later in (timedelta(hours=2), timedelta(hours=5)):
        space.clock.at = DAY + later
        opener(space, run)().run_once(scheduled=True)
    assert run.by[("2026-11-19", RISKS, "things/b")] == 0
    space.clock.at = DAY + timedelta(days=1, minutes=1)
    opener(space, run)().run_once(scheduled=True)
    assert run.by[("2026-11-20", RISKS, "things/b")] == 1


def test_serve_reads_a_raised_cap_at_its_next_wake_and_says_calls_resume(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    workspace = space.open()
    printed, wakes = cli._passes(workspace), [0]

    def sleep(_: float) -> None:
        wakes[0] += 1
        if wakes[0] == 1:
            space.settings("budget: { daily_usd: 500 }\n")
        space.clock.at += timedelta(minutes=5)

    run_forever(workspace, interval=300, stop=lambda: wakes[0] >= 2, sleep=sleep, on_pass=printed, say=cli._echo)
    said = [line for line in capsys.readouterr().out.splitlines() if line.startswith(("Daily", "wrote"))]
    assert said == ["Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 2 records and 1 agent run wait.",
                    "Daily budget: cap is now $500.00, so model calls resume",
                    "wrote     reports/weekly  articles/reports/weekly.md"]
    assert workspace.ledger.caps.total == 500


def test_serve_takes_a_cap_out_only_once_two_wakes_read_it_gone_and_keeps_its_caps_on_a_bad_file(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from cerebro.runner import _read_caps

    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    workspace, kept, said = space.open(), {}, []
    for spoiled in ("budget: { daily_usd: [\n", ""):
        space.settings(spoiled)
        for _ in range(2):
            _read_caps(workspace, kept, said.append)
    assert workspace.ledger.caps.total == 50 and len(said) == 2, said
    assert said[0].startswith("settings: settings.yaml is not YAML at") and said[1].startswith(
        "settings: settings.yaml is not a map of settings, so the daily budget keeps")
    space.settings("{}\n")
    _read_caps(workspace, kept, said.append)
    assert workspace.ledger.caps.total == 50
    _read_caps(workspace, kept, said.append)
    assert workspace.ledger.caps.total is None and len(said) == 2  # no cap was reached, so nothing resumed



def test_agents_run_fails_an_article_whose_cut_run_handed_in_text_that_did_not_stand(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = collection(tmp_path, ("a", "b"))
    space.clock.at = DAY + timedelta(hours=1)

    class Dropped(Logged):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            done = super().run(agent, evidence, tools, budget)
            if agent.target == "things/a":
                done.status, done.note = "budget spent", "the run holds 41000 tokens, its budget of 40000"
                done.result.text = "## Summary\n\nA claim citing nothing it read [^no_such_fact]."
            return done

    monkeypatch.setattr(space, "open", opener(space, Dropped(space)))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER)
    assert ran.exit_code == 1, ran.output
    assert [line for line in ran.output.splitlines() if line.startswith("failed   things/article on things/a: run ")]


def test_a_settings_file_that_raises_as_it_reads_keeps_the_serve_loop_going(tmp_path: Path) -> None:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    workspace, wakes, said = space.open(), [0], []

    def sleep(_: float) -> None:
        wakes[0] += 1
        if wakes[0] == 1:
            space.settings("budget: { daily_usd: 50 }\nnote: 2026-02-30\n")  # a date YAML cannot build
        space.clock.at += timedelta(minutes=5)

    assert run_forever(workspace, interval=300, stop=lambda: wakes[0] >= 3, sleep=sleep, say=said.append) == 3
    assert workspace.ledger.caps.total == 50 and len([line for line in said if line.startswith("settings: ")]) == 1


def test_a_run_without_subagents_the_cap_cut_waits_in_a_pass(tmp_path: Path) -> None:
    space = collection(tmp_path, ("a", "b"))
    space.clock.at = DAY + timedelta(hours=1)
    result = opener(space, Unfinished(space, COMPOSER, "things/a", how="cut"))().run_once(scheduled=True)
    assert not result.failed and "things/article on things/a" in result.waiting_runs


def test_agents_run_runs_an_article_with_nothing_to_cite_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.article import Identity

    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    (space.root / "agents" / "reports" / "weekly.md").unlink()
    (space.root / "agents" / "things").mkdir()
    (space.root / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: things, kind: no_such_kind }\nheartbeat: [daily]\n---\n\n"
        "# Output\n\n## Summary\n")
    space.store.create(Identity("things/a", "things", "A"))
    run = Logged(space)
    monkeypatch.setattr(space, "open", opener(space, run))
    ran = space.cli(monkeypatch, "agents", "run", "things/article")
    # nothing to cite: the writer is asked once, writes nothing, and the run is no failure
    assert ran.exit_code == 0 and run.n == 1 and "runs failed" not in ran.output, ran.output
    assert ran.output.splitlines()[0].startswith("ran      things/article on things/a: wrote nothing: "), ran.output


def test_a_cap_taken_out_needs_two_reads_in_a_row_with_no_error_between(tmp_path: Path) -> None:
    from cerebro.runner import _read_caps

    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    workspace, kept = space.open(), {}
    for text in ("{}\n", "budget: { daily_usd: [\n", "{}\n"):
        space.settings(text)
        _read_caps(workspace, kept, lambda line: None)
    assert workspace.ledger.caps.total == 50


def test_a_raise_says_which_calls_a_layers_own_cap_still_holds(tmp_path: Path) -> None:
    from cerebro.runner import _read_caps

    space = Workspace(tmp_path, "budget: { daily_usd: 50, compilation_daily_usd: 20 }\n")
    space.spent("2026-11-19T09:00:00Z", "compilation", 60.0)
    workspace, said = space.open(), []
    space.settings("budget: { daily_usd: 500, compilation_daily_usd: 20 }\n")
    _read_caps(workspace, {}, said.append)
    assert said == [("Daily budget: cap is now $500.00, so model calls resume, but for compilation calls, whose own "
                     "cap is still reached")]


def test_a_failed_part_and_a_cut_one_on_an_article_take_one_more_try_between_them(tmp_path: Path) -> None:
    """Both parts run in the first pass, the cut one after the other failed: the article fails whole."""
    import threading

    space = composed(tmp_path)
    failed = threading.Event()

    class Both(Unfinished):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            if (agent.id, agent.target) == (RISKS, "things/b"):
                try:
                    return super().run(agent, evidence, tools, budget)
                finally:
                    failed.set()
            if (agent.id, agent.target) == (PEOPLE, "things/b") and not self.tries[f"{PEOPLE} on things/b"]:
                assert failed.wait(5)
                self.tries[f"{PEOPLE} on things/b"] += 1
                self.space.spent(self.space.clock.at.isoformat().replace("+00:00", "Z"), "compilation", 60.0)
                return Run("run_cut", agent.id, agent.target, "budget spent", note="cut")
            return super().run(agent, evidence, tools, budget)

    run = Both(space, RISKS, "things/b", times=99)
    passes(space, run, 6, raise_at=1)
    assert run.tries[f"{RISKS} on things/b"] == 2 and run.tries[f"{PEOPLE} on things/b"] == 2


def test_a_held_part_is_said_to_run_next_pass_only_when_the_period_owes_its_article(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = composed(tmp_path)
    run = Unfinished(space, RISKS, "things/a")  # the day's pass leaves things/a owed for one more try
    opener(space, run)().run_once(scheduled=True)
    space.clock.at = DAY + timedelta(minutes=5)
    real = compile_module.run_agent

    def held(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if (agent.id, agent.target) == (PEOPLE, "things/b"):
            raise HeldForRoom("held for room under the cap, runs next pass: runs under way hold it")
        return real(agent, *args, **kwargs)

    monkeypatch.setattr(compile_module, "run_agent", held)
    monkeypatch.setattr(space, "open", opener(space, run))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER)
    assert (f"held     {PEOPLE} on things/b: held for room under the cap, runs at its writer's next scheduled run, "
            "from 2026-11-20 00:00") in ran.output.splitlines()


def test_agents_run_waits_for_a_part_the_cap_cut_and_fails_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = composed(tmp_path)
    space.clock.at = DAY + timedelta(hours=1)
    monkeypatch.setattr(space, "open", opener(space, Unfinished(space, RISKS, "things/a", how="cut")))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER)
    assert f"waiting  {RISKS} on things/a: the daily budget is spent" in ran.output.splitlines(), ran.output
    assert "failed" not in ran.output and "Error" not in ran.output and ran.exit_code == 0, ran.output


@pytest.mark.parametrize("who", [COMPOSER, RISKS])
def test_one_article_by_hand_whose_run_spent_its_own_budget_says_it_as_a_pass_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, who: str) -> None:
    """The composer's own run cut after its parts returned lands them, as in a pass; a part cut fails the article,
    and the one line names the keys that set the budget."""
    space = composed(tmp_path)
    space.clock.at = DAY + timedelta(hours=1)
    monkeypatch.setattr(space, "open", opener(space, Unfinished(space, who, "things/a", how="own")))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER, "things/a")
    if who == COMPOSER:
        assert ran.exit_code == 0 and "failed" not in ran.output, ran.output
        assert ran.output.splitlines()[0] == "wrote    things/a  articles/things/a.md"
        return
    said = (f"Error: {COMPOSER}: run run_cut spent its own budget, not a daily cap, before it handed in text: budget: "
            f"{{ tokens_per_run, tool_calls }} in agents/{RISKS}.md sets it: the run holds 41000 tokens, its budget of "
            "40000, and it handed in nothing before then, so nothing lands")
    assert ran.exit_code == 1 and ran.output.splitlines() == [said], ran.output


def test_an_article_every_part_of_which_is_held_by_hand_gets_one_held_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = composed(tmp_path)
    passes(space, Logged(space), 1)  # the day's scheduled run
    space.clock.at = DAY + timedelta(hours=1)
    real = compile_module.run_agent

    def held(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if agent.target == "things/b" and agent.id in (RISKS, PEOPLE):
            raise HeldForRoom("held for room under the cap, runs next pass: runs under way hold it")
        return real(agent, *args, **kwargs)

    monkeypatch.setattr(compile_module, "run_agent", held)
    monkeypatch.setattr(space, "open", opener(space, Logged(space)))
    ran = space.cli(monkeypatch, "agents", "run", COMPOSER)
    assert ran.exit_code == 0, ran.output
    said = (f"held     {COMPOSER} on things/b: held for room under the cap, runs at its writer's next scheduled run, "
            "from 2026-11-20 00:00")
    assert ran.output.splitlines() == ["wrote    things/a  articles/things/a.md", said], ran.output
