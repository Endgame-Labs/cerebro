"""The `refused` lines that `status`, `agents list`, `distill` and `serve` print for a file they skip quote the file,
so each is masked with every value the workspace's secrets store holds, through the one mask a refusal uses: no
line any of the four prints shows a stored value, whole or six characters of it."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro import Cerebro
from cerebro.cli import main

VALUES = ["Zq7mPlainVal93", "fake9 pass8 phrase7", "abc<def>ghi9", "PrefixKey77", "PrefixKey77Longer",
          "two  spaces9x", "tab\tvalue77", 'quo"te\\val1x', "k9Xw2m", "pässwörd-ünïq"]  # made up, each stored

AGENT = "---\n{front}\n---\n\n# Instructions\n\nWrite.\n"


def quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t") + '"'


def windows(value: str) -> set[str]:
    out = set()
    for form in (value, " ".join(value.split()), value.replace("\t", "\\t")):
        out |= {form[n:n + 6] for n in range(len(form) - 5)}
    return out


def workspace(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace whose every agent and source file is refused, each quoting one stored value."""
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    runner = CliRunner()
    assert runner.invoke(main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    assert runner.invoke(main, ["trust", str(root)]).exit_code == 0
    store = Cerebro(root, trusted=True).secrets
    assert store is not None
    for n, value in enumerate(VALUES):
        store.set(f"made-up-{n}", value)
    agents, sources = root / "agents" / "things", root / "sources"
    agents.mkdir(parents=True)
    (agents / "one.md").write_text(AGENT.format(front=f"heartbeat: [{quoted(VALUES[0])}]"))
    (root / "agents" / VALUES[1]).mkdir()
    (root / "agents" / VALUES[1] / "curator.md").write_text(AGENT.format(front="heartbeat: [change]"))
    (agents / "two.md").write_text(AGENT.format(front=f"{quoted(VALUES[2])}: 1"))
    (sources / "three.yaml").write_text(f"kind: files\nselect:\n  - glob: \"local/**/*.md\"\n    extract: "
                                        f"{quoted(VALUES[3])}\n")
    (agents / "four.md").write_text(AGENT.format(front=f"{quoted(VALUES[4])}: 1"))
    (sources / "five.yaml").write_text(f"kind: files\n{quoted(VALUES[5])}: 1\n")
    (sources / "six.yaml").write_text(f"kind: files\n{quoted(VALUES[6])}: 1\n")
    (agents / "seven.md").write_text(AGENT.format(front=f"heartbeat: [{quoted(VALUES[7])}]"))
    (sources / "eight.yaml").write_text(f"kind: {quoted(VALUES[8])}\n")
    (sources / "nine.yaml").write_text(f"kind: files\n{quoted(VALUES[9])}: 1\n{quoted(VALUES[9])}: 2\n")


@pytest.mark.parametrize("command", [["status"], ["status", "--json"], ["agents", "list"], ["agents", "list", "--json"],
                                     ["distill"], ["serve", "--once"]])
def test_no_line_status_agents_distill_or_serve_prints_shows_a_stored_value(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]) -> None:
    root = tmp_path / "ws"
    workspace(root, monkeypatch)
    done = CliRunner().invoke(main, [*command, "--workspace", str(root)])
    assert "refused" in done.output and "Traceback" not in done.output, done.output
    shown = [value for value in VALUES if any(window in done.output for window in windows(value))]
    assert not shown, done.output
