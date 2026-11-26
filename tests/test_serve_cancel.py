"""A stop of `serve` cancels: no model call starts once it comes, an agent's
run under way is cancelled from the stop's own thread, what it spent is recorded, and the pass ends by its own road.
A copy cut off before it was written is written by the next pass, and a lease whose process is gone is taken over."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from cerebro import Caller, Cerebro, runner
from cerebro.article import Identity
from cerebro.budget import Caps, Ledger, Stopped, stop, stopping
from cerebro.compile import UNMIRRORED
from cerebro.errors import CerebroError
from cerebro.fakes import FakeExtraction, FakeSecrets, MemoryStore
from cerebro.run import STOPPED, PydanticRun

pytestmark = pytest.mark.synced

YOU = Caller("you", "person")
AGENT = """\
---
evidence:
  - facts: {}
heartbeat: [change]
---

Summarize the notes.
"""
KEY = "kestrel-made-up-7c1d-value"  # made up, held in a stand-in secrets store


def lay_out_workspace(root: Path) -> None:
    (root / "agents" / "digests").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\nbudget:\n  daily_usd: 5\n")
    (root / "agents" / "digests" / "weekly.md").write_text(AGENT)


def test_a_stop_cancels_an_agent_run_on_the_model_loop_from_another_thread_and_its_call_is_spent(
        tmp_path: Path) -> None:
    asked = threading.Event()

    async def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        asked.set()
        await asyncio.sleep(30)  # a model call under way, as a slow provider's is
        return ModelResponse(parts=[ToolCallPart("nothing_to_write", {"reason": "late"})])

    root = tmp_path / "ws"
    lay_out_workspace(root)
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), agent_run=PydanticRun(FunctionModel(reply))) as workspace:
        workspace.store.create(Identity("digests/weekly", "digests", "Weekly"))
        workspace.tell(YOU, "The review moved to Friday.", "digests/weekly", "fact")
        out: list[BaseException | None] = []

        def compiling() -> None:
            try:
                workspace.compile(workspace.agent("digests/weekly"), this="digests/weekly", scheduled=True)
                out.append(None)
            except BaseException as error:  # noqa: BLE001 - what ended it is the point
                out.append(error)

        worker = threading.Thread(target=compiling)
        worker.start()
        assert asked.wait(10)
        began = time.monotonic()
        stop(workspace.store)
        worker.join(5)
        took = time.monotonic() - began
        runs = [row.data for row in workspace.store.rows("agent-runs")]
        spent = [row.data for row in workspace.store.rows("spend") if row.data.get("layer") == "compilation"]
    assert not worker.is_alive() and took < 2, took
    assert len(out) == 1 and isinstance(out[0], Stopped), out
    assert [run["status"] for run in runs] == [STOPPED]
    assert len(spent) == 1 and spent[0]["usd"] > 0 and spent[0]["run"] == runs[0]["run"], spent


def test_once_serve_is_stopping_a_model_call_inside_an_agents_run_is_refused_and_a_requests_own_is_not() -> None:
    """A request has the stop's wait to answer, so its own calls, a search's re-ranking or a tell's sort, still run;
    a call inside an agent's run, a tool's, is refused, as the run itself is cancelled."""
    from datetime import UTC, datetime

    from cerebro.budget import under

    store = MemoryStore()
    ledger = Ledger(store, Caps())
    stop(store)
    assert stopping(store) and not stopping(MemoryStore())
    ledger.check("rerank", datetime.now(UTC))
    ledger.reserve("compilation", datetime.now(UTC), None, 1000)
    with under(store), pytest.raises(Stopped):
        ledger.check("rerank", datetime.now(UTC))


def test_an_articles_copy_cut_off_before_it_was_written_is_written_by_the_next_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The process ends between the article's write and its copy's, as an end past the stop's bound does: the copy
    is owed in the same write that landed the article, so the next pass writes it."""
    from cerebro import compile as compiling
    from cerebro.run import PydanticRun as Run

    root = tmp_path / "ws"
    lay_out_workspace(root)
    run = Run.scripted(lambda prompt: "The review moved [^" + prompt.split("[^")[1].split("]")[0] + "].")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), agent_run=run) as workspace:
        workspace.store.create(Identity("digests/weekly", "digests", "Weekly"))
        workspace.tell(YOU, "The review moved to Friday.", "digests/weekly", "fact")

        def ended(*args: Any, **kwargs: Any) -> None:
            raise SystemExit(130)  # the process ends here, before the copy is written

        with monkeypatch.context() as patch:
            patch.setattr(compiling, "mirror", ended)
            with pytest.raises(SystemExit):
                workspace.compile(workspace.agent("digests/weekly"), this="digests/weekly", scheduled=True)
        assert workspace.store.article("digests/weekly") is not None
        assert "digests/weekly" in (workspace.store.state(UNMIRRORED) or [])
        runner._render(workspace, runner.PassResult())
        assert (root / "articles" / "digests" / "weekly.md").is_file()
        assert workspace.store.state(UNMIRRORED) in (None, [])


