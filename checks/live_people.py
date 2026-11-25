"""One live check of whom the people curator proposes in a first run's discovery, on real models: an attendee whose
address is at an account's domain is placed at that account by the address and proposed; a bare name, and an
address at a domain no account has, wait for a fact; the workspace's own people are never proposed. Not part of the
test suite: it calls real models and costs some cents a trial.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_people.py
    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_people.py --trials 3
    uv run python checks/live_people.py --fake

Each trial makes a workspace with `cerebro init
--template gtm --email ann@ourco.example`, keeps its accounts and people curators alone so a trial costs little, writes
made-up notes for three accounts, and runs discovery only: nothing is approved or compiled, so no fact exists. The
people curator's first round reads the notes with the files source's `notes` question, as it does in a person's
first run.

It prints, across the trials, how many of the attendees placed by address were proposed (want all), how many bare
names and addresses at no account's domain were (want none), how many of the own side and how many at a personal
mailbox were (want none), and the reason the curator gave for each one it left out. It fails when the placed share
falls under `PLACED_FLOOR`, or when any one bare name, address at no account's domain, own-side person or personal
mailbox is proposed in more than `WRONG_CEILING` of the trials.

`--fake` runs the same steps and counts on the stand-ins, a blank workspace with a people curator scripted to follow
the rule. A test runs it.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from cerebro.app import Cerebro

YOU, NAME, OWN = "ann@ourco.example", "Ann Lee", "ourco.example"
PLACED = {"Nessa Torvik": "nessa@brightwater.example", "Priya Nair": "priya@brightwater.example",
          "Omar Haddad": "omar@kestrel.example", "Grace Liu": "grace@novaline.example"}
BARE = ["Lina Brooks", "Tom Reyes"]  # no address on any note
ELSEWHERE = {"Lee Park": "lee@harborconsult.example", "Mia Chen": "mia@fieldworks.example"}  # at no account's domain
PERSONAL = {"Jo Hart": "jo-hart-example@yahoo.com", "Ray Ito": "ray-ito-example@hotmail.com"}  # a personal mailbox names no account
OWN_SIDE = {"Ann Lee": YOU, "Sam Okoye": "sam@ourco.example", "Bo Lin": ""}  # Bo Lin: placed on our side in words
NOTES = {
    "local/calls/2026-11-03-brightwater-discovery.md": """Account: Brightwater Logistics
Attendees: Nessa Torvik <nessa@brightwater.example>, Mia Chen <mia@fieldworks.example>, Ann Lee <ann@ourco.example>
Date: 2026-11-03

Nessa Torvik, VP of Operations at Brightwater Logistics, said their warehouse team loses about six hours a week
reconciling shipment records by hand. Mia Chen, an outside advisor to Nessa's team, asked about the data export.
""",
    "local/calls/2026-11-14-brightwater-security.md": """Account: Brightwater Logistics
Attendees: Nessa Torvik <nessa@brightwater.example>, Priya Nair <priya@brightwater.example>, Sam Okoye <sam@ourco.example>
Date: 2026-11-14

Priya Nair, Brightwater's IT security lead, asked for single sign-on and a data retention policy.
Sam Okoye said the security questionnaire will be back to Priya by 21 November.
""",
    "local/calls/2026-11-05-kestrel-intro.md": """Account: Kestrel Foods
Attendees: Omar Haddad <omar@kestrel.example>, Lina Brooks, Lee Park <lee@harborconsult.example>, Bo Lin, Ray Ito <ray-ito-example@hotmail.com>
Date: 2026-11-05

Omar Haddad said Kestrel is comparing two vendors for invoice matching. Lee Park, a consultant Kestrel hired for the
selection, will score both. Ray Ito, a retired buyer Omar knows, sat in.
Bo Lin from our side will send a reference customer by 12 November.
""",
    "local/meetings/2026-11-07-novaline-kickoff.md": """Account: Novaline Health
Attendees: Grace Liu <grace@novaline.example>, Tom Reyes, Jo Hart <jo-hart-example@yahoo.com>, Sam Okoye <sam@ourco.example>
Date: 2026-11-07

