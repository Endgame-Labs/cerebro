"""One live check of the accounts curator's calls group on real models: a Gong stand-in holds thirty calls of the
last month, three with each of ten companies, both sides speaking, and the person took part in one. The group "from
calls you attended" may hold only the account of that call. Not part of the test suite: it calls real models and costs some cents a trial.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_gong_attended.py
    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_gong_attended.py --trials 3
    uv run python checks/live_gong_attended.py --fake

Each trial makes a workspace with `cerebro init
--template gtm --email ann@ourco.example`, keeps its accounts curator alone and no source on disk, hands it the Gong
stand-in, and runs discovery only. It prints each account the curator proposed with its group and reason, then
PASS, or FAIL with every reason: an account in the calls group whose calls the person was not on, or the attended
call's account missing from it in more than half the trials.

`--fake` runs the same steps on the stand-ins, with an accounts curator scripted to ask what its prompt says and admit
each row it gets under that group. A test runs it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from cerebro.app import Cerebro

YOU, NAME, OWN = "ann@ourco.example", "Ann Lee", "ourco.example"
COLLEAGUE = "bo@ourco.example"
NOW = datetime.now(UTC).replace(microsecond=0)
ATTENDED = "cobaltmill.example"  # the one call the person took part in
OTHERS = ["harrowgate.example", "mossbank.example", "tinlark.example", "quarrystone.example", "fernwick.example",
          "brindlecove.example", "ashgrove.example", "pellmark.example", "duneford.example"]
GROUP = "from calls you attended in the last 90 days (gong)"


def calls() -> list[dict[str, Any]]:
    """Three calls of the last month with each of ten outside companies, both sides speaking, a colleague on each and
    Gong tying each to its Salesforce account, so every company clears the bar of a working relationship: the person
    is on one call alone, the first company's."""
    out = []
    for n, domain in enumerate([ATTENDED, *OTHERS]):
        for k in range(3):
            ours = [COLLEAGUE, *([YOU] if domain == ATTENDED and k == 0 else [])]
            started = (NOW - timedelta(days=2 + 3 * k + n)).isoformat().replace("+00:00", "Z")
            out.append({"metaData": {"id": f"{7100000000000000000 + 10 * n + k}", "started": started,
                                     "title": "Weekly call"},
                        "context": [{"system": "Salesforce", "objects": [
                            {"objectType": "Account", "objectId": f"001SAMPLE{n:06d}",
                             "fields": [{"name": "Name", "value": domain.split(".")[0].title()}]}]}],
                        "parties": [{"name": "Them", "emailAddress": f"lead@{domain}", "speakerId": f"{n}{k}1"},
                                    *({"name": "Us", "emailAddress": a, "speakerId": f"{n}{k}{i + 2}"}
                                      for i, a in enumerate(ours))]})
    return out


def gong(root: Path) -> Any:
    """A Gong source over the ten calls, read through Gong's own reply shapes, its key a dummy."""
    from cerebro.connector import Ours
    from cerebro.connectors.gong import GongConnector, GongKey
    from cerebro.source import SourceFile, Window

    held = calls()

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path != "/v2/calls/extensive":
            return httpx.Response(404, json={"errors": ["No calls found corresponding to the provided filters"]})
        wanted = body.get("filter", {})
        found = [c for c in held if (not wanted.get("callIds") or c["metaData"]["id"] in wanted["callIds"])
                 and c["metaData"]["started"] >= wanted.get("fromDateTime", "")]
        if not found:
            return httpx.Response(404, json={"errors": ["No calls found corresponding to the provided filters"]})
        return httpx.Response(200, json={"records": {"totalRecords": len(found)}, "calls": found})

    source = SourceFile(name="gong", kind="gong", auth="api_key", window=Window(behind="90d"))
    return GongConnector(source, http=httpx.Client(transport=httpx.MockTransport(handle)),
                         credentials=lambda: GongKey("key", "secret"),
                         ours=Ours(addresses=(YOU,), domains=(OWN,)))


