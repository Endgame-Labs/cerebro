"""Trust kept by processes at once is never lost: each change of the trust file holds its lock across its read and
its write, shown here on a short run of real processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from cerebro.cli import main

TRUST = "import sys; from pathlib import Path; from cerebro.trust import trust; trust(Path(sys.argv[1]))"


def test_four_processes_trusting_four_workspaces_at_once_lose_no_record(tmp_path: Path) -> None:
    config = Path(os.environ["XDG_CONFIG_HOME"])
    folders = [tmp_path / f"ws{n}" for n in range(4)]
    for folder in folders:
        assert CliRunner().invoke(main, ["init", str(folder), "--daily-usd", "1"]).exit_code == 0
    file = config / "cerebro" / "trusted.json"
    env = {**os.environ, "XDG_CONFIG_HOME": str(config), "CEREBRO_SECRETS_STORE": "file"}
    lost = 0
    for _ in range(10):
        file.unlink(missing_ok=True)
        procs = [subprocess.Popen([sys.executable, "-c", TRUST, str(f)], env=env) for f in folders]
        assert [p.wait(timeout=60) for p in procs] == [0] * 4
        kept = {entry["path"] for entry in json.loads(file.read_text())}
        lost += sum(str(f.resolve()) not in kept for f in folders)
    assert lost == 0
