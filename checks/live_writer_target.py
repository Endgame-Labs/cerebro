"""Whether a writer whose evidence is a wide facts query writes about its own article, on real models. Not part of
the test suite: it calls real models and costs some cents, under a daily cap of $1 a trial.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_writer_target.py [--trials 2]

Each trial makes a blank workspace with four things, one record each whose facts all name it, and one writer for
the collection whose only evidence is every fact of the workspace, so the evidence alone does not say which thing
an article is. The facts come from the stand-in facts layer, one a paragraph, so the writer and grounding are the
only models. It runs one pass, which writes the four articles. An article is right when every fact it cites comes
from its own thing's record and its text names no other thing. It prints each article, what it spent, and
`right <n> of <m>`. It exits 1 when any article is wrong or was not written, else 0. It never prints a key.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

from cerebro import Record, scaffold
from cerebro.app import Cerebro
from cerebro.article import Identity, body
from cerebro.evidence import CITE
from cerebro.fakes import FakeConnector, FakeExtraction

THINGS = {  # record id: the thing's name and what its record says, one fact a paragraph
    "kiln": ("North Kiln", ["North Kiln fires glazed tiles on Mondays.", "North Kiln's chimney cracked on 9 November.",
                            "North Kiln is kept by the evening crew."]),
    "yard": ("Fennel Yard", ["Fennel Yard stores the spare ladders.", "Fennel Yard flooded on 12 November.",
                             "Fennel Yard is kept by the morning crew."]),
    "annex": ("Slate Annex", ["Slate Annex holds the paint store.", "Slate Annex lost power on 14 November.",
                              "Slate Annex is kept by the weekend crew."]),
    "pier": ("Moss Pier", ["Moss Pier takes the ferry at dusk.", "Moss Pier's railing was mended on 16 November.",
                           "Moss Pier is kept by the night crew."]),
}
WRITER = """---
evidence:
  - query: { from: facts, limit: 200 }
heartbeat: [change]
---

# Instructions

Describe the thing: what it is used for, who keeps it, and what happened there lately. Cite every sentence.

# Output

## About
"""


def path(record: str) -> str:
    return "things/" + THINGS[record][0].lower().replace(" ", "-")


def trial(folder: Path) -> tuple[int, int, float]:
    root = folder / "workspace"
    scaffold.init(root, daily_usd=1, timezone="UTC", environ=os.environ)
    (root / "agents" / "things").mkdir(parents=True)
    (root / "agents" / "things" / "article.md").write_text(WRITER)
    records = [Record(id=id, source="notes", revision="1", at=f"2026-11-{10 + n}T09:00:00Z", body="\n\n".join(said))
               for n, (id, (_, said)) in enumerate(THINGS.items())]
    notes = FakeConnector("notes", records)
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), connectors=[notes]) as workspace:
        for id, (name, _) in THINGS.items():
            workspace.store.create(Identity(path(id), "things", name))
        result = workspace.run_once()
        print(f"compiled {sorted(result.compiled)}, failed {result.failed}, ran and wrote nothing {result.ran}")
        record_of = {f.id: f.record for f in workspace.extraction.facts()}
        right = 0
        for id, (name, _) in THINGS.items():
            article = workspace.store.article(path(id))
            text = body(article) if article is not None else ""
            others = [n for i, (n, _) in THINGS.items() if i != id and n.lower() in text.lower()]
            strays = [c for c in dict.fromkeys(CITE.findall(text)) if record_of.get(c) != id]
            ok = bool(text.strip()) and not others and not strays
            right += ok
            said = (f", names {others}" if others else "") + (f", cites {len(strays)} of others" if strays else "")
            print(f"--- {path(id)}: {'right' if ok else 'WRONG'}{said}")
            print(re.sub(r"\n+", "\n", text.strip()) or "(nothing written)")
        return right, len(THINGS), workspace.ledger.spent(workspace.clock())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()
    if not (os.environ.get("FIREWORKS_API_KEY") and os.environ.get("TYPESAFE_API_KEY")):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    right = total = 0
    spent = 0.0
    for n in range(args.trials):
        with tempfile.TemporaryDirectory() as folder:
            os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
            os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
            print(f"== trial {n + 1}")
            got, of, usd = trial(Path(folder))
            right, total, spent = right + got, total + of, spent + usd
    print(f"spent ${spent:.4f}")
    print(f"right {right} of {total}")
    return 0 if right == total else 1


if __name__ == "__main__":
    sys.exit(main())
