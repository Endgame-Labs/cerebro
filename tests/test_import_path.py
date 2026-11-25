"""No command puts a workspace folder on Python's import path: a file in a workspace named like a standard module never
runs in place of it, from the command line, standard input and output, or the image's health check."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PY = Path(__file__).resolve().parents[1]
CEREBRO = Path(sys.executable).parent / "cerebro"
PLANTED = ("json.py", "typing.py", "sitecustomize.py", "usercustomize.py", "urllib/__init__.py", "urllib/request.py",
           "click.py", "yaml.py")


def _planted(root: Path) -> Path:
    """A workspace nobody trusted, with a file named like each of several modules, each of which leaves a
    mark if run."""
    marks = root.parent / "marks"
    marks.mkdir()
    (root / "agents").mkdir(parents=True)
    (root / "settings.yaml").write_text("budget:\n  daily_usd: 1\n")
    for rel in PLANTED:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"open({str(marks / rel.replace('/', '_'))!r}, 'w').write('ran')\n")
    return marks


def _health_check() -> list[str]:
    """The image's health check, as its Dockerfile gives it."""
    found = re.search(r"HEALTHCHECK[^\n]*\\\n\s*CMD (\[.*\])", (PY / "Dockerfile").read_text())
    assert found, "the Dockerfile has a health check in exec form"
    return json.loads(found.group(1))


@pytest.mark.skipif(not CEREBRO.is_file(), reason="the cerebro entry point is not installed beside this python")
def test_a_module_planted_in_an_untrusted_workspace_is_run_by_no_command(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    marks = _planted(root)
    environ = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and k != "PYTHONSAFEPATH"} | {
        "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
        "PATH": f"{CEREBRO.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    commands = [[str(CEREBRO), "--help"], [str(CEREBRO), "status"], [str(CEREBRO), "mcp"],
                [str(CEREBRO), "tokens", "list"], _health_check()]
    running = [subprocess.Popen(command, cwd=root, env=environ, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL) for command in commands]
    for process in running:
        assert process.wait(60) is not None
    assert sorted(p.name for p in marks.iterdir()) == []
