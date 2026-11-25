"""Every live check under `checks/`, one after another in the foreground. Not part of the test suite: each check
calls real models and costs some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/run_all.py [name ...]

It prints a table, one row for each check: pass, retried, skip or fail, what the check says it spent, how long it
took, and for all but a clean pass, the reason. A check skips, exiting 0 with a first line `skip: <reason>`, only for
want of something this machine need not have, such as a key; one that cannot run for a broken install fails. A check
that passed only on a second try prints `retried: <reason>`. It exits 1 if any check failed, or if none ran. Names,
such as `live_pass`, run those checks alone. Each check's own output goes to `checks/.runs/<name>.log`, beside this
file, and is never shown here.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOT_CHECKS = {"run_all.py", "spend.py", "side_by_side.py"}  # this runner, its reducer, and the spend helper
SPENT = re.compile(r"^\s*(?:spent|evaluation|usage)\b.*?\$(\d+(?:\.\d+)?)", re.MULTILINE)
LIMIT = 3600  # seconds one check may run before it is stopped and counted as failed
RAN = "run_all:"  # the last line of each log: the check's verdict and its seconds, which side_by_side.py reads


def checks(names: list[str]) -> list[Path]:
    found = sorted(p for p in HERE.glob("*.py") if p.name not in NOT_CHECKS and not p.name.startswith("_"))
    return [p for p in found if not names or p.stem in names]


def verdict(code: int, said: str) -> tuple[str, str]:
    """What a check's exit code and output say: its verdict, and the reason for any but a clean pass."""
    lines = [line.strip() for line in said.splitlines() if line.strip()]
    if code != 0:
        after = lines[max((n for n, line in enumerate(lines) if line == "FAIL"), default=-1) + 1:]
        reasons = [line[2:] for line in after if line.startswith("- ")]
        return "fail", (reasons[0] if reasons else lines[-1] if lines else f"exit {code} with no output")
    if lines and lines[0].startswith("skip:"):
        return "skip", lines[0].removeprefix("skip:").strip()
    retried = [line.removeprefix("retried:").strip() for line in lines if line.startswith("retried:")]
    return ("retried", retried[0]) if retried else ("pass", "")


def main() -> int:
    logs = HERE / ".runs"
    logs.mkdir(exist_ok=True)
    rows = []
    for path in checks(sys.argv[1:]):
        began = time.monotonic()
        try:
            done = subprocess.run([sys.executable, str(path)], check=False, cwd=HERE.parent, capture_output=True,
                                  text=True, timeout=LIMIT)
            code, said = done.returncode, done.stdout + done.stderr
        except subprocess.TimeoutExpired as error:
            code, said = 124, f"{error.stdout or ''}{error.stderr or ''}\n- stopped after {LIMIT} seconds"
            said = said if isinstance(said, str) else said.decode(errors="replace")
        seconds = time.monotonic() - began
        spent = SPENT.findall(said)
        result, reason = verdict(code, said)
        (logs / f"{path.stem}.log").write_text(f"{said}\n{RAN} {result} {seconds:.0f}s\n", encoding="utf-8")
        rows.append((result, path.stem, f"${spent[-1]}" if spent else "$?", seconds, reason))
        print(f"{result:<8} {path.stem:<14} {rows[-1][2]:>9} {seconds:6.0f}s  {reason[:150]}", flush=True)
    counts = {v: sum(1 for r in rows if r[0] == v) for v in ("pass", "retried", "skip", "fail")}
    print(", ".join(f"{n} {v}" for v, n in counts.items()))
    if not counts["pass"] and not counts["retried"]:
        print("nothing ran: every check skipped or failed")
        return 1
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
