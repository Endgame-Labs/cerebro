"""One live run of a `sql` evidence line on real models: a blank workspace in a temporary folder with a daily cap of $1,
a small SQLite file of invented deals by region under `data/`, a `database` source over it, and one agent whose only
evidence is a `sql` line that sums the amounts by region. Not part of the test suite: it calls real models and costs
about two cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_sql.py

It runs a pass, a second pass with the database unchanged, then changes one amount and runs a third. It prints what
each pass did, the article as a person reads it, what grounding dropped, what it cost, then PASS, or FAIL with every
reason. It never prints a key. It passes only when the article states each region's total as the query gave it, every
sentence cites a row of the query, the second pass runs nothing, and the third writes the new total.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from live_gtm import sentences

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import passages
from cerebro.evidence import CITE
from cerebro.runner import PassResult

DEALS = [("North", 12500, "open"), ("North", 4000, "won"), ("South", 30000, "open"), ("East", 7250, "open"),
         ("East", 1750, "lost"), ("West", 18000, "won")]
QUERY = "select region, sum(amount) as total from deals group by region order by region"
SOURCE = "kind: database\nurl: sqlite:data/deals.sqlite\n"
AGENT = f"""---
evidence:
  - sql: {{ source: warehouse, query: "{QUERY}" }}
heartbeat: [change]
---

# Instructions

Write the total deal amount of each region, one sentence for each region, in the order the rows give them. State
each total as its row gives it. Each sentence cites the row it rests on.

# Output

## Totals by region
"""


def totals(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        return dict(db.execute("select region, sum(amount) from deals group by region").fetchall())


def show(n: int, result: PassResult, seconds: float) -> None:
    print(f"pass {n} ({seconds:.0f}s): compiled {result.compiled}, {len(result.runs)} runs, failed {result.failed}, ran and wrote nothing {result.ran}")
    for line in result.warnings:
        print(f"  warning {line[:400]}")


def written(workspace: Cerebro, reasons: list[str], want: dict[str, int]) -> str:
    article = workspace.store.article("reports/regions")
    text = "\n\n".join(f"## {s.heading}\n{s.text}" for s in (passages(article) if article else []))
    print(f"--- reports/regions ---\n{text or '(nothing written)'}\n---")
    for sentence in sentences(text):
        cited = CITE.findall(sentence)
        if not cited or not all(c.startswith("sql:warehouse:") for c in cited):
            reasons.append(f"a sentence cites no row of the query: {sentence[:160]!r}")
    plain = text.replace(",", "")
    for region, total in want.items():
        if not re.search(rf"{region}\b[^\n]*?\b{total}\b", plain):
            reasons.append(f"{region}'s total {total} is not stated as the query gave it")
    return text


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    reasons: list[str] = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, daily_usd=1, timezone="UTC", domains=["ourco.example"], environ=os.environ)
        (root / "data").mkdir()
        db_path = root / "data" / "deals.sqlite"
        with sqlite3.connect(db_path) as db:
            db.execute("create table deals (region text, amount integer, stage text)")
            db.executemany("insert into deals values (?, ?, ?)", DEALS)
        db.close()
        (root / "sources" / "warehouse.yaml").write_text(SOURCE, encoding="utf-8")
        (root / "agents" / "reports").mkdir(parents=True)
        (root / "agents" / "reports" / "regions.md").write_text(AGENT, encoding="utf-8")
        now = [datetime.now(UTC)]  # Cerebro's clock, moved past the agent's cooldown before the database changes
        with Cerebro(root, trusted=True, clock=lambda: now[0]) as workspace:
            print(f"agents: {len(workspace.agents)} loaded, refused {workspace.refused}")
            for n, change in ((1, None), (2, None), (3, ("East", 9000))):
                if change:
                    now[0] += timedelta(hours=5)  # an agent with a sql line waits four hours between runs by default
                    print("clock: five hours on, past the agent's default cooldown")
                    with sqlite3.connect(db_path) as db:
                        db.execute("update deals set amount = ? where region = ? and stage = 'open'", change[::-1])
                    db.close()
                    print(f"changed: {change[0]}'s open deal is now {change[1]}")
                began = time.monotonic()
                result = workspace.run_once()
                show(n, result, time.monotonic() - began)
                if n == 2 and (result.runs or result.compiled):
                    reasons.append("the second pass ran something with the database unchanged")
                if n != 2:
                    if "reports/regions" not in result.compiled:
                        reasons.append(f"pass {n} did not write reports/regions")
                    written(workspace, reasons, totals(db_path))
            now = workspace.clock()
            print(f"spent: ${workspace.ledger.spent(now):.4f} (compilation ${workspace.ledger.spent(now, 'compilation'):.4f}),"
                  f" {time.monotonic() - started:.0f}s")
    if reasons:
        print("FAIL")
        for reason in dict.fromkeys(reasons):
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
