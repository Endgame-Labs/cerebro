"""The grounding corpus as an eval: every hand-labelled case scored by the real decision model, as grounding scores a
sentence. Not part of the test suite, and run by hand. It is `cerebro eval` on the corpus with no workspace folder:
one way to run an eval. Run it again whenever the judge's wording or model changes.

    TYPESAFE_API_KEY=... uv run python checks/ground_eval.py [--model jev-1.13.0] [--workers 8] [--group voice]
    FIREWORKS_API_KEY=... uv run python checks/ground_eval.py --provider fireworks --model rune

Each case is one draft sentence, or a few, with the facts they cite, from `tests/data/ground/corpus.json`, labelled
supported or unsupported before anything ran against it. A case scores as its weakest sentence. It prints what
`evals.lines` prints: the share of supported cases at or above the floor, `ground.FLOOR`, and of unsupported ones under
it, overall and per group, each case that came out against its label with its score and why, and what the calls
spent, kept in a ledger of its own with no cap. It fails, and exits 1, when fewer supported cases hold than
`--min-held`, 75% by default, or fewer unsupported ones fall under the floor than `--min-marked`, 85%. It prints no
key. With no key set it says so and stops.

The run ends with one block: the two shares over all cases and over the held back cases (split `held_out`, and one
in ten of the rest by a hash of the id), the cost, and each miss.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from cerebro import evals
from cerebro.budget import Caps, Ledger
from cerebro.fakes import MemoryStore
from cerebro.models import DEFAULTS, HttpDecider, ModelSpec

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "data" / "ground" / "corpus.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Score every labelled case with the real decision model.")
    parser.add_argument("--provider", default=DEFAULTS["decision"].provider, help="the decision model's provider")
    parser.add_argument("--model", default=DEFAULTS["decision"].model, help="the decision model, as settings name it")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--group", default="", help="only the cases of this group, such as voice or held_out")
    parser.add_argument("--min-held", type=float, default=0.75, help="the least share of supported cases held")
    parser.add_argument("--min-marked", type=float, default=0.85, help="the least share of unsupported cases marked")
    parser.add_argument("--json", default="", help="a file to write each case's score to")
    args = parser.parse_args()
    spec = ModelSpec(args.provider, args.model, job="decision")
    if not os.environ.get(spec.variable):
        print(f"skip: {spec.variable} is not set")
        return 0
    started = time.monotonic()
    report = evals.run(None, evals.load(CORPUS), decider=HttpDecider(spec), model=spec,
                       ledger=Ledger(MemoryStore(), Caps(evaluation=None)), cap_usd=None, group=args.group,
                       workers=max(1, args.workers))
    for line in evals.lines(report):
        print(line)
    print(f"took {time.monotonic() - started:.1f}s")
    for line in measure(report):
        print(line)
    if args.json:
        Path(args.json).write_text(json.dumps({s.id: {"label": s.label, "score": s.outcome, "right": s.score,
                                                      "reason": s.reason} for s in report.scored}, indent=1))
    every = report.groups["all"]
    held = every.held / every.supported if every.supported else 0.0
    marked = every.marked / every.unsupported if every.unsupported else 0.0
    low = [f"held {held:.0%} of supported cases, under the floor of {args.min_held:.0%}"] if held < args.min_held else []
    low += [f"marked {marked:.0%} of unsupported cases, under the floor of {args.min_marked:.0%}"] \
        if marked < args.min_marked else []
    for line in low:
        print(f"FAIL: {line}")
    print("PASS" if not low else "FAIL")
    return 1 if low else 0


def held_back(case: dict) -> bool:
    """Whether a case is held back from tuning: split held_out, or one in ten by a hash of its id."""
    return case.get("split") == "held_out" or int(hashlib.sha256(case["id"].encode()).hexdigest(), 16) % 10 == 0


def measure(report: evals.Report) -> list[str]:
    """The closing block: the two shares over all cases and over the held back ones, the cost, and each miss."""
    raw = {c["id"]: c for c in json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]}
    scored = [s for s in report.scored if s.score is not None]

    def rates(cases: list) -> str:
        good = [s for s in cases if s.label == evals.SUPPORTED]
        bad = [s for s in cases if s.label != evals.SUPPORTED]
        return (f"supported held {sum(bool(s.score) for s in good)} of {len(good)}, unsupported marked "
                f"{sum(bool(s.score) for s in bad)} of {len(bad)}")

    held = [s for s in scored if held_back(raw.get(s.id, {"id": s.id}))]
    out = [f"all cases: {rates(scored)}",
           f"held back ({len(held)} cases): {rates(held)}",
           f"cost: ${report.usd:.4f} over {report.calls} decision calls" + (
               f", {len(report.unscored)} cases unscored" if report.unscored else "")]
    missed = [s for s in scored if not s.score]
    out.append(f"missed {len(missed)}: {' '.join(s.id for s in missed)}")
    for s in missed:
        back = " (held back)" if held_back(raw.get(s.id) or {"id": s.id}) else ""
        out.append(f"  {s.id}{back} {s.label}: {s.outcome}, {s.reason}")
    return out


if __name__ == "__main__":
    sys.exit(main())
