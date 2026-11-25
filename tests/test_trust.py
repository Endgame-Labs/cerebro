"""Trust is for a workspace, not a path: the record holds the folder's real path and the id the workspace keeps in its
store, so a different workspace put at a trusted path is not trusted."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cerebro import trust as trusting
from cerebro.project import load
from cerebro.trust import UntrustedWorkspace

pytestmark = pytest.mark.untrusted


def _workspace(root: Path, marker: Path) -> Path:
    root.mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "extensions.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    return root


def test_a_different_workspace_put_at_a_trusted_path_is_not_trusted_and_its_code_does_not_run(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "workspace", tmp_path / "A_RAN")
    trusting.trust(root)
    kept = json.loads(trusting.trusted_path().read_text())
    assert kept == [{"path": str(root), "id": trusting.workspace_id(root),
                     "store": {"class": "sqlite", "path": str(root / ".cerebro" / "cerebro.sqlite")}}]
    assert len(kept[0]["id"]) == 32
    load(root)
    assert (tmp_path / "A_RAN").exists()
    shutil.rmtree(root)  # as another workspace put on a container's /workspace volume, with no store of its own
    _workspace(root, tmp_path / "B_RAN")
    with pytest.raises(UntrustedWorkspace, match="is not a workspace you trust"):
        load(root)
    assert not (tmp_path / "B_RAN").exists() and not (root / ".cerebro").exists()
    other = _workspace(tmp_path / "other", tmp_path / "C_RAN")
    trusting.trust(other)
    shutil.rmtree(root)
    shutil.copytree(other, root)  # a copy of another trusted workspace, its store and id with it, at the first path
    with pytest.raises(UntrustedWorkspace):
        load(root)
    assert not (tmp_path / "C_RAN").exists()


def test_a_moved_store_or_a_record_by_path_alone_trusts_nothing_until_trusted_again(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "workspace", tmp_path / "RAN")
    trusting.trust(root)
    (root / "settings.yaml").write_text("store:\n  path: .cerebro/other.sqlite\n")
    with pytest.raises(UntrustedWorkspace):
        load(root)
    trusting.trust(root)
    load(root)
    assert (tmp_path / "RAN").exists()
    trusting.trusted_path().write_text(json.dumps([str(root)]))  # a record by path alone
    assert not trusting.trusted(root)


def test_trusting_a_second_workspace_keeps_the_first_and_a_copy_beside_it_whose_name_begins_the_same_is_not_trusted(
        tmp_path: Path) -> None:
    first = _workspace(tmp_path / "workspace", tmp_path / "A_RAN")
    (first / "settings.yaml").write_text(f"store:\n  path: {tmp_path / 'kept.sqlite'}\n")  # outside the folder
    second = _workspace(tmp_path / "other", tmp_path / "B_RAN")
    trusting.trust(first)
    trusting.trust(second)
    assert trusting.trusted(first) and trusting.trusted(second)
    load(first)
    assert (tmp_path / "A_RAN").exists()
    copy = tmp_path / "workspace-x"
    shutil.copytree(first, copy)  # the same store, id and settings, at a path the first one's begins
    assert not trusting.trusted(copy)


def test_a_trust_list_that_does_not_read_is_refused_and_never_read_as_empty(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "workspace", tmp_path / "RAN")
    trusting.trust(root)
    trusting.trusted_path().write_text("[{not json")
    with pytest.raises(trusting.TrustError, match="does not read"):
        trusting.trusted(root)
    with pytest.raises(trusting.TrustError, match="does not read"):
        trusting.trust(root)
    assert trusting.trusted_path().read_text() == "[{not json"  # what the person may want to mend is kept


def test_a_swapped_workspace_naming_another_store_class_is_refused_before_that_class_is_imported(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from click.testing import CliRunner

    from cerebro.cli import main

    root = _workspace(tmp_path / "workspace", tmp_path / "RAN")
    trusting.trust(root)
    planted = tmp_path / "modules"
    planted.mkdir()
    (planted / "planted_store.py").write_text(f"open({str(tmp_path / 'IMPORTED')!r}, 'w').write('x')\nThing = 1\n")
    monkeypatch.syspath_prepend(str(planted))
    (root / "settings.yaml").write_text("store:\n  class: planted_store:Thing\n")  # as a swapped workspace's says
    assert not trusting.trusted(root)
    assert "store     class planted_store:Thing" in trusting.allows(root)
    for command in (["status"], ["serve", "--once"], ["agents", "list"], ["tokens", "list"], ["mcp"],
                    ["sources", "new", "x"]):
        CliRunner().invoke(main, [*command, "--workspace", str(root)])
    assert not (tmp_path / "IMPORTED").exists() and "planted_store" not in sys.modules


def test_a_script_in_an_agent_file_that_starts_with_a_byte_order_mark_is_listed_as_the_loader_reads_it(
        tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "agents" / "notes").mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "agents" / "notes" / "a.md").write_text(
        "﻿---\nevidence:\n  - script: { run: ./steal.sh }\nheartbeat: [change]\n---\n\n# Output\n\n## Status\n",
        encoding="utf-8")
    assert "run       ./steal.sh, the script agents/notes/a.md runs" in trusting.allows(root)
