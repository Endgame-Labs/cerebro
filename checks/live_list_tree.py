"""The labelled set of 300 made-up proposals for a research lab, arranged by the built call on real models: projects,
people, papers and datasets, ten pairs that are one thing under two names, ten near namesakes that are not, and the
people, papers and datasets that belong under a project. Not part of the test suite: it calls a real model and costs
some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_list_tree.py [--line "..."]

It prints how long the call took and what it cost, how many of the ten same-thing pairs it put together, how many of
the ten near namesakes it kept apart, and how many children sit under the right project, then PASS, or FAIL with
every reason: a reply or a group the checks refused, a same-thing pair apart, or near namesakes together. It never
prints a key.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.tree import Row, arrange, tree

CASES = Path(__file__).resolve().parent / "tree_cases.json"


def rows(cases: dict) -> list[Row]:
    return [Row(r["id"], r["by"], r["target"].partition("/")[0], r["payload"]["name"], r["reason"],
                keys=r["payload"].get("keys") or {}, path=r["target"],
                linked=(r["payload"].get("fields") or {}).get("linked") or [])
            for r in cases["rows"]]


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY"):
        print("skip: set FIREWORKS_API_KEY")
        return 0
    line = sys.argv[sys.argv.index("--line") + 1] if "--line" in sys.argv else ""
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    truth = cases["truth"]
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, template="gtm", daily_usd=3, domains=["lab.example"], environ=os.environ)
        with Cerebro(root, trusted=True) as workspace:
            began = time.monotonic()
            groups, note = arrange(workspace, rows(cases), line)
            seconds = time.monotonic() - began
            usd = workspace.ledger.spent(workspace.clock())
    tops = tree(groups, {}, {})
    group_of = {row.id: n for n, g in enumerate(groups) for row in g.rows}
    top_of = {row.id: top for top in tops for g in (top, *top.under) for row in g.rows}
    ids_of: dict[str, list[str]] = {}
    for id, thing in truth["thing"].items():
        ids_of.setdefault(thing, []).append(id)
    name_thing = {r["payload"]["name"]: truth["thing"][r["id"]] for r in cases["rows"]}
    same = sum(len({group_of[i] for i in ids_of[t]}) == 1 for t, *_ in truth["same"])
    near = sum(group_of[ids_of[t][0]] != group_of[ids_of[name_thing[other]][0]] for t, _, other in truth["near"])
    lead_thing = {id: truth["thing"][top.lead.id] for id, top in top_of.items() if top.lead is not None}
    placed = sum(lead_thing.get(id) == project for id, project in truth["project"].items())
    print(f"arranged  {len(groups)} groups of {len(cases['rows'])} proposals in {seconds:.0f}s for ${usd:.4f}")
    print(f"same      {same} of {len(truth['same'])} same-thing pairs together")
    print(f"near      {near} of {len(truth['near'])} near namesakes kept apart")
    print(f"placed    {placed} of {len(truth['project'])} under the right project")
    print(f"ticked    {sum(g.tick for g in groups)}; top {[t.name for t in tops[:5]]}")
    reasons = [note] if note else []
    if same < len(truth["same"]):
        reasons.append(f"{len(truth['same']) - same} same-thing pairs were left apart")
    if near < len(truth["near"]):
        reasons.append(f"{len(truth['near']) - near} near namesakes were put together")
    print("\n".join(["FAIL", *(f"- {r}" for r in reasons)]) if reasons else "PASS")
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
