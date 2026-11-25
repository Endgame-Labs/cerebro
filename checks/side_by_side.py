"""Compare a base and a branch on the logs of `run_all.py`, three rounds a side or more. Not a check itself.

    uv run python checks/side_by_side.py <base folder> <branch folder>

Each folder holds one copy of `checks/.runs/` per round, such as `round-1/`. It prints one row per check per round,
then for each side the low, middle and high of each number: pass (1 or 0), dollars, seconds, any question score, and,
for `live_quiet_passes`, its self wakes, a curator woken on nothing but its own acts, and beside them its other
wakes. Then it names each real difference:
- a score drops when the branch's middle run is under the base's lowest, and rises when over its highest;
- the self wakes fall by the same rule turned round: the branch's middle under the base's lowest;
- a check passes two or more fewer times on the branch than on the base.
Cost, time and other wakes are recorded beside each check, never judged.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from statistics import median

from run_all import RAN, SPENT

SCORE = re.compile(r"\bscore (\d+(?:\.\d+)?)")
WAKES = re.compile(r"^(self|other) wakes: (\d+)$", re.MULTILINE)  # as live_quiet_passes.py prints them
COLUMNS = ("pass", "dollars", "seconds", "score", "self wakes", "other wakes")
FALLS = ("self wakes",)  # lower is better
RECORDED = ("dollars", "seconds", "other wakes")  # numbers never judged


def numbers(log: str) -> dict[str, float]:
    """What one check's log says: pass, dollars, seconds, its first score, and its change wakes."""
    ran = [line.split() for line in log.splitlines() if line.startswith(RAN)]
    out: dict[str, float] = {}
    if ran:
        out["pass"] = float(ran[-1][1] in ("pass", "retried"))
        out["seconds"] = float(ran[-1][2].rstrip("s"))
    if spent := SPENT.findall(log):
        out["dollars"] = float(spent[-1])
    if score := SCORE.search(log):
        out["score"] = float(score.group(1))
    for kind, n in WAKES.findall(log):
        out[f"{kind} wakes"] = float(n)
    return out


def side(folder: Path) -> dict[str, list[dict[str, float]]]:
    """Each check's numbers, one entry per round, in round order."""
    out: dict[str, list[dict[str, float]]] = {}
    for round_ in sorted(p for p in folder.iterdir() if p.is_dir()):
        for log in sorted(round_.glob("*.log")):
            out.setdefault(log.stem, []).append(numbers(log.read_text(encoding="utf-8", errors="replace")))
    return out


def spread(rounds: list[dict[str, float]], name: str) -> tuple[float, float, float] | None:
    values = [r[name] for r in rounds if name in r]
    return (min(values), median(values), max(values)) if values else None


def differences(check: str, base: list[dict[str, float]], branch: list[dict[str, float]]) -> list[str]:
    """Each real difference between the two sides of one check, by the rules above."""
    out = []
    passed = sum(r.get("pass", 0) for r in base), sum(r.get("pass", 0) for r in branch)
    if passed[0] - passed[1] >= 2:
        out.append(f"{check}: passed {passed[1]:g} times on the branch against {passed[0]:g} on the base")
    for name in sorted({n for r in base + branch for n in r} - {"pass", *RECORDED}):
        old, new = spread(base, name), spread(branch, name)
        if old is None or new is None:
            continue
        if new[1] < old[0]:
            word = "fell" if name in FALLS else "dropped"
            out.append(f"{check}: {name} {word}: branch middle {new[1]:g} under base lowest {old[0]:g}")
        elif new[1] > old[2]:
            out.append(f"{check}: {name} rose: branch middle {new[1]:g} over base highest {old[2]:g}")
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print("give two folders: the base's rounds, then the branch's")
        return 1
    sides = {"base": side(Path(sys.argv[1])), "branch": side(Path(sys.argv[2]))}
    print("| side | check | round | " + " | ".join(COLUMNS) + " |")
    for name, checks in sides.items():
        for check, rounds in sorted(checks.items()):
            for n, r in enumerate(rounds, 1):
                cells = [f"{r[k]:g}" if k in r else "" for k in COLUMNS]
                print(f"| {name} | {check} | {n} | " + " | ".join(cells) + " |")
    print("\n| check | number | base low, middle, high | branch low, middle, high |")
    found = []
    for check in sorted(set(sides["base"]) | set(sides["branch"])):
        base, branch = sides["base"].get(check, []), sides["branch"].get(check, [])
        for name in sorted({n for r in base + branch for n in r}):
            shown = [", ".join(f"{v:g}" for v in s) if (s := spread(rounds, name)) else "none" for rounds in (base, branch)]
            print(f"| {check} | {name} | {shown[0]} | {shown[1]} |")
        found += differences(check, base, branch)
    print("\nreal differences:" if found else "\nno real difference")
    for line in found:
        print(f"- {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
