"""One live run of a call article on real models: the prep before the call, and the write-up once it has happened.
A made-up gtm workspace keeps only the calls writer. Each call is a meeting from a calendar, its fields as the
calendar connector gives them, status upcoming or past among them, and some have a recording: notes of what was
said. Each article is admitted as a person admits one, bound by its event's `calendar_event_id`, so what it measures
is the writer alone. Not part of the test suite: it calls real models and costs some cents, under a daily cap of $3.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_call.py
    uv run python checks/live_call.py --fake             # the same steps on the stand-ins, to see the plumbing
    uv run python checks/live_call.py --keep <folder>    # make the workspace there and leave it

Cases, each read from the article's headings and text:
  upcoming    a call five days ahead holds a prep and no write-up: who is coming, which its invite gives, and only
              prep parts, each sentence of them citing what it rests on
  recorded    a call three days back with a recording holds the write-up (Summary, Decisions) and no prep
  unrecorded  a call three days back with no recording says so in a line under Summary, and holds no prep
  turned      a call under way, which the calendar says is upcoming until it ends, holds a prep as upcoming does; once
              the calendar says past and its recording comes in, the write-up, and the prep is gone

It prints what grounding dropped in each pass, each article, then a line per case and what it cost. It exits 0 when every case holds, 1 otherwise or when
it could not run. It never prints a key.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from _paths import ran_lines

from cerebro import cli
from cerebro.app import Cerebro
from cerebro.article import Identity, Property, body
from cerebro.caller import Caller
from cerebro.ground import ABSENCE, split_sentences

YOU, NAME = "robin@ourco.example", "Robin Vale"
NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
TODAY = NOW.replace(hour=15)
CALLS = {  # article: event id, title, start, attendees, agenda
    "upcoming": ("harrow-qbr", "Harrow Mills QBR", TODAY + timedelta(days=5), "mara@harrowmills.example",
                 "Quarterly review: rollout status, renewal timing."),
    "recorded": ("quillon-pilot", "Quillon Labs pilot review", TODAY - timedelta(days=3), "teo@quillonlabs.example",
                 "Review the pilot results."),
    "unrecorded": ("ostrava-intro", "Ostrava Freight intro", TODAY - timedelta(days=3), "ines@ostrava.example",
                   "Introductions and their freight tracking needs."),
    "turned": ("brightwater-kickoff", "Brightwater kickoff", NOW - timedelta(hours=1), "nessa@brightwater.example",
               "Kick off the onboarding project."),
}
RECORDINGS = {
    "recorded": """Teo Lind said the Quillon Labs pilot met its target: report time fell from four hours to forty
minutes across two teams. Teo agreed to move to a paid plan for both teams from January. Robin Vale owes Teo a
quote for two teams by December 4. Teo asked whether single sign-on comes with the paid plan, and Robin said she
would confirm by the same date.""",
    "turned": """Nessa Torvik said Brightwater wants onboarding done before its peak season starts on January 15. Nessa
and Robin Vale agreed that Brightwater sends its shipment data export by December 1. Robin owes Nessa an onboarding
plan by November 28. Nessa pushed back on a weekly status call and asked for one every two weeks.""",
}


def key(case: str) -> str:
    id, _, start, _, _ = CALLS[case]
    return f"{id}#{start:%Y%m%d}"


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def source(past: set[str], recorded: set[str]) -> str:
    """A files source in the calendar's shape: one entry a meeting, fields only, as the calendar connector lists
    one; and one entry a recording, read for facts, bound to its meeting by the event's key."""
    lines = ["kind: files", "window: { behind: all }", "select:"]
    for case, (id, title, start, who, agenda) in CALLS.items():
        status = "past" if case in past else "upcoming"
        lines += [f'  - glob: "calendar/{id}.md"', "    extract: records", "    into: calls",
                  f'    identity: {{ calendar_event_id: "{key(case)}" }}',
                  (f'    fields: {{ title: "{title}", start: "{stamp(start)}", status: {status}, '
                   f'attendees: "{who}, {YOU}", description: "{agenda}" }}')]
        if case in recorded:
            lines += [f'  - glob: "recordings/{id}.md"', "    extract: facts", "    into: calls",
                      f'    identity: {{ calendar_event_id: "{key(case)}" }}']
    return "\n".join(lines) + "\n"


def write(root: Path, past: set[str], recorded: set[str]) -> None:
    (root / "sources" / "local.yaml").write_text(source(past, recorded))
    for case, (id, title, start, who, agenda) in CALLS.items():
        (root / "calendar").mkdir(exist_ok=True)
        (root / "calendar" / f"{id}.md").write_text(f"{title}\n")
        if case in recorded:
            (root / "recordings").mkdir(exist_ok=True)
            (root / "recordings" / f"{id}.md").write_text(
                f"Date: {start:%Y-%m-%d}\nAttendees: {who}, {YOU}\n\n{RECORDINGS[case]}\n")


def made(root: Path, curator: bool = False) -> None:
    cli.main(["init", str(root), "--email", YOU, "--name", NAME, "--daily-usd", "3", "--timezone", "UTC",
              "--org", "Ourco", "--template", "gtm"], standalone_mode=False)
    for folder in (root / "agents").iterdir():
        if folder.is_dir() and folder.name != "calls":
            shutil.rmtree(folder)  # the call writer is what this measures
    if not curator:
        (root / "agents" / "calls" / "curator.md").unlink()
    for file in (root / "sources").glob("*.yaml"):
        file.unlink()
    write(root, {"recorded", "unrecorded"}, {"recorded"})


