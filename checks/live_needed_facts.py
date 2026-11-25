"""One live check of whether the facts a record holds come out of the facts layer, on real models: three short
made-up notes, each turned into facts in a fresh workspace by `cerebro init --template gtm`, read by its files source
and handed to the workspace's own facts layer, as a pass hands a record. Not part of the test suite: it calls real
models and costs about a cent a trial.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_needed_facts.py

`--trials N` runs each note N times (10 by default),
`--case A` (or B, C, several times) runs only those notes, `--out <file>` writes each trial's facts and drops as one
JSON line, and `--again <file>` reports those lines again and runs nothing. It never prints a key.

The notes:
- A, a call note whose second sentence says a network upgrade slips to "January", which moves a go-live. The
  extractor tends to add a year and a risk the note does not state, and the facts layer then drops the fact as not
  grounded. It counts a trial as stored when a fact holds the slip and adds no year or risk.
- B, a call note of six sentences said by people at one company, named on the note's `Account:` header line. It
  counts the share of facts that name that company.
- C, a note of another kind, filed under a collection of its own: a field study's review,
  with a month given without its year and a consequence the speaker states. It counts the share of facts that name
  the study, named on its `Study:` header line, and is there to keep the engine's guidance general.

For every note it prints any sentence that gave no fact, and any fact that says more than its note: a year the note
does not give, or a risk, threat or judgment the note does not state. It prints Factoid's calls per record and what
they cost at the prices below, and the candidates the facts layer dropped. It exits 0 when it ran to the end.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from cerebro import scaffold
from cerebro.app import Cerebro

SOURCE = """kind: files
window: { behind: all }
select:
  - glob: "local/calls/**/*.md"
    extract: facts
    into: calls
  - glob: "local/studies/**/*.md"
    extract: facts
    into: studies
"""

# Each note: its path, its text, the words that name its subject, and each body sentence by words only it holds.
CASES = {
    "A": {"path": "local/calls/2026-11-17-novaline-status.md", "subject": r"novaline", "text": """Account: Novaline Health
Attendees: Grace Liu <grace@novaline.example>, Sam Okoye <sam@ourco.example>
Date: 2026-11-17

Grace Liu said the Tacoma clinic's staff training is done.

Grace Liu raised that the second clinic's network upgrade slips to January, which moves its go-live.
""", "sentences": {"training done": r"training", "slip to January": r"January"}},
    "B": {"path": "local/calls/2026-11-18-larkspur-discovery.md", "subject": r"larkspur", "text": """Account: Larkspur Freight
Attendees: Mara Quist <mara@larkspur.example>, Ines Calder <ines@larkspur.example>, Tobias Fenn <tobias@ourco.example>
Date: 2026-11-18

Mara Quist: Our depot team still matches carrier invoices by hand, about 30 hours a week.

Mara Quist: We want a pilot running at the Reno depot before the February peak.

Ines Calder: Finance has capped this year's spend on it at $55,000.

Ines Calder: Legal will need to review the data processing terms before anything is signed.

Tobias Fenn: I'll send the integration guide to Mara by 25 November.

Mara Quist: Larkspur's IT lead, Oren Vale, will join the technical review on 3 December.
""", "sentences": {"30 hours by hand": r"30 hours|by hand", "pilot at Reno": r"Reno|pilot",
                "$55,000 cap": r"55,000|55000", "legal review": r"legal|data processing",
                "integration guide": r"integration guide", "Oren Vale review": r"Oren Vale|technical review"}},
    "C": {"path": "local/studies/2026-11-19-fenwick-review.md", "subject": r"fenwick", "text": """Study: Fenwick Soil Trial
Attendees: Ana Brill <ana@fenwicklab.example>, Theo Marsh <theo@ourco.example>
Date: 2026-11-19

Ana Brill reported that the moisture sensors in the third plot failed during the October rains.

Ana Brill said the replacement sensors arrive in March, which pushes back the second sampling round.

Theo Marsh will share the calibration notes with Ana Brill by 28 November.

The grant covers sampling through June.
""", "sentences": {"sensors failed": r"fail", "sensors arrive in March": r"March",
                "calibration notes": r"calibration", "grant through June": r"June|grant"}},
}
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
# What a fact adds that its note does not say: a year after a month the note gives alone, or a risk or judgment.
ADDED = {"a year": rf"\b(?:{MONTHS})(?: \d{{1,2}})?,? 20\d\d", "a risk or judgment":
         r"\brisk|jeopard|threat|concern|\bblock|\bimpact|critical|significant|setback"}
# $ per million tokens, in and out, per Factoid workload: the extraction model as Cerebro prices it, Jev as
# Cerebro prices Typesafe, and embedding, which Cerebro does not price, at a conservative $0.10 in.
PRICE = {"extract": (0.15, 0.5), "repair": (0.15, 0.5), "situate": (0.15, 0.5), "choose": (0.042, 0.042),
         "embed": (0.10, 0.0)}


def added(text: str, note: str) -> list[str]:
    """What a fact says that its note does not: each kind of addition whose words the fact has and the note lacks."""
    return [what for what, rx in ADDED.items() if re.search(rx, text, re.IGNORECASE) and not re.search(rx, note, re.IGNORECASE)]


def stored_slip(text: str, note: str) -> bool:
    """Case A's fact: the slip to January, and nothing the note does not say."""
    return bool(re.search(r"January", text) and re.search(r"slip|network upgrade|delay", text, re.IGNORECASE)
                and not added(text, note))


