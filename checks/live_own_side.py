"""One live check of the workspace's own side on real models: a workspace made by `cerebro init --email` at a made-up
domain, through the same code a person's `init` runs, notes whose attendee lines carry addresses, a first run with
its list approved whole, then a pass. Not part of the test suite: it calls real models and costs some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_own_side.py
    uv run python checks/live_own_side.py --fake
    uv run python checks/live_own_side.py --teammates
    uv run python checks/live_own_side.py --keep <folder>

The workspace is the gtm template's, made with
`init --template gtm --email ann@ourco.example`, so its own domain, `ourco.example`, comes from the address alone.
Its notes are four calls and meetings: two colleagues at the own domain, and people at two outside organisations,
Brightwater Logistics and Tidewater Freight, with one colleague on every note.

It prints each article with its keys, the rows each collection has on the first run's list, the tokens of each
curator's opening, what it spent, then PASS, or FAIL with every reason. It never prints a key. It reads what is on
disk, the articles under `articles/` and their front matter, and passes only when:
- `read_ours` gives the own domain, so the engine knows its own side;
- no article is keyed by the own domain or a colleague's address, named for the own domain or the workspace's own
  name, or filed under the own domain;
- no colleague has an article;
- each outside organisation has an article;
- no colleague's address is a key value of an outside organisation's article.

With `--teammates` the workspace also has a `team` collection whose curator's file says the workspace's own members
get articles there. Its articles are left out of the checks above, and it passes only when, as well, each colleague
has an article in `team` keyed by their own address, and facts of their records reach it.

An article's keys are the values under `identity:` in its front matter, and its name is its `title:`. The colleagues
an article's facts name are listed under `ours:`, which is printed beside the keys and is no key.

`--fake` runs the same steps and checks on the stand-ins, a blank workspace made the same way, with scripted curators
that admit the two organisations and three outside people, and with `--teammates` the two colleagues in `team`. A test
runs it. `--keep <folder>` makes the workspace there and leaves it, to read after.
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

import yaml

from cerebro.app import Cerebro
from cerebro.run import AgentRun

YOU, NAME = "ann@ourco.example", "Ann Lee"
OWN = "ourco.example"
COLLEAGUES = {"ann@ourco.example": "Ann Lee", "bo@ourco.example": "Bo Lin"}
OUTSIDE = {"brightwater.example": "Brightwater Logistics", "tidewater.example": "Tidewater Freight"}
PEOPLE = {"nessa@brightwater.example": "Nessa Torvik", "sam@brightwater.example": "Sam Okoye",
          "ravi@tidewater.example": "Ravi Menon"}
NOTES = {
    "local/calls/2026-11-03-brightwater-discovery.md": """Account: Brightwater Logistics
Attendees: Nessa Torvik <nessa@brightwater.example>, Sam Okoye <sam@brightwater.example>, Ann Lee <ann@ourco.example>
Date: 2026-11-03

Nessa Torvik, VP of Operations at Brightwater Logistics, said their warehouse team loses about six hours a week
reconciling shipment records by hand. Sam Okoye, their IT lead, asked whether single sign-on is supported.
Ann Lee from our side will send a security overview by 10 November.
""",
    "local/meetings/2026-11-07-brightwater-review.md": """Account: Brightwater Logistics
Attendees: Nessa Torvik <nessa@brightwater.example>, Bo Lin <bo@ourco.example>
Date: 2026-11-07

Nessa Torvik asked Bo Lin for a pilot plan covering two warehouses. Bo Lin will draft it by 14 November.
""",
    "local/calls/2026-11-10-tidewater-intro.md": """Account: Tidewater Freight
Attendees: Ravi Menon <ravi@tidewater.example>, Bo Lin <bo@ourco.example>
Date: 2026-11-10

Ravi Menon, head of finance operations at Tidewater Freight, took an intro call with Bo Lin from our side.
Ravi said Tidewater matches about 4,000 carrier invoices a month by hand.
""",
    "local/meetings/2026-11-12-tidewater-followup.md": """Account: Tidewater Freight
Attendees: Ravi Menon <ravi@tidewater.example>, Ann Lee <ann@ourco.example>, Bo Lin <bo@ourco.example>
Date: 2026-11-12

Ravi Menon asked Ann Lee for pricing for 5,000 invoices a month. Ann Lee will send it by 19 November.
""",
}
TEAM = "team"  # the collection of the workspace's own members, with --teammates
TEAM_CURATOR = """---
authority: { admit: true, archive: false, merge: false, split: false }
evidence:
  - query: { from: facts, window: last_90_days }
  - held: {}
heartbeat: [change, daily]
---

# Instructions

