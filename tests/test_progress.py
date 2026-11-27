"""A pass says a line as each stage starts and as each agent's run ends, as it happens, under serve --once and under
serve, before the lines it prints at its end."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from test_by_hand import OTTER_NOTE, _fill
from test_cli import EachHeading, run, stand_ins  # noqa: F401 - the fixture is used by name

from cerebro import Cerebro
from cerebro.fakes import FakeDecider, FakeExtraction
from cerebro.models import TokenCount
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
    assert re.fullmatch(r"ran       projects/otter: wrote articles/projects/otter\.md, \$0\.\d{4}", lines[ran])
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


class _Spending(EachHeading):
    """The scripted model, its run reading and writing tokens as a real one's does."""

    def run(self, *args: Any) -> Any:
        done = super().run(*args)
        done.usage = replace(done.usage, input_tokens=100_000, output_tokens=1_000)
        return done


class _PaidJudge(FakeDecider):
    """The stand-in judge, each call of which spends as a real one's does."""

    def decide(self, state: Any, questions: Any) -> Any:
        return replace(super().decide(state, questions), usage=TokenCount(input_tokens=10_000))


COMPOSER = ("---\nmodel: { provider: fireworks, model: kimi-k3 }\nsubagents: [risks]\n---\n\n"
            "A brief on the Otter launch.\n\n# Composing\n\nJoin the parts as they stand.\n")
RISKS = ("---\nevidence:\n  - facts: { window: last_90_days }\nheartbeat: [change]\n---\n\n"
         "Name the risks to the Otter launch.\n\n# Output\n\n## Risks\n")


def test_a_runs_line_splits_its_cost_into_writing_and_checks_and_its_row_keeps_its_tokens(
        tmp_path: Path, stand_ins: None) -> None:  # noqa: F811
    """A composer on a model of its own, over a subagent that names none: the checks are what the judge spent."""
    root = _workspace(tmp_path)
    (root / "agents" / "reports" / "subagents").mkdir(parents=True)
    (root / "agents" / "reports" / "weekly.md").write_text(COMPOSER)
    (root / "agents" / "reports" / "subagents" / "risks.md").write_text(RISKS)
    said: list[str] = []
    with Cerebro(root, agent_run=_Spending(), extraction=FakeExtraction(), decider=_PaidJudge(),
                 clock=lambda: datetime(2026, 11, 18, 9, tzinfo=UTC)) as workspace:
        workspace.run_once(say=said.append)
        rows = {row.data["agent"]: row.data for row in workspace.store.rows("agent-runs")}  # each agent's latest
        ids = {rows[agent]["run"] for agent in ("reports/weekly", "reports/subagents/risks")}
        judged = workspace.models["decision"].model
        checks = sum(row.data["usd"] for row in workspace.store.rows("spend")
                     if row.data["run"] in ids and row.data["model"] == judged)
    line = next(line for line in said if line.startswith("ran       reports/weekly: "))
    found = re.fullmatch(r"ran       reports/weekly: wrote articles/reports/weekly\.md, "
                         r"\$([\d.]+) \(writing \$([\d.]+), checks \$([\d.]+)\)", line)
    assert found and checks > 0, line
    total, writing, checked = (float(n) for n in found.groups())
    digits = len(found.group(3).partition(".")[2])
    assert checked == round(checks, digits) and round(writing + checked, digits) == total, line
    assert (rows["reports/subagents/risks"]["input_tokens"], rows["reports/subagents/risks"]["output_tokens"]) == (
        100_000, 1_000)
