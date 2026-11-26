"""Labels over parts of a list, as a call article's next steps carry them, grounded on the real decision model. Not
part of the test suite, and run by hand.

    FIREWORKS_API_KEY=... uv run python checks/live_list_labels.py --provider fireworks --model rune

Each case of `checks/list_label_cases.yaml` is one part of a made-up call article, labelled by hand before anything
ran against it: supported-in-other-words when every label and item in it should land, truly-unsupported when one of
its labels claims what the facts under it do not hold and should go. It prints each case, what grounding dropped and
the judge's answer on each lead, then what the calls spent. It fails, and exits 1, when a case lands against its
label. It prints no key, and with no key set it says so and stops.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from cerebro import evals
from cerebro.agent import Agent
from cerebro.article import Citation
from cerebro.ground import ground, quote_for
from cerebro.models import DEFAULTS, Decider, Decision, HttpDecider, ModelSpec, cost

CASES = Path(__file__).resolve().parent / "list_label_cases.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ground each list label case against the real decision model.")
    parser.add_argument("--provider", default=DEFAULTS["decision"].provider, help="the decision model's provider")
    parser.add_argument("--model", default=DEFAULTS["decision"].model, help="the decision model, as settings name it")
    args = parser.parse_args()
    spec = ModelSpec(args.provider, args.model, job="decision")
    if not os.environ.get(spec.variable):
        print(f"skip: {spec.variable} is not set")
        return 0
    suite, raw = evals.load(CASES), yaml.safe_load(CASES.read_text(encoding="utf-8"))
    heading = raw["heading"]
    agent = Agent(id="calls/article", path=Path("agents/calls/article.md"), role="writer", target=raw["article"],
                  prompt="")
    citable = {id: Citation("fact", id, f.record, quote_for(f)) for id, f in suite.facts.items()}
    spent, wrong = 0.0, []
    for case in suite.cases:
        decider = _Leads(HttpDecider(spec))
        got = ground(agent, {heading: case.sentence}, citable, decider=decider, facts=suite.facts, today=suite.today,
                     context=suite.context)
        spent += cost(spec.provider, spec.model, got.usage)
        landed = got.parts.get(heading, "").strip() == case.sentence.strip()
        right = landed is (case.label == evals.SUPPORTED)
        wrong += [] if right else [case.id]
        print(f"case {case.id} {case.label}: {'landed' if landed else 'changed'}{' RIGHT' if right else ' WRONG'}")
        for line in [*got.dropped, *(f"judge: {said}" for said in decider.said)]:
            print(f"  {line}")
    print(f"spent ${spent:.4f}")
    print("PASS" if not wrong else f"FAIL: {len(wrong)} against their label: {' '.join(wrong)}")
    return 1 if wrong else 0


class _Leads(Decider):
    """The decision model, keeping its answer on each lead it was asked about, as
    `'Ours:' names 0.93 (names 0.93, states_held 0.05, states_unheld 0.02)`."""

    def __init__(self, inner: Decider):
        self.inner, self.said = inner, []

    def decide(self, state, questions) -> Decision:
        decision = self.inner.decide(state, questions)
        leads = {item["id"]: item["lead"] for item in state["sentences"] if item.get("lead")}
        for k, a in decision.answers.items():
            if k in leads:
                spread = ", ".join(f"{name} {p:.2f}" for name, p in sorted(a.probabilities.items()))
                self.said.append(f"{leads[k]!r} {a.choice} {a.confidence or 0:.2f} ({spread})")
        return decision


if __name__ == "__main__":
    sys.exit(main())
