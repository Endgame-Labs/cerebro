"""One live check of a workspace's kinds of fact on real models: `cerebro init --template gtm` in a temporary folder,
three short synthetic notes about invented companies and people, each plainly holding a risk, a next step or a
decision, then one pass. Not part of the test suite: it calls real models and costs about two cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_kinds.py

Compilation's daily cap is set to a hundredth of a
cent, so the pass extracts and the cap holds back its context agents, at most one run starting, and extraction's
cap is ten cents. It prints the kinds the workspace defines, each fact
with its kind, the kind notices, and what the pass spent, then PASS, or FAIL with every reason. It never prints a key.
It passes only when:
- no part of the pass failed, a source that needs the Salesforce CLI among them, but an agent the compilation cap
  holds back;
- the template's kinds loaded, `risk_objection` among them, and no agent file filters on a kind the workspace lacks;
- the facts came back with more than one kind, every one of them a kind the workspace defines;
- at least one fact is a `risk_objection`;
- a `query` on facts filtered to `risk_objection` returns such facts and no other.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from pathlib import Path

from _no_sf import refuse_sf
from _paths import ran_lines

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.caller import Caller

NOTES = {
    "local/calls/2026-11-04-larkspur-review.md": """Account: Larkspur Freight
Attendees: Mira Castell <mira@larkspurfreight.example>, Joel Anand <joel@ourco.example>
Date: 2026-11-04

Mira Castell, head of finance at Larkspur Freight, said she is worried the rollout cost will exceed their budget,
and that this concern could stop the purchase.
""",
    "local/calls/2026-11-06-tannery-sync.md": """Account: Tannery Row Bakeries
Attendees: Owen Pike <owen@tanneryrow.example>, Joel Anand <joel@ourco.example>
Date: 2026-11-06

Owen Pike and Joel Anand agreed that the next step is a product demo for Tannery Row's store managers on
13 November.
""",
    "local/email/2026-11-09-quillfield-decision.md": """Account: Quillfield Labs
Attendees: Hana Sato <hana@quillfield.example>, Joel Anand <joel@ourco.example>
Date: 2026-11-09

Hana Sato wrote that Quillfield Labs has decided to sign the two-year contract, and that she approved it on
9 November.
""",
}
RISK = "risk_objection"


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    reasons: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # the workspace's secrets folder, outside the workspace
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, template="gtm", daily_usd=1, domains=["ourco.example"], environ=os.environ)
        settings = root / "settings.yaml"
        text = re.sub(r"budget:\n  daily_usd: .*\n", "", settings.read_text(encoding="utf-8"))
        settings.write_text(text + "budget:\n  extraction_daily_usd: 0.1\n  compilation_daily_usd: 0.0001\n",
                            encoding="utf-8")
        for rel, body in NOTES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding="utf-8")
        with Cerebro(root, trusted=True) as workspace:
            workspace.sf_run = refuse_sf
            defined = workspace.extraction.kinds()
            print(f"kinds: {', '.join(defined)}")
            if RISK not in defined:
                reasons.append(f"the template's kinds did not load: the workspace has {defined}")
            result = workspace.run_once()
            print(f"pass: listed {result.listed}, {result.read} records read, {result.extracted} facts, "
                  f"{len(result.runs)} runs")
            for line in ran_lines(result):
                print(line)
            for key, why in result.failed.items():
                if why.startswith("BudgetSpent:"):  # an agent the check's own compilation cap holds back
                    print(f"  held    {key}")
                    continue
                print(f"  failed  {key}: {why}")
                reasons.append(f"{key} failed: {why}")
            facts = workspace.extraction.facts()
            for fact in facts:
                print(f"- [{fact.kind}] {fact.text}")
            kinds = {f.kind for f in facts}
            if len(kinds) < 2:
                reasons.append(f"the facts came back with {len(kinds)} kind: {sorted(kinds)}")
            if kinds - set(defined):
                reasons.append(f"facts carry kinds the workspace does not define: {sorted(kinds - set(defined))}")
            if not any(f.kind == RISK for f in facts):
                reasons.append(f"no fact is a {RISK}")
            notices = workspace.class_notices()
            for line in notices:
                print(f"  notice {line}")
            if any("filters facts on" in line for line in notices):
                reasons.append("an agent file filters facts on a kind the workspace does not define")
            found = workspace.lookup.query(Caller("you", "person"), "facts", filters={"classes": RISK})
            got = [getattr(item, "kind", None) for item in found.items]
            print(f"query from facts, classes {RISK}: {len(got)} facts, of kinds {sorted(set(map(str, got)))}")
            if not got or any(kind != RISK for kind in got):
                reasons.append(f"a query filtered on {RISK} gave {got}")
            now = workspace.clock()
            print(f"spent: ${workspace.ledger.spent(now):.4f}")
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
