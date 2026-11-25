"""An iCal feed read by hand into occurrences: one for each meeting, or each date of a recurring one, in a window.

The reader takes line folding, parameters and escapes as RFC 5545 writes them. A time zone is read by its IANA name,
by the Windows name Outlook writes, or else by the offsets of the feed's own `VTIMEZONE`; an unknown one, a floating
time and an all-day date read in the feed's `X-WR-TIMEZONE`, or UTC. A recurring event is expanded inside the window
by `calendar_rrule`, less its `EXDATE`s, plus its `RDATE`s, and with each date an override replaces taken by the
override. A rule it does not read leaves the event as its first occurrence, and is counted. A stream that is not a
calendar, or one that ends inside a block, does not read: `FeedError`, so a feed cut short never takes a meeting
away. Nothing here reads the clock or the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import CerebroError
from .calendar_rrule import Rule, RuleError, parse_rule, parse_wall

__all__ = ["Feed", "FeedError", "Instance", "Person", "Span", "read_feed"]

MAX_DEPTH = 8
MAX_SERIES = 5000

WINDOWS_ZONES = {
    "alaskan standard time": "America/Anchorage", "arab standard time": "Asia/Riyadh",
    "arabian standard time": "Asia/Dubai", "argentina standard time": "America/Argentina/Buenos_Aires",
    "atlantic standard time": "America/Halifax", "aus eastern standard time": "Australia/Sydney",
    "canada central standard time": "America/Regina", "cen. australia standard time": "Australia/Adelaide",
    "central america standard time": "America/Guatemala", "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw", "central standard time": "America/Chicago",
    "central standard time (mexico)": "America/Mexico_City", "china standard time": "Asia/Shanghai",
    "e. africa standard time": "Africa/Nairobi", "e. australia standard time": "Australia/Brisbane",
    "e. south america standard time": "America/Sao_Paulo", "eastern standard time": "America/New_York",
    "egypt standard time": "Africa/Cairo", "fle standard time": "Europe/Kiev", "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik", "gtb standard time": "Europe/Bucharest",
    "hawaiian standard time": "Pacific/Honolulu", "india standard time": "Asia/Kolkata",
    "israel standard time": "Asia/Jerusalem", "korea standard time": "Asia/Seoul",
    "mexico standard time": "America/Mexico_City", "mountain standard time": "America/Denver",
    "new zealand standard time": "Pacific/Auckland", "newfoundland standard time": "America/St_Johns",
    "pacific standard time": "America/Los_Angeles", "pacific standard time (mexico)": "America/Tijuana",
    "pakistan standard time": "Asia/Karachi", "romance standard time": "Europe/Paris",
    "russian standard time": "Europe/Moscow", "sa pacific standard time": "America/Bogota",
    "se asia standard time": "Asia/Bangkok", "singapore standard time": "Asia/Singapore",
    "south africa standard time": "Africa/Johannesburg", "tokyo standard time": "Asia/Tokyo",
    "turkey standard time": "Europe/Istanbul", "us eastern standard time": "America/Indiana/Indianapolis",
    "us mountain standard time": "America/Phoenix", "utc": "UTC", "w. australia standard time": "Australia/Perth",
    "w. europe standard time": "Europe/Berlin", "west pacific standard time": "Pacific/Port_Moresby",
    "tasmania standard time": "Australia/Hobart", "west asia standard time": "Asia/Tashkent",
    "dateline standard time": "Etc/GMT+12", "coordinated universal time": "UTC",
    "singapore malay peninsula": "Asia/Singapore", "taipei standard time": "Asia/Taipei",
    "w. central africa standard time": "Africa/Lagos", "namibia standard time": "Africa/Windhoek",
    "morocco standard time": "Africa/Casablanca", "e. europe standard time": "Europe/Chisinau",
    "belarus standard time": "Europe/Minsk", "iran standard time": "Asia/Tehran",
    "bangladesh standard time": "Asia/Dhaka", "sri lanka standard time": "Asia/Colombo",
    "nepal standard time": "Asia/Kathmandu", "venezuela standard time": "America/Caracas",
    "pacific sa standard time": "America/Santiago", "montevideo standard time": "America/Montevideo",
    "paraguay standard time": "America/Asuncion", "sa western standard time": "America/La_Paz",
    "sa eastern standard time": "America/Cayenne", "greenland standard time": "America/Godthab",
    "azores standard time": "Atlantic/Azores", "cape verde standard time": "Atlantic/Cape_Verde",
    "mauritius standard time": "Indian/Mauritius", "jordan standard time": "Asia/Amman",
    "middle east standard time": "Asia/Beirut", "syria standard time": "Asia/Damascus",
    "ekaterinburg standard time": "Asia/Yekaterinburg", "n. central asia standard time": "Asia/Novosibirsk",
    "north asia standard time": "Asia/Krasnoyarsk", "north asia east standard time": "Asia/Irkutsk",
    "yakutsk standard time": "Asia/Yakutsk", "vladivostok standard time": "Asia/Vladivostok",
    "magadan standard time": "Asia/Magadan", "kamchatka standard time": "Asia/Kamchatka",
    "fiji standard time": "Pacific/Fiji", "samoa standard time": "Pacific/Apia",
    "tonga standard time": "Pacific/Tongatapu", "cen. australia daylight time": "Australia/Adelaide",
    "aus central standard time": "Australia/Darwin", "mid-atlantic standard time": "Atlantic/South_Georgia",
    "afghanistan standard time": "Asia/Kabul", "myanmar standard time": "Asia/Yangon",
    "mongolia standard time": "Asia/Ulaanbaatar", "ulaanbaatar standard time": "Asia/Ulaanbaatar",
    "georgian standard time": "Asia/Tbilisi", "caucasus standard time": "Asia/Yerevan",
    "azerbaijan standard time": "Asia/Baku", "arabic standard time": "Asia/Baghdad",
    "kaliningrad standard time": "Europe/Kaliningrad", "libya standard time": "Africa/Tripoli",
    "sudan standard time": "Africa/Khartoum", "central brazilian standard time": "America/Cuiaba",
    "bahia standard time": "America/Bahia", "central standard time (mexico) 2": "America/Mexico_City",
}

_CONFERENCE = re.compile(r"(?i)https?://[^\s<>\"')\]]*(?:zoom\.us|zoomgov\.com|meet\.google\.com|teams\.microsoft\."
                         r"com|teams\.live\.com|webex\.com|gotomeet\.me|gotomeeting\.com|whereby\.com|bluejeans\.com|"
                         r"chime\.aws)[^\s<>\"')\]]*")
_CONFERENCE_PROPS = ("X-GOOGLE-CONFERENCE", "X-MICROSOFT-SKYPETEAMSMEETINGURL",
                     "X-MICROSOFT-ONLINEMEETINGEXTERNALLINK", "CONFERENCE")


class FeedError(CerebroError, ValueError):
    """A feed that does not read: not a calendar, or cut short inside a block."""


# -- the stream
@dataclass
class _Prop:
    name: str
    value: str
    params: dict[str, list[str]] = field(default_factory=dict)

    def param(self, name: str) -> str:
        return (self.params.get(name) or [""])[0]


@dataclass
class _Block:
    name: str
    props: list[_Prop] = field(default_factory=list)
    subs: list[_Block] = field(default_factory=list)

    def first(self, name: str) -> _Prop | None:
        return next((p for p in self.props if p.name == name), None)

    def all(self, name: str) -> list[_Prop]:
        return [p for p in self.props if p.name == name]

    def text(self, name: str) -> str:
        prop = self.first(name)
        return _unescape(prop.value) if prop is not None else ""


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split(text: str, sep: str) -> list[str]:
    """`text` split at `sep` where it is not inside double quotes."""
    parts, quoted, begin = [], False, 0
    for i, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif char == sep and not quoted:
            parts.append(text[begin:i])
            begin = i + 1
    return parts + [text[begin:]]


def _line(line: str) -> _Prop | None:
    """`NAME;PARAM=value;PARAM="a,b":value`. The first colon outside quotes ends the name and parameters."""
    quoted, colon = False, -1
    for i, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            colon = i
            break
    if colon <= 0:
        return None
    head = _split(line[:colon], ";")
    name = head[0].strip().upper()
    if not name:
        return None
    prop = _Prop(name, line[colon + 1:])
    for part in head[1:]:
        key, eq, value = part.partition("=")
        if eq:
            prop.params.setdefault(key.strip().upper(), []).extend(
                v.strip().strip('"') for v in _split(value, ","))
    return prop


def _unescape(text: str) -> str:
    """A TEXT value: `\\n` and `\\N` are line breaks, and any other escaped character stands for itself."""
    if "\\" not in text:
        return text
    out, i = [], 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i < len(text) - 1:
            i += 1
            char = "\n" if text[i] in "nN" else text[i]
        out.append(char)
        i += 1
    return "".join(out)


def _blocks(text: str) -> list[_Block]:
    text = text.removeprefix("﻿")
    if not text.lstrip(" \t\r\n").startswith("BEGIN:"):
        raise FeedError("it is not an iCalendar stream")
    roots: list[_Block] = []
    stack: list[_Block] = []
    for raw in _unfold(text):
        prop = _line(raw)
        if prop is None:
            continue
        if prop.name == "BEGIN":
            if len(stack) >= MAX_DEPTH:
                raise FeedError(f"its blocks nest more than {MAX_DEPTH} deep")
            block = _Block(prop.value.strip().upper())
            (stack[-1].subs if stack else roots).append(block)
            stack.append(block)
        elif prop.name == "END":
            name = prop.value.strip().upper()
            for i in range(len(stack) - 1, -1, -1):
                if stack[i].name == name:
                    del stack[i:]
                    break
        elif stack:
            stack[-1].props.append(prop)
    if not roots:
        raise FeedError("it is not an iCalendar stream")
    if stack:
        raise FeedError(f"it ends before END:{stack[0].name}, so it was cut short")
    return roots


# -- time zones
def _load(name: str) -> ZoneInfo | None:
    """The zone an IANA or Windows name stands for. A name with a path in front, as
    `/mozilla.org/20050126_1/America/New_York`, is read by its last labels."""
    name = name.strip().strip('"')

    def attempt(candidate: str) -> ZoneInfo | None:
        if not candidate or candidate == "Local":
            return None
        candidate = WINDOWS_ZONES.get(candidate.lower(), candidate)
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return None

    found = attempt(name)
    labels = name.strip("/").split("/")
    for i in range(1, len(labels)):
        if found is not None:
            break
        found = attempt("/".join(labels[i:]))
    return found


def _offset(text: str) -> int | None:
    """`+hhmm` or `-hhmmss` in seconds."""
    text = text.strip()
    if len(text) < 5 or text[0] not in "+-" or not text[1:5].isdigit():
        return None
    seconds = int(text[1:3]) * 3600 + int(text[3:5]) * 60
    if len(text) >= 7:
        if not text[5:7].isdigit():
            return None
        seconds += int(text[5:7])
    return -seconds if text[0] == "-" else seconds


@dataclass(eq=False)
class _Observance:
    """One STANDARD or DAYLIGHT block: when it begins, in local time, the rule that repeats it, and the offsets."""

    start: datetime
    before: int
    after: int
    rule: Rule | None = None
    dates: list[datetime] = field(default_factory=list)
    _years: dict[int, list[tuple[datetime, int]]] = field(default_factory=dict)

    def onsets(self, year: int) -> list[tuple[datetime, int]]:
        """The UTC instants, naive, it begins in the years around `year`, each with its offset after."""
        if year not in self._years:
            walls = [self.start] if self.rule is None else self.rule.between(
                self.start, datetime(year - 1, 1, 1), datetime(year + 2, 1, 1))
            self._years[year] = [(w - timedelta(seconds=self.before), self.after) for w in walls + self.dates]
        return self._years[year]


@dataclass(eq=False)
class _Offsets:
    """A `VTIMEZONE` read for its offsets, the fallback for a TZID no zone database knows."""

    id: str
    observances: list[_Observance]

    def offset_at(self, instant: datetime) -> int:
        best: tuple[datetime, int] | None = None
        for observance in self.observances:
            for onset in observance.onsets(instant.year):
                if onset[0] <= instant and (best is None or onset[0] > best[0]):
                    best = onset
        if best is None:
            return min(self.observances, key=lambda o: o.start).before
        return best[1]

    def instant(self, wall: datetime) -> datetime:
        guess = wall - timedelta(seconds=self.observances[0].after)
        for _ in range(3):
            following = wall - timedelta(seconds=self.offset_at(guess))
            if following == guess:
                break
            guess = following
        return guess


def _offsets(block: _Block) -> _Offsets | None:
    observances = []
    for sub in block.subs:
        if sub.name not in ("STANDARD", "DAYLIGHT"):
            continue
        after, before = _offset(sub.text("TZOFFSETTO")), _offset(sub.text("TZOFFSETFROM"))
        start = sub.first("DTSTART")
        read = parse_wall(start.value) if start is not None else None
        if after is None or read is None:
            continue
        observance = _Observance(read[0], after if before is None else before, after)
        rule = sub.first("RRULE")
        if rule is not None:
            try:
                observance.rule = parse_rule(rule.value)
            except RuleError:
                pass
        for prop in sub.all("RDATE"):
            observance.dates += [r[0] for r in map(parse_wall, prop.value.split(",")) if r is not None]
        observances.append(observance)
    return _Offsets(block.text("TZID"), observances) if observances else None


@dataclass(frozen=True)
class _Zone:
    """Where an event's wall clock is read: a zone from the database, or a `VTIMEZONE`'s offsets."""

    name: str
    loc: tzinfo | None = None
    offsets: _Offsets | None = None

    def at(self, wall: datetime) -> datetime:
        """The UTC instant of a wall reading."""
        if self.offsets is not None:
            return self.offsets.instant(wall).replace(tzinfo=UTC)
        return wall.replace(tzinfo=self.loc or UTC).astimezone(UTC)

    def wall(self, instant: datetime) -> datetime:
        """The wall reading of a UTC instant."""
        if self.offsets is not None:
            naive = instant.astimezone(UTC).replace(tzinfo=None)
            return naive + timedelta(seconds=self.offsets.offset_at(naive))
        return instant.astimezone(self.loc or UTC).replace(tzinfo=None)


_UTC = _Zone("UTC", UTC)


class _Book:
    """The TZIDs of one feed."""

    def __init__(self, roots: list[_Block]):
        self.offsets: dict[str, _Offsets] = {}
        self.default = _UTC
        for root in roots:
            for prop in root.all("X-WR-TIMEZONE"):
                loc = _load(prop.value)
                if loc is not None:
                    self.default = _Zone(loc.key, loc)
            for sub in root.subs:
                if sub.name == "VTIMEZONE":
                    read = _offsets(sub)
                    if read is not None and read.id:
                        self.offsets[read.id] = read

    def find(self, tzid: str) -> _Zone:
        """The database first, with the Windows names, then the feed's own offsets, else the feed's default."""
        tzid = tzid.strip().strip('"')
        if not tzid:
            return self.default
        if tzid.upper() in ("UTC", "GMT", "Z"):
            return _UTC
        loc = _load(tzid)
        if loc is not None:
            return _Zone(loc.key, loc)
        for id, offsets in self.offsets.items():
            if id == tzid or id.lower() == tzid.lower():
                return _Zone(id, offsets=offsets)
        return self.default

    def time(self, prop: _Prop) -> tuple[datetime, _Zone, datetime, bool] | None:
        """A DTSTART-like value: its instant, its zone, its wall reading, and whether it is a date alone."""
        read = parse_wall(prop.value)
        if read is None:
            return None
        wall, utc = read
        if prop.param("VALUE").upper() == "DATE" or len(prop.value.strip()) == 8:
            return self.default.at(wall), self.default, wall, True
        if utc:
            return wall.replace(tzinfo=UTC), _UTC, wall, False
        zone = self.find(prop.param("TZID"))
        return zone.at(wall), zone, wall, False


# -- events
@dataclass
class Person:
    """An organizer or an attendee: a name, an address in lower case, and a reply in lower case, any of them
    empty."""

    name: str = ""
    email: str = ""
    response: str = ""


@dataclass
class Instance:
    """One occurrence: a single event, one date of a recurring one, or an override of one. `id` is the UID, with `#`
    and the occurrence's key for a date of a recurring event. `start` and `end` are UTC instants; an all-day event
    runs midnight to midnight in the feed's zone. `zone` is the IANA name it is written in, or the TZID its
    `VTIMEZONE` gives. `revision` is its `SEQUENCE` and `LAST-MODIFIED`."""

    id: str
    uid: str
    title: str
    description: str
    location: str
    start: datetime
    end: datetime
    all_day: bool
    zone: str
    cancelled: bool
    tentative: bool
    revision: str
    organizer: Person | None
    attendees: list[Person]
    conference: str


@dataclass
class Span:
    """The time a read keeps: from `low`, or all of the past when it is None, up to `high`."""

    low: datetime | None
    high: datetime

    def holds(self, start: datetime, end: datetime) -> bool:
        return (self.low is None or end >= self.low) and start < self.high


@dataclass
class Feed:
    """What one read gave: the occurrences in the span in start order, how many events the stream held, those left
    out for want of a UID or a start, and those whose rule is not read, which stand as their first date."""

    instances: list[Instance]
    uids: set[str] = field(default_factory=set)  # every event the stream holds, in the span or not
    events: int = 0
    unread: int = 0
    unexpanded: int = 0


@dataclass
class _Event:
    uid: str
    title: str
    description: str
    location: str
    start: datetime
    end: datetime
    all_day: bool
    zone: _Zone
    wall: datetime
    rule: Rule | None = None
    bad_rule: bool = False
    exdates: list[datetime] = field(default_factory=list)
    exdays: set[str] = field(default_factory=set)
    rdates: list[datetime] = field(default_factory=list)
    rid: datetime | None = None
    cancelled: bool = False
    tentative: bool = False
    seq: int = 0
    modified: str = ""
    organizer: Person | None = None
    attendees: list[Person] = field(default_factory=list)
    conference: str = ""


def _duration(text: str) -> timedelta | None:
    """An iCalendar DURATION, such as `PT1H30M` or `P1D`."""
    text = text.strip().upper()
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    if not text.startswith("P"):
        return None
    total, timed, number = timedelta(), False, ""
    units = {("W", False): timedelta(weeks=1), ("D", False): timedelta(days=1), ("H", True): timedelta(hours=1),
             ("M", True): timedelta(minutes=1), ("S", True): timedelta(seconds=1)}
    for char in text[1:]:
        if char.isdigit():
            number += char
        elif char == "T":
            timed = True
        elif number and (char, timed) in units:
            total += int(number) * units[(char, timed)]
            number = ""
        else:
            return None
    return sign * total if not number else None


def _person(prop: _Prop) -> Person | None:
    who = Person(_unescape(prop.param("CN")).strip(), "", prop.param("PARTSTAT").strip().lower())
    value = prop.value.strip()
    if value[:7].lower() == "mailto:":
        value = value[7:]
    if "@" in value:
        who.email = value.strip().lower()
    elif "@" in prop.param("EMAIL"):
        who.email = prop.param("EMAIL").strip().lower()
    return who if who.email or who.name else None


def _conference(block: _Block, *texts: str) -> str:
    """The link to join: the properties Google and Microsoft write and RFC 7986's, then a link in the text."""
    for name in _CONFERENCE_PROPS:
        prop = block.first(name)
        if prop is not None:
            value = _unescape(prop.value).strip()
            if value.lower().startswith("http"):
                return value
    for text in texts:
        found = _CONFERENCE.search(text)
        if found:
            return found.group(0).rstrip(".,;:!?")
    return ""


