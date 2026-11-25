"""An agent whose evidence cannot be gathered: one failed row while it fails the same way, a wait as after a failed
run, and no other agent made due by that row."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cerebro import Cerebro, scaffold
from cerebro.apps import App, evidence
from cerebro.fakes import FakeConnector, FakeExtraction, FakeSecrets, ScriptedRun
from cerebro.run import AgentRun, Run, Usage
from cerebro.runner import run_forever
from cerebro.secrets import MARK
from cerebro.source import SourceError

START = datetime(2026, 11, 19, 9, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def after(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Line:
    """An evidence line that fails while `down` is set, and counts how often it is gathered."""

    def __init__(self) -> None:
        self.down: str | None = "the line is down"
        self.gathered = 0

    def read(self) -> list[dict[str, str]]:
        self.gathered += 1
        if self.down:
            raise RuntimeError(self.down)
        return [{"id": "up1", "text": "The line is up.", "source": "line"}]


def lay_out_workspace(tmp_path: Path, reader: bool = False) -> Path:
    root = scaffold.init(tmp_path / "things", environ={}).root
    (root / "agents" / "reports").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "reports" / "broken.md").write_text(
        "---\nevidence:\n  - flaky: {}\nheartbeat: [change]\n---\n\nSum it up.\n\n# Output\n\n## Summary\n")
    if reader:
        (root / "agents" / "reports" / "watch.md").write_text(
            "---\nevidence:\n  - query: { from: agent-runs }\nheartbeat: [change]\n---\n\n"
            "Say how the agents ran.\n\n# Output\n\n## Summary\n")
    return root


def opened(root: Path, line: Line, clock: Clock, calls: list[str]) -> Cerebro:
    @evidence("flaky")
    def flaky() -> list[dict[str, str]]:
        """A line that fails while it is down."""
        return line.read()

    def write(prompt: str) -> str:
        calls.append("watch" if "Say how the agents ran" in prompt else "broken")
        return "## Summary\n\nThe line is up. [^up1]"

    return Cerebro(root, extraction=FakeExtraction(), agent_run=ScriptedRun(write, clock=clock), trusted=True,
                   clock=clock, apps=[App(name="lines", kinds=[], functions=[flaky])])


def failed_rows(workspace: Cerebro) -> list[dict]:
    return [r.data for r in workspace.store.rows("agent-runs", {"agent": "reports/broken"}) if r.data["status"] == "failed"]


def test_a_gather_that_fails_the_same_way_keeps_one_row_and_waits_as_a_failed_run_waits(tmp_path: Path) -> None:
    line, clock, calls = Line(), Clock(), []
    with opened(lay_out_workspace(tmp_path), line, clock, calls) as workspace:
        first = workspace.run_once()
        assert "the line is down" in first.failed["reports/broken"] and line.gathered == 1
        clock.after(29)
        waiting = workspace.run_once()
        assert "reports/broken" not in waiting.failed and line.gathered == 1  # inside the 30 seconds: not gathered
        assert "waits until 2026-11-19T09:00:30Z" in waiting.skipped["reports/broken"]
        clock.after(1)
        assert "reports/broken" in workspace.run_once().failed and line.gathered == 2  # gathered again, the same way
        clock.after(59)
        workspace.run_once()
        assert line.gathered == 2  # the wait doubled to 60 seconds
        clock.after(1)
        workspace.run_once()
        assert line.gathered == 3
        for _ in range(240):  # two hours of serve's 30 second passes: at most one gather an hour once it has doubled
            clock.after(30)
            workspace.run_once()
        assert line.gathered <= 3 + 8, line.gathered
        assert len(failed_rows(workspace)) == 1 and calls == []


def test_a_change_to_workspace_md_or_the_agent_file_ends_the_wait(tmp_path: Path) -> None:
    line, clock, calls = Line(), Clock(), []
    root = lay_out_workspace(tmp_path)
    with opened(root, line, clock, calls) as workspace:
        workspace.run_once()
        clock.after(5)
        with (root / "workspace.md").open("a", encoding="utf-8") as file:
            file.write("\nThe fiscal year starts July 1.\n")
        workspace.run_once()
        assert line.gathered == 2 and len(failed_rows(workspace)) == 1  # gathered at once, still the same failure
        clock.after(5)
        agent = root / "agents" / "reports" / "broken.md"
        agent.write_text(agent.read_text().replace("Sum it up.", "Sum it all up."))
        line.down = None
        clock.after(1)
        done = workspace.run_once()
        assert line.gathered == 3 and "reports/broken" not in done.failed and calls == ["broken"]


def test_a_gather_that_fails_then_succeeds_then_fails_again_is_two_failures(tmp_path: Path) -> None:
    line, clock, calls = Line(), Clock(), []
    root = lay_out_workspace(tmp_path)
    with opened(root, line, clock, calls) as workspace:
        workspace.run_once()
        line.down = None
        clock.after(30)
        workspace.run_once()
        assert calls == ["broken"]
        line.down = "the line is down"
        clock.after(30)
        assert "reports/broken" in workspace.run_once().failed
        assert [r["note"].rsplit(": ", 1)[-1] for r in failed_rows(workspace)] == ["the line is down"] * 2


def test_a_new_reason_is_the_same_failure_and_the_wait_doubles(tmp_path: Path) -> None:
    line, clock, calls = Line(), Clock(), []
    with opened(lay_out_workspace(tmp_path), line, clock, calls) as workspace:
        workspace.run_once()
        for n in range(3):  # a reason that says a time or a request id differs on each try
            line.down = f"the line is down, request {n}"
            clock.after(30 * 2 ** n)
            workspace.run_once()
        assert [r["note"].rsplit(": ", 1)[-1] for r in failed_rows(workspace)] == ["the line is down"]
        assert line.gathered == 4 and workspace.store.state("failing:reports/broken:reports/broken")["times"] == 4


def test_a_run_by_hand_gathers_through_the_wait_and_keeps_no_second_row_for_the_same_failure(tmp_path: Path) -> None:
    line, clock, calls = Line(), Clock(), []
    with opened(lay_out_workspace(tmp_path), line, clock, calls) as workspace:
        workspace.run_once()
        clock.after(1)
        agent = next(a for a in workspace.agents if a.id == "reports/broken")
        with pytest.raises(Exception, match="the line is down"):
            workspace.compile(agent, scheduled=True)
        assert line.gathered == 2 and len(failed_rows(workspace)) == 1


def test_an_agent_on_a_schedule_keeps_one_row_for_one_failure(tmp_path: Path) -> None:
    line, clock, calls = Line(), Clock(), []
    root = lay_out_workspace(tmp_path)
    agent = root / "agents" / "reports" / "broken.md"
    agent.write_text(agent.read_text().replace("heartbeat: [change]", "heartbeat: [hourly, change]"))
    passes = [0]
    with opened(root, line, clock, calls) as workspace:
        run_forever(workspace, interval=30, stop=lambda: passes[0] >= 2 * 120, sleep=clock.after,
                    on_pass=lambda result: passes.__setitem__(0, passes[0] + 1))
        assert line.gathered > 6 and len(failed_rows(workspace)) == 1


def test_a_failed_gather_never_makes_a_reader_of_agent_runs_due(tmp_path: Path) -> None:
    line, clock, calls = Line(), Clock(), []
    workspace = opened(lay_out_workspace(tmp_path, reader=True), line, clock, calls)
    passes = [0]
    with workspace:
        run_forever(workspace, interval=30, stop=lambda: passes[0] >= 120, sleep=clock.after,
                    on_pass=lambda result: passes.__setitem__(0, passes[0] + 1))
        assert calls.count("watch") == 1 and len(failed_rows(workspace)) == 1  # its first run, and never again
        line.down = "the line is down again, another way"
        clock.after(3600)
        workspace.run_once()
        assert len(failed_rows(workspace)) == 1 and calls.count("watch") == 1  # the same failure, whatever it says


SECRET = "zq-dummy-7f3a9c2e5b1d4f60a8c3e9d2b7a1"


# Where the secret starts in the note: whole before its 300-character cut, just up to it, across it with 1, 6, half, all
# but 6 and all but 1 of its characters past it, at the cut and past it. Each place between answers as its neighbours.
# The mask that stands for it is shorter, so the places where the mask meets the cut are its own edges.
ACROSS = sorted({250, *(300 - len(SECRET) + k for k in (-1, 0, 1, 6, len(SECRET) // 2, len(SECRET) - 6,
                                                         len(SECRET) - 1, len(SECRET), len(SECRET) + 1)),
                 *(300 - len(MARK) + k for k in (0, 1, len(MARK) - 1))})


@pytest.mark.parametrize("offset", ACROSS)
def test_a_secret_across_the_cut_of_a_failed_gathers_note_is_masked_whole(tmp_path: Path, offset: int) -> None:
    root = scaffold.init(tmp_path / "things", environ={}).root
    (root / "agents" / "reports").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "reports" / "down.md").write_text(
        "---\nevidence:\n  - sql: { source: warehouse, query: \"select 1\" }\nheartbeat: [change]\n---\n\nSum.\n\n"
        "# Output\n\n## Summary\n")
    head = "gather: agents/reports/down.md: evidence 1, sql warehouse: "

    class Down(FakeConnector):
        def select(self, query: str) -> list:
            raise SourceError("x" * (offset - len(head)) + SECRET + " and more after it")

    with Cerebro(root, extraction=FakeExtraction(), agent_run=ScriptedRun(lambda p: ""), trusted=True,
                 secrets=FakeSecrets({"WAREHOUSE_PASSWORD": SECRET}), connectors=[Down("warehouse", [])]) as workspace:
        workspace.run_once()
        notes = [r.data.get("note", "") for r in workspace.store.rows("agent-runs")]
    note = notes[-1]
    assert note.startswith(head) and len(note) <= 300
    assert not any(SECRET[:k] in note for k in range(4, len(SECRET) + 1)), note[-60:]
    assert not note.endswith(tuple(MARK[:k] for k in range(1, len(MARK)))) or note.endswith(MARK)


def test_a_byte_that_is_not_utf8_in_workspace_md_is_read_by_a_run_that_lands_once(tmp_path: Path) -> None:
    line, clock, calls = Line(), Clock(), []
    root = lay_out_workspace(tmp_path)
    line.down = None
    (root / "workspace.md").write_bytes(b"---\nname: things\n---\n\nQuillmoor\xe9 sells things.\n")
    with opened(root, line, clock, calls) as workspace:
        for _ in range(4):
            assert workspace.run_once().failed == {}
            clock.after(30)
    assert calls == ["broken"]


class Refused(AgentRun):
    """A paid run that fails the same way every time."""

    def __init__(self) -> None:
        self.paid = 0

    def run(self, agent, evidence, tools, budget) -> Run:
        self.paid += 1
        return Run(f"run_{self.paid}", agent.id, agent.target, "failed", note="the provider refused it",
                   usage=Usage(model="m", requests=1))


def paid_over_hours(tmp_path: Path, every: int) -> int:
    root = lay_out_workspace(tmp_path)
    reads, clock, run = [0], Clock(), Refused()

    @evidence("flaky")
    def flaky() -> list[dict[str, str]]:
        """A line that fails one read in `every`."""
        reads[0] += 1
        if every and reads[0] % every == 0:
            raise ConnectionError("the line timed out")
        return [{"id": "up1", "text": "The line says the same thing.", "source": "line"}]

    passes = [0]
    with Cerebro(root, extraction=FakeExtraction(), agent_run=run, trusted=True, clock=clock,
                 apps=[App(name="lines", kinds=[], functions=[flaky])]) as workspace:
        run_forever(workspace, interval=30, stop=lambda: passes[0] >= 120, sleep=clock.after,
                    on_pass=lambda result: passes.__setitem__(0, passes[0] + 1))
    return run.paid


def test_a_failed_gather_adds_to_a_failing_runs_wait_and_a_good_gather_does_not_end_it(tmp_path: Path) -> None:
    steady = paid_over_hours(tmp_path / "steady", 0)
    flapping = paid_over_hours(tmp_path / "flapping", 20)
    assert flapping <= steady, (steady, flapping)  # the hold counts both, and only a run that works ends it


def test_a_pass_prints_a_failed_gathers_reason_masked_as_its_row_is(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path)

    @evidence("flaky")
    def flaky() -> list[dict[str, str]]:
        """Calls an API with its key in the address, and the API is down."""
        raise RuntimeError(f"503 for url 'https://api.example.invalid/v1/items?key={SECRET}'")

    said: list[str] = []
    with Cerebro(root, extraction=FakeExtraction(), agent_run=ScriptedRun(lambda p: ""), trusted=True,
                 secrets=FakeSecrets({"API_KEY": SECRET}), apps=[App(name="lines", kinds=[], functions=[flaky])]) as workspace:
        result = workspace.run_once(say=said.append)
        shown = [*said, *result.failed.values()]
        assert any(f"key={MARK}" in line for line in shown) and not any(SECRET in line for line in shown)
