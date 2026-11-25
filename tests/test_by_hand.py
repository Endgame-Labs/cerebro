"""A run by hand: `agents run` and `compile` on an agent a person just wrote, over facts the wiki took in before it
existed, as the README's steps and the `agents new` skeleton lead a person to do."""

from __future__ import annotations

import re
from pathlib import Path

from click.testing import Result
from test_cli import run, stand_ins  # noqa: F401 - the fixture is used by name

OTTER_NOTE = "Project: Otter\nDate: 2026-11-10\n\nThe Otter launch moved to March 3.\n\nVega owns the Otter launch.\n"


def _fill(workspace: Path, id: str) -> None:
    """Fill the skeleton `agents new` wrote with the README's words, its heartbeat left as the skeleton has it."""
    path = workspace / "agents" / f"{id}.md"
    text = path.read_text()
    assert "facts: { window: last_90_days }" in text and "heartbeat: [manual]" in text
    path.write_text(text.replace("Say what this agent writes", "Write where the Otter launch stands. Say what"))


def _new_workspace_with_facts_first(tmp_path: Path) -> Path:
    workspace = tmp_path / "otter-workspace"
    assert run("init", str(workspace), "--daily-usd", "5").exit_code == 0
    (workspace / "local" / "otter.md").write_text(OTTER_NOTE)
    taken = run("serve", "--once", "--workspace", str(workspace))  # the facts come in before the agent exists
    assert "2 facts extracted" in taken.output, taken.output
    made = run("agents", "new", "projects/otter", "--workspace", str(workspace))
    assert made.output.splitlines()[-1] == "next     fill in agents/projects/otter.md, then cerebro agents run projects/otter --show-prompt"
    _fill(workspace, "projects/otter")
    return workspace


def _wrote(result: Result) -> bool:
    return re.search(r"^wrote +projects/otter  articles/projects/otter\.md$", result.output, re.MULTILINE) is not None


def test_agents_run_on_a_new_agent_links_the_facts_taken_in_before_it_and_writes(tmp_path: Path,
                                                                                 stand_ins: None) -> None:  # noqa: F811
    workspace = _new_workspace_with_facts_first(tmp_path)
    ran = run("agents", "run", "projects/otter", "--workspace", str(workspace))
    assert ran.exit_code == 0 and _wrote(ran), ran.output
    assert "Vega owns the Otter launch" in (workspace / "articles" / "projects" / "otter.md").read_text()


def test_compile_on_a_new_agent_links_the_facts_taken_in_before_it_and_writes(tmp_path: Path,
                                                                              stand_ins: None) -> None:  # noqa: F811
    workspace = _new_workspace_with_facts_first(tmp_path)
    ran = run("compile", "--article", "projects/otter", "--workspace", str(workspace))
    assert ran.exit_code == 0 and _wrote(ran), ran.output


def test_a_by_hand_run_that_has_nothing_to_cite_says_so_and_never_says_since_its_last_write(
        tmp_path: Path, stand_ins: None) -> None:  # noqa: F811
    workspace = tmp_path / "empty-workspace"
    assert run("init", str(workspace), "--daily-usd", "5").exit_code == 0
    assert run("agents", "new", "projects/otter", "--workspace", str(workspace)).exit_code == 0
    _fill(workspace, "projects/otter")
    ran = run("agents", "run", "projects/otter", "--workspace", str(workspace))  # it runs
    assert ran.exit_code == 0, ran.output  # and wrote nothing, which is no failure
    assert "since its last write" not in ran.output
    assert "wrote nothing" in ran.output, ran.output


def test_a_by_hand_run_of_a_written_article_reruns_it_and_says_how_a_pass_would_treat_it(
        tmp_path: Path, stand_ins: None) -> None:  # noqa: F811
    workspace = _new_workspace_with_facts_first(tmp_path)
    assert _wrote(run("agents", "run", "projects/otter", "--workspace", str(workspace)))
    again = run("agents", "run", "projects/otter", "--workspace", str(workspace))  # nothing moved: a run by hand runs anyway
    assert again.exit_code == 0 and _wrote(again), again.output
    compiled = run("compile", "--article", "projects/otter", "--workspace", str(workspace))
    assert compiled.exit_code == 0 and _wrote(compiled), compiled.output


def test_a_pass_that_skips_a_written_article_names_the_command_that_rebuilds_it(tmp_path: Path,
                                                                                stand_ins: None) -> None:  # noqa: F811
    workspace = _new_workspace_with_facts_first(tmp_path)
    path = workspace / "agents" / "projects" / "otter.md"
    path.write_text(path.read_text().replace("heartbeat: [manual]", "heartbeat: { on: [change], cooldown: 0 }"))
    assert _wrote(run("serve", "--once", "--workspace", str(workspace)))
    again = run("serve", "--once", "--workspace", str(workspace))
    assert ("skipped   projects/otter: nothing it reads has moved since its last write: cerebro agents run "
            "projects/otter runs it again now") in again.output, again.output
