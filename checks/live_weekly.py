"""How well the sales template's calls curator keeps each conversation of a weekly call on one article, on real models:
no article holding two dates, and each recording on its own date's article. Not part of the test suite: it calls real
models and costs some cents a trial, under a daily cap of $3.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_weekly.py --trials 2
    ... uv run python checks/live_weekly.py --no-rule    # the control: the curator without its pairing paragraph

Each trial makes the gtm template in a temporary folder,
keeps only the calls curator and writer, and admits one made-up account by hand, Harrow Mills. Its calendar has
"Harrow Mills weekly" at 00:00 UTC on five weeks, 26, 19, 12 and 5 days back and 2 days ahead, and a one-off "Harrow
Mills pricing" 5 days back at 12:00, all with the same person of Harrow Mills. The first run's list is approved whole,
as a person approves one. Then three recordings come from a call recorder:
  r19   of the meeting 19 days back, naming its event: it binds by the event's key
  r12   of the meeting 12 days back, naming no event, three minutes after its start: only the curator can pair it
  r5    of the meeting 5 days back, naming its event, two minutes before its start, the day before in UTC: its key
        takes that day, so only the curator can pair it
A fold is an article bound by two meetings, or a recording on another meeting's article. A split is a recording on an
article of its own, or on none. It prints what each record is on, a line per trial, and a PASS line when no trial
folds or splits and every meeting has an article, else a FAIL line, and what it cost. It exits 0 on PASS, 1 otherwise.
It never prints a key.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from live_call import YOU, made, passes, stamp

from cerebro.app import Cerebro
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.connector import Ours, Record, address_keys, event_key
from cerebro.connectors.calendar import CalendarConnector
from cerebro.fakes import FakeConnector
from cerebro.identity import bound_to
from cerebro.records import proposals
from cerebro.source import SourceFile, Window

OURS = Ours((YOU,), ("ourco.example",))
FIRST = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=26)
PRICING = FIRST + timedelta(days=21, hours=12)
SERIES = "harrow-weekly@check.example"
MARA = "mara@harrowmills.example"
RULE = "One call, one article."  # the pairing paragraph of the calls curator, which --no-rule drops


def ics(t: datetime) -> str:
    return f"{t:%Y%m%dT%H%M%SZ}"


FEED = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Made up//Check//EN
BEGIN:VEVENT
UID:{SERIES}
DTSTAMP:20261001T000000Z
DTSTART:{ics(FIRST)}
DTEND:{ics(FIRST + timedelta(minutes=30))}
RRULE:FREQ=WEEKLY;COUNT=5
SUMMARY:Harrow Mills weekly
DESCRIPTION:Weekly check-in on the rollout.
ORGANIZER;CN=Robin Vale:mailto:{YOU}
ATTENDEE;CN=Mara Okafor;PARTSTAT=ACCEPTED:mailto:{MARA}
END:VEVENT
BEGIN:VEVENT
UID:harrow-pricing@check.example
DTSTAMP:20261001T000000Z
DTSTART:{ics(PRICING)}
DTEND:{ics(PRICING + timedelta(minutes=30))}
SUMMARY:Harrow Mills pricing
ORGANIZER;CN=Robin Vale:mailto:{YOU}
ATTENDEE;CN=Mara Okafor;PARTSTAT=ACCEPTED:mailto:{MARA}
END:VEVENT
END:VCALENDAR
"""


def recording(id: str, meeting: datetime, minutes: int, event: str, said: str) -> tuple[datetime, Record]:
    """A recording as the Gong connector gives one: its own id, its parties' addresses carried, and the event's key
    when it names one, dated by its own start."""
    started = meeting + timedelta(minutes=minutes)
    keys = {"gong_call_id": [id]} | address_keys([MARA, YOU], OURS)
    fields = {"title": "Harrow Mills / Ourco"}
    if event:
        fields["calendar_event_id"] = event
        keys["calendar_event_id"] = [event_key(event, started)]
    record = Record(id=id, source="gong", revision="1", at=stamp(started), keys=keys, fields=fields, body=said,
                    into="calls", extract="facts")
    record.carried = ["email", "domain"]
    return meeting, record


