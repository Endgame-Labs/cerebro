"""Granola meetings as a source, read only, through Granola's hosted MCP server.

The connector reaches Granola through one function handed in, `call(tool, arguments)`, which sends a tool call to
the MCP server on the stored login and gives the reply. It never reads a key, never opens a connection, and calls
only the four tools that read: `list_meeting_folders`, `list_meetings`, `get_meetings` and `get_meeting_transcript`.
Any other tool name is refused before it is sent.

One meeting is two records: `notes:<id>` with its summary and your notes, and
`transcript:<id>` once Granola holds a transcript the login may read. Both open with the meeting's title, start and
attendees, and both land in `calls`. The notes' revision is the time Granola says the meeting changed, or a hash of
the notes when it says none; the transcript's is a hash of its text. A record's time is the meeting's start, its
url the meeting in Granola, and its keys are what can bind it to an article: the attendees' addresses and their
domains, the meeting id and the calendar event id. Its fields carry what a later merge with another source's call
needs: the start, the attendees and those from outside your company, and the calendar event id.

A listing asks `list_meetings` for the file's whole window, 90 days unless it says otherwise, once for each folder
`select` names or once in all. That listing has no bodies and costs one call. Only a meeting that is new, that
Granola says changed, or that changed less than three days before the last check with nothing to tell by, is read
with `get_meetings`, ten ids a call, and has its transcript asked for; a record is listed when its revision moved.
A meeting the listing no longer holds, deleted, out of the login's sight or older than the window, is in `removed`.
The cursor carries each meeting's change time and short hashes of its revisions, never a title, a name or a word
said, so the connector keeps nothing of a meeting between calls.

A curator may ask it `meetings` and `meetings_by_domain`, as `QUESTIONS` says: one `list_meetings` over the days the
question names, for the meetings you captured or sat in, once for each folder `select` names, and one row for each
outside domain of their attendees, as `questions.rows` makes them, never a title, a note or a word said. Only your
own Granola answers: one whose source file names a colleague as its owner gives no rows.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, ClassVar

from ..clock import system_clock
from ..connector import NOBODY, Connector, Listing, Ours, Record, RecordNotFound, address_keys, event_key, gone, guard
from ..connector import CursorError as SharedCursorError
from ..errors import CerebroError
from ..source import SourceError, SourceFile
from .connecting import Connecting, Way
from .granola_parse import Meeting, ReplyError, Turn, clock, parse_folders, parse_meetings, parse_transcript
from .mcp_source_client import ToolRefused
from .questions import LATEST, MOST_ENGAGED, Conversation, Days, asked, rows

__all__ = ["READ_TOOLS", "CursorError", "FolderNotFound", "GranolaConnector", "GranolaError", "NotLoggedIn",
           "Reply", "ReplyError", "ToolError", "WriteRefused", "notes_text", "transcript_text"]

READ_TOOLS = ("list_meeting_folders", "list_meetings", "get_meetings", "get_meeting_transcript")
NOTES, TRANSCRIPT = "notes:", "transcript:"
INTO = "calls"
CARRIED = ("email", "domain")  # a meeting's parties' keys point at them: its ids name it
SOURCE_KEYS = ("auth", "url", "extract", "window", "into", "owner", "select", "freshness")
SELECT_KEYS = ("folders",)
REFUSED_KEYS = {"command": "a granola source reads granola's own server: delete command"}
PAGE_SIZE = 100
BATCH = 10
LOOKBACK = timedelta(hours=72)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRANOLA_URL = "https://notes.granola.ai/d/"
_CURSOR = "granola1."
_MEETING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DENIED = ("transcript access", "not permitted", "does not permit", "permission denied", "access denied", "forbidden",
           "upgrade your plan", "not allowed")
_OWN_LINES = re.compile(r"(Summary:|Your notes:)$")  # the headings a notes record writes itself
# The questions a curator may ask the source with `ask`, each with the params it takes.
QUESTIONS = {
    "meetings": "one row for each outside domain of the meetings you captured or sat in over the last `days` (30 by "
                "default, at most 90), the latest meeting first: how many and the first and last day; never a title, "
                "a note or a transcript; params: days",
    "meetings_by_domain": "the same rows over the last `days` (90 by default, at most 90), the most meetings first, "
                          "for who you meet most; params: days",
}
MAX_ASK_DAYS = 90
TAKES = {"meetings": {"days": Days(30, 1, MAX_ASK_DAYS)}, "meetings_by_domain": {"days": Days(90, 1, MAX_ASK_DAYS)}}
# What `list_meetings` is asked for in a question: only the meetings of the login's own person.
INVOLVEMENT = {"captured_by_me": True, "listed_as_participant": True}
_EXPIRED = ("token expired", "token has expired", "unauthorized", "unauthenticated", "invalid_token", "invalid token")


class GranolaError(CerebroError):
    """Something about the Granola source stops a listing or a read."""


class CursorError(GranolaError, SharedCursorError):
    """A cursor this connector did not give, or one that does not read."""


class FolderNotFound(GranolaError, LookupError):
    """`select.folders` names a folder Granola does not hold."""


class NotLoggedIn(GranolaError, PermissionError):
    """The Granola login is gone, expired past renewing or refused."""


class ToolError(GranolaError):
    """A Granola tool answered with an error. The message names the tool and never repeats the reply."""


class WriteRefused(GranolaError, PermissionError):
    """The connector was about to call a tool that is not one of the four that read."""


@dataclass
class Reply:
    """One tool reply as the MCP client gives it: its text, its structured content when it has some, and whether
    the server marked it an error."""

    text: str = ""
    structured: Any = None
    is_error: bool = False


Call = Callable[[str, dict[str, Any]], Any]
"""Gives a `Reply`, its text alone, or the plain data the MCP client's `reader` gives, raising `ToolRefused` for a
reply the server marks as an error."""


def _stamp(when: datetime | None) -> str:
    """A time in UTC to the second."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if when is not None else ""