def _event(block: _Block, book: _Book) -> _Event | None:
    uid = block.text("UID").strip()
    begin = block.first("DTSTART")
    read = book.time(begin) if uid and begin is not None else None
    if read is None:
        return None
    start, zone, wall, all_day = read
    event = _Event(uid, block.text("SUMMARY").strip(), block.text("DESCRIPTION").strip(),
                   block.text("LOCATION").strip(), start, start, all_day, zone, wall)
    end_prop, ends = block.first("DTEND"), None
    if end_prop is not None:
        read_end = book.time(end_prop)
        ends = read_end[0] if read_end is not None and read_end[0] >= start else None
    elif block.first("DURATION") is not None:
        length = _duration(block.text("DURATION"))
        ends = start + length if length is not None else None
    if ends is None and all_day:
        ends = zone.at(wall + timedelta(days=1))
    event.end = ends or start
    rule = block.first("RRULE")
    if rule is not None:
        try:
            event.rule = parse_rule(rule.value)
        except RuleError:
            event.bad_rule = True
    for prop in block.all("EXDATE"):
        for value in prop.value.split(","):
            excluded = book.time(_Prop(prop.name, value, prop.params))
            if excluded is None:
                continue
            if excluded[3] and not all_day:
                event.exdays.add(excluded[2].strftime("%Y%m%d"))
            else:
                event.exdates.append(excluded[0])
    for prop in block.all("RDATE"):
        if prop.param("VALUE").upper() == "PERIOD":
            continue
        for value in prop.value.split(","):
            added = book.time(_Prop(prop.name, value, prop.params))
            if added is not None:
                event.rdates.append(added[0])
    rid = block.first("RECURRENCE-ID")
    if rid is not None:
        named = book.time(rid)
        if named is not None:
            event.rid = named[0]
    status = block.text("STATUS").strip().upper()
    event.cancelled, event.tentative = status == "CANCELLED", status == "TENTATIVE"
    try:
        event.seq = int(block.text("SEQUENCE").strip() or 0)
    except ValueError:
        event.seq = 0
    event.modified = (block.text("LAST-MODIFIED") or block.text("DTSTAMP")).strip()
    organizer = block.first("ORGANIZER")
    event.organizer = _person(organizer) if organizer is not None else None
    for prop in block.all("ATTENDEE"):
        if prop.param("CUTYPE").upper() in ("RESOURCE", "ROOM"):
            continue
        who = _person(prop)
        if who is not None:
            event.attendees.append(who)
    event.conference = _conference(block, event.description, event.location)
    return event


