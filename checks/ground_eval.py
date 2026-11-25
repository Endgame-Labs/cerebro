"""The grounding corpus as an eval: every hand-labelled case grounded against the real decision model. Not part of the
test suite, and run by hand. It is `cerebro eval` on the corpus with no workspace folder: one way to run an eval.

    TYPESAFE_API_KEY=... uv run python checks/ground_eval.py [--model jev-1.13.0] [--workers 8] [--group rows]
    FIREWORKS_API_KEY=... uv run python checks/ground_eval.py --provider fireworks --model rune
    FIREWORKS_API_KEY=... uv run python checks/ground_eval.py --repair glm-5p3

Each case is one draft sentence with the facts it cites, from `tests/data/ground/corpus.json`, labelled supported or
unsupported before anything ran against it. It prints what `evals.lines` prints: the rate of supported cases kept
whole and of unsupported ones rejected, overall and per group, each case that came out against its label and why,
and what the calls spent, kept in a ledger of its own with no cap. It fails, and exits 1, when it keeps fewer supported
cases than `--min-kept`, 75% by default, or drops fewer unsupported ones than `--min-dropped`, 90%. It prints no
key. With no key set it says so and stops. With `--repair <model>`, a Fireworks writing model repairs each
sentence that fails, as a compile does: a repaired case is
counted apart, each rewrite is printed, and so is what the rewrites spent.

The run ends with one block: supported cases kept whole of all supported, unsupported ones dropped of all unsupported,
the same two counts over the held back cases (split `held_out`, and one in ten of the rest by a hash of the id), the
cost, and one line for each miss, with the step that took a
supported case and the judge's answers on its sentence and any lead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from cerebro import evals
from cerebro.budget import Caps, Ledger
from cerebro.evidence import CITE
from cerebro.fakes import MemoryStore
from cerebro.models import DEFAULTS, Decider, Decision, HttpDecider, ModelSpec, TokenCount, cost
from cerebro.run import PydanticRewriter

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "data" / "ground" / "corpus.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ground every labelled case against the real decision model.")
    parser.add_argument("--provider", default=DEFAULTS["decision"].provider, help="the decision model's provider")
    parser.add_argument("--model", default=DEFAULTS["decision"].model, help="the decision model, as settings name it")
    parser.add_argument("--repair", default="", help="a Fireworks writing model that repairs a failed sentence")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--group", default="", help="only the cases of this group, such as rows or held_out")
    parser.add_argument("--no-model", action="store_true", help="ground with no decider: the rule of words alone")
    parser.add_argument("--min-kept", type=float, default=0.75, help="the least share of supported cases kept whole")
    parser.add_argument("--min-dropped", type=float, default=0.90, help="the least share of unsupported cases dropped")
    args = parser.parse_args()
    spec = ModelSpec(args.provider, args.model, job="decision")
    if not args.no_model and not os.environ.get(spec.variable):
        print(f"skip: {spec.variable} is not set")
        return 0
    rewriter = _Counted(PydanticRewriter.from_spec(ModelSpec("fireworks", args.repair))) if args.repair else None
    started = time.monotonic()
    kept = None if args.no_model else _Kept(HttpDecider(spec))
    report = evals.run(None, evals.load(CORPUS), decider=kept, model=spec,
                       ledger=Ledger(MemoryStore(), Caps(evaluation=None)), cap_usd=None, group=args.group,
                       workers=max(1, args.workers), rewriter=rewriter)
    for line in evals.lines(report):
        print(line)
    if rewriter is not None:
        for case in report.scored:
            for line in case.dropped:
                if ": repaired " in line:
                    print(f"repair {case.id} ({case.label}): {line}")
        print(f"repair: {rewriter.calls} calls, {rewriter.input} input and {rewriter.output} output tokens, "
              f"${cost('fireworks', args.repair, rewriter):.4f}")
    print(f"took {time.monotonic() - started:.1f}s")
    for line in measure(report, kept):
        print(line)
    every = report.groups["all"]
    kept = every.kept_whole / every.supported if every.supported else 0.0
    dropped = every.rejected / every.unsupported if every.unsupported else 0.0
    low = [f"kept {kept:.0%} of supported cases, under the floor of {args.min_kept:.0%}"] if kept < args.min_kept else []
    low += [f"dropped {dropped:.0%} of unsupported cases, under the floor of {args.min_dropped:.0%}"] \
        if dropped < args.min_dropped else []
    for line in low:
        print(f"FAIL: {line}")
    print("PASS" if not low else "FAIL")
    return 1 if low else 0


def held_back(case: dict) -> bool:
    """Whether a case is held back from tuning: split held_out, or one in ten by a hash of its id."""
    return case.get("split") == "held_out" or int(hashlib.sha256(case["id"].encode()).hexdigest(), 16) % 10 == 0


def measure(report: evals.Report, kept: _Kept | None) -> list[str]:
    """The closing block: the two rates over all cases and over the held back ones, the cost, and each miss with its
    step and the judge's answers."""
    raw = {c["id"]: c for c in json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]}
    scored = [s for s in report.scored if s.score is not None]

    def rates(cases: list) -> str:
        good = [s for s in cases if s.label == evals.SUPPORTED]
        bad = [s for s in cases if s.label != evals.SUPPORTED]
        whole = sum(s.outcome == evals.WHOLE for s in good)
        gone = sum(s.outcome != evals.WHOLE for s in bad)
        return f"supported kept {whole} of {len(good)}, unsupported dropped {gone} of {len(bad)}"

    held = [s for s in scored if held_back(raw.get(s.id, {"id": s.id}))]
    out = [f"all cases: {rates(scored)}",
           f"held back ({len(held)} cases): {rates(held)}",
           f"cost: ${report.usd:.4f} over {report.calls} decision calls" + (
               f", {len(report.unscored)} cases unscored" if report.unscored else "")]
    missed = [s for s in scored if not s.score]
    out.append(f"missed {len(missed)}: {' '.join(s.id for s in missed)}")
    for s in missed:
        case = raw.get(s.id, {})
        said = "; ".join(kept.said(case.get("sentence", ""))) if kept else ""
        out.append(f"  {s.id}{' (held back)' if held_back(case or {'id': s.id}) else ''} {s.label}: {s.outcome}"
                   + (f" by the {s.step}" if s.step else "") + (f"; judge: {said}" if said else ""))
    return out