def _header(meeting: Meeting) -> list[str]:
    """The lines both records open with: the title, the start and the attendees."""
    lines = [" ".join(meeting.title.split())]
    if meeting.start is not None:
        lines.append("Started: " + _stamp(meeting.start))
    people = [f"{p.name} <{p.email}>" if p.name and p.email else p.name or p.email for p in meeting.people]
    if people:
        lines.append("Attendees: " + ", ".join(people))
    return lines


def notes_text(meeting: Meeting) -> str:
    """A meeting's notes record as distill reads it: the header, then the summary and the notes you took."""
    lines = _header(meeting)
    if meeting.summary:
        lines += ["", "Summary:", guard(meeting.summary, _OWN_LINES)]
    if meeting.notes:
        lines += ["", "Your notes:", guard(meeting.notes, _OWN_LINES)]
    return "\n".join(lines)


def transcript_text(meeting: Meeting, turns: Sequence[Turn]) -> str:
    """A meeting's transcript as distill reads it: the header, then one line a turn, marked with the time into the
    meeting when the turn carries one, and who spoke, or `Speaker` for a voice no one named."""
    lines = _header(meeting)
    for turn in turns:
        mark = f"[{clock(turn.at)}] " if turn.at >= 0 else ""
        said = " ".join(turn.text.split())  # one line a turn, so a turn cannot forge the next
        lines += ["", f"{mark}{' '.join((turn.speaker or 'Speaker').split())}: {said}"]
    return "\n".join(lines)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _short(revision: str) -> str:
    return hashlib.sha256(revision.encode()).hexdigest()[:16]



# What connect says: its one way in, through the browser.
CONNECTING = Connecting(
    title="Granola",
    needs="your own Granola login, in the browser",
    choices=(Way("Sign in through the browser", word="browser", auth="oauth", says=(
        ("Granola signs you in through your browser: connect opens it next and waits up to ten minutes for you to "
        "sign in."),
        ("Granola lists participants incompletely, so connect a calendar source too: most meetings come into scope "
        "by their calendar invite.")),
        connected="Connected to Granola. The login is kept under {secret} in {where}.",
        kept=("Granola is connected already, and the login kept under {secret} in {where} stays. Pass --replace to "
              "sign in again."),
        plain=(("Granola lists participants incompletely, so connect a calendar source too: most meetings come into "
                "scope by their calendar invite."),)),),
    connected="Connected to Granola with a token. The token is kept under {secret} in {where}.",
    kept=("Granola is connected already, and the token kept under {secret} in {where} stays. Pass --replace to "
          "replace it."))