HOLDING = """
import fcntl, sys, time
held = open(sys.argv[1], "w")
fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
time.sleep(30)
"""


def test_a_lease_whose_process_ended_is_taken_over_at_once_in_one_line_and_a_live_ones_never(tmp_path: Path) -> None:
    """A lease names its process's holder file, which that process holds locked while
    it runs, in whatever process or container of this machine, so one whose lock is free ended."""
    from types import SimpleNamespace

    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "settings.yaml").write_text("{}\n")
    workspace = Cerebro(tmp_path / "ws", store=MemoryStore(), extraction=FakeExtraction(), trusted=True)
    holders = tmp_path / "ws" / ".cerebro" / "holders"
    holders.mkdir(parents=True)
    later, live, dead = "2999-01-01T00:00:00Z", "a" * 32, "b" * 32
    (holders / f"{dead}.lock").write_text("")  # its process ended: nothing holds its lock
    alive = subprocess.Popen([sys.executable, "-c", HOLDING, str(holders / f"{live}.lock")], stdout=subprocess.PIPE,
                             text=True)
    try:
        assert alive.stdout is not None and alive.stdout.readline().strip() == "held"
        workspace.store.set_state("lease:projects/a", {"by": "a-pass-that-died", "until": later, "pid": 4242,
                                                   "holder": dead})
        workspace.store.set_state("lease:projects/b", {"by": "a-pass-that-runs", "until": later, "pid": alive.pid,
                                                   "holder": live})
        workspace.store.set_state("lease:projects/c", {"by": "an-older-pass", "until": later})  # names no holder
        workspace.store.set_state("lease:projects/d", {"by": "another-machine", "until": later, "pid": 7,
                                                   "holder": "c" * 32})  # no file of it here to lock
        said: list[str] = []
        result = runner.PassResult(say=said.append)
        took = {name: runner._lease(workspace, SimpleNamespace(id=f"projects/{name}"), result.token, result)
                for name in "abcd"}
    finally:
        alive.kill()
        alive.wait(5)
    assert took["a"] == "" and workspace.store.state("lease:projects/a")["pid"] == os.getpid()
    assert took["b"] == took["c"] == took["d"] == later
    assert said == ["note      took over the leases of pid 4242, which ended without letting them go"]
    assert not (holders / f"{dead}.lock").exists()
    runner.let_go(workspace)


def test_a_tool_refusal_is_masked_of_every_stored_secret_before_it_reaches_the_caller(tmp_path: Path) -> None:
    from cerebro.mcp import Refusal, _Surface

    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "settings.yaml").write_text("{}\n")
    with Cerebro(tmp_path / "ws", store=MemoryStore(), extraction=FakeExtraction(), trusted=True,
                 secrets=FakeSecrets({"vault": KEY})) as workspace:
        surface = _Surface(workspace, 1000, False, workspace.clock)

        def refused() -> None:
            raise CerebroError(f"the store went away (pass {KEY})")

        with pytest.raises(Refusal) as said:
            surface.answer("find", YOU, {}, refused)
    assert KEY not in str(said.value) and "[secret]" in str(said.value), str(said.value)