def _key(event: _Event, start: datetime) -> str:
    """The occurrence of `event` that starts at `start`: its instant in UTC, or for an all-day event its date where
    the event is, the date Google's instance ids carry."""
    if event.all_day:
        return event.zone.wall(start).strftime("%Y%m%d")
    return start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _build(event: _Event, start: datetime, end: datetime, id: str, master: _Event | None = None) -> Instance:
    """The occurrence from `start` to `end`. A field an override leaves out reads as its master's."""
    instance = Instance(id, event.uid, event.title, event.description, event.location, start.astimezone(UTC),
                        end.astimezone(UTC), event.all_day, event.zone.name, event.cancelled, event.tentative,
                        f"{event.seq}:{event.modified}", event.organizer, event.attendees, event.conference)
    if master is not None:
        instance.title = instance.title or master.title
        instance.description = instance.description or master.description
        instance.location = instance.location or master.location
        instance.conference = instance.conference or master.conference
        instance.organizer = instance.organizer or master.organizer
        instance.attendees = instance.attendees or master.attendees
    return instance


def _expand(master: _Event, span: Span, replaced: set[str]) -> list[Instance]:
    length = master.end - master.start
    if master.rule is None and not master.rdates:
        instance = _build(master, master.start, master.end, master.uid)
        return [instance] if span.holds(instance.start, instance.end) else []
    excluded = {_key(master, x) for x in master.exdates}
    starts: list[datetime] = []
    if master.rule is not None:
        rule, zone, beyond_high = master.rule, master.zone, span.high + timedelta(days=2)

        def keep(wall: datetime) -> bool:
            start = zone.at(wall)
            if span.holds(start, start + length):
                starts.append(start)
            return len(starts) < MAX_SERIES and start < beyond_high

        rule.each(master.wall, zone.wall(span.high) + timedelta(days=2),
                  lambda wall: rule.beyond(wall, lambda w: zone.at(w).replace(tzinfo=None)), keep)
    else:
        starts.append(master.start)
    for added in master.rdates:
        if span.holds(added, added + length) and added not in starts:
            starts.append(added)
    out = []
    for start in starts:
        key = _key(master, start)
        if key in excluded or key in replaced or master.zone.wall(start).strftime("%Y%m%d") in master.exdays:
            continue
        out.append(_build(master, start, start + length, f"{master.uid}#{key}"))
    return out


