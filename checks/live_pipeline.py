"""One live run of the gtm template's pipeline report on real models, over the Salesforce stand-in org the connector's
tests use: its accounts Acme Corp and Initech and their four deals admitted, the org listed through the real
connector, the opportunities agent setting each deal's stage, amount, close date and owner from its facts, then the
report compiled. The curators are taken out. Not part of the test suite: it calls real models and costs a few cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_pipeline.py

It prints each deal's properties, the report as a reader sees it, and what grounding dropped, then PASS, or FAIL
when the report wrote nothing, a row names a deal the wiki does not hold, a line that links one deal gives a stage or
a close date other than the one the org gives the deal, no line links a deal, a row puts a deal under a part the org
says it is not in, or a part the org gives a deal has no heading. It never prints a key.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from _paths import ran_lines
from test_connector_salesforce import FakeOrg, make, seed

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import Identity, passages
from cerebro.caller import Caller

ARTICLES = [  # what the accounts and opportunities curators admit from the org, by its ids
    ("accounts/acme-corp", "Acme Corp", "001000000000001AAA"),
    ("accounts/initech", "Initech", "001000000000004AAA"),
    ("opportunities/acme-corp-renewal", "Acme Corp renewal", "006000000000001AAA"),
    ("opportunities/acme-corp-expansion", "Acme Corp expansion", "006000000000002AAA"),
    ("opportunities/acme-corp-pilot", "Acme Corp pilot", "006000000000003AAA"),
    ("opportunities/initech-platform", "Initech platform", "006000000000004AAA"),
]


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    reasons: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, template="gtm", daily_usd=1, domains=["ourco.example"], environ=os.environ)
        for path in (root / "agents").iterdir():  # the report and the deals' writer alone, so no curator spends
            if path.name == "opportunities":
                (path / "curator.md").unlink()
            elif path.name != "reports":
                shutil.rmtree(path)
        with Cerebro(root, trusted=True, connectors=[make(FakeOrg(seed()))]) as workspace:
            by = Caller("you", "person")
            for id, title, key in ARTICLES:
                workspace.store.create(Identity(id, id.split("/")[0], title, {"salesforce_id": [key]}, "active", by,
                                            "2026-11-01T00:00:00Z"))
            result = workspace.run_once()
            for line in ran_lines(result, ""):
                print(line)
            for key, why in result.failed.items():
                print(f"failed  {key}: {why}")
            for id, _, _ in ARTICLES[2:]:
                props = workspace.store.article(id).properties
                print(f"{id}: " + ", ".join(f"{k}={v.value}" for k, v in sorted(props.items())))
            report = workspace.store.article("reports/pipeline")
            shown = "\n".join(f"## {compilation.heading}\n{compilation.text}" for compilation in (passages(report) if report else []))
            print(f"\n=== reports/pipeline\n{shown}")
            print("\ndropped by grounding:")
            for line in result.warnings:
                print(f"  {line[:400]}")
            print(f"\nspent: ${workspace.ledger.spent(workspace.clock()):.4f}")
            if report is None or not report.compilations:
                reasons.append("the report wrote nothing")
            held = {id for id, _, _ in ARTICLES}
            text = "\n".join(s.text for s in report.compilations) if report else ""
            reasons += [f"a row links {path}, which the wiki does not hold" for path in
                        re.findall(r"\]\(\.\./([^)]+)\.md\)", text) if path not in held]
            org = {row["Id"]: row for row in seed()["Opportunity"]}
            deals = {id: {"stage": org[key]["StageName"], "close_date": org[key]["CloseDate"],
                          "closed": org[key]["IsClosed"]} for id, _, key in ARTICLES[2:]}
            reasons += mismatches(text, deals)
            reasons += misfiled(shown, deals, workspace.clock().date())
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


def mismatches(text: str, deals: dict) -> list[str]:
    """Each line that links one deal and gives a stage or a close date other than the org's for it, and a report that
    links no deal at all. A stage is any stage the org gives a deal. A close date is the cell under a table's Close column,
    or a date written as YYYY-MM-DD right after a word such as close or closes."""
    stages = {deal["stage"] for deal in deals.values()}
    found, checked, close_column = [], 0, None
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.lstrip().startswith("|") else []
        if cells and not any("](" in c for c in cells):
            names = [c.lower() for c in cells]
            close_column = next((i for i, c in enumerate(names) if c.startswith("close")), close_column)
        linked = [path for path in re.findall(r"\]\(\.\./([^)]+)\.md\)", line) if path in deals]
        if len(set(linked)) != 1:
            continue
        checked += 1
        own_stage, close = deals[linked[0]]["stage"], deals[linked[0]]["close_date"]
        for stage in stages - {own_stage}:
            if re.search(rf"\b{re.escape(stage)}\b", line) and not re.search(rf"\b{re.escape(own_stage)}\b", line):
                found.append(f"{linked[0]} is {own_stage}, and the report gives it as {stage}")
        given = re.findall(r"clos\w*\D{0,20}?(\d{4}-\d{2}-\d{2})", line, re.IGNORECASE)
        if cells and close_column is not None and close_column < len(cells):
            given += re.findall(r"\d{4}-\d{2}-\d{2}", cells[close_column])
        found += [f"{linked[0]} closes on {close}, and the report gives {date}" for date in given if date != close]
    if not checked:
        found.append("no line of the report links one deal, so no stage or close date could be checked")
    return found


def misfiled(shown: str, deals: dict, today: date) -> list[str]:
    """Each table row or list item of `shown` that puts a deal where the org says it does not belong: under no
    level-two heading, an open deal under Closed, a closed one under any other heading, or a deal whose close date is
    ahead under Past due; and each part the org gives a deal that has no heading: Closed for a deal closed this
    quarter, Past due for an open deal whose close date has passed, and Needs attention this week for any open deal."""
    found, heading = [], ""
    for line in shown.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip().casefold()
            continue
        if not line.lstrip().startswith(("|", "- ", "* ", "+ ")):
            continue
        for path in dict.fromkeys(re.findall(r"\]\(\.\./([^)]+)\.md\)", line)):
            if (deal := deals.get(path)) is None:
                continue
            closed, ahead = deal["closed"], date.fromisoformat(deal["close_date"]) >= today
            if not heading or (heading == "closed") != closed or (heading == "past due" and ahead):
                state = "closed" if closed else f"open, closing {deal['close_date']}"
                found.append(f"{path} is {state}, and the report lists it under {heading.capitalize() or 'no heading'}")
    quarter = (today.year, (today.month - 1) // 3)
    held = {line[3:].strip().casefold() for line in shown.splitlines() if line.startswith("## ")}
    for part, has in (
            ("Closed", lambda d, on: d["closed"] and (on.year, (on.month - 1) // 3) == quarter),
            ("Past due", lambda d, on: not d["closed"] and on < today),
            ("Needs attention this week", lambda d, on: not d["closed"])):
        if part.casefold() not in held and (named := [p for p, d in deals.items()
                                                      if has(d, date.fromisoformat(d["close_date"]))]):
            found.append(f"the report has no {part} heading, and the org puts {', '.join(named)} there")
    return found


if __name__ == "__main__":
    sys.exit(main())