def test_a_traceback_serve_prints_is_masked_of_every_stored_secret(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from cerebro import cli

    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "settings.yaml").write_text("{}\n")
    with Cerebro(tmp_path / "ws", store=MemoryStore(), extraction=FakeExtraction(), trusted=True,
                 secrets=FakeSecrets({"vault": KEY})) as workspace, pytest.raises(SystemExit) as ended, \
            cli._masked_errors(workspace):
        raise RuntimeError(f"the store went away (pass {KEY})")
    err = capsys.readouterr().err
    assert ended.value.code == 1 and "Traceback" in err and "[secret]" in err and KEY not in err, err


def test_a_call_whose_model_call_the_stop_refused_is_answered_with_whether_it_wrote(tmp_path: Path) -> None:
    """A stop ends a tool call: the caller gets a refusal, never an error a client sends again
    blindly, and it says whether the call wrote, from what the store committed for it."""
    from cerebro.mcp import STOPPED_NOTHING, STOPPED_WROTE, Calls, Refusal, _Surface
    from cerebro.store import SQLiteStore

    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "settings.yaml").write_text("{}\n")
    store = SQLiteStore(None)
    with Cerebro(tmp_path / "ws", store=store, extraction=FakeExtraction(), trusted=True) as workspace:
        surface = _Surface(workspace, 1000, False, workspace.clock, Calls(store))

        def refused_before() -> None:
            raise Stopped("serve is stopping")

        def refused_after() -> None:
            store.set_state("told", {"words": "kept"})
            raise Stopped("serve is stopping")

        said = []
        for call in (refused_before, refused_after):
            with pytest.raises(Refusal) as refusal:
                surface.answer("tell", YOU, {}, call)
            said.append(str(refusal.value))
    assert said == [STOPPED_NOTHING, STOPPED_WROTE]


def test_a_stop_while_the_first_run_asks_its_curators_records_nothing_of_it_so_the_next_start_asks_again(
        tmp_path: Path) -> None:
    """A curator cut off is not one that read and proposed nothing: the first run's list is never closed by a stop,
    and the next start asks its curators, so a person still decides the list."""
    sys.path.insert(0, str(Path(__file__).parent))
    from test_first_run import ADMITS, NOW, RECORDS, Script
    from test_first_run import lay_out_workspace as first_workspace

    from cerebro.fakes import FakeConnector
    from cerebro.first_run import _state, run
    from cerebro.records import proposals

    class StopsMidRun(Script):
        store: Any = None

        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            done = super().run(agent, evidence, tools[:0], budget)
            stop(self.store)
            done.status, done.note, done.result = STOPPED, "serve stopped before the run ended", None
            return done

    def opened(script: Any) -> Cerebro:
        return Cerebro(root, agent_run=script, extraction=FakeExtraction(), clock=lambda: NOW, trusted=True,
                       connectors=[FakeConnector("crm", list(RECORDS))])

    root = tmp_path / "ws"
    root.mkdir()
    first_workspace(root, knowledge=True)
    script = StopsMidRun({"things/curator": list(ADMITS)})
    with opened(script) as workspace:
        script.store = workspace.store
        with pytest.raises(Stopped):
            run(workspace, choose=None)
        session = _state(workspace) or {}
    assert session.get("status") not in ("waiting", "done"), session
    again = Script({"things/curator": list(ADMITS)})
    with opened(again) as workspace:
        run(workspace, choose=None)
        waiting = [p.target for p in proposals(workspace.store, "waiting")]
    assert "things/curator" in again.ran and waiting, (again.ran, waiting)


