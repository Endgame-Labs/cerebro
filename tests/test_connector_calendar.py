"""The calendar connector: the `Connector` suite over a feed string, the recorded fixture feed and its expected output,
then keys, the window, recurrences, what a feed that does not read does, and a curator's questions, which read the
feed and nothing else."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from suites.connector import *
from suites.connector import every_page
from test_fetch import Asking, curate

from cerebro.connector import Ours, QuestionError, Record, RecordNotFound, event_key, outside_domain
from cerebro.connectors.calendar import CalendarConnector, CalendarError, CursorError
from cerebro.connectors.calendar_ical import Span, read_feed
from cerebro.connectors.calendar_rrule import RuleError, parse_rule
from cerebro.connectors.questions import machine_address
from cerebro.fakes import MemoryStore
from cerebro.fetch import fetch_tool
from cerebro.identity import bound_to, reached
from cerebro.source import SourceError, SourceFile, Window

DATA = Path(__file__).parent / "data" / "calendar"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
ME = "jane.doe@vandelay.example"
COMPANY = "vandelay.example"
JANE = f"ORGANIZER;CN=Jane Doe:mailto:{ME}\nATTENDEE;CN=Jane Doe;PARTSTAT=ACCEPTED:mailto:{ME}\n"


def calendar_source(behind: str = "", ahead: str = "", **settings: object) -> SourceFile:
    return SourceFile(name="calendar", kind="calendar", window=Window(behind, ahead), into="meetings",
                      settings=dict(settings))


def wrap(*events: str) -> str:
    body = "".join(f"BEGIN:VEVENT\n{e.strip()}\nEND:VEVENT\n" for e in events)
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Fixture//Test//EN\n{body}END:VCALENDAR\n"


def make(feed: Callable[[], str] | str, source: SourceFile | None = None, now: datetime = NOW,
         **options) -> CalendarConnector:
    text = feed
    return CalendarConnector(source or calendar_source(), feed if callable(feed) else (lambda: text),
                             now=lambda: now, ours=Ours((ME,), (COMPANY,)), **options)


def revision(record: Record) -> str:
    whole = [record.at, record.keys, record.fields, record.body, record.extract, record.into]
    return hashlib.sha256(json.dumps(whole, sort_keys=True).encode()).hexdigest()[:16]


def meeting(id: str, at: str, keys: dict[str, list[str]], body: str, **fields: object) -> Record:
    """A meeting as the connector gives it: named by its event's id, lower case, and carrying its people's keys."""
    when = datetime.fromisoformat(at)
    keys = keys | {"calendar_event_id": list(dict.fromkeys([event_key(id, when), event_key(id.partition("#")[0], when)]))}
    record = Record(id=id, source="calendar", revision="", at=at, keys=keys, body=body, extract="none",
                    carried=[k for k in ("email", "domain") if k in keys],
                    into="meetings", fields={"title": "Meeting", "all_day": False, "timezone": "UTC",
                                             "status": "upcoming", "attendees": [], "organizer": None,
                                             "location": None, "conferencing_url": None,
                                             "description": body or None, "source": "calendar"} | fields)
    record.revision = revision(record)
    return record


# -- the suite, over a feed held as events by UID
QBR = ("UID:acme-qbr@fixture.example\nDTSTAMP:20261101T000000Z\nDTSTART:20261118T170000Z\nDTEND:20261118T180000Z\n"
       "SUMMARY:Acme QBR\nDESCRIPTION:Agenda: renewal\\, then pricing.\nLOCATION:Video call\n"
       "X-GOOGLE-CONFERENCE:https://meet.google.com/fix-ture-001\n" + JANE +
       "ATTENDEE;CN=Wile Coyote;PARTSTAT=TENTATIVE:mailto:Wile@Acme.example\n")
SYNC = ("UID:northwind-sync@fixture.example\nDTSTART;TZID=America/New_York:20261110T090000\n"
        "DTEND;TZID=America/New_York:20261110T093000\nRRULE:FREQ=WEEKLY;COUNT=2\nSUMMARY:Northwind sync\n"
        "ATTENDEE;CN=Nora Buyer:mailto:nora.buyer@mail.northwind.example\n"
        "ATTENDEE;CN=Pat Guest:mailto:go-example@gmail.com\n")
OFFSITE = ("UID:acme-offsite@fixture.example\nDTSTART;VALUE=DATE:20261120\nDTEND;VALUE=DATE:20261121\n"
           "SUMMARY:Acme site visit\nATTENDEE;CN=Wile Coyote:mailto:wile@acme.example\n")
NOISE = ("UID:acme-cancelled@fixture.example\nDTSTART:20261119T150000Z\nSTATUS:CANCELLED\nATTENDEE:mailto:w@acme.example",
         "UID:comp-review@fixture.example\nDTSTART:20261119T150000Z\nSUMMARY:Comp review\n" + JANE +
         "ATTENDEE;CN=Casey:mailto:casey@eu.vandelay.example\n",
         "UID:dentist@fixture.example\nDTSTART:20261119T160000Z\nSUMMARY:Dentist\n")


@pytest.fixture
def events() -> dict[str, str]:
    return {"acme-qbr@fixture.example": QBR, "northwind-sync@fixture.example": SYNC,
            "acme-offsite@fixture.example": OFFSITE} | {f"noise{i}": e for i, e in enumerate(NOISE)}


@pytest.fixture
def held() -> list[Record]:
    wile = {"name": "Wile Coyote", "email": "wile@acme.example"}
    nora = {"name": "Nora Buyer", "email": "nora.buyer@mail.northwind.example"}
    pat = {"name": "Pat Guest", "email": "go-example@gmail.com"}
    sync_keys = {"email": ["go-example@gmail.com", "nora.buyer@mail.northwind.example"],
                 "domain": ["northwind.example"]}
    return [
        meeting("acme-qbr@fixture.example", "2026-11-18T17:00:00Z",
                {"email": ["wile@acme.example"], "domain": ["acme.example"]}, "Agenda: renewal, then pricing.",
                title="Acme QBR", date="2026-11-18", start="2026-11-18T17:00:00Z", end="2026-11-18T18:00:00Z",
                attendees=["me", wile | {"response": "tentative"}], organizer="me", location="Video call",
                conferencing_url="https://meet.google.com/fix-ture-001", ours=["Jane Doe"]),
        meeting("northwind-sync@fixture.example#20261110T140000Z", "2026-11-10T14:00:00Z", sync_keys, "",
                title="Northwind sync", date="2026-11-10", start="2026-11-10T14:00:00Z",
                end="2026-11-10T14:30:00Z", timezone="America/New_York", status="past", attendees=[nora, pat]),
        meeting("northwind-sync@fixture.example#20261117T140000Z", "2026-11-17T14:00:00Z", sync_keys, "",
                title="Northwind sync", date="2026-11-17", start="2026-11-17T14:00:00Z",
                end="2026-11-17T14:30:00Z", timezone="America/New_York", attendees=[nora, pat]),
        meeting("acme-offsite@fixture.example", "2026-11-20T00:00:00Z",
                {"email": ["wile@acme.example"], "domain": ["acme.example"]}, "", title="Acme site visit",
                date="2026-11-20", start="2026-11-20", end="2026-11-21", all_day=True, attendees=[wile]),
    ]


@pytest.fixture
def connector(events: dict[str, str]) -> CalendarConnector:
    return make(lambda: wrap(*events.values()), page_size=2)


@pytest.fixture
def add(events: dict[str, str]) -> Callable[[Record], str]:
    def add(record: Record) -> str:
        text = events.get(record.id, "")
        if text:
            text = re.sub(r"DESCRIPTION:.*\n", "", text) + f"DESCRIPTION:{record.body}\nSEQUENCE:9\n"
        else:
            start = datetime.fromisoformat(record.at) if record.at else NOW + timedelta(days=1)
            text = (f"UID:{record.id}\nDTSTART:{start:%Y%m%dT%H%M%SZ}\nSUMMARY:Added\n"
                    f"DESCRIPTION:{record.body}\nATTENDEE:mailto:guest@outside.example\n")
        events[record.id] = text
        return record.id
    return add


@pytest.fixture
def remove(events: dict[str, str]) -> Callable[[str], None]:
    return lambda id: events.pop(id)


# -- the recorded fixture feed
def dump(text: str, at: datetime) -> str:
    """A dump of a read, one line an occurrence, so it compares with its golden file."""
    feed = read_feed(text, Span(at - timedelta(days=90), at + timedelta(days=14)))
    lines = [f"events {feed.events} unread {feed.unread} unexpanded {feed.unexpanded}"]
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    for i in feed.instances:
        who = "; ".join(f"{a.name} <{a.email}> {a.response}" for a in i.attendees)
        lines.append(f"{i.id} | {json.dumps(i.title)} | {i.start:{stamp}} to {i.end:{stamp}} | zone {i.zone} | "
                     f"allday {str(i.all_day).lower()} cancelled {str(i.cancelled).lower()} tentative "
                     f"{str(i.tentative).lower()} | rev {i.revision} | org {i.organizer.email if i.organizer else ''}"
                     f" | conf {i.conference} | loc {json.dumps(i.location)} | who {who}")
    return "\n".join(lines) + "\n"


FEED_CLOCK = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def test_the_recorded_feed_reads_as_its_golden_instances() -> None:
    assert dump((DATA / "feed.ics").read_text(), FEED_CLOCK) == (DATA / "feed-instances.golden").read_text()


def test_the_recorded_feed_is_synthetic() -> None:
    text = (DATA / "feed.ics").read_text()
    addresses = re.findall(r"mailto:([^\s;]+)", text, re.IGNORECASE)
    hosts = re.findall(r"https?://([^/\s]+)", text)
    assert addresses and all(a.endswith(".example") for a in addresses)
    assert set(hosts) <= {"meet.google.com", "teams.microsoft.com", "zoom.us"}
    assert "fix-ture" in text and "000000001" in text  # the links lead nowhere


def test_a_calendar_source_that_names_no_collection_lands_its_meetings_with_the_calls_they_belong_to() -> None:
    source = SourceFile(name="calendar", kind="calendar", window=Window("90d", "14d"))
    records = every_page(make((DATA / "feed.ics").read_text(), source, now=FEED_CLOCK)).records
    assert records and {r.into for r in records} == {"calls"}  # the gtm template keeps a meeting on its call's article


def test_the_recorded_feed_gives_its_expected_meetings() -> None:
    connector = make((DATA / "feed.ics").read_text(), calendar_source("90d", "14d"), now=FEED_CLOCK)
    ids = [r.id for r in every_page(connector).records]
    assert "comp-review@fixture.example" not in ids and "dentist@fixture.example" not in ids
    assert "acme-cancelled@fixture.example" not in ids
    assert connector.notices == [("2 meetings in the window have no one from outside, only you or colleagues, "
                                  "and are not listed")]
    weekly = connector.get("acme-weekly@fixture.example#20261020T190000Z").fields
    assert weekly | {"attendees": None} == {
        "title": "Acme weekly", "date": "2026-10-20", "start": "2026-10-20T19:00:00Z", "end": "2026-10-20T20:00:00Z",
        "all_day": False, "timezone": "America/New_York", "status": "upcoming", "attendees": None, "organizer": "me",
        "location": "Video call", "conferencing_url": "https://meet.google.com/fix-ture-000",
        "description": "Standing check-in. Agenda: rollout status, security review.", "source": "calendar",
        "ours": ["Jane Doe"]}  # who on it is the workspace's own
    offsite = connector.get("acme-offsite@fixture.example").fields
    assert (offsite["date"], offsite["start"], offsite["end"], offsite["all_day"]) == ("2026-10-20", "2026-10-20",
                                                                                        "2026-10-21", True)
    sync = connector.get("northwind-sync@fixture.example")
    assert sync.fields["attendees"] == ["me", {"name": "Nora Buyer", "email": "nora.buyer@northwind.example"},
                                        {"name": "Pat Guest"}]
    assert sync.fields["description"] == ("Join: https://teams.microsoft.com/l/meetup-join/fixture-0001\n"
                                          "Pricing for the depot rollout.")
    assert sync.fields["timezone"] == "America/Los_Angeles"
    assert sync.keys == {"email": ["nora.buyer@northwind.example"], "domain": ["northwind.example"],
                         "calendar_event_id": ["northwind-sync@fixture.example#20261016"]}


# -- keys, fields and what is kept
def test_keys_are_outside_addresses_and_domains_never_yours_or_a_public_mail_services() -> None:
    assert outside_domain("a@mail.acme.example", Ours()) == "acme.example"
    assert outside_domain("a@eu.acme.co.example", Ours()) == "acme.co.example"
    assert outside_domain("a-example@gmail.com", Ours()) == outside_domain("a@co.uk", Ours()) == outside_domain("a@[10.0.0.1]", Ours()) == ""
    assert outside_domain("a@10.0.0.1", Ours()) == outside_domain("not an address", Ours()) == ""
    assert outside_domain("a@eu.vandelay.example", Ours(domains=(COMPANY,))) == outside_domain("a-example@mail.yahoo.co.uk", Ours()) == ""
    assert outside_domain("a@acme.co.example", Ours()) == "acme.co.example"
    assert outside_domain("x@y@shop.acme.example.", Ours()) == "acme.example"


def test_a_meeting_with_only_you_and_colleagues_is_never_listed_and_names_nothing(connector) -> None:
    every_page(connector)
    kept = repr(vars(connector)) + repr([connector.get(id) for id in
                                         ("acme-qbr@fixture.example", "acme-offsite@fixture.example")])
    assert "Comp review" not in kept and "Dentist" not in kept and "casey" not in kept
    for id in ("comp-review@fixture.example", "dentist@fixture.example", "acme-cancelled@fixture.example"):
        with pytest.raises(RecordNotFound):
            connector.get(id)


def test_the_connector_keeps_no_copy_of_the_feed(connector: CalendarConnector) -> None:
    listing = every_page(connector)
    kept = repr(vars(connector)) + str(listing.cursor)
    assert "renewal" not in kept and "Acme QBR" not in kept and "wile@" not in kept


def test_it_only_reads_the_feed_it_is_handed_once_a_call_and_asks_nothing_of_it(events) -> None:
    calls = []

    def feed(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return wrap(*events.values())
    connector = make(feed)
    connector.list()
    connector.get("acme-qbr@fixture.example")
    assert calls == [((), {}), ((), {})]


def test_a_description_is_cut_at_four_thousand_bytes_in_fields_and_kept_whole_in_the_body() -> None:
    long = "é" * 2500
    connector = make(wrap(f"UID:x\nDTSTART:20261118T170000Z\nDESCRIPTION:{long}\nATTENDEE:mailto:a@b.example"))
    record = connector.get("x")
    assert record.body == long
    assert len(record.fields["description"].encode()) <= 4000 and record.fields["description"].endswith("…")
    assert record.fields["description"] == "é" * 1998 + "…"


def test_a_timed_meeting_reads_in_the_workspaces_zone_and_an_all_day_one_keeps_its_own_days() -> None:
    from zoneinfo import ZoneInfo
    text = wrap("UID:t\nDTSTART:20261118T030000Z\nDTEND:20261118T040000Z\nATTENDEE:mailto:a@b.example",
                "UID:d\nDTSTART;VALUE=DATE:20261120\nATTENDEE:mailto:a@b.example")
    connector = CalendarConnector(calendar_source(), lambda: text, now=lambda: NOW, zone=ZoneInfo("America/Denver"))
    timed = connector.get("t").fields
    assert (timed["date"], timed["start"], timed["end"]) == ("2026-11-17", "2026-11-17T20:00:00-07:00",
                                                             "2026-11-17T21:00:00-07:00")
    day = connector.get("d").fields
    assert (day["date"], day["start"], day["end"]) == ("2026-11-20", "2026-11-20", "2026-11-21")


# -- the window and changes
def test_a_meeting_is_listed_again_when_it_ends_and_is_dropped_not_removed_when_it_ages_out(events) -> None:
    clock = [NOW]
    connector = CalendarConnector(calendar_source(behind="7d"), lambda: wrap(*events.values()),
                                  now=lambda: clock[0], ours=Ours((ME,), (COMPANY,)))
    first = every_page(connector)
    clock[0] = datetime(2026, 11, 18, 18, 30, tzinfo=UTC)
    later = every_page(connector, first.cursor)
    assert [(r.id, r.fields["status"]) for r in later.records] == [
        ("acme-qbr@fixture.example", "past"), ("northwind-sync@fixture.example#20261117T140000Z", "past")]
    assert later.removed == []  # the meeting happened: it left the window, and its facts stay


def test_the_window_reaches_ahead_fourteen_days_unless_the_file_says_and_behind_may_be_all() -> None:
    text = wrap("UID:near\nDTSTART:20261201T100000Z\nATTENDEE:mailto:a@b.example",
                "UID:far\nDTSTART:20261202T100000Z\nATTENDEE:mailto:a@b.example",
                "UID:old\nDTSTART:20200101T100000Z\nATTENDEE:mailto:a@b.example")
    assert [r.id for r in make(text).list().records] == ["near"]
    assert [r.id for r in make(text, calendar_source(behind="all", ahead="20d")).list().records] == [
        "far", "near", "old"]


def test_a_feed_that_does_not_read_fails_the_listing_and_takes_nothing_away(events) -> None:
    feed = [wrap(*events.values())]
    connector = make(lambda: feed[0])
    cursor = every_page(connector).cursor
    feed[0] = feed[0][:feed[0].index("END:VCALENDAR")]
    with pytest.raises(CalendarError, match="ends before END:VCALENDAR, so it was cut short"):
        connector.list(since=cursor)
    feed[0] = "<html>Sign in</html>"
    with pytest.raises(CalendarError, match="not an iCalendar stream"):
        connector.get("acme-qbr@fixture.example")


def test_a_cursor_it_did_not_give_is_refused_and_a_changed_source_file_lists_everything_anew(events) -> None:
    cursor = every_page(make(lambda: wrap(*events.values()))).cursor
    again = every_page(make(lambda: wrap(*events.values()), calendar_source(ahead="2d")), cursor)
    assert [r.id for r in again.records] == ["acme-qbr@fixture.example",
                                             "northwind-sync@fixture.example#20261110T140000Z",
                                             "northwind-sync@fixture.example#20261117T140000Z"]
    assert again.removed == []  # the offsite is past the shorter window ahead, not cancelled
    with pytest.raises(CursorError, match="list with no cursor to start over"):
        make("").list(since="files1.abc")


@pytest.mark.parametrize(("source", "message"), [
    (SourceFile(name="mail", kind="gmail"), "sources/mail.yaml is kind gmail: a calendar connector reads kind"),
])
def test_a_source_file_a_calendar_does_not_read_is_refused_with_one_line(source: SourceFile, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        make("", source)


# -- recurrences
def ids(text: str, low: datetime, high: datetime) -> list[str]:
    return [f"{i.id} {i.title}" for i in read_feed(text, Span(low, high)).instances]


def test_an_override_replaces_its_date_and_exdate_drops_one_and_both_name_all_day_dates_where_the_event_is() -> None:
    text = ("BEGIN:VCALENDAR\nX-WR-TIMEZONE:Europe/Berlin\n"
            "BEGIN:VEVENT\nUID:offsite7@calendar.example\nDTSTART;VALUE=DATE:20260920\nDTEND;VALUE=DATE:20260921\n"
            "RRULE:FREQ=WEEKLY;COUNT=3\nEXDATE;VALUE=DATE:20260927\nSUMMARY:Offsite\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:offsite7@calendar.example\nRECURRENCE-ID;VALUE=DATE:20261004\nDTSTART;VALUE=DATE:20261005\n"
            "DTEND;VALUE=DATE:20261006\nSUMMARY:Offsite moved\nEND:VEVENT\nEND:VCALENDAR\n")
    assert ids(text, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC)) == [
        "offsite7@calendar.example#20260920 Offsite", "offsite7@calendar.example#20261004 Offsite moved"]


def test_a_rule_it_does_not_read_stands_as_its_first_date_and_says_so() -> None:
    text = wrap("UID:odd\nDTSTART:20261118T100000Z\nRRULE:FREQ=HOURLY;COUNT=5\nATTENDEE:mailto:a@b.example")
    connector = make(text)
    assert [r.id for r in connector.list().records] == ["odd"]
    assert connector.notices == [("1 recurring events use a rule this connector does not read, so each shows as "
                                  "its first date alone")]


def test_lines_unfold_and_parameters_and_escapes_read() -> None:
    text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:e1\r\nDTSTART:20261115T100000Z\r\nDTEND:20261115T110000Z\r\n"
            "SUMMARY:Plan\\, then\\; ship\\nnext line and a long fold that goes \r\n over the limit\r\n"
            "ATTENDEE;CN=\"Doe\\, Jane: PhD\":mailto:Jane.Doe@Acme.example\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    instance = read_feed(text, Span(None, NOW)).instances[0]
    assert instance.title == "Plan, then; ship\nnext line and a long fold that goes over the limit"
    assert (instance.attendees[0].name, instance.attendees[0].email) == ("Doe, Jane: PhD", "jane.doe@acme.example")


@pytest.mark.parametrize(("rule", "start", "want"), [
    ("FREQ=DAILY;INTERVAL=2;COUNT=3", "20261102T090000", ["11-02", "11-04", "11-06"]),
    ("FREQ=WEEKLY;BYDAY=MO,TH;UNTIL=20261112", "20261102T090000", ["11-02", "11-05", "11-09", "11-12"]),
    ("FREQ=MONTHLY;BYDAY=-1FR;COUNT=3", "20261030T090000", ["10-30", "11-27", "12-25"]),
    ("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;COUNT=2", "20261030T090000", ["10-30", "11-30"]),
    ("FREQ=MONTHLY;BYMONTHDAY=31;COUNT=2", "20261031T090000", ["10-31", "12-31"]),
    ("FREQ=YEARLY;BYMONTH=11;BYDAY=4TH;COUNT=2", "20261126T090000", ["11-26", "11-25"]),
])
def test_the_rules_calendars_write_for_meetings_expand(rule: str, start: str, want: list[str]) -> None:
    got: list[datetime] = []
    first = datetime.strptime(start, "%Y%m%dT%H%M%S")
    parse_rule(rule).each(first, datetime(2028, 1, 1), lambda wall: parse_rule(rule).beyond(wall, lambda w: w),
                          lambda wall: got.append(wall) is None)
    assert [f"{w:%m-%d}" for w in got] == want and all(w.hour == 9 for w in got)


@pytest.mark.parametrize("rule", ["FREQ=HOURLY", "FREQ=DAILY;BYYEARDAY=1", "INTERVAL=2", "FREQ=WEEKLY;BYDAY=XX"])
def test_a_rule_beyond_those_shapes_is_refused(rule: str) -> None:
    with pytest.raises(RuleError):
        parse_rule(rule)


def test_a_zone_no_database_knows_reads_by_the_feeds_own_offsets_and_a_windows_name_by_its_table() -> None:
    feed = read_feed((DATA / "feed.ics").read_text(), Span(None, datetime(2027, 1, 1, tzinfo=UTC)))
    starts = {i.id: (i.start, i.zone) for i in feed.instances}
    assert starts["zone-fallback@fixture.example"] == (datetime(2026, 10, 15, 4, 30, tzinfo=UTC), "Fixture Mean Time")
    assert starts["northwind-sync@fixture.example"] == (datetime(2026, 10, 16, 16, tzinfo=UTC), "America/Los_Angeles")


def test_the_organizer_counts_as_a_key_and_an_address_of_yours_anywhere_is_never_one() -> None:
    text = wrap("UID:o\nDTSTART:20261118T170000Z\nDURATION:PT45M\nORGANIZER;CN=Wile:mailto:wile@acme.example\n"
                "ATTENDEE;CN=Jane:mailto:bh@home.example\nATTENDEE;CN=Jane:mailto:bh@home.example\n"
                "ATTENDEE;CN=Ann:mailto:ann@acme.example\nATTENDEE;CN=Ann again:mailto:ann@acme.example\n"
                "ATTENDEE;CN=Room;CUTYPE=ROOM:mailto:room@acme.example\nATTENDEE;CN=Bo:mailto:bo@acme.example")
    connector = CalendarConnector(calendar_source(), lambda: text, now=lambda: NOW, ours=Ours((ME, "bh@home.example"), (COMPANY,)))
    record = connector.get("o")
    assert record.keys == {"email": ["ann@acme.example", "bo@acme.example", "wile@acme.example"],
                           "domain": ["acme.example"], "calendar_event_id": ["o#20261118"]}
    assert record.carried == ["email", "domain"]  # its people's keys point at them: its event id names it
    assert record.fields["title"] == "Meeting" and record.fields["end"] == "2026-11-18T17:45:00Z"
    assert record.fields["attendees"] == ["me", {"name": "Ann", "email": "ann@acme.example"},
                                          {"name": "Bo", "email": "bo@acme.example"}]
    assert record.fields["organizer"] == {"name": "Wile", "email": "wile@acme.example"}


def test_an_all_day_meeting_keeps_the_days_of_its_own_zone_and_a_whole_day_steps_by_the_calendar() -> None:
    from zoneinfo import ZoneInfo
    berlin = ("BEGIN:VCALENDAR\nX-WR-TIMEZONE:Europe/Berlin\nBEGIN:VEVENT\nUID:d\nDTSTART;VALUE=DATE:20261120\n"
              "ATTENDEE:mailto:a@b.example\nEND:VEVENT\nEND:VCALENDAR\n")
    day = make(berlin).get("d")
    assert (day.at, day.fields["date"], day.fields["end"], day.fields["timezone"]) == (
        "2026-11-19T23:00:00Z", "2026-11-20", "2026-11-21", "Europe/Berlin")
    # 1 November ends daylight saving time in New York: a day ahead of 08:00 EDT is 08:00 EST, 13:00 UTC.
    edge = wrap("UID:in\nDTSTART:20261101T123000Z\nATTENDEE:mailto:a@b.example",
                "UID:out\nDTSTART:20261101T130000Z\nATTENDEE:mailto:a@b.example")
    connector = CalendarConnector(calendar_source(ahead="1d"), lambda: edge, zone=ZoneInfo("America/New_York"),
                                  now=lambda: datetime(2026, 10, 31, 12, tzinfo=UTC))
    assert [r.id for r in connector.list().records] == ["in"]
    hours = make(edge, calendar_source(ahead="36h"), now=datetime(2026, 10, 31, 1, tzinfo=UTC))
    assert [r.id for r in hours.list().records] == ["in"]


def test_an_until_in_utc_ends_a_zoned_series_and_an_override_takes_what_it_leaves_out_from_its_master() -> None:
    text = wrap("UID:w\nDTSTART;TZID=America/New_York:20261102T090000\nDTEND;TZID=America/New_York:20261102T100000\n"
                "RRULE:FREQ=WEEKLY;UNTIL=20261116T140000Z\nEXDATE;VALUE=DATE:20261109\nRDATE:20261112T150000Z\n"
                "SUMMARY:Weekly\nDESCRIPTION:Agenda.\nORGANIZER:mailto:o@acme.example\n"
                "ATTENDEE:mailto:a@acme.example",
                "UID:w\nRECURRENCE-ID;TZID=America/New_York:20261116T090000\nDTSTART:20261116T160000Z\n"
                "DTEND:20261116T170000Z\nSUMMARY:Weekly moved",
                "SUMMARY:No id\nDTSTART:20261116T160000Z")
    connector = make(text)
    records = every_page(connector).records
    assert [(r.id, r.fields["title"]) for r in records] == [
        ("w#20261102T140000Z", "Weekly"), ("w#20261112T150000Z", "Weekly"), ("w#20261116T140000Z", "Weekly moved")]
    moved = connector.get("w#20261116T140000Z")
    assert moved.body == "Agenda." and moved.fields["organizer"] == {"email": "o@acme.example"}
    assert moved.keys == {"email": ["a@acme.example", "o@acme.example"], "domain": ["acme.example"],
                          "calendar_event_id": ["w#20261116t140000z", "w#20261116"]}
    assert connector.notices == ["1 events have no id or no start, and were left out"]


def test_a_meeting_cancelled_deleted_or_taken_out_of_its_series_is_removed(events) -> None:
    feed = dict(events)
    connector = make(lambda: wrap(*feed.values()))
    cursor = every_page(connector).cursor
    feed["acme-qbr@fixture.example"] = feed["acme-qbr@fixture.example"] + "\nSTATUS:CANCELLED"
    del feed["acme-offsite@fixture.example"]
    feed["northwind-sync@fixture.example"] += "\nEXDATE;TZID=America/New_York:20261117T090000"
    assert every_page(connector, cursor).removed == ["acme-offsite@fixture.example", "acme-qbr@fixture.example",
                                                      "northwind-sync@fixture.example#20261117T140000Z"]


# -- a curator's questions
class Feed:
    """The feed as a stand-in: it records each read, and takes nothing a read could change it by."""

    def __init__(self, *events: str):
        self.events, self.reads = list(events), 0

    def __call__(self, *args: object, **kwargs: object) -> str:
        assert not args and not kwargs, "the connector handed the feed something, which a read never does"
        self.reads += 1
        return wrap(*self.events)


def invite(uid: str, start: str, organizer: str, *attendees: str) -> str:
    """An event at `start`, half an hour long, organized by an address, each attendee as `address:REPLY`."""
    who = "".join(f"ATTENDEE;PARTSTAT={reply}:mailto:{email}\n"
                  for email, reply in (a.split(":") for a in attendees))
    return (f"UID:{uid}\nDTSTART:{start}\nDTEND:{start[:9]}{int(start[9:11]) + 1:02d}{start[11:]}\n"
            f"SUMMARY:Secret plans for {uid}\nDESCRIPTION:Never in a row.\nORGANIZER:mailto:{organizer}\n{who}")


INVITES = (
    invite("i1", "20261110T150000Z", "noreply@calendar.initech.example", f"{ME}:ACCEPTED", "bob@initech.example:ACCEPTED"),
    invite("i2", "20261103T150000Z", "bob@initech.example", f"{ME}:ACCEPTED"),
    invite("g1", "20261116T150000Z", "carol@globex.example", f"{ME}:NEEDS-ACTION"),
    invite("g2", "20261125T150000Z", ME, "carol@globex.example:ACCEPTED", "casey@vandelay.example:ACCEPTED"),
)


def test_a_curator_asks_for_meetings_admits_a_domain_and_the_meetings_of_that_domain_reach_it(
        events: dict[str, str], tmp_path: Path) -> None:
    connector, store = make(Feed(*events.values())), MemoryStore()
    qbr, offsite = connector.get("acme-qbr@fixture.example"), connector.get("acme-offsite@fixture.example")
    assert reached(store, qbr) == []
    asked: list[dict] = []

    def script(tools: dict[str, Callable[..., object]]) -> None:
        asked.append(tools["fetch"](source="calendar", question="meetings"))
        tools["admit"](name="Acme", keys=asked[0]["rows"][0]["keys"], reason="a meeting on the calendar")

    done = curate(store, Asking(script), {"calendar": connector}, tmp_path)
    assert asked[0]["rows"] == [
        {"id": "acme.example", "keys": {"domain": ["acme.example"]}, "into": "",
         "fields": {"records": 2, "noun": "meetings", "took_part": 1, "how": ["accepted", "organized"],
                    "directions": "one", "first": "2026-11-18", "last": "2026-11-20", "sender": "person"}},
        {"id": "northwind.example", "keys": {"domain": ["northwind.example"]},
         "into": "", "fields": {"records": 2, "noun": "meetings", "took_part": 0, "first": "2026-11-10",
                                "last": "2026-11-17"}}]
    assert done.admitted == ["accounts/acme"]
    # its people's domain reaches the account, and binds the meeting to nothing: it only carries it
    assert reached(store, qbr) == reached(store, offsite) == ["accounts/acme"]
    assert bound_to(store, qbr) == bound_to(store, offsite) == []
    assert reached(store, connector.get("northwind-sync@fixture.example#20261117T140000Z")) == []


def test_a_row_tells_how_you_took_part_whether_invites_went_both_ways_and_whether_a_machine_sent_them() -> None:
    rows = {r.id: r for r in make(Feed(*INVITES)).ask("meetings").records}
    assert rows["initech.example"].fields == {
        "records": 2, "noun": "meetings", "took_part": 2, "how": ["accepted"], "directions": "both",
        "first": "2026-11-03", "last": "2026-11-10", "sender": "person and machine"}
    assert rows["globex.example"].fields == {
        "records": 2, "noun": "meetings", "took_part": 1, "how": ["organized"], "directions": "both",
        "first": "2026-11-16", "last": "2026-11-25", "sender": "person"}
    assert set(rows) == {"initech.example", "globex.example"}  # a colleague's domain is never a row


def test_each_side_an_invite_reaches_counts_as_its_direction_and_a_machine_alone_is_the_sender() -> None:
    feed = Feed(invite("o1", "20261112T150000Z", ME, "eve@omni.example:ACCEPTED"),
                invite("o2", "20261112T160000Z", ME, "dana@hooli.example:NEEDS-ACTION"),
                invite("o3", "20261112T170000Z", "dee@umbrella.example", f"{ME}:ACCEPTED"),
                invite("o4", "20261112T180000Z", "no-reply@dunder.example", f"{ME}:DECLINED"))
    rows = {r.id: r.fields for r in make(feed).ask("meetings").records}
    assert {d: (f["how"], f["directions"], f["sender"]) for d, f in rows.items() if "how" in f} == {
        "omni.example": (["organized"], "both", "person"), "hooli.example": (["organized"], "one", "person"),
        "umbrella.example": (["accepted"], "both", "person")}
    assert (rows["dunder.example"]["took_part"], rows["dunder.example"]["directions"],
            rows["dunder.example"]["sender"]) == (0, "one", "machine")


@pytest.mark.parametrize(("address", "machine"), [
    ("noreply@initech.example", True), ("noreply+invites@initech.example", True), ("No-Reply-7@initech.example", True),
    ("bounces@initech.example", True), ("bounce-42@initech.example", True), ("calendar-notification@initech.example", True),
    ("ana@calendar.initech.example", True), ("ana@eu.notifications.initech.example", True),
    ("ana@initech.example", False), ("calendar.team@initech.example", False), ("ana@noreply.example", False),
    ("ana@eu.calendar.example", False),
    ("ana+noreply@initech.example", False), ("not-an-address", False),
])
def test_an_address_reads_as_a_machines_by_its_local_part_or_a_label_in_front_of_its_domain(
        address: str, machine: bool) -> None:
    assert machine_address(address) is machine


def test_meetings_puts_the_latest_first_and_meetings_by_domain_the_most_you_took_part_in() -> None:
    connector = make(Feed(*INVITES))
    latest = connector.ask("meetings")
    assert [r.id for r in latest.records] == ["globex.example", "initech.example"]
    assert (latest.removed, latest.notices, latest.more, latest.cursor) == ([], [], False, None)
    assert [r.id for r in connector.ask("meetings_by_domain").records] == ["initech.example", "globex.example"]
    one = connector.ask("meetings", limit=1)
    assert [r.id for r in one.records] == ["globex.example"] and one.more and one.cursor is None
    assert not connector.ask("meetings", limit=2).more
    same_day = Feed(invite("a", "20261112T090000Z", "bo@globex.example"),
                    invite("b", "20261112T170000Z", "ana@initech.example"))
    assert [r.id for r in make(same_day).ask("meetings").records] == ["initech.example", "globex.example"]


def test_a_rows_days_are_the_meetings_dates_in_the_workspaces_zone() -> None:
    from zoneinfo import ZoneInfo

    feed = Feed(invite("k", "20261118T170000Z", "ana@initech.example"))
    rows = make(feed, zone=ZoneInfo("Pacific/Kiritimati")).ask("meetings").records
    assert (rows[0].fields["first"], rows[0].fields["last"]) == ("2026-11-19", "2026-11-19")


def test_days_and_ahead_bound_the_meetings_a_question_reads() -> None:
    connector = make(Feed(*INVITES))
    near = connector.ask("meetings", {"days": 1, "ahead": 0}).records
    assert [(r.id, r.fields["records"], r.fields["first"]) for r in near] == [("globex.example", 1, "2026-11-16")]
    assert {r.id: r.fields["records"] for r in connector.ask("meetings", {"days": "7", "ahead": 10}).records} == {
        "globex.example": 2, "initech.example": 1}
    assert {r.id: r.fields["records"] for r in connector.ask("meetings", {"days": 7, "ahead": 5}).records} == {
        "globex.example": 1, "initech.example": 1}
    assert connector.ask("meetings", {"days": 365, "ahead": 365}).records


def test_a_question_reads_the_feed_once_and_nothing_else_and_a_row_carries_no_title_or_description() -> None:
    feed = Feed(*INVITES, QBR)
    connector = make(feed)
    for question in connector.questions:
        before = feed.reads
        answer = connector.ask(question, {"days": 30})
        assert feed.reads == before + 1 and answer.records
        assert all(r.body is None and r.source == "calendar" for r in answer.records)
        text = json.dumps([[r.id, r.keys, r.fields, r.url, r.links, r.into] for r in answer.records])
        for said in ("Secret plans", "Never in a row", "Acme QBR", "Agenda", "Video call", "meet.google", "@"):
            assert said not in text
    assert feed.events == [*INVITES, QBR]  # what the feed holds, as a read leaves it


@pytest.mark.parametrize(("question", "params", "limit", "says"), [
    ("meetings", {"days": "soon"}, 25, "is not a whole number of days"),
    ("meetings", {"days": 400}, 25, "outside 0 to 365"),
    ("meetings", {"ahead": -1}, 25, "outside 0 to 365"),
    ("meetings_by_domain", {"days": True}, 25, "is not a whole number of days"),
    ("meetings", {"since": "2026-11-01"}, 25, "takes no param 'since': give days, ahead"),
    ("meetings", None, 0, "is not a count of rows"),
    ("busiest_days", None, 25, "ask one of meetings, meetings_by_domain"),
])
def test_a_question_with_params_it_does_not_take_is_refused_before_the_feed_is_read(
        question: str, params: dict | None, limit: int, says: str) -> None:
    feed = Feed(*INVITES)
    with pytest.raises(QuestionError) as refused:
        make(feed).ask(question, params, limit=limit)
    assert says in str(refused.value) and "calendar" in str(refused.value) and feed.reads == 0


def test_a_colleagues_calendar_answers_no_question_and_is_not_read() -> None:
    feed = Feed(*INVITES)
    source = SourceFile(name="calendar", kind="calendar", into="meetings", owner="casey@vandelay.example")
    answer = make(feed, source).ask("meetings")
    assert answer.records == [] and feed.reads == 0 and (answer.removed, answer.more) == ([], False)
    assert answer.notices == [("calendar is casey@vandelay.example's calendar, and only your own answers a curator: "
                               "ask your own calendar source")]


def test_through_fetch_a_feed_that_does_not_read_is_one_line_back_to_the_curator() -> None:
    reply = fetch_tool({"calendar": make("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\n")})(
        source="calendar", question="meetings")
    assert reply["refused"].startswith("calendar: the feed does not read") and "\n" not in reply["refused"]


def test_a_side_told_after_a_change_reads_a_meeting_as_a_connector_built_with_it_reads_it() -> None:
    new = "jane@vandelay-labs.example"  # an address of yours profile.md gains while serve runs
    text = wrap("UID:sync@fixture.example\nDTSTART:20261118T170000Z\nDTEND:20261118T180000Z\nSUMMARY:Sync\n"
                f"ORGANIZER;CN=Jane Doe:mailto:{new}\nATTENDEE;CN=Jane Doe;PARTSTAT=ACCEPTED:mailto:{new}\n"
                "ATTENDEE;CN=Wile Coyote:mailto:wile@acme.example\n")
    told = make(text)
    cursor = told.list(None).cursor
    told.take_ours(Ours((ME, new), (COMPANY,)))
    fresh = CalendarConnector(calendar_source(), lambda: text, now=lambda: NOW, ours=Ours((ME, new), (COMPANY,)))
    assert told.list(None).records[0].fields == fresh.list(None).records[0].fields
    assert told.list(None).records[0].fields["organizer"] == "me"
    assert told.list(cursor).records, "a change of side lists everything anew, as a restart does"


@pytest.mark.timeout(120)
def test_a_description_is_cut_on_bytes_and_masked_before_while_the_mask_is_held() -> None:
    from stored_shapes import swept

    from cerebro.connector import masking
    from cerebro.connectors.calendar import MAX_DESCRIPTION, _clip

    body = "the key is tok_qzvxmplrwbnhtk and more words follow here"
    went = ["the key is tok_qzvx…", "the key is tok_qzvxmplr…", "the key is tok_qzvxmplrwbnhtk…"]  # a cut at a character, with an ellipsis
    assert [_clip(body, n) for n in (22, 26, 33)] == went
    with masking(lambda text: text.replace("stored-only", "[secret]")):  # no stored value in it: as it was, so
        assert [_clip(body, n) for n in (22, 26, 33)] == went  # no revision moves
    blob = "Log:\n" + json.dumps({f"k{n}": n for n in range(3000)}, separators=(",", ":"))
    assert len(_clip(blob, MAX_DESCRIPTION).encode()) == MAX_DESCRIPTION
    assert not swept(lambda text: _clip(text, MAX_DESCRIPTION), MAX_DESCRIPTION)