def _instances(events: list[_Event], span: Span) -> list[Instance]:
    """One UID's occurrences: the master's, less what its overrides replace, and each override as its own."""
    master: _Event | None = None
    overrides = []
    for event in events:
        if event.rid is not None:
            overrides.append(event)
        elif master is None or event.seq > master.seq:
            master = event
    out = _expand(master, span, {_key(o, o.rid) for o in overrides}) if master is not None else []
    for override in overrides:
        instance = _build(override, override.start, override.end, f"{override.uid}#{_key(override, override.rid)}",
                          master)
        if span.holds(instance.start, instance.end):
            out.append(instance)
    return out


def read_feed(text: str, span: Span) -> Feed:
    """The occurrences of a feed inside `span`, in start order. Raises `FeedError` for a feed that does not read."""
    roots = _blocks(text)
    book = _Book(roots)
    feed = Feed([])
    groups: dict[str, list[_Event]] = {}
    for root in roots:
        for block in root.subs:
            if block.name != "VEVENT":
                continue
            feed.events += 1
            event = _event(block, book)
            if event is None:
                feed.unread += 1
                continue
            feed.unexpanded += event.bad_rule
            groups.setdefault(event.uid, []).append(event)
    feed.uids = set(groups)
    for events in groups.values():
        feed.instances += _instances(events, span)
    feed.instances.sort(key=lambda i: (i.start, i.id))
    return feed