Grace Liu, director of IT at Novaline Health, kicked off the rollout to their three clinics.
Tom Reyes asked for the training schedule. Jo Hart, a nurse who uses the product at another clinic, joined.
""",
}
PLACED_FLOOR = 0.3  # of the placed attendees proposed, across the trials: the curator may judge some under its bar
WRONG_CEILING = 0  # trials in which any one bare name, address elsewhere or own-side person may be proposed


def made(root: Path, fake: bool) -> None:
    """The workspace, made by `cerebro init` as a person's terminal runs it, with only its accounts and people
    curators kept, then the notes under local/."""
    from cerebro import cli

    args = ["init", str(root), "--email", YOU, "--name", NAME, "--daily-usd", "3", "--timezone", "UTC",
            *(() if fake else ("--template", "gtm"))]
    cli.main(args, standalone_mode=False)
    if not fake:
        from live_gtm import LOCAL

        (root / "sources" / "local.yaml").write_text(LOCAL)
        for curator in (root / "agents").glob("*/curator.md"):
            if curator.parent.name not in ("accounts", "people"):
                curator.unlink()
    for rel, text in NOTES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def stand_in(root: Path) -> Cerebro:
    """The blank workspace on the stand-ins: a people curator scripted to follow the rule, admitting each attendee
    placed by address with that address, and leaving out the rest with a reason."""
    from cerebro.connector import Record
    from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
    from cerebro.run import AgentRun

    records = [Record(id=f"n{n}", source="notes", revision="1", at=f"2026-11-{3 + n:02d}T09:00:00Z",
                      keys={"email": re.findall(r"<([^>]+)>", text)}, fields={"subject": text.splitlines()[0]},
                      body=text) for n, text in enumerate(NOTES.values())]
    calls = [*(("admit", {"name": name, "keys": {"email": [address]}, "reason": "Attendee at the account's domain.",
                          "group": "in your meetings"}) for name, address in PLACED.items()),
             *(("leave_out", {"name": name, "reason": "No address, and no fact places them yet."}) for name in BARE),
             *(("leave_out", {"name": name, "reason": "An address at no account's domain, and no fact places them."})
               for name in ELSEWHERE),
             *(("leave_out", {"name": name, "reason": "A personal mailbox, which names no account."})
               for name in PERSONAL)]

    class Scripted(AgentRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            done = ScriptedRun(lambda p: {}, list(calls) if agent.id == "people/curator" else []).run(
                agent, evidence, tools, budget)
            return replace(done, id=f"run_{uuid.uuid4().hex[:8]}")

    folder = root / "agents" / "people"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "curator.md").write_text("---\nauthority: { admit: true, archive: true, merge: true, split: true }\nheartbeat: [daily]\n---\n\nKeep the people.\n")
    return Cerebro(root, agent_run=Scripted(), extraction=FakeExtraction(),
                   connectors=[FakeConnector("notes", records)], trusted=True)


def _slug(name: str) -> str:
    return "-".join(name.lower().split())


def trial(root: Path, fake: bool) -> dict[str, Any]:
    """One discovery: whom the people curator proposed, and its reason for each it left out."""
    from cerebro.curate import LEFT_OUT
    from cerebro.first_run import discover
    from cerebro.records import proposals

    made(root, fake)
    with stand_in(root) if fake else Cerebro(root, trusted=True) as workspace:
        discover(workspace, say=print)
        filed = [p for p in proposals(workspace.store) if p.by.name == "people/curator"
                 and p.kind == "article.admit" and p.target.startswith("people/")]
        proposed = {str(p.payload.get("name") or p.target.rpartition("/")[2]) for p in filed}
        proposed |= {p.target.rpartition("/")[2] for p in filed}
        left = {str(row.data.get("name") or row.data.get("id") or ""): str(row.data.get("reason", ""))
                for row in workspace.store.rows(LEFT_OUT, {"agent": "people/curator"})}
        spent = workspace.ledger.spent(workspace.clock())
    keys = {p.target: p.payload.get("keys") for p in filed}
    return {"proposed": proposed, "left": left, "spent": spent, "keys": keys}


def named(name: str, found: set[str]) -> bool:
    return name in found or _slug(name) in found or any(name.lower() in f.lower() for f in found)


def verdict(runs: list[dict[str, Any]]) -> list[str]:
    """Every reason the trials fail: the placed share under `PLACED_FLOOR`, or any one bare name, address at no
    account's domain, own-side person or personal mailbox proposed in more than `WRONG_CEILING` trials."""
    placed = sum(named(name, r["proposed"]) for r in runs for name in PLACED)
    reasons = []
    if placed < PLACED_FLOOR * len(PLACED) * len(runs):
        reasons.append(f"placed attendees proposed {placed} of {len(PLACED) * len(runs)}, under {PLACED_FLOOR:.0%}")
    for what, names in {"bare name": BARE, "address at no account's domain": list(ELSEWHERE),
                        "own side": list(OWN_SIDE), "personal mailbox": list(PERSONAL)}.items():
        for name in names:
            times = sum(named(name, r["proposed"]) for r in runs)
            if times > WRONG_CEILING:
                reasons.append(f"{name} ({what}) was proposed in {times} of {len(runs)} trials")
    return reasons


def main() -> int:
    fake = "--fake" in sys.argv
    trials = int(sys.argv[sys.argv.index("--trials") + 1]) if "--trials" in sys.argv else (1 if fake else 3)
    if not fake and (not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY")):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY, or run "
              "with --fake on the stand-ins")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    runs = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        for n in range(trials):
            print(f"\n=== trial {n + 1} of {trials}")
            runs.append(trial(Path(folder) / f"wiki{n}", fake))
    placed = sum(named(name, r["proposed"]) for r in runs for name in PLACED)
    wrong = {"bare names": BARE, "addresses at no account's domain": list(ELSEWHERE), "own side": list(OWN_SIDE),
             "personal mailboxes": list(PERSONAL)}
    counts = {what: sum(named(name, r["proposed"]) for r in runs for name in names) for what, names in wrong.items()}
    print(f"\n{"placed by address proposed":<36}{placed} of {len(PLACED) * trials}")
    for what, names in wrong.items():
        print(f"{what + ' proposed':<36}{counts[what]} of {len(names) * trials}")
    for n, r in enumerate(runs, 1):
        print(f"trial {n}: spent ${r['spent']:.4f}, proposed {sorted(r['proposed'])}, keys {r['keys']}")
        for who, reason in sorted(r["left"].items()):
            print(f"  left out {who}: {reason}")
    reasons = verdict(runs)
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