def admit(workspace: Cerebro) -> None:
    you = Caller("you", "person")
    for case, (id, title, start, _, _) in CALLS.items():
        article = f"calls/{case}"
        workspace.store.create(Identity(article, "calls", title, {"calendar_event_id": [key(case)]}, "active", you,
                                        "2026-11-01T00:00:00Z", "a made-up call"))
        workspace.store.set_field(article, "start", Property(stamp(start), you, "2026-11-01T00:00:00Z"))


def stand_in(root: Path, connectors: tuple = ()) -> Cerebro:
    """The stand-ins: the fake facts layer, and runs that write one line citing the first fact of their evidence."""
    from cerebro.fakes import FakeExtraction, ScriptedRun

    def cited(prompt: str) -> str:
        ids = re.findall(r"^- \[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1], re.MULTILINE)
        return f"## Summary\n\nWhat the evidence holds [^{ids[0]}]." if ids else ""

    return Cerebro(root, trusted=True, agent_run=ScriptedRun(cited), extraction=FakeExtraction(), connectors=connectors)


def passes(workspace: Cerebro, label: str, least: int = 1) -> None:
    """Passes until one runs nothing, after `least` of them: a record first held in one pass wakes its curator in
    the next."""
    for n in range(1, 4 + least):
        result = workspace.run_once()
        print(f"{label} pass {n}: runs {len(result.runs)}, facts {result.extracted}"
              + (f", failed {result.failed}" if result.failed else ""))
        for line in [*ran_lines(result), *(f"  {line}" for line in result.warnings)]:
            print(line)
        if not result.runs and n >= least:
            return


def text(workspace: Cerebro, case: str) -> str:
    found = workspace.store.article(f"calls/{case}")
    return body(found) if found is not None else ""


PREP = ("Who is coming", "What changed", "Open items", "Suggested next step")


def prep_parts(said: str) -> int:
    """How many of the prep's headings `said` holds: a write-up holds none."""
    return sum(f"## {h}" in said for h in PREP)


def absences(said: str) -> int:
    """How many sentences of `said` cite nothing and say the evidence holds none of something, which grounding
    keeps by that same rule."""
    text = re.sub(r"^#.*$", "", said, flags=re.MULTILINE)
    return sum("[^" not in s.text and bool(ABSENCE.search(s.text)) for s in split_sentences(text))


def prep(said: str) -> bool:
    """Whether `said` is a prep: who is coming, no heading but the prep's own, and every sentence citing, but one
    grounding keeps as saying the evidence holds none of something."""
    headings = re.findall(r"^## (.+)$", said, re.MULTILINE)
    text = re.sub(r"^#.*$", "", said, flags=re.MULTILINE)
    uncited = sum("[^" not in s.text for s in split_sentences(text))
    return "Who is coming" in headings and all(h in PREP for h in headings) and uncited == absences(said)


def write_up(said: str) -> bool:
    return ("Summary" in said or "Decisions" in said) and not prep_parts(said)


def main() -> int:
    fake = "--fake" in sys.argv
    keep = Path(sys.argv[sys.argv.index("--keep") + 1]).resolve() if "--keep" in sys.argv else None
    if not fake and (not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY")):
        print("could not run: set FIREWORKS_API_KEY and TYPESAFE_API_KEY, "
              "or run with --fake on the stand-ins")
        return 1
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = keep or Path(folder) / "wiki"
        made(root)
        def opened() -> Cerebro:
            return stand_in(root) if fake else Cerebro(root, trusted=True)

        with opened() as workspace:
            admit(workspace)
            passes(workspace, "first ")
            before = text(workspace, "turned")
            spent = workspace.ledger.spent(workspace.clock())
        write(root, {"recorded", "unrecorded", "turned"}, {"recorded", "turned"})  # it happened, and was recorded
        with opened() as workspace:  # a source's file is read as the workspace opens
            passes(workspace, "second")
            said = {case: text(workspace, case) for case in CALLS}
            spent = max(spent, workspace.ledger.spent(workspace.clock()))
        for case, words in said.items():
            print(f"\n----- calls/{case}\n{words}")
        print(f"\n----- calls/turned before it happened\n{before}\n")
        unrecorded = said["unrecorded"]
        checks = {
            "upcoming": prep(said["upcoming"]),
            "recorded": write_up(said["recorded"]) and "Decisions" in said["recorded"],
            "unrecorded": write_up(unrecorded) and "record" in unrecorded.lower() and "Decisions" not in unrecorded,
            "turned": prep(before) and write_up(said["turned"]) and "Decisions" in said["turned"],
        }
        absent = {"upcoming": absences(said["upcoming"]), "turned": absences(before)}
        for case, ok in checks.items():
            n = absent.get(case, 0)
            print(f"{'ok  ' if ok else 'FAIL'} {case}" + (f", {n} absence line{'s' * (n > 1)}" if n else ""))
        print(f"spent    ${spent:.4f}")
        if keep is not None:
            print(f"kept     {root}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