def made(root: Path, fake: bool) -> None:
    from cerebro import cli

    args = ["init", str(root), "--email", YOU, "--name", NAME, "--daily-usd", "3", "--timezone", "UTC",
            *(() if fake else ("--template", "gtm"))]
    cli.main(args, standalone_mode=False)
    for curator in (root / "agents").glob("*/curator.md"):
        if curator.parent.name != "accounts":
            curator.unlink()
    for source in (root / "sources").glob("*.yaml"):
        source.unlink()


def stand_in(root: Path) -> Cerebro:
    """The blank workspace on the stand-ins, with an accounts curator that asks Gong for the calls the person took
    part in, as the gtm curator's prompt says, and admits each row under the calls group."""
    from cerebro.fakes import FakeExtraction, ScriptedRun
    from cerebro.run import AgentRun

    class Scripted(AgentRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            by_name = {tool.__name__: tool for tool in tools}
            rows = by_name["fetch"](source="gong", question="calls_by_domain", params={"took_part": "me"})
            for row in (rows or {}).get("rows", []):
                by_name["admit"](name=row["id"].split(".")[0].title(), keys=row["keys"],
                                 reason=f"{row['fields'].get('took_part')} call you took part in", group=GROUP)
            return replace(ScriptedRun(lambda p: {}).run(agent, evidence, [], budget), id=f"run_{uuid.uuid4().hex[:8]}")

    folder = root / "agents" / "accounts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "curator.md").write_text("---\nauthority: { admit: true, archive: true, merge: true, split: true }\nheartbeat: [daily]\n---\n\nKeep the accounts.\n")
    return Cerebro(root, agent_run=Scripted(), extraction=FakeExtraction(), connectors=[gong(root)], trusted=True)


def trial(root: Path, fake: bool) -> dict[str, Any]:
    from cerebro.first_run import discover
    from cerebro.records import proposals

    made(root, fake)
    with stand_in(root) if fake else Cerebro(root, trusted=True, connectors=[gong(root)]) as workspace:
        discover(workspace, say=print)
        filed = [p for p in proposals(workspace.store) if p.by.name == "accounts/curator" and p.kind == "article.admit"]
        spent = workspace.ledger.spent(workspace.clock())
    rows = [{"target": p.target, "keys": p.payload.get("keys") or {}, "reason": p.reason} for p in filed]
    return {"rows": rows, "spent": spent}


def domains(row: dict[str, Any]) -> list[str]:
    return [str(d).lower() for d in row["keys"].get("domain", [])] + [row["target"].rpartition("/")[2]]


def main() -> int:
    fake = "--fake" in sys.argv
    trials = int(sys.argv[sys.argv.index("--trials") + 1]) if "--trials" in sys.argv else 1
    if not fake and (not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY")):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY, or run "
              "with --fake on the stand-ins")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    runs = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ.setdefault("XDG_CONFIG_HOME", str(Path(folder) / "config"))
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        for n in range(trials):
            print(f"\n=== trial {n + 1} of {trials}")
            runs.append(trial(Path(folder) / f"wiki{n}", fake))
    reasons, found = [], 0
    for n, run in enumerate(runs, 1):
        print(f"trial {n}: spent ${run['spent']:.4f}")
        grouped = [row for row in run["rows"] if "calls you attended" in row["reason"]]  # the group rides the reason
        for row in run["rows"]:
            print(f"  {row['target']}: {row['reason']}")
        wrong = [row["target"] for row in grouped if not any(ATTENDED.split(".")[0] in d for d in domains(row))]
        if wrong:
            reasons.append(f"trial {n}: in the calls group from calls the person was not on: {wrong}")
        found += any(any(ATTENDED.split(".")[0] in d for d in domains(row)) for row in grouped)
        print(f"  calls group: {len(grouped)} accounts, {len(wrong)} from calls the person was not on")
    if found * 2 < len(runs):
        reasons.append(f"the attended call's account was in the calls group in {found} of {len(runs)} trials")
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    from spend import watch

    watch()
    sys.exit(main())
