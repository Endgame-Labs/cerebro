"""A fresh workspace, blank or from any bundled template, runs its first pass without starting a program: nothing it
lays down needs a login or a tool the person has not set up. A stub first on `PATH` records every start of the
command-line tools a source may call, and `subprocess.Popen` records any other start."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.fakes import FakeExtraction, FakeSecrets, MemoryStore, ScriptedRun

STUBBED = ("sf", "sfdx")  # the command-line tools a bundled connector can start


@pytest.mark.parametrize("template", [*scaffold.templates(), None], ids=lambda t: t or "blank")
def test_a_fresh_workspace_runs_its_first_pass_and_starts_no_program(
        template: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    scaffold.init(root, template=template, environ={})
    bin, log = tmp_path / "bin", tmp_path / "stub.log"
    bin.mkdir()
    for name in STUBBED:
        stub = bin / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\necho "{name} is blocked"\nexit 97\n')
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ.get('PATH', '')}")
    started: list[Any] = []
    real = subprocess.Popen

    class Recorded(real):  # type: ignore[misc, valid-type]
        def __init__(self, args: Any, *rest: Any, **options: Any) -> None:
            started.append(args)
            super().__init__(args, *rest, **options)

    monkeypatch.setattr(subprocess, "Popen", Recorded)
    with Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), secrets=FakeSecrets(),
                 agent_run=ScriptedRun(lambda prompt: {})) as workspace:
        result = workspace.run_once()
    assert started == []
    assert not log.exists(), log.read_text()
    assert result.failed == {}
