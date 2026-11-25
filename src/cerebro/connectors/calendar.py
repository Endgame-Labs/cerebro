"""An iCal feed as a source, read only: each meeting in its window is one record.

The connector is read only by nature. It is handed a function that gives the feed's text, and it reads the whole
feed on every `list` and `get`, as the docs say a calendar check does. It never fetches, never reads a secret address
from the environment or the disk, and has no way to write anything back.

A record's id is the event's UID, and for one date of a recurring event the UID, `#`, and that occurrence's key:
its start in UTC as `20261020T190000Z`, or an all-day date as `20261020`. Its time is its start. Its keys, what can
bind it to an article, are the addresses of its organizer and attendees under `email`, and their companies'
domains under `domain`, never yours and never one at your company's domains, nor a public mail service's domain.
Its fields are a meeting's, less what binding fills in: a timed meeting's `date`,
`start` and `end` in the workspace's zone, an all-day meeting's days as the event names them, and `status` upcoming
until it ends. Its body is the organizer's description, which a meeting extracts into nothing unless its source
file says otherwise. Its revision is a hash of all that, so it moves when the event changes, and when it ends.

The window reaches `window.behind` back, 90 days unless the file says, or all the past, and `window.ahead` on, 14
days unless the file says, whole days stepped in the workspace's zone. A cancelled event, one outside the window, and
one with no one from outside, only you or colleagues, is not listed. Only one cancelled, deleted from the feed, or
taken out of its series inside the window is in `removed`: one that left the window or the scope still exists. A
feed that does not read raises `CalendarError` and takes nothing away.

The connector keeps no copy of the feed. A listing's cursor carries each listed id and its revision, which is all
it needs to know an event changed or left.

A curator may ask it `meetings` and `meetings_by_domain`, as `QUESTIONS` says: the feed read once over the days the
question names, one row for each outside domain of its meetings, as `questions.rows` makes them, never a title or a
description. Only your own calendar answers: one whose source file names a colleague as its owner gives no rows.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import zlib
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ..clock import system_clock
from ..connector import (
    NOBODY,
    Connector,
    CursorError,
    Listing,
    Ours,
    Record,
    RecordNotFound,
    address_keys,
    event_key,
    gone,
    masked_first,
    own_people,
)
from ..errors import CerebroError
from ..source import SourceError, SourceFile, span
from .calendar_ical import FeedError, Instance, Person, Span, read_feed
from .connecting import Connecting, Need, Prompt, Way
from .questions import LATEST, MOST_ENGAGED, Conversation, Days, asked, machine_address, rows

__all__ = ["CalendarConnector", "CalendarError", "CursorError"]

KIND = "calendar"
SOURCE_KEYS = ("auth", "extract", "window", "into", "owner", "freshness")
REFUSED_KEYS = {"select": "a calendar reads its whole feed: delete select"}
INTO = "calls"
# The keys an event only carries, which point at its people and their companies: its `calendar_event_id` names it.
CARRIED = ("email", "domain")
DEFAULT_AHEAD = "14d"
PAGE_SIZE = 100
MAX_REDIRECTS = 5  # hops a feed may take, each on https
MAX_DESCRIPTION = 4000
CUT_MARK = "…"
_CURSOR = "calendar1."
# The questions a curator may ask the source with `ask`, each with the params it takes.
QUESTIONS = {
    "meetings": "one row for each outside domain of the meetings from `days` back (14 by default) to `ahead` on (14 "
                "by default), the latest meeting first: how many, how many you accepted or organized, whether "
                "invites went both ways, the first and last day, and whether a person or a machine organized them; "
                "never a title or a description; params: days, ahead",
    "meetings_by_domain": "the same rows from `days` back (90 by default) to `ahead` on (14 by default), the most "
                          "meetings you took part in first, for who you meet most; params: days, ahead",
}
MAX_ASK_DAYS = 365
TAKES = {"meetings": {"days": Days(14, 0, MAX_ASK_DAYS), "ahead": Days(14, 0, MAX_ASK_DAYS)},
         "meetings_by_domain": {"days": Days(90, 0, MAX_ASK_DAYS), "ahead": Days(14, 0, MAX_ASK_DAYS)}}


class CalendarError(CerebroError, ValueError):
    """The feed does not read: not a calendar, or cut short. Nothing is taken away."""


def _stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _clip(text: str, limit: int) -> str:
    """Text cut to `limit` bytes of UTF-8, the cut's mark included, at a character's edge."""
    text = masked_first(text)
    data = text.encode()
    if len(data) <= limit:
        return text
    cut = data[:max(limit - len(CUT_MARK.encode()), 0)].decode(errors="ignore")
    return cut.rstrip(" \n") + CUT_MARK


# What connect says and asks: where the calendar lives first, where the terminal draws, then where that provider
# keeps the secret address; on a plain line, both places at once.
CONNECTING = Connecting(
    plain=(("In Google Calendar, open Settings, pick the calendar, then Integrate calendar, and copy the secret "
           "address in iCal format."),
           ("In Outlook on the web, open Settings, Calendar, Shared calendars, publish the calendar, and copy its ICS "
           "link."), ""),
    asks=(Prompt("Secret address", "Secret address"),),
    title="Calendar",
    choices=(
        Way("Google Calendar", says=(
            ("Provide the secret iCal address for this calendar. Open `Settings`, select your calendar under "
            "`Settings for my calendars`, click `Integrate calendar`, then copy `Secret address in iCal format`."), ""),
            asks=(Prompt("Secret address", "Secret address"),)),
        Way("Outlook Calendar (web-based)", says=(
            "Open `Settings > Calendar > Shared calendars`, publish the calendar, then copy its `ICS link`.", ""),
            asks=(Prompt("ICS link", "ICS link"),)),
        Way("Other calendar (ICS link)", says=(
            ("Publish the calendar from its app's sharing settings, then copy its `ICS link`, the address of its iCal "
            "feed."), ""),
            asks=(Prompt("ICS link", "ICS link"),))),
    later="The calendar is not connected yet. Run cerebro connect {name} when you have its secret address.",
    later_check="the calendar is not connected yet: run cerebro connect {name} when you have its secret address",
    needs="the secret address of your calendar's iCal feed",
    connected="Stored the calendar address as {secret}, in {where}.",
    reads_kept=False)  # asked for each time


class CalendarConnector(Connector):
    """A `kind: calendar` source over the feed `feed()` gives. `now` is the clock the window and status are read
    by, `zone` the workspace's time zone, and `ours` your own addresses and your company's domains, which are never
    keys."""

    IDENTITY_KEYS: ClassVar[Mapping[str, int]] = {"calendar_event_id": 500}  # one meeting, by its event: what its calendar entry, recording and notes share

    connecting = CONNECTING

    @classmethod
    def login_name(cls, source: SourceFile) -> str | None:
        """`calendar` for a source named calendar, else `calendar_<name>`, each dot and dash an underscore."""
        return "calendar" if source.name == "calendar" else "calendar_" + re.sub(r"[-.]", "_", source.name)

    @classmethod
    def needs(cls, source: SourceFile, item: str) -> Need | None:
        return Need("address", item, "the calendar's secret address")

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> CalendarConnector:
        login = app.login(source)

        def feed() -> str:
            address = login()
            address = "https://" + address[len("webcal://"):] if address.startswith("webcal://") else address
            if not address.startswith("https://"):
                raise CalendarError(f"{source.name}: the address kept for it is not https: run cerebro connect "
                                    f"{source.name} --replace with the calendar's secret address")
            try:
                reply = app.http.get(address)
                for _ in range(MAX_REDIRECTS):  # each hop must stay on https, so the address never goes in clear
                    if not reply.is_redirect:
                        break
                    if not str(reply.next_request.url if reply.next_request else "").startswith("https://"):
                        raise CalendarError(f"{source.name}: the feed redirects off https, so it is not followed: "
                                            f"give the calendar's https secret address")
                    reply = app.http.send(reply.next_request)
            except httpx.HTTPError as error:  # its text can carry the address, so only its type is said
                raise CalendarError(f"{source.name}: the feed did not answer ({type(error).__name__}): check the "
                                    f"network, then check again") from None
            if reply.is_redirect or reply.status_code >= 400:
                raise CalendarError(f"{source.name}: the secret address no longer works (HTTP {reply.status_code}): "
                                    f"an admin may have turned off external sharing")
            return reply.text

        return cls(source, feed, now=app.clock, zone=app.zone, ours=app.ours)

    def __init__(self, source: SourceFile, feed: Callable[[], str], *, now: Callable[[], datetime] = system_clock,
                 zone: tzinfo = UTC, ours: Ours = NOBODY, page_size: int = PAGE_SIZE):
        label = f"sources/{source.name}.yaml"
        if source.kind != KIND:
            raise SourceError(f"{label} is kind {source.kind}: a calendar connector reads kind calendar alone")
        try:
            self.behind = source.behind()
            self.ahead = span(source.window.ahead or DEFAULT_AHEAD)
        except ValueError as error:
            raise SourceError(f"{label} window: {error}") from None
        self.name = source.name
        self.source = source
        self.feed = feed
        self.now = now
        self.zone = zone
        self.page_size = max(1, page_size)
        self.notices: list[str] = []
        self.take_ours(ours)

    def take_ours(self, ours: Ours) -> None:
        """Tell your side from theirs by `ours`, at the start and after a change: your addresses, the company's
        domains, and the source's config, which holds both, so a change lists everything anew as a restart would."""
        self.ours = ours
        self.me = frozenset(e.strip().lower() for e in ours.addresses)
        self.company = frozenset(d.strip().strip(".").lstrip("@").lower() for d in ours.domains)
        config = json.dumps([asdict(self.source.window), self.source.extract, self.source.into, sorted(self.me),
                             sorted(self.company), str(self.zone)], sort_keys=True)
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]

    # -- the window
    def _step(self, moment: datetime, length: timedelta, sign: int) -> datetime:
        """`length` on from `moment`, or back: whole days as calendar days in the workspace's zone."""
        if length % timedelta(days=1):
            return moment + sign * length
        return (moment.astimezone(self.zone) + sign * length).astimezone(UTC)

    def span(self, now: datetime) -> Span:
        """The window as of `now`: from `behind` back, or all the past, to `ahead` on."""
        low = self._step(now, self.behind, -1) if self.behind is not None else None
        return Span(low, self._step(now, self.ahead, 1))

    # -- people
    def _person(self, who: Person) -> str | dict[str, str]:
        if who.email and who.email in self.me:
            return "me"
        return {k: v for k, v in (("name", who.name), ("email", who.email), ("response", who.response)) if v}

    def _keys(self, instance: Instance) -> dict[str, list[str]]:
        people = ([instance.organizer] if instance.organizer else []) + instance.attendees
        keys = address_keys((p.email for p in people), self.ours)
        # its own occurrence, and its day, the form a recording takes whose event id has no date of its own
        named = list(dict.fromkeys(k for k in (event_key(instance.id, instance.start),
                                               event_key(instance.uid, instance.start)) if k))
        return keys | ({"calendar_event_id": named} if named else {})

    # -- one meeting
    def _dates(self, instance: Instance) -> tuple[str, str, str]:
        """A meeting's date, start and end: a timed one's in the workspace's zone, an all-day one's days as the
        event names them, in its own zone."""
        if instance.all_day:
            try:
                own: tzinfo = ZoneInfo(instance.zone)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                own = UTC
            date = instance.start.astimezone(own).date().isoformat()
            return date, date, instance.end.astimezone(own).date().isoformat()
        start, end = (_stamp(t.astimezone(self.zone)) for t in (instance.start, instance.end))
        return instance.start.astimezone(self.zone).date().isoformat(), start, end

    def _record(self, instance: Instance, now: datetime) -> Record:
        date, start, end = self._dates(instance)
        seen: set[str] = set()
        attendees = []
        for who in instance.attendees:
            if who.email and who.email in seen:
                continue
            seen.add(who.email)
            attendees.append(self._person(who))
        fields = {
            "title": instance.title or "Meeting", "date": date, "start": start, "end": end,
            "all_day": instance.all_day, "timezone": instance.zone or None,
            "status": "upcoming" if instance.end > now else "past", "attendees": attendees,
            "organizer": self._person(instance.organizer) if instance.organizer else None,
            "location": instance.location or None, "conferencing_url": instance.conference or None,
            "description": _clip(instance.description, MAX_DESCRIPTION) or None, "source": KIND,
        }
        people = ([instance.organizer] if instance.organizer else []) + instance.attendees
        if mine := own_people((p.name, p.email) for p in people if p.email and self.ours.holds(p.email)):
            fields["ours"] = mine  # who on it is the workspace's own, by address, as Gmail says: never a key
        record_keys = self._keys(instance)
        record = Record(id=instance.id, source=self.name, revision="", at=_stamp(instance.start),
                        keys=record_keys, fields=fields, body=instance.description,
                        extract=self.source.extract or "none", into=self.source.into or INTO,
                        carried=[k for k in CARRIED if k in record_keys])
        whole = json.dumps([record.at, record.keys, record.fields, record.body, record.extract, record.into],
                           sort_keys=True)
        record.revision = hashlib.sha256(whole.encode()).hexdigest()[:16]
        return record

    def _read(self, notices: list[str]) -> dict[str, Record]:
        """Every meeting the feed holds in the window, whole, by id."""
        now = self.now()
        try:
            feed = read_feed(self.feed(), self.span(now))
        except FeedError as error:
            raise CalendarError(f"{self.name}: the feed does not read: {error}: check the address gives the whole "
                                f"calendar, then list again; nothing was taken away") from None
        if feed.unexpanded:
            notices.append(f"{feed.unexpanded} recurring events use a rule this connector does not read, so each "
                           f"shows as its first date alone")
        if feed.unread:
            notices.append(f"{feed.unread} events have no id or no start, and were left out")
        records, alone = {}, 0
        self._held = (feed.uids, {i.id for i in feed.instances if i.cancelled}, self.span(now))
        for instance in feed.instances:
            if instance.cancelled:
                continue
            record = self._record(instance, now)
            if not set(record.keys) & set(CARRIED):  # no one from outside: its own id alone
                alone += 1
                continue
            records[record.id] = record
        if alone:
            notices.append(f"{alone} meetings in the window have no one from outside, only you or colleagues, and "
                           f"are not listed")
        return records

    # -- the cursor
    def _cursor(self, seen: dict[str, str]) -> str:
        state = json.dumps({"config": self._config, "seen": seen}, sort_keys=True, separators=(",", ":"))
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(state.encode())).decode()

    def _seen(self, cursor: str) -> tuple[dict[str, str], bool]:
        """The revisions a cursor saw, and whether the source file and settings were the same then."""
        try:
            if not cursor.startswith(_CURSOR):
                raise ValueError
            state = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
            seen = state["seen"]
            if not isinstance(seen, dict) or not all(isinstance(v, str) for v in seen.values()):
                raise ValueError
        except (ValueError, KeyError, TypeError, zlib.error, binascii.Error):
            raise CursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with no "
                              f"cursor to start over") from None
        return seen, state.get("config") == self._config

    # -- the contract
    def list(self, since: str | None = None) -> Listing:
        seen, same = self._seen(since) if since is not None else ({}, True)
        notices: list[str] = []
        current = self._read(notices)
        self.notices = notices
        removed = gone([id for id in seen if id not in current], self._exists)
        if not same:
            seen = {}  # the source file changed, so every meeting is listed anew
        changed = sorted(id for id, record in current.items() if seen.get(id) != record.revision)
        page = changed[:self.page_size]
        kept = {id: revision for id, revision in seen.items() if id in current}
        kept.update({id: current[id].revision for id in page})
        records = [current[id] for id in page]
        for record in records:
            record.body = None
        return Listing(records, self._cursor(kept), removed, len(changed) > len(page), list(self.notices))

    def _exists(self, id: str) -> bool:
        """Whether the feed, as last read, still holds a meeting a listing no longer found. One that left the window,
        or no longer has anyone from outside, still exists; one cancelled, deleted, or taken out of its series
        inside the window does not."""
        uids, cancelled, span = self._held
        uid, _, key = id.partition("#")
        if uid not in uids or id in cancelled:
            return False
        if not key:
            return True
        try:
            start = datetime.strptime(key, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) if "T" in key else \
                datetime.strptime(key, "%Y%m%d").replace(tzinfo=self.zone)
        except ValueError:
            return True
        return not ((span.low is None or start >= span.low) and start < span.high)

    # -- a curator's questions
    questions = QUESTIONS

    def _conversation(self, instance: Instance) -> Conversation:
        """What a meeting says of who took part, never its title or description: you took part when you accepted
        it or organized it; the invite went both ways when the organizer is on one side and someone who accepted
        on the other; a machine organized it when the organizer is outside and reads as one. A meeting with no
        organizer tells neither sides nor who organized it."""
        people = ([instance.organizer] if instance.organizer else []) + instance.attendees
        day = self._dates(instance)[0]
        at = datetime.fromisoformat(day).replace(tzinfo=UTC) if instance.all_day else instance.start
        talk = Conversation(at=at, day=day,
                            outside=address_keys((p.email for p in people), self.ours).get("domain", []))
        if any(p.email in self.me and p.response == "accepted" for p in instance.attendees):
            talk.how.append("accepted")
        organizer = instance.organizer
        if organizer is None or not organizer.email:
            return talk
        if organizer.email in self.me:
            talk.how.append("organized")
        ours = self.ours.holds(organizer.email)
        theirs = not ours
        for who in instance.attendees:
            if who.email and who.response == "accepted" and who.email != organizer.email:
                held = self.ours.holds(who.email)
                ours, theirs = ours or held, theirs or not held
        talk.sides = (ours, theirs)
        talk.machine = not self.ours.holds(organizer.email) and machine_address(organizer.email)
        talk.person = not talk.machine
        return talk

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """A curator's question, as `QUESTIONS` lists them: the feed read once, as `list` reads it, over the days
        the question names, and one row for each outside domain of its meetings that are not cancelled. A
        colleague's calendar gives no rows, so only meetings you could be on are yours."""
        read = asked(f"calendar source {self.name}", QUESTIONS, TAKES, question, params, limit)
        if self.source.owner not in ("", "me"):
            notice = (f"{self.name} is {self.source.owner}'s calendar, and only your own answers a curator: ask your "
                      "own calendar source")
            return Listing([], None, [], False, [notice])
        now = self.now()
        span = Span(self._step(now, timedelta(days=read["days"]), -1),
                    self._step(now, timedelta(days=read["ahead"]), 1))
        try:
            feed = read_feed(self.feed(), span)
        except FeedError as error:
            raise CalendarError(f"{self.name}: the feed does not read: {error}: check the address gives the whole "
                                f"calendar, then ask again") from None
        talks = [self._conversation(i) for i in feed.instances if not i.cancelled]
        order = MOST_ENGAGED if question == "meetings_by_domain" else LATEST
        return rows(self.name, "meetings", talks, order, limit)

    def get(self, id: str) -> Record:
        record = self._read([]).get(id)
        if record is None:
            raise RecordNotFound(f"{self.name} has no meeting {id} in its window: list the source for the ids it "
                                 f"holds")
        return record
