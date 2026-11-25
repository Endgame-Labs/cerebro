"""A file in a workspace's prompts/ folder that no bundled prompt is named after is read by nothing, and `prompts
list` and `status` say so, with the names a file there can take."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro import prompts, scaffold
from cerebro.cli import main

LINE = (f"prompts/extraction.md is named after no prompt Cerebro bundles, so nothing reads it: a file there takes the "
        f"place of the bundled prompt of its name, one of {', '.join(prompts.names())}: eject one with cerebro prompts "
        "eject <name> and bring your words into it, or move the file aside")


def lay_out_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "w"
    scaffold.init(root, environ={})
    (root / "prompts").mkdir()
    (root / "prompts" / "extraction.md").write_text("Pick out each fact the text states.\n", encoding="utf-8")
    (root / "prompts" / "rules.md").write_text(prompts.load("rules"), encoding="utf-8")
    return root


def test_prompts_list_names_a_file_no_bundled_prompt_is_named_after(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path)
    shown = CliRunner().invoke(main, ["prompts", "list", "--workspace", str(root)])
    assert shown.exit_code == 0, shown.output
    lines = shown.output.splitlines()
    assert "rules  ejected, read from prompts/rules.md" in lines
    assert lines[-2:] == ["extraction  prompts/extraction.md, read by nothing: no bundled prompt has this name",
                          f"{len(prompts.names())} prompts, 1 ejected, 1 file read by nothing."]
    rows = json.loads(CliRunner().invoke(main, ["prompts", "list", "--workspace", str(root), "--json"]).output)
    assert rows[-1] == {"name": "extraction", "ejected": False, "bundled": False, "path": "prompts/extraction.md"}
    assert {"name": "rules", "ejected": True, "bundled": True, "path": "prompts/rules.md"} in rows
    assert all(set(row) == {"name", "ejected", "bundled", "path"} for row in rows)


def written_as(root: Path, spelling: str) -> bool:
    """Write the rules prompt under `spelling` alone, and give whether the name rules.md opens it, as it does on a
    disk that ignores case."""
    folder = root / "prompts"
    (folder / "rules.md").unlink()
    (folder / spelling).write_text("Mine.\n", encoding="utf-8")
    return (folder / "rules.md").is_file()


@pytest.mark.parametrize("spelling", ["rules.md", "Rules.md", "RULES.MD"])
def test_a_prompt_file_that_rules_md_opens_is_read_however_it_is_spelt(tmp_path: Path, spelling: str) -> None:
    root = lay_out_workspace(tmp_path)
    if not written_as(root, spelling):  # a disk that tells the spellings apart: one file under both names stands in
        (root / "prompts" / "rules.md").hardlink_to(root / "prompts" / spelling)
    assert prompts.load("rules", root) == "Mine."
    assert prompts.unread(root) == ["extraction.md"]
    assert prompts.outdated(root) == [LINE]
    shown = CliRunner().invoke(main, ["prompts", "list", "--workspace", str(root)])
    assert "rules  ejected, read from prompts/rules.md" in shown.output.splitlines(), shown.output
    assert shown.output.splitlines()[-1] == f"{len(prompts.names())} prompts, 1 ejected, 1 file read by nothing."


@pytest.mark.parametrize("spelling", ["Rules.md", "RULES.MD"])
def test_a_prompt_file_in_another_spelling_is_read_where_the_disk_ignores_case_and_named_where_not(
        tmp_path: Path, spelling: str) -> None:
    root = lay_out_workspace(tmp_path)
    if written_as(root, spelling):
        assert prompts.load("rules", root) == "Mine."
        assert prompts.unread(root) == ["extraction.md"]
    else:
        assert prompts.load("rules", root) == prompts.load("rules")
        assert prompts.unread(root) == sorted([spelling, "extraction.md"])
        assert f"prompts/{spelling} is named after no prompt Cerebro bundles" in "\n".join(prompts.outdated(root))
        shown = CliRunner().invoke(main, ["prompts", "list", "--workspace", str(root)])
        assert f"{Path(spelling).stem}  prompts/{spelling}, read by nothing: no bundled prompt has this name" in \
            shown.output.splitlines(), shown.output


def test_status_names_it_in_a_notice(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path)
    assert prompts.outdated(root) == [LINE]
    shown = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert shown.exit_code == 0 and f"notice    {LINE}" in shown.output.splitlines(), shown.output
