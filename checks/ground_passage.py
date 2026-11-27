"""One long passage scored as grounding scores it, on the real decision model: twelve sentences where later ones
build on facts cited only in earlier ones, such as the last, which rests on the first, and one leans on an earlier
fact that only seems to back it. Not part of the test suite, and run by hand.

    TYPESAFE_API_KEY=... uv run python checks/ground_passage.py [--tries 4]
    FIREWORKS_API_KEY=... uv run python checks/ground_passage.py --provider fireworks --model rune

Each try scores the passage once and prints each sentence's score, how many requests it took and what they spent.
It fails, and exits 1, when a sentence labelled backed lands marked, one labelled not backed lands unmarked, or any
sentence goes unscored, in any try. It prints no key. With no key set it says so and stops.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from cerebro.facts import Fact
from cerebro.ground import Editor, Judge, fields_of
from cerebro.models import DEFAULTS, Decider, Decision, HttpDecider, ModelSpec, cost

FACTS = {f"f_m{n}": text for n, text in enumerate([
    "Fennick Field Station moved its weather mast to the north ridge on March 3, 2026.",
    "The weather mast was moved because its old site flooded twice in February 2026.",
    "Orla Brenn leads the weather mast work at Fennick Field Station.",
    "The north ridge site has mains power and a fibre line.",
    "Since the move, the weather mast has logged a reading every ten minutes.",
    "The station's two rain gauges were not moved and stay at the old site.",
    "The rain gauges at the old site are read by hand once a day.",
    "The station plans to add a wind profiler next year.",
    "The wind profiler needs mains power.",
    "Orla Brenn sends a monthly data summary to the regional office."], 1)}
PASSAGE = [  # each sentence, and whether the facts it cites, read with the sentences before it, back it
    ("Fennick Field Station moved its weather mast to the north ridge on March 3 [^f_m1].", True),
    ("It moved because the old site flooded twice in February [^f_m2].", True),
    ("Orla Brenn leads that work [^f_m3].", True),
    ("The new site has mains power and a fibre line [^f_m4].", True),
    ("Since the move, the mast has logged a reading every ten minutes [^f_m5].", True),
    ("Its two rain gauges stayed behind at the old site [^f_m6].", True),
    ("There they are read by machine every hour [^f_m7].", False),
    ("A wind profiler is planned for next year [^f_m8].", True),
    ("The profiler needs mains power, which the mast's new home on the north ridge has [^f_m9].", True),
    ("The profiler will stand on the north ridge, by the mast [^f_m4].", False),
    ("Orla Brenn, who leads the mast work, sends the regional office a monthly data summary [^f_m10].", True),
    ("Since March 3, the mast has logged its readings from the north ridge [^f_m5].", True)]


class Counted(Decider):
    """The decision model, counting the requests it answered and what they cost."""

    def __init__(self, spec: ModelSpec):
        self.inner, self.spec, self.requests, self.usd = HttpDecider(spec), spec, 0, 0.0

    def decide(self, state: Any, questions: Any) -> Decision:
        decision = self.inner.decide(state, questions)
        self.requests += 1
        self.usd += cost(self.spec.provider, self.spec.model, decision.usage)
        return decision


def found(ids: list[str]) -> dict[str, dict[str, Any]]:
    facts = {id: Fact(id, text, "Fennick Field Station", "", "2026-03-10T10:00:00Z", "local", "local/notes/mast.md")
             for id, text in FACTS.items()}
    return {id: fields_of(id, None, facts[id]) or {} for id in ids if id in facts}


def main() -> int:
    parser = argparse.ArgumentParser(description="Score one long passage with the real decision model.")
    parser.add_argument("--provider", default=DEFAULTS["decision"].provider, help="the decision model's provider")
    parser.add_argument("--model", default=DEFAULTS["decision"].model, help="the decision model, as settings name it")
    parser.add_argument("--tries", type=int, default=4)
    args = parser.parse_args()
    spec = ModelSpec(args.provider, args.model, job="decision")
    if not os.environ.get(spec.variable):
        print(f"skip: {spec.variable} is not set")
        return 0
    text, wrong, usd = " ".join(said for said, _ in PASSAGE), [], 0.0
    for n in range(args.tries):
        decider = Counted(spec)
        verdicts = Editor(Judge(decider, "notes/mast", "A field station's notes.", "2026-03-20"), found).judged(text)
        usd += decider.usd
        print(f"try {n + 1}: {decider.requests} requests, ${decider.usd:.4f}")
        for at, (verdict, (_, backed)) in enumerate(zip(verdicts, PASSAGE, strict=True), 1):
            miss = "unscored" if verdict.score is None else "against its label" if verdict.weak == backed else ""
            wrong += [f"try {n + 1}, sentence {at} came out {miss}"] if miss else []
            score = f"{verdict.score:.2f}" if verdict.score is not None else "unscored"
            print(f"  {'MISS ' if miss else ''}{at} {'backed' if backed else 'not backed'}: {score}, {verdict.reason}")
    print(f"spent ${usd:.4f} over {args.tries} tries")
    for line in wrong:
        print(f"FAIL: {line}")
    print("PASS" if not wrong else "FAIL")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