RECORDINGS = [  # each with the start of the meeting it records
    recording("r19", FIRST + timedelta(days=7), 1, SERIES,
              "Mara Okafor said the rollout reached the second warehouse. Robin Vale owes Mara a training date."),
    recording("r12", FIRST + timedelta(days=14), 3, "",
              "Mara Okafor said the third warehouse goes live in December. Mara asked for a usage report."),
    recording("r5", FIRST + timedelta(days=21), -2, SERIES,
              "Mara Okafor said Harrow Mills wants to add two more sites next year. Robin owes Mara a quote."),
]


def drop_rule(root: Path) -> None:
    path = root / "agents" / "calls" / "curator.md"
    parts = path.read_text(encoding="utf-8").split("\n\n")
    kept = [part for part in parts if not part.startswith(RULE)]
    if len(kept) != len(parts) - 1:
        raise SystemExit(f"FAIL {path}: the pairing paragraph is not there to drop")
    path.write_text("\n\n".join(kept), encoding="utf-8")


def trial(root: Path, n: int, no_rule: bool) -> dict[str, float]:
    made(root, curator=True)
    for file in (root / "sources").glob("*.yaml"):
        file.unlink()
    if no_rule:
        drop_rule(root)
    calendar = CalendarConnector(SourceFile(name="calendar", kind="calendar", window=Window("30d", "14d")),
                                 lambda: FEED, ours=OURS)
    recorder = FakeConnector("gong", [])
    with Cerebro(root, trusted=True, connectors=[calendar, recorder]) as workspace:
        workspace.store.create(Identity("accounts/harrow-mills", "accounts", "Harrow Mills", {"domain": ["harrowmills.example"]},
                                        "active", Caller("you", "person"), "2026-10-01T00:00:00Z", "a made-up account"))
        passes(workspace, f"trial {n} first")
        first = [p.id for p in proposals(workspace.store, "waiting")]
        if first:
            workspace.decide(Caller("you", "person"), accept=first)
        passes(workspace, f"trial {n} approved")
        for _, record in RECORDINGS:
            recorder.add(record)
        passes(workspace, f"trial {n} recorded", least=2)
        spent = workspace.ledger.spent(workspace.clock())
        records = {r.id: r for c in workspace.connectors.values() for r in c.list().records}
        on = {id: [a for a in bound_to(workspace.store, r) if a.startswith("calls/")] for id, r in records.items()}
    for id, articles in sorted(on.items()):
        print(f"  trial {n} {id:48} on {articles}")
    meetings = {id: articles for id, articles in on.items() if id.endswith("@check.example") or "#" in id}
    held = [a for articles in meetings.values() for a in articles]
    folds = sum(held.count(a) > 1 for a in set(held))
    splits = 0
    for meeting, record in RECORDINGS:
        want = next(articles for id, articles in meetings.items()
                    if abs(datetime.fromisoformat(records[id].at) - meeting) < timedelta(minutes=1))
        got = on.get(record.id, [])
        if got != want or not got:
            if got and any(a in held for a in got):
                folds += 1
                print(f"  trial {n} FOLD {record.id} on {got}, not {want}")
            else:
                splits += 1
                print(f"  trial {n} SPLIT {record.id} on {got or 'nothing'}, not {want}")
    missing = sum(not articles for articles in meetings.values())
    print(f"trial {n}: {len(meetings)} meetings, {folds} folds, {splits} splits, {missing} meetings on no article, "
          f"${spent:.4f}")
    return {"folds": folds, "splits": splits, "missing": missing, "spent": spent}


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("could not run: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 1
    logging.getLogger("httpx").setLevel(logging.WARNING)
    trials = int(sys.argv[sys.argv.index("--trials") + 1]) if "--trials" in sys.argv else 2
    no_rule = "--no-rule" in sys.argv
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        results = [trial(Path(folder) / f"wiki{n}", n, no_rule) for n in range(1, trials + 1)]
    total = {key: sum(r[key] for r in results) for key in results[0]}
    ok = not (total["folds"] or total["splits"] or total["missing"])
    print(f"{'PASS' if ok else 'FAIL'} {trials} trials{' without the pairing paragraph' if no_rule else ''}: "
          f"{total['folds']:.0f} folds, {total['splits']:.0f} splits, {total['missing']:.0f} meetings on no article "
          f"of {trials * 6} meetings and {trials * len(RECORDINGS)} recordings; spent ${total['spent']:.4f}, "
          f"${total['spent'] / trials:.4f} a trial")
    return 0 if ok else 1


if __name__ == "__main__":
    from spend import watch

    watch()
    sys.exit(main())