def test_a_run_that_finished_as_the_stop_came_lands_its_text_so_it_is_not_paid_for_again(tmp_path: Path) -> None:
    """Grounding a finished run asks the judge outside any run, which the stop does not refuse: the paid text lands."""
    sys.path.insert(0, str(Path(__file__).parent))
    from test_cli import _cites_each_fact

    from cerebro.fakes import FakeDecider, ScriptedRun
    from cerebro.run import AgentRun

    class FinishesAsStopComes(AgentRun):
        store: Any = None

        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            done = ScriptedRun(_cites_each_fact).run(agent, evidence, tools, budget)
            stop(self.store)
            return done

    root = tmp_path / "ws"
    lay_out_workspace(root)
    script, decider = FinishesAsStopComes(), FakeDecider()
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), agent_run=script, decider=decider) as workspace:
        script.store = workspace.store
        workspace.store.create(Identity("digests/weekly", "digests", "Weekly"))
        workspace.tell(YOU, "The review moved to Friday.", "digests/weekly", "fact")
        workspace.compile(workspace.agent("digests/weekly"), this="digests/weekly", scheduled=True)
        article = workspace.store.article("digests/weekly")
    assert decider.asked and article is not None and "Friday" in " ".join(part.text for part in article.compilations)


def test_a_tools_call_the_stop_refused_ends_its_run_as_stopped_with_its_request_counted(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pydantic_ai.usage import RequestUsage

    from cerebro import budget
    from cerebro.agent import Agent, Budget
    from cerebro.compile import run_agent
    from cerebro.models import ModelSpec

    store = MemoryStore()
    ledger = Ledger(store, Caps())
    model = ModelSpec("fireworks", "glm-5p3")

    def search(words: str) -> str:
        """Search the wiki."""
        budget._stop_of(store).set = True  # the stop's flag is set, and its cancel of this run not yet
        ledger.check("rerank", datetime.now(UTC), model)
        return "nothing"

    async def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("search", {"words": "review"})],
                             usage=RequestUsage(input_tokens=5000, output_tokens=40))

    agent = Agent(id="digests/weekly", path="agents/digests/weekly.md", role="writer", target="digests/weekly",
                  prompt="Summarize the notes.", budget=Budget())
    with pytest.raises(Stopped):
        run_agent(agent, [], [search], agent_run=PydanticRun(FunctionModel(reply)), store=store, ledger=ledger,
                  model=model)
    rows = [row.data for row in store.rows("agent-runs")]
    assert [(row["status"], row["requests"]) for row in rows] == [(STOPPED, 1)]
    assert [row.data["usd"] > 0 for row in store.rows("spend")] == [True]


def test_a_stopped_writer_keeps_its_periods_run_due_and_records_the_article_it_finished(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    sys.path.insert(0, str(Path(__file__).parent))
    from test_cli import _cites_each_fact

    from cerebro.fakes import ScriptedRun
    from cerebro.run import AgentRun

    now = datetime(2026, 11, 18, 9, tzinfo=UTC)

    class StopsOnSecond(AgentRun):
        store: Any = None

        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            done = ScriptedRun(_cites_each_fact, clock=lambda: now).run(agent, evidence, tools, budget)
            if agent.target.endswith("/b"):
                stop(self.store)
                done.status, done.result, done.note = STOPPED, None, "serve stopped before the run ended"
            return done

    root = tmp_path / "ws"
    (root / "agents" / "things").mkdir(parents=True)
    (root / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts }\nheartbeat: [daily]\n---\n\nWrite the thing.\n")
    (root / "settings.yaml").write_text("timezone: UTC\ncompile:\n  concurrency: 1\n")
    script = StopsOnSecond()
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), agent_run=script, clock=lambda: now) as workspace:
        script.store = workspace.store
        for name in "ab":
            workspace.store.create(Identity(f"things/{name}", "things", name.upper()))
            workspace.tell(YOU, f"Thing {name} moved to Friday.", f"things/{name}", "fact")
        agent = workspace.agent("things/article")
        result = runner.PassResult()
        result.due[agent.id] = "daily"
        runner._compile_due(workspace, [(agent, "daily")], result)
        beat = workspace.store.state(f"heartbeat:{agent.id}") or {}
    assert not beat.get("scheduled") and not beat.get("retried"), beat  # the day's run is still due
    assert "things/a" in (beat.get("articles") or {}) and "things/b" not in (beat.get("articles") or {}), beat
