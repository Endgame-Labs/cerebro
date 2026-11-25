"""A copy of a workspace on the machine that holds the original, as a git clone of its folder would make: the copy
carries the original's id, which opens one secrets store, so trust sees the clash and --new-id gives the copy its own."""

from __future__ import annotations

import shutil
from pathlib import Path

from test_cli import run


def test_trust_on_a_copy_of_a_workspace_names_the_clash_and_new_id_gives_the_copy_a_working_id(tmp_path: Path) -> None:
    original, copy = tmp_path / "gtm-workspace", tmp_path / "elsewhere" / "gtm-workspace"
    assert run("init", str(original), "--daily-usd", "5").exit_code == 0
    assert run("status", "--workspace", str(original)).exit_code == 0  # the original holds its id
    shutil.copytree(original, copy, ignore=shutil.ignore_patterns(".cerebro", "articles"))  # as a clone carries it
    id_line = next(line for line in (copy / "settings.yaml").read_text().splitlines() if line.startswith("id:"))

    refused = run("trust", str(copy))
    assert refused.exit_code == 1 and "trusted" not in refused.output.replace("trust --new-id", "")
    said = refused.output.strip().splitlines()[-1]
    assert f"the id: key in {copy.resolve() / 'settings.yaml'}" in said and str(original.resolve()) in said
    assert said.endswith(f"run cerebro trust --new-id {copy.resolve()}")

    fixed = run("trust", "--new-id", str(copy))
    assert fixed.exit_code == 0 and fixed.output.splitlines()[-1] == f"trusted   {copy.resolve()}", fixed.output
    new_line = next(line for line in (copy / "settings.yaml").read_text().splitlines() if line.startswith("id:"))
    assert new_line != id_line and "# opens this workspace's secrets store" in new_line  # its comment kept
    assert run("status", "--workspace", str(copy)).exit_code == 0
    assert run("status", "--workspace", str(original)).exit_code == 0  # the original keeps its id and its store


def test_the_load_error_for_a_shared_id_names_the_file_the_key_and_the_command(tmp_path: Path) -> None:
    original, copy = tmp_path / "a" / "workspace", tmp_path / "b" / "workspace"
    assert run("init", str(original), "--daily-usd", "5").exit_code == 0
    assert run("status", "--workspace", str(original)).exit_code == 0
    shutil.copytree(original, copy, ignore=shutil.ignore_patterns(".cerebro", "articles"))
    failed = run("status", "--workspace", str(copy))
    assert failed.exit_code == 1
    assert f"the id: key in {copy.resolve() / 'settings.yaml'}" in failed.output
    assert f"cerebro trust --new-id {copy.resolve()}" in failed.output