This collection is about the workspace's own members: each colleague on the workspace's own side whom the sources
show at work gets one article here, keyed by their own address as email. Nobody outside the workspace gets one here.
"""
TEAM_ARTICLE = """---
evidence:
  - facts: { window: last_90_days }
---

Write what this colleague did lately and what they owe, from their facts.

# Output

## Summary
"""


def made(root: Path, fake: bool, teammates: bool) -> None:
    """The workspace, made by `cerebro init` as a person's terminal runs it, with only an email to give its own
    side, then the notes under local/, and with `teammates` the team collection's files."""
    from cerebro import cli

    args = ["init", str(root), "--email", YOU, "--name", NAME, "--daily-usd", "3", "--timezone", "UTC",
            *(() if fake else ("--template", "gtm"))]
    cli.main(args, standalone_mode=False)
    if not fake:
        from live_gtm import LOCAL

        (root / "sources" / "local.yaml").write_text(LOCAL)
    for rel, text in NOTES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    if teammates:
        (root / "agents" / TEAM).mkdir(parents=True, exist_ok=True)
        (root / "agents" / TEAM / "curator.md").write_text(TEAM_CURATOR)
        (root / "agents" / TEAM / "article.md").write_text(TEAM_ARTICLE)


def stand_in(root: Path, teammates: bool) -> Cerebro:
    """The blank workspace on the stand-ins: an accounts curator and a people curator whose scripts admit the outside
    organisations and people, with `teammates` a team curator whose script admits each colleague by their own address,
    and a writer for each that cites every fact it reads."""
    from cerebro.connector import Record
    from cerebro.curate import carried_out
    from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
    from cerebro.records import proposals

    records = [Record(id=f"n{n}", source="notes", revision="1", at=f"2026-11-{3 + n:02d}T09:00:00Z",
                      keys={"email": re.findall(r"<([^>]+)>", text)}, fields={"subject": text.splitlines()[0]},
                      body=text) for n, text in enumerate(NOTES.values())]
    script = {
        "accounts/curator": [("admit", {"name": name, "keys": {"domain": [domain]}, "reason": "Named on two notes.",
                                        "group": "named in your notes"}) for domain, name in OUTSIDE.items()],
        "people/curator": [("admit", {"name": name, "keys": {"email": [address]}, "reason": "On a note.",
                                      "group": "named in your notes"}) for address, name in PEOPLE.items()],
        f"{TEAM}/curator": [("admit", {"name": name, "keys": {"email": [address]}, "reason": "On the notes.",
                                       "group": "named in your notes"}) for address, name in COLLEAGUES.items()],
    }

    class Scripted(AgentRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            def cite(prompt: str) -> dict[str, str]:
                facts = re.findall(r"\[\^(fact_\w+)\] ([^(\n]+)", prompt)
                return {"Summary": " ".join(f"{text.strip()} [^{id}]" for id, text in facts) or "Nothing yet."}

            # a curator carries out what was approved of its collection, naming it, as a real one does on its next run
            approved = [p for p in proposals(made.store, "accepted") if not carried_out(p)
                        and p.target.startswith(agent.target + "/")] if agent.role == "curator" else []
            calls = [("admit", {"name": p.payload["name"], "keys": p.payload.get("keys", {}), "reason": "Approved.",
                                "proposals": [p.id]}) for p in approved] or (
                list(script.get(agent.id, [])) if agent.role == "curator" else [])
            done = ScriptedRun(lambda p: {} if agent.role == "curator" else cite(p), calls).run(
                agent, evidence, tools, budget)
            return replace(done, id=f"run_{uuid.uuid4().hex[:8]}")

    for collection in ("accounts", "people", *((TEAM,) if teammates else ())):
        folder = root / "agents" / collection
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "curator.md").write_text(f"---\nauthority: {{ admit: true, archive: true, merge: true, split: true }}\nheartbeat: [daily]\n---\n\nKeep the {collection}.\n")
        (folder / "article.md").write_text("---\nevidence:\n  - query: { from: facts, source: notes }\n---\n\n"
                                           "Write the article.\n\n# Output\n\n## Summary\n")
    made = Cerebro(root, agent_run=Scripted(), extraction=FakeExtraction(),
                   connectors=[FakeConnector("notes", records)], trusted=True)
    return made


class Openings(AgentRun):
    """The workspace's own agent runs, with the tokens of each curator's opening kept by its agent's id."""

    def __init__(self, inner: AgentRun) -> None:
        self.inner = inner
        self.tokens: dict[str, list[int]] = {}

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        from cerebro.run import tokens

        done = self.inner.run(agent, evidence, tools, budget)
        if agent.role == "curator":
            self.tokens.setdefault(agent.id, []).append(tokens(next((e.text for e in done.transcript
                                                                     if e.role == "user"), "")))
        return done


