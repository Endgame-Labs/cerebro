"""A pass says a line as each stage starts and as each agent's run ends, as it happens, under serve --once and under
serve, before the lines it prints at its end."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from test_by_hand import OTTER_NOTE, _fill
from test_cli import EachHeading, run, stand_ins  # noqa: F401 - the fixture is used by name

from cerebro import Cerebro
from cerebro.fakes import FakeExtraction
from cerebro.runner import run_forever


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "otter-workspace"
    assert run("init", str(workspace), "--daily-usd", "5").exit_code == 0
    (workspace / "local" / "otter.md").write_text(OTTER_NOTE)
    assert run("agents", "new", "projects/otter", "--workspace", str(workspace)).exit_code == 0
    _fill(workspace, "projects/otter")
    path = workspace / "agents" / "projects" / "otter.md"
    path.write_text(path.read_text().replace("heartbeat: [manual]", "heartbeat: [change]"))
    return workspace


def test_serve_once_says_each_stage_and_each_agents_run_with_its_cost_before_its_summary(
        tmp_path: Path, stand_ins: None) -> None:  # noqa: F811
    lines = run("serve", "--once", "--workspace", str(_workspace(tmp_path))).output.splitlines()
    stages = [i for i, line in enumerate(lines) if line.startswith("stage     ")]
    assert [lines[i].split(":")[0] for i in stages] == ["stage     list", "stage     extract", "stage     compile"]
    ran = next(i for i, line in enumerate(lines) if line.startswith("ran       projects/otter: wrote "))
    assert lines[ran].startswith("ran       projects/otter: wrote articles/projects/otter.md, $0.")
    summary = lines.index("wrote     projects/otter  articles/projects/otter.md")
    assert stages[0] < stages[1] < stages[2] < ran < summary  # said as it happened, then the summary


def test_serve_says_the_same_lines_on_each_pass(tmp_path: Path, stand_ins: None) -> None:  # noqa: F811
    root = _workspace(tmp_path)
    said: list[str] = []
    passes = []
    with Cerebro(root, agent_run=EachHeading(), extraction=FakeExtraction(),
                 clock=lambda: datetime(2026, 11, 18, 9, tzinfo=UTC)) as workspace:
        run_forever(workspace, interval=0, stop=lambda: len(passes) >= 1, sleep=lambda _: None,
                    on_pass=passes.append, say=said.append)
    assert said[0] == "stage     list: checking the sources"
    assert any(line.startswith("ran       projects/otter: wrote articles/projects/otter.md, $") for line in said)
