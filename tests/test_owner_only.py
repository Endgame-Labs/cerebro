"""What a workspace keeps is readable by its owner alone: `.cerebro/` and every folder under it 0700, every file 0600,
the store's file and SQLite's files beside it, Factoid's data and the search index among them, and the mirrored
`articles/` the same. A workspace open to other accounts is tightened on open, once, and init says when the folder it was given is
open to other accounts."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro import Cerebro, private
from cerebro.article import Article, Identity, mirror
from cerebro.cli import main
from cerebro.fakes import FakeExtraction, ScriptedRun
from cerebro.shown import shell_path
from cerebro.store import DEFAULT_PATH

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Windows has no owner, group and other modes")


@pytest.fixture
def open_mask() -> Iterator[None]:
    """A creation mask of 0 for the test, the widest a process can start with."""
    old = os.umask(0)
    try:
        yield
    finally:
        os.umask(old)


def modes(top: Path) -> dict[str, int]:
    found = {".": stat.S_IMODE(os.lstat(top).st_mode)}
    for folder, dirs, files in os.walk(top):
        for name in [*dirs, *files]:
            path = Path(folder) / name
            found[str(path.relative_to(top))] = stat.S_IMODE(os.lstat(path).st_mode)
    return found


def owner_only(top: Path) -> bool:
    return all(mode == (private.FOLDER if (top / rel).is_dir() else private.FILE) for rel, mode in modes(top).items())


def made(root: Path) -> Path:
    assert CliRunner().invoke(main, ["init", str(root), "--daily-usd", "1", "--timezone", "UTC"]).exit_code == 0
    return root


def test_a_pass_that_writes_facts_and_a_search_row_leaves_every_file_under_cerebro_the_owners_alone(
        tmp_path: Path, open_mask: None) -> None:
    from test_facts import on_factoid

    root = made(tmp_path / "desk")
    (root / "local" / "2026-11-12-sync.md").write_text("Priya Raman moved the launch to 8 December.\n")
    handler, _ = on_factoid(root)
    with Cerebro(root, trusted=True, extraction=handler, agent_run=ScriptedRun(lambda prompt: {})) as workspace:
        assert workspace.run_once(agents=False).extracted == 1
        workspace.index.upsert("articles", [{"id": "projects/launch", "text": "Launch moved to 8 December."}])
        assert workspace.index.search("articles", "launch", 5)
        found = modes(root / ".cerebro")  # SQLite's side files are there while the store is open
    assert {"cerebro.sqlite", "cerebro.sqlite-wal", "cerebro.sqlite-shm", "factoid", "factoid/store.db",
            "factoid/index.db", "factoid/calls.db", "factoid/origins.json"} <= set(found)
    assert owner_only(root / ".cerebro"), found
    assert stat.S_IMODE(os.stat(root).st_mode) == private.FOLDER


def test_a_workspace_open_to_others_is_tightened_on_open_and_says_so_once(tmp_path: Path) -> None:
    root = made(tmp_path / "desk")
    state = root / ".cerebro"
    (state / "factoid").mkdir()
    (state / "factoid" / "store.db").write_text("")
    for path in modes(state):
        target = state / path
        os.chmod(target, 0o755 if target.is_dir() else 0o644)
    os.chmod(state / "factoid" / "store.db", 0o400)  # narrower than 0600 already: never widened
    first = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    said = [line for line in first.output.splitlines() if "modes:" in line]
    assert said == [("notice    modes: .cerebro could be read by other accounts on this machine, so cerebro made "
                    "them readable by you alone")]
    found = modes(state)
    assert found["."] == found["factoid"] == 0o700 and found["cerebro.sqlite"] == 0o600
    assert found["factoid/store.db"] == 0o400
    again = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert again.exit_code == 0 and "modes:" not in again.output


def test_a_mode_cerebro_cannot_change_is_said_in_one_line_and_the_workspace_still_opens(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = made(tmp_path / "desk")
    os.chmod(root / DEFAULT_PATH, 0o644)

    chmod = os.chmod

    def refused(path: object, mode: int, **options: object) -> None:
        if Path(str(path)).is_relative_to(root):
            raise PermissionError(1, "Operation not permitted")
        chmod(path, mode, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "chmod", refused)
    with Cerebro(root, extraction=FakeExtraction()) as workspace:
        said = [n for n in workspace.notices if n.startswith("modes:")]
    assert said == [(f"modes: .cerebro/cerebro.sqlite can be read by other accounts on this machine, and cerebro "
                    f"could not change that (Operation not permitted): run chmod -R go-rwx "
                    f"{shell_path(root / DEFAULT_PATH)}")]


def test_a_store_settings_put_elsewhere_is_made_and_kept_the_owners_alone(tmp_path: Path, open_mask: None) -> None:
    root = made(tmp_path / "desk")
    settings = root / "settings.yaml"
    elsewhere = tmp_path / "kept" / "deep" / "desk.sqlite"
    settings.write_text(settings.read_text() + f"store:\n  path: {elsewhere}\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction()) as workspace:
        workspace.store.set_state("touched", "yes")
    assert stat.S_IMODE(os.stat(elsewhere).st_mode) == 0o600
    assert {stat.S_IMODE(os.stat(p).st_mode) for p in elsewhere.parent.glob("desk.sqlite*")} == {0o600}
    assert stat.S_IMODE(os.stat(elsewhere.parent).st_mode) == stat.S_IMODE(os.stat(tmp_path / "kept").st_mode) == 0o700
    os.chmod(elsewhere, 0o644)
    with Cerebro(root, trusted=True, extraction=FakeExtraction()) as workspace:
        assert any(n.startswith(f"modes: {elsewhere}") for n in workspace.notices)
    assert stat.S_IMODE(os.stat(elsewhere).st_mode) == 0o600


def test_the_mirrored_articles_are_the_owners_alone(tmp_path: Path, open_mask: None) -> None:
    root = tmp_path / "desk"
    root.mkdir()
    article = Article(Identity("projects/apollo", "projects", "Apollo"))
    path = mirror(article, root)
    os.chmod(path, 0o644)
    mirror(article, root)
    assert owner_only(root / "articles") and path.is_file()


def test_init_makes_a_new_folder_the_owners_alone(tmp_path: Path, open_mask: None) -> None:
    root = made(tmp_path / "new" / "desk")
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "new").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "agents").st_mode) == 0o700
    assert owner_only(root / ".cerebro")


def test_init_into_a_folder_open_to_others_says_how_to_close_it_and_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "shared desk"
    root.mkdir()
    os.chmod(root, 0o755)
    result = CliRunner().invoke(main, ["init", str(root), "--daily-usd", "1", "--timezone", "UTC"])
    assert result.exit_code == 0
    said = [line for line in result.output.splitlines() if "other accounts" in line]
    assert said == [(f"notice    other accounts on this machine can read {shell_path(root)}, and so the articles under "
                    f"it: chmod 700 {shell_path(root)} closes it")]
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o755
    closed = tmp_path / "closed"
    closed.mkdir(mode=0o700)
    quiet = CliRunner().invoke(main, ["init", str(closed), "--daily-usd", "1", "--timezone", "UTC"])
    assert quiet.exit_code == 0 and "other accounts" not in quiet.output


def test_on_windows_no_mode_is_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(private, "POSIX", False)
    folder = tmp_path / "state"
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    assert private.tighten([folder], tmp_path) == [] and not private.open_to_others(folder)
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o755