class GranolaConnector(Connector):
    """A `kind: granola` source. `call` sends one tool call to Granola's MCP server and gives the reply. `now` is
    the clock the window is read by. `ours` holds your company's email domains, which tell the attendees from
    outside apart; with none, every attendee counts as outside."""

    IDENTITY_KEYS: ClassVar[Mapping[str, int]] = {"calendar_event_id": 500}  # one meeting, by its event: what its calendar entry, recording and notes share

    connecting = CONNECTING
    mcp_server: ClassVar[str] = "https://mcp.granola.ai/mcp"
    mcp_probe = ("list_meeting_folders", {}, READ_TOOLS)

    @classmethod
    def login_name(cls, source: SourceFile) -> str | None:
        """Every source of the kind keeps one login, under the kind's name."""
        return "granola"

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> GranolaConnector:
        return cls(source, app.mcp_session(source, cls.mcp_server, READ_TOOLS), now=app.clock, ours=app.ours,
                   zone=app.zone)

    def __init__(self, source: SourceFile, call: Call, *, now: Callable[[], datetime] | None = None,
                 page_size: int = PAGE_SIZE, ours: Ours = NOBODY, zone: tzinfo = UTC):
        if source.kind != "granola":
            raise SourceError(f"sources/{source.name}.yaml is kind {source.kind}: a granola connector reads kind "
                              f"granola alone")
        self.name = source.name
        self.source = source
        self._call = call
        self.now = now or system_clock
        self.zone = zone  # the workspace's zone, in which a question's days are counted
        self.page_size = max(1, page_size)
        self.ours = ours
        self.behind = source.behind()
        self.folders = [name for entry in source.select for name in entry.folders]
        self.notices: list[str] = []
        config = json.dumps([source.window.behind, source.extract, source.into, self.folders], sort_keys=True)
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]

    # -- talking to Granola
    def _ask(self, tool: str, arguments: dict[str, Any]) -> Reply:
        if tool not in READ_TOOLS:
            raise WriteRefused(f"{self.name}: {tool} is not a tool that reads: the granola connector calls "
                               f"{', '.join(READ_TOOLS)} alone")
        try:
            reply = self._call(tool, arguments)
        except ToolRefused as error:
            reply = Reply(text=error.text, is_error=True)
        if isinstance(reply, str):
            reply = Reply(text=reply)
        elif not isinstance(reply, Reply):  # structured content, or text that read as JSON
            reply = Reply(text=json.dumps(reply), structured=reply)
        if reply.is_error and any(word in reply.text.lower() for word in _EXPIRED):
            raise NotLoggedIn(f"{self.name}: the granola login is gone or expired: run cerebro "
                              f"connect {self.name} --replace, then list again")
        return reply

    def _failed(self, tool: str) -> ToolError:
        return ToolError(f"{self.name}: the granola tool {tool} answered with an error: list again later, and if it "
                         f"fails again, log in again with cerebro connect {self.name} --replace")

    def _folder_ids(self) -> list[str]:
        if not self.folders:
            return [""]
        reply = self._ask("list_meeting_folders", {})
        if reply.is_error:
            raise self._failed("list_meeting_folders")
        held = parse_folders(reply.text, reply.structured)
        ids, missing = [], []
        for name in self.folders:
            found = next((id for id, title in held if id == name or title.lower() == name.lower()), None)
            (ids if found is not None else missing).append(found or name)
        if missing:
            raise FolderNotFound(f"sources/{self.name}.yaml: select.folders names no folder {', '.join(missing)} in "
                                 f"granola: write a folder's title or id as list_meeting_folders gives it")
        return ids

    def _listing(self, now: datetime, floor: datetime | None = None, *, mine: bool = False) -> dict[str, Meeting]:
        """Every meeting the window holds, by id, each once, from the listing alone: from `floor` when one is
        given, and else from the source file's window; only those you captured or sat in when `mine`."""
        if floor is None:
            floor = now - self.behind if self.behind is not None else EPOCH
        found: dict[str, Meeting] = {}
        for folder in self._folder_ids():
            arguments: dict[str, Any] = {"time_range": "custom", "custom_start": _stamp(floor),
                                         "custom_end": _stamp(now)}
            if folder:
                arguments["folder_id"] = folder
            if mine:
                arguments["involvement"] = dict(INVOLVEMENT)
            reply = self._ask("list_meetings", arguments)
            if reply.is_error:
                raise self._failed("list_meetings")
            for meeting in parse_meetings(reply.text, reply.structured):
                # Granola reads a range by day, so a meeting earlier on the first day comes back too.
                if meeting.start is None or meeting.start >= floor:
                    found.setdefault(meeting.id, meeting)
        return found

    def _details(self, ids: Sequence[str], *, strict: bool = False) -> dict[str, Meeting]:
        """The meetings `get_meetings` gives for `ids`, by the id asked. One the reply leaves out is not there."""
        found: dict[str, Meeting] = {}
        for start in range(0, len(ids), BATCH):
            batch = list(ids[start:start + BATCH])
            reply = self._ask("get_meetings", {"meeting_ids": batch})
            if reply.is_error:
                if strict:
                    raise self._failed("get_meetings")
                self.notices.append(f"get_meetings: a batch of {len(batch)} meetings answered with an error, "
                                    f"left for the next listing")
                continue
            got = {m.id.strip().lower(): m for m in parse_meetings(reply.text, reply.structured)}
            for id in batch:
                if (meeting := got.get(id.strip().lower())) is not None:
                    meeting.id = id
                    found[id] = meeting
        return found

    def _turns(self, meeting_id: str) -> list[Turn] | None:
        """A meeting's transcript, or None when its workspace withholds it."""
        reply = self._ask("get_meeting_transcript", {"meeting_id": meeting_id})
        if reply.is_error:
            if any(word in reply.text.lower() for word in _DENIED):
                return None
            raise self._failed("get_meeting_transcript")
        return parse_transcript(reply.text, reply.structured)

    # -- records
    def _record(self, meeting: Meeting, part: str, text: str) -> Record:
        emails = list(dict.fromkeys(meeting.emails()))
        keys: dict[str, list[str]] = {"granola_meeting_id": [meeting.id]} | address_keys(emails, self.ours)
        fields: dict[str, Any] = {"title": meeting.title.strip(), "part": part.rstrip(":")}
        if meeting.start is not None:
            fields["starts"] = _stamp(meeting.start)
        if emails:
            fields["attendees"] = emails
            fields["outside"] = [e for e in emails if not self.ours.holds(e)]
        if meeting.calendar_event_id:
            fields["calendar_event_id"] = meeting.calendar_event_id
            if named := event_key(meeting.calendar_event_id, meeting.start):  # as the calendar writes it: one key for both
                keys["calendar_event_id"] = [named]
        if part == NOTES:
            revision = f"granola-updated:{meeting.updated}" if meeting.updated else _sha(text)
            links = []
        else:
            revision, links = _sha(text), [NOTES + meeting.id]
        return Record(id=part + meeting.id, source=self.name, revision=revision, at=_stamp(meeting.start),
                      url=meeting.url or GRANOLA_URL + meeting.id, keys=keys, links=links, fields=fields,
                      body=text, extract=self.source.extract or "facts", into=self.source.into or INTO,
                      carried=[k for k in CARRIED if k in keys])

    def _records(self, meeting: Meeting) -> tuple[Record, Record | None]:
        """A meeting's notes, and its transcript when Granola holds one the login may read."""
        notes = self._record(meeting, NOTES, notes_text(meeting))
        turns = self._turns(meeting.id)
        if turns is None:
            self.notices.append(f"{TRANSCRIPT}{meeting.id}: the meeting's workspace withholds its transcript, so "
                                f"the notes are read alone")
        if not turns:
            return notes, None
        return notes, self._record(meeting, TRANSCRIPT, transcript_text(meeting, turns))

    # -- the cursor
    def _cursor(self, state: dict[str, Any]) -> str:
        text = json.dumps({"config": self._config, **state}, sort_keys=True, separators=(",", ":"))
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(text.encode())).decode()

    def _state(self, cursor: str | None) -> tuple[dict[str, Any], bool]:
        """What a cursor saw, and whether the source file read the same then."""
        if cursor is None:
            return {"seen": {}}, True
        try:
            if not cursor.startswith(_CURSOR):
                raise ValueError
            state = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
            seen = state["seen"]
            if not isinstance(seen, dict) or not all(isinstance(v, list) and len(v) == 3 for v in seen.values()):
                raise ValueError
        except (ValueError, KeyError, TypeError, zlib.error, binascii.Error):
            raise CursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with no "
                              f"cursor to start over") from None
        return state, state.get("config") == self._config

    def _due(self, meeting: Meeting, seen: list[str] | None, checked: datetime | None) -> bool:
        """Whether a listed meeting is read: new, said to have changed, or recent with nothing to tell by."""
        if seen is None or meeting.updated != seen[0]:
            return True
        if meeting.updated and seen[2]:
            return False
        return checked is None or meeting.start is None or meeting.start >= checked - LOOKBACK

    # -- the contract
    def list(self, since: str | None = None) -> Listing:
        self.notices = []
        now = self.now()
        state, same = self._state(since)
        seen: dict[str, list[str]] = state["seen"]
        listed = self._listing(now)
        removed = []
        absent = sorted(set(seen) - set(listed))  # deleted, or only out of the window: granola is asked which
        still = self._details(absent, strict=True) if absent else {}
        for id in gone(absent, still.__contains__):
            removed.append(NOTES + id)
            if seen[id][2]:
                removed.append(TRANSCRIPT + id)
        for id in absent:
            del seen[id]
        if not same:
            seen = {}  # the source file changed, so every record is listed anew
        run = state.get("pass") or {"started": _stamp(now), "checked": state.get("checked", ""), "done": []}
        checked = datetime.fromisoformat(run["checked"]) if run["checked"] else None
        done = set(run["done"])
        due = sorted((m for id, m in listed.items() if id not in done and self._due(m, seen.get(id), checked)),
                     key=lambda m: (_stamp(m.start), m.id))
        records: list[Record] = []
        read = 0
        while read < len(due) and len(records) < self.page_size:
            batch = due[read:read + min(BATCH, self.page_size)]
            read += len(batch)
            details = self._details([m.id for m in batch])
            for meeting in batch:
                done.add(meeting.id)
                detail = details.get(meeting.id)
                if detail is None:
                    self.notices.append(f"{NOTES}{meeting.id}: listed, but get_meetings gave nothing for it, so it "
                                        f"waits for the next listing")
                    continue
                try:
                    notes, transcript = self._records(detail)
                except ToolError:  # one meeting's transcript failing stops no other: it stays due
                    done.discard(meeting.id)
                    self.notices.append(f"{TRANSCRIPT}{meeting.id}: get_meeting_transcript answered with an error, "
                                        f"so the meeting waits for the next listing")
                    continue
                was = seen.get(meeting.id, ["", "", ""])
                now_seen = [meeting.updated, _short(notes.revision), _short(transcript.revision) if transcript else ""]
                if now_seen[1] != was[1]:
                    records.append(notes)
                if transcript is not None and now_seen[2] != was[2]:
                    records.append(transcript)
                if transcript is None and was[2]:
                    removed.append(TRANSCRIPT + meeting.id)
                seen[meeting.id] = now_seen
        for record in records:
            record.body = None  # a listing carries no body, and the text is not kept
        more = read < len(due)
        # A pass that goes on to another page carries what it read; one that ends is the last check.
        state = {"seen": seen, "pass": {**run, "done": sorted(done)}} if more else {"seen": seen,
                                                                                   "checked": run["started"]}
        return Listing(records, self._cursor(state), removed, more, list(self.notices))

    # -- a curator's questions
    questions = QUESTIONS

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """A curator's question, as `QUESTIONS` lists them: the meetings `list_meetings` gives over the days the
        question names, through the same read `list` makes and nothing more, so no note or transcript is read.
        Each meeting is one you attended, since only yours are asked for; Granola names no organizer and, without
        a transcript, no speaker, so a row tells no sides and no sender. A colleague's Granola gives no rows."""
        read = asked(f"granola source {self.name}", QUESTIONS, TAKES, question, params, limit)
        if self.source.owner not in ("", "me"):
            notice = (f"{self.name} is {self.source.owner}'s granola, and only your own answers a curator: ask your "
                      "own granola source")
            return Listing([], None, [], False, [notice])
        now = self.now()
        talks = [Conversation(at=m.start, day=m.start.astimezone(self.zone).date().isoformat() if m.start else "",
                              outside=address_keys(m.emails(), self.ours).get("domain", []), how=["attended"])
                 for m in self._listing(now, now - timedelta(days=read["days"]), mine=True).values()]
        order = MOST_ENGAGED if question == "meetings_by_domain" else LATEST
        return rows(self.name, "meetings", talks, order, limit)

    def get(self, id: str) -> Record:
        part, meeting_id = self._parse_id(id)
        if not _MEETING_ID.match(meeting_id):
            raise RecordNotFound(f"{self.name} has no record {id!r}: an id is notes:<meeting id> or "
                                 f"transcript:<meeting id>, as a listing gives it")
        meeting = self._details([meeting_id], strict=True).get(meeting_id)
        if meeting is None:
            raise RecordNotFound(f"{self.name} has no record {id}: the granola login sees no meeting {meeting_id}; "
                                 f"list the source for the ids it holds")
        if part == NOTES:
            return self._record(meeting, NOTES, notes_text(meeting))
        turns = self._turns(meeting_id)
        if not turns:
            why = "withholds it" if turns is None else "holds none yet"
            raise RecordNotFound(f"{self.name} has no record {id}: granola {why} for the meeting; read "
                                 f"{NOTES}{meeting_id} for its notes")
        return self._record(meeting, TRANSCRIPT, transcript_text(meeting, turns))

    def _parse_id(self, id: str) -> tuple[str, str]:
        """A record id as a listing gives it, or as a citation names it, after the source's name."""
        id = id.strip().removeprefix(self.name + ":")
        for part in (NOTES, TRANSCRIPT):
            if id.startswith(part):
                return part, id[len(part):]
        return NOTES, id