def priced(meter: dict[str, dict[str, float]]) -> float:
    return sum((row["input_tokens"] * PRICE.get(w, (0.15, 0.5))[0] + row["output_tokens"] * PRICE.get(w, (0.15, 0.5))[1])
               / 1e6 for w, row in meter.items())


def trial(case: dict, folder: Path) -> dict:
    """One note in a fresh workspace: read by its files source, extracted by the workspace's facts layer."""
    root = folder / "workspace"
    scaffold.init(root, template="gtm", daily_usd=1, domains=["ourco.example"], environ=os.environ)
    (root / "sources" / "local.yaml").write_text(SOURCE, encoding="utf-8")
    note = root / case["path"]
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(case["text"], encoding="utf-8")
    with Cerebro(root, trusted=True) as workspace:
        connector = workspace.connectors["local"]
        record = connector.get(case["path"])
        started = time.monotonic()
        extracted = workspace.extraction.extract(record)
        seconds = time.monotonic() - started
        handler = getattr(workspace.extraction, "handler", workspace.extraction)
        meter = {k: dict(v) for k, v in handler.app.client.meter.rows.items()}
        dropped = getattr(workspace.extraction, "dropped", dict)()
    return {"facts": [f.text for f in extracted.facts], "seconds": round(seconds, 1), "meter": meter,
            "dropped": dropped, "body": record.body or ""}


def report(name: str, case: dict, rows: list[dict]) -> list[str]:
    ok = [r for r in rows if "error" not in r]
    lines = [f"case {name}: {len(ok)} of {len(rows)} trials ran"]
    for r in rows:
        if "error" in r:
            lines.append(f"  error: {r['error']}")
    if not ok:
        return lines
    body = ok[0]["body"]
    facts = [f for r in ok for f in r["facts"]]
    if name == "A":
        hit = sum(any(stored_slip(f, body) for f in r["facts"]) for r in ok)
        lines.append(f"  A stored {hit}/{len(ok)}")
    named = sum(bool(re.search(case["subject"], f, re.IGNORECASE)) for f in facts)
    lines.append(f"  {name} naming the subject {named}/{len(facts)} = {named / max(len(facts), 1):.0%}, "
                 f"{len(facts) / len(ok):.1f} facts a trial")
    for label, rx in case["sentences"].items():
        lost = sum(not any(re.search(rx, f, re.IGNORECASE) for f in r["facts"]) for r in ok)
        if lost:
            lines.append(f"  lost: the sentence on {label} gave no fact in {lost} of {len(ok)} trials")
    more = [(f, added(f, body)) for f in facts if added(f, body)]
    lines.append(f"  says more than its note: {len(more)} facts")
    for text, what in more:
        lines.append(f"    + {', '.join(what)}: {text}")
    calls: Counter = Counter()
    for r in ok:
        for w, row in r["meter"].items():
            calls[w] += row["calls"]
    cost = sum(priced(r["meter"]) for r in ok)
    lines.append(f"  calls a record: {', '.join(f'{w} {n / len(ok):.1f}' for w, n in sorted(calls.items()))}; "
                 f"${cost / len(ok):.4f} a record, ${cost:.4f} in all")
    drops: Counter = Counter()
    for r in ok:
        for row in r["dropped"].values():
            drops.update(row.get("why") or {})
            drops["candidates"] += row.get("candidates") or 0
    if drops:
        why = ", ".join(f"{k} {n}" for k, n in drops.most_common() if k != "candidates")
        lines.append(f"  dropped by the facts layer: {drops['candidates']} candidates in {len(ok)} trials: {why}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--case", action="append", choices=sorted(CASES))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--again", type=Path, help="report the trials an earlier --out wrote, and run none")
    args = parser.parse_args()
    if args.again:
        rows = [json.loads(line) for line in args.again.read_text(encoding="utf-8").splitlines() if line.strip()]
        for name in args.case or sorted(CASES):
            print("\n".join(report(name, CASES[name], [r for r in rows if r["case"] == name])))
        print(f"spent about ${sum(priced(r.get('meter', {})) for r in rows):.4f}")
        return 0
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    total = 0.0
    out: list[str] = []
    for name in args.case or sorted(CASES):
        case, rows = CASES[name], []
        for n in range(1, args.trials + 1):
            with tempfile.TemporaryDirectory() as folder:
                os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # secrets, outside the workspace
                os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
                try:
                    row = trial(case, Path(folder))
                except Exception as error:  # noqa: BLE001 - one failed trial is reported, and the rest go on
                    row = {"error": f"{type(error).__name__}: {error}"}
            rows.append(row)
            total += priced(row.get("meter", {}))
            print(f"{name} {n}: " + (row.get("error") or " | ".join(row["facts"])), flush=True)
            if args.out:
                with args.out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"case": name, "trial": n, **row}) + "\n")
        out += report(name, case, rows)
    print("\n".join(out))
    print(f"spent about ${total:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
