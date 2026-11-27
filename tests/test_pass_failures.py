"""A curator's run that fails is a failure of the pass, however the runtime reports it: the pass's line says it
failed and why, and its count holds it. The pass exits 1 only when nothing landed: an article written, facts extracted or retracted, an admit, or an agent run that did not fail. A
failed curator waits before it runs again, as a writer's failed run does; one that stopped at its budget with nothing
decided is a run, says the key to raise, and waits for its file to change."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from cerebro import Cerebro, PassResult, cli, first_run
from cerebro.cli import main
from cerebro.fakes import FakeExtraction
from cerebro.run import AgentRun, PydanticRun, Run, Usage

CURATOR = """---
heartbeat: [change]
---

# Instructions

Decide which things the wiki keeps.
"""

FAILURES = {
    "a server error": ModelHTTPError(500, "made-up-model", {"message": "the server fell over"}),
    "a timeout": httpx.ReadTimeout("the read timed out"),
    "a dropped connection": httpx.RemoteProtocolError("Server disconnected without sending a response."),
}


@pytest.mark.parametrize("way", list(FAILURES))
def test_a_curators_run_that_fails_inside_the_model_loop_is_a_failure_of_the_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: str) -> None:
    root = tmp_path / "ws"
    (root / "agents" / "things").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    (root / "agents" / "things" / "curator.md").write_text(CURATOR)
    asked: list[str] = []

    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        asked.append(way)
        raise FAILURES[way]

    def opened(where: Any) -> Cerebro:
        return Cerebro(where, trusted=True, extraction=FakeExtraction(), agent_run=PydanticRun(FunctionModel(fail)))

    monkeypatch.setattr(cli, "open_workspace", opened)
    monkeypatch.setattr(first_run, "run", lambda *args, **kwargs: None)  # an ordinary pass, past the first run
    done = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    assert asked, done.output  # the model loop was entered, and raised
    lines = done.output.splitlines()
    ran = next(line for line in lines if line.startswith("ran       things/curator"))
    assert ran.startswith("ran       things/curator: failed, ") and "changed nothing" not in done.output, done.output
    assert any(line.startswith("failed    things/curator: ") for line in lines), done.output
    assert lines[-1] == "Done: 1 failed.", done.output
    assert done.exit_code == 1  # a failure with nothing landed fails the pass


class Stand(AgentRun):
    """Each agent's run as `ways` says, by agent id: `done`, `failed`, `budget spent` with the note `notes` gives, or
    `raise`. It counts who it was asked for."""

    def __init__(self, ways: dict[str, str], note: str = "the run holds 120000 tokens, its budget of 100000"):
        self.ways, self.note, self.asked = ways, note, []

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Run:
        self.asked.append(agent.id)
        way = self.ways.get(agent.id, "done")
        if way == "raise":
            raise RuntimeError("the stand-in raised")
        note = {"failed": "the stand-in failed", "budget spent": self.note}.get(way, "")
        return Run(f"run_{len(self.asked)}", agent.id, agent.target, way, note=note,
                   usage=Usage(model="stand-in", requests=1))


def curators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand: Stand, *names: str) -> Path:
    root = tmp_path / "ws"
    (root / "settings.yaml").parent.mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    for name in names:
        (root / "agents" / name).mkdir(parents=True)
        (root / "agents" / name / "curator.md").write_text(CURATOR)
    monkeypatch.setattr(cli, "open_workspace", lambda where: Cerebro(where, trusted=True, extraction=FakeExtraction(),
                                                                 agent_run=stand))
    monkeypatch.setattr(first_run, "run", lambda *args, **kwargs: None)  # an ordinary pass, past the first run
    return root


def serve_once(root: Path) -> Any:
    return CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])


@pytest.mark.parametrize(("note", "key"), [("the run holds 120000 tokens, its budget of 100000", "budget.tokens_per_run"),
                                           ("The next tool call(s) would exceed the tool_calls_limit of 90",
                                            "budget.tool_calls")])
def test_a_curator_that_stops_at_its_budget_says_so_counts_as_a_run_and_waits_for_its_budget_to_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, note: str, key: str) -> None:
    stand = Stand({"things/curator": "budget spent"}, note)
    root = curators(tmp_path, monkeypatch, stand, "things")
    done = serve_once(root)
    stop = (f"stopped at its budget for things collection with nothing decided, after reading 0 items of its "
            f"evidence: raise {key} in agents/things/curator.md")
    assert f"ran       things/curator: {stop}, $" in done.output and "changed nothing" not in done.output, done.output
    assert f"warning   things/curator: {stop}" in done.output, done.output
    assert done.output.splitlines()[-1] == "Done." and done.exit_code == 0, done.output
    waited = serve_once(root)  # nothing moved, and its budget is as it was: no run, no spend
    assert stand.asked == ["things/curator"], waited.output
    why = (f"its run stopped at its budget with nothing decided, so it waits until agents/things/curator.md changes: "
           f"raise {key} there")
    assert f"skipped   things/curator: {why}" in waited.output and waited.exit_code == 0, waited.output
    shown = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert f"waiting   things/curator: {why}" in shown.output, shown.output
    (root / "agents" / "things" / "curator.md").write_text(CURATOR.replace("---\n", "---\nbudget: { tool_calls: 80 }\n", 1))
    serve_once(root)
    assert stand.asked == ["things/curator", "things/curator"]


def test_a_curator_that_stopped_at_its_budget_stays_fresh_after_a_run_by_hand_until_its_inputs_move(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stand = Stand({"things/curator": "budget spent"})
    root = curators(tmp_path, monkeypatch, stand, "things")
    serve_once(root)
    serve_once(root)
    assert stand.asked == ["things/curator"]
    by_hand = CliRunner().invoke(main, ["agents", "run", "things/curator", "--workspace", str(root)])
    assert stand.asked == ["things/curator"] * 2, by_hand.output
    after = serve_once(root)
    assert stand.asked == ["things/curator"] * 2, after.output
    assert "things/curator: skipped, nothing it reads has moved" in after.output, after.output


@pytest.mark.parametrize("way", ["failed", "raise"])
def test_a_curator_whose_run_failed_waits_and_the_pass_and_status_say_until_when(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: str) -> None:
    stand = Stand({"things/curator": way})
    root = curators(tmp_path, monkeypatch, stand, "things")
    assert serve_once(root).exit_code == 1
    waited = serve_once(root)
    assert stand.asked == ["things/curator"], waited.output
    line = next(line for line in waited.output.splitlines() if line.startswith("skipped   things/curator: "))
    assert re.fullmatch(r"skipped   things/curator: its run failed 1 time, so it waits until \S+Z before it runs "
                        r"again, or until its file, workspace\.md or settings\.yaml changes", line), waited.output
    assert waited.exit_code == 0 and waited.output.splitlines()[-1] == "Done.", waited.output
    shown = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert "waiting   " + line.removeprefix("skipped   ") in shown.output.splitlines(), shown.output


@pytest.mark.parametrize("way", ["failed", "raise"])
def test_a_curator_that_fails_beside_one_that_ran_and_changed_nothing_exits_0(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: str) -> None:
    stand = Stand({"things/curator": way})
    root = curators(tmp_path, monkeypatch, stand, "things", "others")
    done = serve_once(root)
    assert "ran       others/curator: changed nothing" in done.output, done.output
    assert any(line.startswith("failed    things/curator: ") for line in done.output.splitlines()), done.output
    assert done.output.splitlines()[-1] == "Done: 1 failed.", done.output
    assert done.exit_code == 0, done.output  # a run that did not fail landed


def test_a_writer_whose_run_stops_at_its_budget_with_nothing_handed_in_fails_and_waits_as_a_failed_run_does(
        tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from cerebro import Record
    from cerebro.fakes import FakeConnector, MemoryStore

    at = [datetime(2026, 11, 17, 9, tzinfo=UTC)]
    (tmp_path / "agents" / "reports").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    notes = FakeConnector("notes", [Record(id="n1", source="notes", revision="1", at="2026-11-10T09:00:00Z",
                                           body="Note one.")])
    stand = Stand({"reports/weekly": "budget spent"})
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: at[0],
                      connectors=[notes], agent_run=stand)
    first = cerebro.run_once()
    assert first.failed["reports/weekly"].startswith("run run_1 wrote nothing: budget spent"), first
    waited = cerebro.run_once()
    assert stand.asked == ["reports/weekly"], waited
    assert waited.skipped["reports/weekly"] == (
        "its run stopped at its budget with nothing handed in (budget.tokens_per_run) 1 time, so it waits until "
        "2026-11-17T09:00:30Z before it runs again, or until its file, workspace.md or settings.yaml changes")
    at[0] += timedelta(seconds=30)
    cerebro.run_once()
    assert stand.asked == ["reports/weekly"] * 2


@pytest.mark.parametrize("way", ["failed", "raise"])
def test_a_first_run_whose_curator_fails_says_it_once_and_counts_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, way: str) -> None:
    stand = Stand({"things/curator": way})
    root = tmp_path / "ws"
    (root / "agents" / "things").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    (root / "agents" / "things" / "curator.md").write_text(CURATOR)
    monkeypatch.setattr(cli, "open_workspace", lambda where: Cerebro(where, trusted=True, extraction=FakeExtraction(),
                                                                 agent_run=stand))
    monkeypatch.setattr(Cerebro, "weighs_nothing", lambda self, agent: False)  # it has something to weigh, so it is asked
    done = CliRunner().invoke(main, ["serve", "--once", "--yes", "--workspace", str(root)])
    # nothing to decide beside it, so its failure is the run's and nothing is built
    assert stand.asked == ["things/curator"], done.output
    failed = [line for line in done.output.splitlines() if line.startswith("failed    ")]
    assert len(failed) == 1 and failed[0].startswith("failed    things/curator: "), done.output
    assert done.output.splitlines()[-1] == "[x] Failed. Run: cerebro serve --once", done.output
    assert done.exit_code == 1, done.output  # nothing landed


def test_a_curator_whose_real_run_spends_its_token_budget_says_so_and_runs_no_more_with_nothing_moved(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.usage import RequestUsage

    asked: list[int] = []

    def spend(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        asked.append(len(messages))
        tool = info.function_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool, {})], usage=RequestUsage(output_tokens=900_000))

    root = tmp_path / "ws"
    (root / "agents" / "things").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    (root / "agents" / "things" / "curator.md").write_text(CURATOR)
    monkeypatch.setattr(cli, "open_workspace", lambda where: Cerebro(
        where, trusted=True, extraction=FakeExtraction(), agent_run=PydanticRun(FunctionModel(spend))))
    monkeypatch.setattr(first_run, "run", lambda *args, **kwargs: None)
    done = serve_once(root)
    assert len(asked) == 1, done.output  # the one reply passed its budget, so no second request was made
    assert "ran       things/curator: stopped at its budget for things collection with nothing decided" in done.output
    assert "raise budget.tokens_per_run in agents/things/curator.md" in done.output and done.exit_code == 0
    for _ in range(2):
        assert "skipped   things/curator: its run stopped at its budget" in serve_once(root).output
    assert len(asked) == 1


def test_a_writers_wait_on_one_article_is_not_ended_by_its_run_on_another(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from cerebro import Record
    from cerebro.article import Identity
    from cerebro.fakes import FakeConnector, MemoryStore, ScriptedRun

    def cited(prompt: str) -> dict[str, str]:
        first = re.search(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])
        return {"Summary": f"x [^{first.group(1)}]"} if first else {"Summary": "x"}

    class Split(AgentRun):
        def __init__(self) -> None:
            self.fine, self.asked = ScriptedRun(cited, clock=lambda: at[0]), []

        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Run:
            self.asked.append(agent.target)
            if agent.target == "things/b":
                return Run(f"run_{len(self.asked)}", agent.id, agent.target, "budget spent",
                           note="the run holds 120000 tokens, its budget of 100000", usage=Usage(model="s", requests=1))
            return self.fine.run(agent, evidence, tools, budget)

    def note(id: str) -> Record:
        return Record(id=id, source="notes", revision="1", at="2026-11-17T08:00:00Z", body=f"Note {id}.")

    at = [datetime(2026, 11, 17, 9, tzinfo=UTC)]
    notes, split = FakeConnector("notes", [note("n0")]), Split()
    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("compile: { concurrency: 1 }\n")
    (tmp_path / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: at[0],
                      connectors=[notes], agent_run=split)
    for id in ("things/a", "things/b"):
        cerebro.store.create(Identity(id, "things", id, {}))
    cerebro.run_once()
    for n in (1, 2):  # new evidence each time: things/a runs, things/b waits out its 30 seconds
        notes.add(note(f"n{n}"))
        at[0] += timedelta(seconds=1)
        cerebro.run_once()
    assert split.asked.count("things/b") == 1 and split.asked.count("things/a") == 3, split.asked


def test_a_first_run_curator_that_failed_then_ran_is_a_warning_and_not_a_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # parts fails at discovery, beside a list that goes on, then runs in the build to make what things proposed for it
    from test_first_run import Script
    from test_first_run_structure import by_record, lay_out_workspace, open_it

    class FailsOnce(Script):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Run:
            if agent.id == "parts/curator" and "parts/curator" not in self.ran:
                self.ran.append(agent.id)
                return Run("run_f", agent.id, agent.target, "failed", note="the stand-in failed")
            return super().run(agent, evidence, tools, budget)

    beside = ("admit", {"record": {"source": "src", "id": "P1"}, "collection": "parts", "reason": "Part of it.",
                        "group": "parts of your things"})
    script = FailsOnce({"things/curator": [by_record("T1"), beside]})
    root = lay_out_workspace(tmp_path)
    monkeypatch.setattr(cli, "open_workspace", lambda where: open_it(root, script))
    done = CliRunner().invoke(main, ["serve", "--once", "--yes", "--workspace", str(root)])
    assert script.ran.count("parts/curator") == 2, done.output
    assert "failed    parts/curator" not in done.output and done.exit_code == 0, done.output
    assert ("warning   parts/curator: failed in this first run, and did not fail when it ran after: discovery for "
            "parts collection did not finish: the stand-in failed") in done.output, done.output


def test_a_first_run_that_stops_on_an_error_still_says_what_failed_before(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_first_run import FakeConnector, Script

    monkeypatch.setattr(FakeConnector, "questions", {"lookup": "a stand-in question"})  # parts is asked
    from test_first_run_structure import by_record, lay_out_workspace, open_it

    from cerebro.errors import CerebroError

    def stops(*args: Any, **kwargs: Any) -> None:
        raise CerebroError("the build could not start")

    class PartsFail(Script):  # beside a list that goes on, so the build starts
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Run:
            if agent.id == "parts/curator":
                return Run("run_f", agent.id, agent.target, "failed", note="the stand-in failed")
            return super().run(agent, evidence, tools, budget)

    script = PartsFail({"things/curator": [by_record("T1")]})
    root = lay_out_workspace(tmp_path)
    monkeypatch.setattr(cli, "open_workspace", lambda where: open_it(root, script))
    monkeypatch.setattr(first_run, "build", stops)
    done = CliRunner().invoke(main, ["serve", "--once", "--yes", "--workspace", str(root)])
    assert "failed    parts/curator: " in done.output and "the build could not start" in done.output, done.output
    assert done.exit_code == 1


def _agent(id: str, role: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=id, role=role)


CURATORS = [_agent("things/a", "curator"), _agent("things/b", "curator"), _agent("things/article", "writer")]


@pytest.mark.parametrize(("result", "kept", "still_fails", "want_exit"), [
    pytest.param(PassResult(compiled=["things/alpha"], failed={"sources/notes": "the check failed"},
                            runs=[Run("r1", "things/article", "things/alpha", "done")]), {}, False, 1,
                 id="a source check failed beside an article written, exits 1"),
    pytest.param(PassResult(failed={"things/a": "a model error"}, runs=[Run("r1", "things/a", "things", "failed"),
                                                                         Run("r2", "things/b", "things", "done")]),
                 {}, False, 0, id="a curator failed beside another that ran, exits 0"),
    pytest.param(PassResult(runs=[Run("r2", "things/a", "things", "done")]), {"things/a": "a model error"}, False, 0,
                 id="a first run's curator failed in its rounds and then ran, exits 0"),
    pytest.param(PassResult(), {"things/a": "a model error"}, True, 1,
                 id="a first run's curator failed and did not run again, nothing landed, exits 1"),
])
def test_a_first_run_exits_by_how_its_run_settles(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], result: PassResult,
        kept: dict[str, str], still_fails: bool, want_exit: int) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(cli, "failing", lambda workspace, what: still_fails)
    workspace = SimpleNamespace(root=tmp_path, agents=CURATORS)
    out = SimpleNamespace(applied=[], quit=False, result=result, lines=[])
    try:
        cli._print_first_run(out, workspace, kept)  # type: ignore[arg-type]
        code = 0
    except SystemExit as stop:
        code = int(stop.code or 0)
    lines = capsys.readouterr().out.splitlines()
    assert code == want_exit, lines
    assert all(f"failed    {what}: {why}" in lines for what, why in result.failed.items() if what not in kept), lines
    for what, why in kept.items():
        assert (f"failed    {what}: {why}" in lines) is still_fails, lines