class _Kept(Decider):
    """The decision model, keeping what each request asked and what it answered, so a miss shows its answers."""

    def __init__(self, inner: Decider):
        self.inner, self.lock = inner, threading.Lock()
        self.kept: list[tuple[list[str], dict]] = []

    def decide(self, state, questions) -> Decision:
        decision = self.inner.decide(state, questions)
        texts = [str(item.get("sentence") or item.get("heads") or "") for item in state.get("sentences", [])]
        with self.lock:
            self.kept.append((texts, dict(decision.answers)))
        return decision

    def said(self, sentence: str) -> list[str]:
        """The answers to each request about `sentence`, as `s1 grounded 0.92 (grounded 0.92, ...)`."""
        plain = " ".join(CITE.sub("", sentence).split())
        out = []
        with self.lock:
            kept = list(self.kept)
        for texts, answers in kept:
            if not any(t and " ".join(t.split()).rstrip(" .") in plain for t in texts):
                continue
            for name, a in sorted(answers.items()):
                if a.choice:
                    spread = ", ".join(f"{k} {v:.2f}" for k, v in sorted(a.probabilities.items()))
                    out.append(f"{name} {a.choice} {a.confidence or 0:.2f} ({spread})")
        return out


class _Counted:
    """A rewriter that counts its calls and tokens, which the corpus ledger does not price."""

    def __init__(self, inner: PydanticRewriter):
        self.inner, self.calls, self.lock = inner, 0, threading.Lock()
        self.input = self.output = 0

    @property
    def input_tokens(self) -> int:
        return self.input

    @property
    def output_tokens(self) -> int:
        return self.output

    cache_read_tokens = cache_write_tokens = 0

    def rewrite(self, system: str, request: str) -> tuple[str, TokenCount]:
        reply, usage = self.inner.rewrite(system, request)
        with self.lock:
            self.calls += 1
            self.input += usage.input_tokens
            self.output += usage.output_tokens
        return reply, usage


if __name__ == "__main__":
    sys.exit(main())
