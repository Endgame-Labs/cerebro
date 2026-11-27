"""One live check of the decision client: a single tiny call to Rune on Fireworks. Not part of the test suite.

    FIREWORKS_API_KEY=... uv run python checks/decision.py

It asks one yes or no, one choice and one score in one request, and prints what came back: the model that answered,
each answer, and the tokens. If Fireworks does not know Rune by its router id, it asks once more by the id the model
list shows, `accounts/fireworks/models/rune`. It fails, and exits 1, unless the answers are the note's: yes, good,
and two years or more. It never prints a key. With no key set it says so and stops.
"""

from __future__ import annotations

import os
import sys

from cerebro.models import Choice, DecisionError, HttpDecider, ModelSpec, Score, YesNo, cost

STATE = {"note": "Acme renewed its contract for two more years on 3 November 2026."}
QUESTIONS = {
    "renewed": YesNo("Does the note say Acme renewed its contract?"),
    "tone": Choice.of("What is the tone of the note?", {"good": "Good news", "bad": "Bad news"}),
    "length": Score.of("How long is the renewal term?", ["under a year", "one year", "two years or more"]),
}


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY"):
        print("skip: FIREWORKS_API_KEY is not set")
        return 0
    for model in ("rune", "accounts/fireworks/models/rune"):
        spec = ModelSpec("fireworks", model, job="decision", retries=0)
        try:
            decision = HttpDecider(spec).decide(STATE, QUESTIONS)
        except DecisionError as error:
            print(f"fail: {spec.wire_id}: {error}")
            continue
        print(f"asked {spec.wire_id}, answered by {decision.model}")
        for name, answer in decision.answers.items():
            print(f"  {name}: {answer}")
        usd = cost("fireworks", "rune", decision.usage)
        print(f"  usage: {decision.usage.input_tokens} in, {decision.usage.output_tokens} out, ${usd:.8f} at Rune's price")
        wrong = wrong_answers(decision.answers)
        for line in wrong:
            print(f"FAIL: {line}")
        print("PASS" if not wrong else "FAIL")
        return 1 if wrong else 0
    return 1


def wrong_answers(answers: dict) -> list[str]:
    """Each answer that is not the one the note gives: Acme renewed, it is good news, and the term is two years."""
    renewed, tone, length = answers.get("renewed"), answers.get("tone"), answers.get("length")
    found = []
    if renewed is None or renewed.yes is None or renewed.yes < 0.5:
        found.append(f"renewed: want yes, got {renewed}")
    if tone is None or tone.choice != "good":
        found.append(f"tone: want good, got {tone}")
    if length is None or length.score is None or length.score < 1.5:
        found.append(f"length: want two years or more, level 2, got {length}")
    return found


if __name__ == "__main__":
    sys.exit(main())
