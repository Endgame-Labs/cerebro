"""A first run whose curators failed: with
nothing proposed, nothing decided in review and a curator that failed, the run fails, exit 1, keeps no session, writes
no article, and the next run asks the curators again; its last line names what to run, the failure's own command, or
the run again when the failure names none. With rows on the list, each failure is a line to check and the list goes
on. Neutral collections only: `things` and `parts`. Fakes only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_first_run import Script, every_curator_weighs  # noqa: F401  (a fixture)
from test_first_run_structure import by_record, lay_out_workspace, open_it

from cerebro.app import MissingModelKey
from cerebro.first_run import KEY, Choices, in_first_run, run

NO_KEY = MissingModelKey("agents/things/curator.md runs on fireworks rune, and no key for it is kept or set: run "
                         "cerebro secrets set fireworks, or export FIREWORKS_API_KEY")
INDEX = ("---\nevidence:\n  - articles: [things, parts]\n    text_only: true\nrendered:\n  Contents: { kind: collections, "
         "style: outline }\nheartbeat: [change]\n---\n\nThe index of this wiki.\n\n# Output\n\n## Contents\n")


# the parts curator is asked before a parent of its parts is proposed: these tests are about what a failed first run
# leaves, not about who has something to weigh
pytestmark = pytest.mark.usefixtures("every_curator_weighs")


class Failing(Script):
    """A curator of `failing` raises `why` when it is run; every other agent runs as `Script` runs it."""

    def __init__(self, failing: set[str], why: Exception, calls: dict[str, Any] | None = None) -> None:
        super().__init__(calls)
        self.failing, self.why = failing, why

    def run(self, agent, evidence, tools, budget):  # type: ignore[no-untyped-def]
        if agent.id in self.failing:
            self.ran.append(agent.id)
            raise self.why
        return super().run(agent, evidence, tools, budget)


def _indexed(root: Path) -> Path:
    (root / "agents" / "digests").mkdir(parents=True)
    (root / "agents" / "digests" / "index.md").write_text(INDEX)
    return root


def _articles(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in (root / "articles").rglob("*.md")) if (root / "articles").exists() \
        else []


def test_a_first_run_where_every_curator_failed_fails_keeps_no_session_and_asks_again(tmp_path: Path) -> None:
    root = _indexed(lay_out_workspace(tmp_path))
    script = Failing({"things/curator", "parts/curator"}, NO_KEY)
    said: list[str] = []
    with open_it(root, script) as workspace:
        out = run(workspace, choose=Choices.starter, say=said.append)
        assert out is not None and out.failed and out.result is None
        assert out.run == "cerebro secrets set fireworks"
        assert workspace.store.state(KEY) is None  # no session, decided or not
        assert in_first_run(workspace)
        assert workspace.store.query("digests") == [] and _articles(root) == []  # nothing built, no article written
        assert not any(line.startswith(("Nothing to propose", "Nothing approved")) for line in out.lines)
        script.failing = set()  # the key is set: the next run asks the curators again, and they propose
        script.calls = {"things/curator": [by_record("T1")], "parts/curator": []}
        again = run(workspace, choose=Choices.starter, say=said.append)
        assert again is not None and not again.failed and again.result is not None
        assert workspace.store.state(KEY)["status"] == "done"
    assert sorted(script.ran[:2]) == ["parts/curator", "things/curator"]  # the failed run asked each once
    assert {"things/curator", "parts/curator"} <= set(script.ran[2:])  # and the next run asked them again


def test_a_curator_failure_that_names_no_command_runs_the_first_run_again(tmp_path: Path) -> None:
    script = Failing({"things/curator", "parts/curator"}, RuntimeError("the provider is down"))
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        out = run(workspace, choose=Choices.starter, say=lambda line: None)
        assert out is not None and out.failed and out.run == ""  # the command that ran it names the run again
        assert workspace.store.state(KEY) is None


def test_some_curators_failed_and_some_proposed_the_list_goes_on_and_each_failure_is_a_line(tmp_path: Path) -> None:
    script = Failing({"parts/curator"}, RuntimeError("the provider is down"), {"things/curator": [by_record("T1")]})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        out = run(workspace, choose=Choices.starter, say=lambda line: None)
        assert out is not None and not out.failed and out.result is not None
        assert workspace.store.state(KEY)["status"] == "done" and workspace.store.article("things/alder-works") is not None
        assert "Discovery for parts collection did not finish: RuntimeError: the provider is down" in out.lines


def test_some_failed_and_the_rest_proposed_nothing_still_fails(tmp_path: Path) -> None:
    script = Failing({"parts/curator"}, RuntimeError("the provider is down"), {"things/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        out = run(workspace, choose=Choices.starter, say=lambda line: None)
        assert out is not None and out.failed and out.result is None and workspace.store.state(KEY) is None


def test_serve_once_exits_1_and_ends_with_the_failures_command(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    from cerebro import cli
    from cerebro.cli import main

    root = _indexed(lay_out_workspace(tmp_path))
    script = Failing({"things/curator", "parts/curator"}, NO_KEY)
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    done = CliRunner().invoke(main, ["serve", "--once", "--yes", "--workspace", str(root)])
    assert done.exit_code == 1, done.output
    lines = done.output.splitlines()
    assert lines[-1].endswith("Failed. Run: cerebro secrets set fireworks"), lines[-3:]
    assert not any(line.startswith(("Nothing to propose", "Done:")) for line in lines)
    assert _articles(root) == []


def test_serve_once_with_a_failure_that_names_no_command_ends_naming_serve_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    from cerebro import cli
    from cerebro.cli import main

    root = lay_out_workspace(tmp_path)
    script = Failing({"things/curator", "parts/curator"}, RuntimeError("the provider is down"))
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    done = CliRunner().invoke(main, ["serve", "--once", "--yes", "--workspace", str(root)])
    assert done.exit_code == 1, done.output
    assert done.output.splitlines()[-1].endswith("Failed. Run: cerebro serve --once")