def on_disk(root: Path) -> list[dict[str, Any]]:
    """Each article under articles/, as its file holds it: its id, the folder it is filed under, and its front
    matter."""
    out = []
    for path in sorted((root / "articles").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        front = yaml.safe_load(text.split("---", 2)[1]) if text.startswith("---") else {}
        out.append({"id": str(path.relative_to(root / "articles").with_suffix("")), "front": front or {}})
    return out


def _values(front: dict[str, Any]) -> list[str]:
    """Every key value an article's front matter holds under `identity:`, folded."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif value is not None:
            found.append(str(value).strip().lower())

    walk(front.get("identity") or {})
    return found


def checked(root: Path, articles: list[dict[str, Any]]) -> list[str]:
    """Every reason the own side was not kept, from what is on disk."""
    from cerebro.project import read_ours

    reasons = []
    ours = read_ours(root)
    if OWN not in ours.domains:
        reasons.append(f"read_ours gives no {OWN}: {ours.domains}")
    own_names = {"ourco", OWN, root.name.lower(), *(n.lower() for n in COLLEAGUES.values())}
    for article in articles:
        keys = _values(article["front"])
        name = str(article["front"].get("title") or "")
        if any(v == OWN or v.endswith(("@" + OWN, "." + OWN)) for v in keys):
            outside = any(v in OUTSIDE or any(v.endswith("@" + d) for d in OUTSIDE) for v in keys)
            reasons.append(f"{article['id']} is keyed by the own side{' beside an outside organisation' if outside else ''}"
                           f": {keys}")
        if name.lower() in own_names or OWN in article["id"].split("/")[0] or article["id"].split("/")[-1] in (
                "ourco", "ourco-example", *(n.lower().replace(" ", "-") for n in COLLEAGUES.values())):
            reasons.append(f"{article['id']} is named for the own side: {name!r}")
    for domain, name in OUTSIDE.items():
        if not any(domain in _values(a["front"]) or name.lower() in str(a["front"]).lower() for a in articles):
            reasons.append(f"{name} ({domain}) has no article")
    return reasons


def team_checked(workspace: Cerebro, articles: list[dict[str, Any]]) -> list[str]:
    """Every reason a colleague has no article in the team collection, or one that no fact reaches, with the facts and
    the records they come from that reach each."""
    facts = {f.id: f for f in workspace.extraction.facts()}
    reasons = []
    for address, name in COLLEAGUES.items():
        mine = [a["id"] for a in articles if a["id"].startswith(f"{TEAM}/") and address in _values(a["front"])]
        if not mine:
            reasons.append(f"{name} has no article in {TEAM} keyed by {address}")
            continue
        tied = workspace.store.facts_of(mine[0])
        records = {facts[f].record for f in tied if f in facts}
        print(f"team     {mine[0]}: {len(tied)} facts from {len(records)} records")
        if not tied:
            reasons.append(f"{mine[0]} is reached by no fact")
    return reasons


def main() -> int:
    fake, teammates = "--fake" in sys.argv, "--teammates" in sys.argv
    keep = Path(sys.argv[sys.argv.index("--keep") + 1]).resolve() if "--keep" in sys.argv else None
    if not fake and (not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY")):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY, or run "
              "with --fake on the stand-ins")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from cerebro.first_run import Choices, run

    listed: list[Any] = []

    def choose(pile: Any) -> Choices:
        if not pile.late:
            listed.extend(pile.lists)
        return Choices([g.ids() for top in pile.groups for g in (top, *top.under) if g.rows], [])

    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = keep or Path(folder) / "wiki"
        made(root, fake, teammates)
        with stand_in(root, teammates) if fake else Cerebro(root, trusted=True) as workspace:
            openings = workspace.agent_run = Openings(workspace.agent_run)
            out = run(workspace, choose=choose, say=print)
            if out is None:
                print("first    the workspace was not in its first run")
            result = workspace.run_once()
            print(f"pass     compiled {sorted(result.compiled)}")
            spent = workspace.ledger.spent(workspace.clock())
            articles = on_disk(root)
            team = team_checked(workspace, articles) if teammates else []
        for article in articles:
            ours = article["front"].get("ours") or []
            print(f"article  {article['id']}: keys {_values(article['front'])}{f', ours {ours}' if ours else ''}")
        for part in listed:
            print(f"rows     {part.collection}: {len(part.rows)}")
        for agent, counts in sorted(openings.tokens.items()):
            print(f"opening  {agent}: {', '.join(str(n) for n in counts)} tokens")
        print(f"spent    ${spent:.4f}")
        reasons = checked(root, [a for a in articles if not a["id"].startswith(f"{TEAM}/")]) + team
        if keep is not None:
            print(f"kept     {root}")
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
