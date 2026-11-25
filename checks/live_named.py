"""How a curator answers proposals another curator filed on its collection, on real models: whether it names the ones
its act answers, names a wrong one, rejects a wrong thing or leaves a proposal alone. Not part of the test suite: it
calls real models and costs some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_named.py [--trials 8]
    uv run python checks/live_named.py --fake

The cases are in `named_cases.json` beside it: for each,
a blank workspace with two curators, the case's articles, and proposals the other curator filed on the case's
collection, each labelled with the thing it asks for, or none when it asks for a wrong thing. Each trial runs the
case's curator once on its daily wake. For each planted proposal it says what the run did: `named` by an act,
`rejected`, or `left`. A wrong id is a proposal named by an act of another thing, or a wrong thing named at all. It
prints every act, then per case how often each was named, rejected or left, how often two proposals of one thing
were named by one act or split across two, the wrong ids, what it spent, and `score <share>`: the share of planted
proposals answered as labelled, a right thing named and a wrong thing rejected or left. It exits 1 on any wrong id
or when the setup did not hold, else 0, whatever the score. It never prints a key."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.curate import proposing
from cerebro.records import Proposal, proposals, propose

CASES = Path(__file__).resolve().parent / "named_cases.json"
CURATOR = ("---\nauthority: { admit: true, archive: false, merge: false, split: false }\nevidence:\n  - held: {}\n"
           "heartbeat: [daily]\n---\n\n")


def workspace(root: Path, case: dict[str, Any], fake: bool) -> Cerebro:
    """A blank workspace with the case's two curators past their first runs, its articles and planted proposals."""
    scaffold.init(root, daily_usd=1, environ=os.environ)
    other = case["filer"].partition("/")[0]
    for collection, prompt in ((case["curator"], case["prompt"]), (other, "Keep what the workshop records.")):
        (root / "agents" / collection).mkdir(parents=True, exist_ok=True)
        (root / "agents" / collection / "curator.md").write_text(
            CURATOR.replace("admit: true", f"admit: {str(case['admit']).lower()}") + prompt + "\n")
    if fake:
        from cerebro.fakes import FakeExtraction, ScriptedRun

        found = Cerebro(root, agent_run=ScriptedRun(lambda p: {}), extraction=FakeExtraction(), trusted=True)
    else:
        found = Cerebro(root, trusted=True)
    at = datetime.now(UTC).isoformat(timespec="seconds")
    for article in case["articles"]:
        found.store.create(Identity(article["id"], article["id"].partition("/")[0], article["title"], article["keys"],
                                    "active"))
    for collection in (case["curator"], other):  # a decided row of its own: neither is in its first run
        propose(found.store, Proposal(f"p_seed_{collection}", "article.link", case["articles"][0]["id"],
                                      Caller(f"{collection}/curator", "agent"), at, status="accepted",
                                      decided_by=Caller("you", "person"), decided_at=at))
    for p in case["planted"]:
        propose(found.store, Proposal(p["id"], "article.admit", f"{case['curator']}/{p['id'][2:].replace('_', '-')}",
                                      Caller(case["filer"], "agent"), at,
                                      payload={"name": p["name"], "keys": p["keys"]}, reason=p["reason"]))
    return found


def of(act: Proposal, planted: list[dict[str, Any]]) -> str | None:
    """The thing an act is of, by a key value or the name it shares with a planted proposal, or None."""
    values = {str(v).lower() for vs in (act.payload.get("keys") or {}).values() if isinstance(vs, list) for v in vs}
    name = str(act.payload.get("name") or act.target.rpartition("/")[2]).lower()
    for p in planted:
        if values & {v.lower() for vs in p["keys"].values() for v in vs} or name == p["name"].lower():
            return p["thing"]
    return None


def trial(case: dict[str, Any], n: int, fake: bool, said: Counter[str], wrong: list[str]) -> float:
    curator = f"{case['curator']}/curator"
    with tempfile.TemporaryDirectory() as folder, workspace(Path(folder) / "ws", case, fake) as ws:
        if proposing(ws.store, curator):
            raise SystemExit(f"setup: {curator} runs in proposal mode")
        if fake:  # the stand-in names the two proposals of the first thing and rejects the wrong thing
            first, second = (p for p in case["planted"] if p["thing"] == case["planted"][0]["thing"])
            bad = next(p for p in case["planted"] if p["thing"] is None)
            ws.agent_run.calls = [("admit", {"name": first["name"], "keys": first["keys"], "reason": "Stocked.",
                                             "proposals": [first["id"], second["id"]]}),
                                  ("reject", {"proposals": [{"id": bad["id"], "reason": "Not one of ours."}]})]
        ws.curate(ws.agent(curator), wake="daily")
        every = {p.id: p for p in proposals(ws.store)}
        acts = [p for p in every.values() if p.by.name == curator and not p.id.startswith("p_seed")]
        for act in acts:
            print(f"   trial {n}: {act.kind} {act.target} {act.status}, names {act.cites}: {act.reason[:120]}")
        for p in case["planted"]:
            by = next((a for a in acts if p["id"] in a.cites), None)
            status = every[p["id"]].status
            seen = "named" if by else "rejected" if status == "rejected" else "left" if status == "waiting" else status
            said[f"{p['id']} {seen}"] += 1
            said["as labelled"] += (seen == "named") == (p["thing"] is not None)
            if by is not None and (p["thing"] is None or of(by, case["planted"]) != p["thing"]):
                wrong.append(f"trial {n}: {p['id']} ({p['name']}) named by {by.kind} {by.target} {by.payload}")
        twins = [p["id"] for p in case["planted"] if p["thing"] == case["planted"][0]["thing"]]
        named_by = {next((a.id for a in acts if id in a.cites), None) for id in twins}
        said["two names of one thing named by one act"] += len(named_by) == 1 and None not in named_by
        said["two names of one thing split across two acts"] += len(named_by - {None}) > 1
        return ws.ledger.spent(ws.clock())


def main() -> int:
    fake = "--fake" in sys.argv
    if not fake and not os.environ.get("FIREWORKS_API_KEY"):
        print("skip: set FIREWORKS_API_KEY")
        return 0
    trials = int(sys.argv[sys.argv.index("--trials") + 1]) if "--trials" in sys.argv else (1 if fake else 8)
    home = tempfile.mkdtemp(prefix="live-named-")
    os.environ["XDG_CONFIG_HOME"] = str(Path(home) / "config")  # its own, never the person's config folder
    os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
    usd, answered, planted, wrong = 0.0, 0, 0, []
    for case in json.loads(CASES.read_text(encoding="utf-8"))["cases"]:
        said: Counter[str] = Counter()
        print(f"-- {case['curator']}/curator, admit: {str(case['admit']).lower()}")
        for n in range(1, trials + 1):
            usd += trial(case, n, fake, said, wrong)
        for key in sorted(k for k in said if k.startswith("p_")):
            print(f"   {key}: {said[key]} of {trials}")
        for key in ("two names of one thing named by one act", "two names of one thing split across two acts"):
            print(f"   {key}: {said[key]} of {trials}")
        answered, planted = answered + said["as labelled"], planted + trials * len(case["planted"])
    for line in wrong:
        print(f"wrong id  {line}")
    print(f"spent     ${usd:.4f}")
    print(f"score {answered / planted:.2f}")
    print("\n".join(["FAIL", *(f"- {line}" for line in wrong)]) if wrong else "PASS")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
