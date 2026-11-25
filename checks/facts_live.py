"""One live check of the facts layer: a short synthetic note, turned into facts by Factoid through real models. Not
part of the test suite: it calls real models.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/facts_live.py

It fails, and exits 1, on fewer than three facts, a fact that quotes nothing the note says, or no fact that holds the
launch's new date.

Factoid extracts on the workspace's default extraction model, embeds on Fireworks, and decides on Jev at Typesafe, the
one decision model Factoid calls today, so it needs a key for both providers. The workspace is a
temporary folder, so no call is replayed from an
earlier run. It prints each fact, the tokens the extraction reports, and Factoid's own count of calls per workload.
It never prints a key. With no Fireworks key set it says so and stops. When it fails, it still prints what Factoid
spent before the failure.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from cerebro.budget import Caps, Ledger
from cerebro.connector import Record
from cerebro.facts import ExtractionFailed, FactoidExtraction
from cerebro.fakes import MemoryStore
from cerebro.models import DEFAULTS, PROVIDERS, ModelSpec

MIN_FACTS = 3  # the note states a move, its reason, an owner and a budget

NOTE = Record(id="notes/2026-11-12-launch.md", source="notes", revision="1", at="2026-11-12T16:30:00Z", body=(
    "Weekly launch sync, 12 November 2026.\n\n"
    "Priya Raman said the Halcyon launch moves from 1 December to 8 December, because the payments review "
    "is not finished.\n\n"
    "Tomas Weber owns the payments review and expects to sign it off by 3 December.\n\n"
    "The launch budget stays at $40,000."))


def build(folder: Path, model: ModelSpec) -> FactoidExtraction:
    """The facts layer this check runs. Its keys come from this process's own environment through the reader, as the
    app's come through the workspace's: Factoid's provider never loads a key file of its own."""
    def key(provider: str) -> str:
        return os.environ.get(PROVIDERS[provider].variable, "") if provider in PROVIDERS else ""

    return FactoidExtraction(folder, model, key=key)


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY"):
        print("skip: FIREWORKS_API_KEY is not set")
        return 0
    model = DEFAULTS["extraction"]
    with tempfile.TemporaryDirectory() as folder:
        handler = build(Path(folder), model)
        started = time.monotonic()
        try:
            extracted = handler.extract(NOTE)
        except ExtractionFailed as error:
            print(f"fail: {error}")
            spent(handler)
            return 1
        finally:
            handler.close()
        seconds = time.monotonic() - started
        print(f"pass: {len(extracted.facts)} facts from {NOTE.id} in {seconds:.1f}s, generating on "
              f"{model.provider}/{model.model}")
        for fact in extracted.facts:
            print(f"- {fact.text}")
            print(f"    subject {fact.subject!r}, kind {fact.kind}, at {fact.at}, id {fact.id}")
            print(f"    mentions {[(m.text, m.role) for m in fact.mentions]}")
            for quote in fact.quotes:
                print(f"    quote {quote.text!r} speaker {quote.speaker!r} at {quote.at} side {quote.side!r}")
        ledger, now = Ledger(MemoryStore(), Caps()), datetime.now(UTC)
        ledger.record_extracted(now, model, extracted)
        print(f"usage: {extracted.input_tokens} in, {extracted.output_tokens} out, ${ledger.spent(now):.6f} as the "
              "ledger prices it")
        spent(handler)
    failures = problems(extracted.facts)
    for line in failures:
        print(f"FAIL: {line}")
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


def problems(facts: list) -> list[str]:
    """What is wrong with the facts of the note: too few, a fact with no quote from the note, or the moved date
    nowhere."""
    found = []
    if len(facts) < MIN_FACTS:
        found.append(f"{len(facts)} facts, and the note states at least {MIN_FACTS} things")
    words = " ".join(NOTE.body.split())
    for fact in facts:
        if not any(" ".join(q.text.split()).strip(" .\"") in words for q in fact.quotes):
            found.append(f"{fact.id} quotes nothing the note says: {fact.text!r}")
    if not any("8" in f.text and "December" in f.text for f in facts):
        found.append("no fact holds the launch's new date, 8 December")
    return found


def spent(handler: FactoidExtraction) -> None:
    """Factoid's own count of model calls and tokens, per workload."""
    for workload, row in sorted(handler.app.client.meter.rows.items()):
        print(f"  factoid {workload}: {int(row['calls'])} calls, {int(row['input_tokens'])} in, "
              f"{int(row['output_tokens'])} out")


if __name__ == "__main__":
    sys.exit(main())
