"""The presumed cases as an eval: each item of `checks/presumed_cases.yaml` grounded against the real decision model,
as `ground_eval.py` grounds its corpus. Not part of the test suite, and run by hand.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/presumed_eval.py

It prints what `ground_eval.py` prints for its corpus, then one line per case: its id, group, label, what became of
it, and the judge's answers on it, found by exactly what it was shown: the main question `s1`, and `n1` for the
guess a mark names. It prints no key, and with no
key set it says so and stops. It fails, and exits 1, when any case comes out against its label.

With `--onward`, a case file that also holds `articles` grounds each case's sentence against an article it cites, as
a later run cites one: the article's text is the citation's evidence, and a guess its yaml marks presumed must never
hold up a sentence that states it as fact.

    ... uv run python checks/presumed_eval.py --onward checks/presumed_onward_cases.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import yaml

from cerebro import evals
from cerebro import ground as grounding
from cerebro.agent import Agent
from cerebro.article import Citation
from cerebro.budget import Caps, Ledger
from cerebro.fakes import MemoryStore
from cerebro.models import DEFAULTS, Decider, Decision, HttpDecider, ModelSpec, cost

CASES = Path(__file__).resolve().parent / "presumed_cases.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ground every presumed case against the real decision model.")
    parser.add_argument("--provider", default=DEFAULTS["decision"].provider, help="the decision model's provider")
    parser.add_argument("--model", default=DEFAULTS["decision"].model, help="the decision model, as settings name it")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--cases", type=Path, default=CASES, help="the case file, such as checks/presumed_named_cases.yaml")
    parser.add_argument("--onward", type=Path, help="a case file whose sentences cite its articles")
    args = parser.parse_args()
    spec = ModelSpec(args.provider, args.model, job="decision")
    if not os.environ.get(spec.variable):
        print(f"skip: {spec.variable} is not set")
        return 0
    if args.onward:
        return onward(args.onward, spec)
    started = time.monotonic()
    kept = _Exact(HttpDecider(spec))
    report = evals.run(None, evals.load(args.cases), decider=kept, model=spec,
                       ledger=Ledger(MemoryStore(), Caps(evaluation=None)), cap_usd=None,
                       workers=max(1, args.workers))
    for line in evals.lines(report):
        print(line)
    print(f"took {time.monotonic() - started:.1f}s")
    asked = {c.id: _asked(c.sentence) for c in evals.load(args.cases).cases}
    print("per case:")
    for s in report.scored:
        said = "; ".join(kept.said(asked[s.id]))
        print(f"case {s.id} {s.group} {s.label}: {s.outcome}{' RIGHT' if s.score else ' WRONG'}"
              + (f"; judge: {said}" if said else ""))
    wrong = [s.id for s in report.scored if not s.score]
    print(f"spent ${report.usd:.4f} over {report.calls} decision calls")
    print("PASS" if not wrong else f"FAIL: {len(wrong)} against their label: {' '.join(wrong)}")
    return 1 if wrong else 0


def onward(path: Path, spec: ModelSpec) -> int:
    """Each case's sentence grounded against the articles and facts it cites, on the real judge, one case at a time."""
    suite = evals.load(path)
    articles = {a["id"]: a["text"] for a in yaml.safe_load(path.read_text(encoding="utf-8"))["articles"]}
    agent = Agent(id=evals.GROUND_AGENT[0], path=Path(f"agents/{evals.GROUND_AGENT[0]}.md"), role="writer",
                  target=evals.GROUND_AGENT[1], prompt=suite.labels)
    citable = {id: Citation("fact", id, f.record, grounding.quote_for(f)) for id, f in suite.facts.items()}
    citable |= {id: Citation("article", id, id) for id in articles}
    spent, wrong = 0.0, []
    for case in suite.cases:
        decider = _Exact(HttpDecider(spec))
        got = grounding.ground(agent, {"Case": case.sentence}, citable, decider=decider, facts=suite.facts,
                               texts=articles, today=suite.today, context=suite.context)
        spent += cost(spec.provider, spec.model, got.usage)
        landed = got.parts.get("Case", "").strip() == case.sentence.strip()
        right = landed is (case.label == evals.SUPPORTED)
        wrong += [] if right else [case.id]
        said = [a for answers in decider.kept.values() for a in answers]
        print(f"case {case.id} {case.label}: {'landed' if landed else 'dropped'}{' RIGHT' if right else ' WRONG'}; "
              f"judge: {'; '.join(said)}")
    print(f"spent ${spent:.4f}")
    print("PASS" if not wrong else f"FAIL: {len(wrong)} against their label: {' '.join(wrong)}")
    return 1 if wrong else 0


def _asked(sentence: str) -> str:
    """A case's one item as the judge is shown it, by which its answers are found: its sentence and the facts it
    cites."""
    root, _ = grounding._read_data(sentence.split("\n")[1:-1], 0)
    items = grounding._data_items(root)
    if not isinstance(items, list) or not items:
        return sentence
    item = items[0]
    cites = " ".join(f"[^{id}]" for id in item.cites)
    claim = f"{item.text.rstrip(' .')} {cites}."
    guess = mark if isinstance(mark := grounding.presumed(item.value, item.key is not None), list) else []
    if mark:
        claim, _ = grounding._unmarked(item, guess, cites)
    return _key(grounding._uncited(claim), item.cites, grounding._aside(item, guess))


def _key(sentence: str, cites: list, aside: dict | None = None) -> str:
    return json.dumps([sentence, sorted(set(cites)), aside or {}], sort_keys=True)


class _Exact(Decider):
    """The decision model, keeping each item's answers by what the judge was shown of it, exactly: two cases whose
    rest reads the same are told apart by what they cite and by their guess. An item's answers are those whose id
    carries its number, its guess's `n1` among them."""

    def __init__(self, inner: Decider):
        self.inner, self.lock = inner, threading.Lock()
        self.kept: dict[str, list[str]] = {}

    def decide(self, state, questions) -> Decision:
        decision = self.inner.decide(state, questions)
        with self.lock:
            asides = {s["id"][1:]: s["set_aside"] for s in state.get("sentences", []) if "set_aside" in s}
            for item in state.get("sentences", []):
                if not item.get("sentence"):
                    continue
                n = item["id"][1:]
                key = _key(item["sentence"], item.get("cited_facts", []), asides.get(n))
                for name, a in sorted(decision.answers.items()):
                    if a.choice and name[1:].split("c")[0] == n:
                        spread = ", ".join(f"{k} {v:.2f}" for k, v in sorted(a.probabilities.items()))
                        self.kept.setdefault(key, []).append(f"{name} {a.choice} {a.confidence or 0:.2f} ({spread})")
        return decision

    def said(self, key: str) -> list[str]:
        with self.lock:
            return list(self.kept.get(key, []))


if __name__ == "__main__":
    sys.exit(main())
