"""No test reaches the person's own home or config folder: the suite runs under a scratch HOME, and a write into the
real folders, or a Cerebro child started with neither HOME nor a config folder, is refused and fails the test."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import conftest
import pytest

from cerebro import scaffold
from cerebro.trust import trusted_path


def test_a_call_handed_no_environment_keeps_its_trust_list_in_the_scratch_home(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path(os.environ["HOME"])
    assert home.name.startswith("cerebro-tests-home-")
    assert not any(str(home).startswith(real) for real in conftest._REAL)
    monkeypatch.delenv("XDG_CONFIG_HOME")  # as a module's fixture runs, before a test's own config folder is named
    root = scaffold.init(tmp_path / "things", environ={}).root
    assert str(root) in trusted_path().read_text(encoding="utf-8") and trusted_path().is_relative_to(home)


def test_a_write_under_the_persons_own_config_folder_is_refused_before_it_lands() -> None:
    if not conftest._REAL:
        pytest.skip("this machine names no config folder of the person's")
    target = Path(conftest._REAL[0]) / "cerebro-tests-must-not-write-here"
    try:
        with pytest.raises(PermissionError, match="tests write nothing under the person's own"):
            target.write_text("x")
        assert not target.exists() and len(conftest._reached) == 1
    finally:
        conftest._reached.clear()  # the refusal was this test's own, so `own_home_untouched` does not fail it


def test_a_child_that_can_load_cerebro_inherits_the_scratch_home_and_a_script_keeps_what_it_was_given() -> None:
    shown = "import os; print(os.environ.get('HOME', 'unset'), os.environ.get('XDG_CONFIG_HOME', 'unset'))"
    bare = {"PATH": os.environ.get("PATH", "")}
    child = subprocess.run([sys.executable, "-c", shown], env=bare, capture_output=True, text=True, check=True)
    assert child.stdout.split() == [os.environ["HOME"], os.environ["XDG_CONFIG_HOME"]]
    script = subprocess.run([sys.executable, "-S", "-c", shown], env=bare, capture_output=True, text=True, check=True)
    assert script.stdout.split() == ["unset", "unset"]
