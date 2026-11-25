"""Reading Granola's replies: meetings, attendees, transcripts and times, as its hosted MCP server writes them.

The rule: read what the reply says and nothing it does not. The server answers in tagged text, a `meetings_data`
element holding one `meeting` element each, with fields as attributes or child elements, and a reply that is JSON
with a list of meetings is read too. Entities are read twice, since a real reply escapes some angle brackets twice.
An attendee is a name, an address, or both; a role or a tag in brackets is not part of a name, and a comma inside
brackets separates nobody. A transcript is tagged blocks, timed lines such as `[1:02] Name: text`, plain
`Name: text` lines, or JSON with a list of turns; an audio source such as `Microphone` names no speaker. A reply
that holds no meetings and says so reads as none; one that does not read raises `ReplyError`, whose message never
repeats the reply's own words, since they can be a meeting's.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..errors import CerebroError

__all__ = ["Meeting", "Person", "ReplyError", "Turn", "clock", "parse_folders", "parse_meetings", "parse_people",
           "parse_time", "parse_transcript"]


class ReplyError(CerebroError, ValueError):
    """A Granola reply does not read as the tool's answer. The message names the tool's problem, never its text."""


@dataclass
class Person:
    """One attendee as Granola lists them. A name may stand alone, with no address."""

    name: str = ""
    email: str = ""


@dataclass
class Meeting:
    """The part of a Granola meeting a connector reads. `updated` is the time Granola says it last changed, as it
    wrote it. `participants` says the reply carried a participants element, even an empty one."""

    id: str
    title: str = ""
    start: datetime | None = None
    updated: str = ""
    calendar_event_id: str = ""
    url: str = ""
    people: list[Person] = field(default_factory=list)
    participants: bool = False
    summary: str = ""
    notes: str = ""

    def emails(self) -> list[str]:
        return [p.email for p in self.people if p.email]


@dataclass
class Turn:
    """One turn of a transcript: who spoke, seconds into the meeting or -1 when the line carries none, and what."""

    speaker: str
    at: int
    text: str


_MEETING = re.compile(r"<meeting\b([^>]*?)(?:/>|>(.*?)</meeting>)", re.DOTALL)
_NOTE = re.compile(r"<(?:note|document|item|result)\b([^>]*?)(?:/>|>(.*?)</(?:note|document|item|result)>)",
                   re.DOTALL)
_FOLDER = re.compile(r"<folder\b([^>]*?)(?:/>|>(.*?)</folder>)", re.DOTALL)
_ATTRIBUTE = re.compile(r"""([A-Za-z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_IN_BRACKETS = re.compile(r"<([^<>\s@,;&]+@[^<>\s@,;&]+)>")
_BARE = re.compile(r"""([^\s<>,;&()"'@]+@[^\s<>,;&()"'@]+\.[^\s<>,;&()"'@.]+)""")
_OPEN_MEETING = re.compile(r"<meeting\b")
_NO_MEETINGS = re.compile(r"\bno (?:meetings|notes)\b|\b0 (?:meetings|notes)\b", re.IGNORECASE)
_TURN_LINE = re.compile(r"^\[(?:(\d+):)?(\d+):(\d{2})\]\s*(.*)$")
_BLOCK = re.compile(r"<(block|turn|segment|utterance|entry|line)\b([^>]*?)(?:/>|>(.*?)</(?:block|turn|segment|"
                    r"utterance|entry|line)>)", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_AUDIO = {"microphone", "mic", "system audio", "system"}
_DECORATIONS = "-*\"'.,:;<> \t"
_LAYOUTS = ("%b %d, %Y %I:%M %p", "%b %d, %Y %I:%M%p", "%b %d, %Y %H:%M", "%b %d %Y %I:%M %p", "%b %d, %Y, %I:%M %p",
            "%B %d, %Y %I:%M %p", "%B %d, %Y %H:%M", "%B %d, %Y", "%b %d, %Y", "%b %d %Y", "%d %b %Y %H:%M",
            "%d %b %Y", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y")
_WEEKDAY = re.compile(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+", re.IGNORECASE)
_ZONE_NAME = re.compile(r"\s+[A-Z]{2,5}$")


def unescape(text: str) -> str:
    """HTML entities read, a second time when the first left some, as `&amp;lt;` for a bracket escaped twice."""
    for _ in range(2):
        once = html.unescape(text)
        if once == text:
            break
        text = once
    return text


def attributes(opening: str) -> dict[str, str]:
    return {m[1].lower(): unescape(m[2] or m[3] or "") for m in _ATTRIBUTE.finditer(opening)}


def _tag_text(body: str, *names: str) -> str | None:
    for name in names:
        match = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", body, re.DOTALL)
        if match is not None:
            return match[1]
    return None


def _child(body: str, *names: str) -> str:
    return unescape(_tag_text(body, *names) or "").strip()


def _first(*values: str) -> str:
    return next((v.strip() for v in values if v and v.strip()), "")


def parse_time(text: str) -> datetime | None:
    """A time as Granola writes one: ISO forms, a written date such as `Oct 7, 2025 2:00 PM` with or without a
    weekday, `at` or a trailing zone name, or whole seconds or milliseconds since 1970. No zone reads as UTC."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit() and int(text) > 10**9:
        seconds = int(text) / 1000 if int(text) > 10**11 else int(text)
        return datetime.fromtimestamp(seconds, UTC)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        when = None
    if when is None:
        plain = text
        for noise in (" ", " ", " at ", "•", "·", "|"):
            plain = plain.replace(noise, " ")
        plain = " ".join(_WEEKDAY.sub("", plain.strip()).split())
        plain = re.sub(r" (am|pm)\b", lambda m: " " + m[1].upper(), plain, flags=re.IGNORECASE)
        for candidate in dict.fromkeys((text, plain, _ZONE_NAME.sub("", plain))):
            for layout in _LAYOUTS:
                try:
                    when = datetime.strptime(candidate, layout)
                    break
                except ValueError:
                    continue
            if when is not None:
                break
    if when is None:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _split_people(block: str) -> list[str]:
    """A participants block cut at newlines, and at commas and semicolons outside brackets of any kind."""
    out, depth, angle, start = [], 0, False, 0
    for i, ch in enumerate(block):
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth > 0:
            depth -= 1
        elif ch == "<":
            angle = True
        elif ch == ">":
            angle = False
        elif ch == "\n" or (ch in ",;" and depth == 0 and not angle):
            out.append(block[start:i])
            start, depth, angle = i + 1, 0, False
    out.append(block[start:])
    return out


def _left_open(text: str) -> bool:
    depth, angle = 0, False
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth > 0:
            depth -= 1
        elif ch == "<":
            angle = True
        elif ch == ">":
            angle = False
    return depth > 0 and not angle


def _closer_of_open(text: str) -> int:
    depth = 0
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            if depth == 0:
                return i
            depth -= 1
    return -1


def _hidden(before: str, text: str, tail: bool) -> list[str] | None:
    """The entries a bracket left open before an address hid from the split, when the text after it starts with a
    separator; None for any other text."""
    text = text.strip()
    if not text.startswith((",", ";")) or not _left_open(before):
        return None
    closer = _closer_of_open(text)
    if closer < 0:
        return _split_people(text)
    entries = _split_people(text[closer + 1:])
    return entries[1:] if tail or len(entries) > 1 else entries


def _has_word(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def _clean_name(name: str) -> str:
    """A name without decorations: a bracketed group after a name ends it, one before any name is dropped, and
    ` from Company` is cut. Empty when no letter or digit is left."""
    name = name.strip().strip(_DECORATIONS)
    while (i := min((j for j in (name.find("("), name.find("[")) if j >= 0), default=-1)) >= 0:
        if _has_word(name[:i]):
            name = name[:i]
            break
        j = name.find(")" if name[i] == "(" else "]", i)
        if j < 0:
            name = name[:i]
            break
        name = (name[:i] + name[j + 1:]).strip()
    if " from " in name:
        name = name[:name.index(" from ")]
    name = name.strip().strip(_DECORATIONS)
    return name if _has_word(name) else ""


def parse_people(block: str) -> list[Person]:
    """The attendees of a participants block, each once, by address or else by name. Addresses are lower case."""
    out: list[Person] = []
    seen: set[str] = set()

    def add(name: str, email: str) -> None:
        name, email = _clean_name(name), email.strip().lower()
        key = email or "name:" + name.lower()
        if (name or email) and key not in seen:
            seen.add(key)
            out.append(Person(name, email))

    for entry in _split_people(block):
        found = list(_IN_BRACKETS.finditer(entry)) or list(_BARE.finditer(entry))
        if not found:
            add(entry, "")
            continue
        previous = 0
        for match in found:
            name = entry[previous:match.start()]
            hidden = _hidden(entry[:previous], name, False)
            if hidden is not None:
                for alone in hidden[:-1]:
                    add(alone, "")
                name = hidden[-1]
            add(name, match[1])
            previous = match.end()
        for alone in _hidden(entry[:previous], entry[previous:], True) or []:
            add(alone, "")
    return out


def _json(reply_text: str, structured: Any) -> Any:
    if structured not in (None, "", {}, []):
        try:
            return json.loads(structured) if isinstance(structured, (str, bytes)) else structured
        except ValueError:
            pass
    text = reply_text.strip()
    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except ValueError:
            return None
    return None


def _meetings_json(top: Any) -> list[Meeting] | None:
    items = top if isinstance(top, list) else next(
        (top[k] for k in ("meetings", "notes", "results", "items") if isinstance(top.get(k), list)), None)
    if items is None:
        return None
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue

        def text(*keys: str, item: dict[str, Any] = item) -> str:
            return _first(*(item[k] for k in keys if isinstance(item.get(k), str)))

        meeting = Meeting(id=text("id", "meeting_id", "note_id"), title=text("title", "name"),
                          updated=text("updated_at", "updated"),
                          calendar_event_id=text("calendar_event_id", "calendarEventId"), url=text("url"),
                          start=parse_time(text("date", "start", "start_time", "created_at")),
                          summary=text("summary"), notes=text("private_notes", "notes"))
        if not meeting.id:
            raise ReplyError("a meeting in the granola reply has no id: the reply is not one this connector reads")
        for key in ("attendees", "participants", "known_participants"):
            if isinstance(item.get(key), list):
                meeting.participants = True
                for person in item[key]:
                    if isinstance(person, str):
                        meeting.people += parse_people(person)
                    elif isinstance(person, dict):
                        email = person.get("email") or person.get("emailAddress") or ""
                        meeting.people.append(Person(str(person.get("name") or "").strip(),
                                                     str(email).strip().lower()))
        out.append(meeting)
    return out


def parse_meetings(text: str, structured: Any = None) -> list[Meeting]:
    """The meetings of a `list_meetings` or `get_meetings` reply. Raises `ReplyError` for one that does not read."""
    top = _json(text, structured)
    if isinstance(top, (list, dict)) and (found := _meetings_json(top)) is not None:
        return found
    text = text.strip()
    elements = list(_MEETING.finditer(text)) or list(_NOTE.finditer(text))
    out = []
    for element in elements:
        attrs, body = attributes(element[1]), element[2] or ""
        meeting = Meeting(
            id=_first(attrs.get("id", ""), attrs.get("meeting_id", ""), attrs.get("note_id", ""),
                      attrs.get("uuid", ""), _child(body, "id", "meeting_id", "note_id")),
            title=_first(attrs.get("title", ""), attrs.get("name", ""), _child(body, "title")),
            updated=_first(attrs.get("updated_at", ""), attrs.get("updated", ""), attrs.get("last_updated", ""),
                           _child(body, "updated_at")),
            calendar_event_id=_first(attrs.get("calendar_event_id", ""), attrs.get("calendar_id", ""),
                                     attrs.get("calendareventid", ""), _child(body, "calendar_event_id")),
            url=_first(attrs.get("url", "")),
            start=parse_time(_first(*(attrs.get(k, "") for k in ("date", "start", "start_time", "created_at",
                                                                    "time")), _child(body, "date", "start_time"))))
        if (people := _tag_text(body, "known_participants", "attendees", "participants")) is not None:
            meeting.people, meeting.participants = parse_people(unescape(people)), True
        if (summary := _tag_text(body, "summary")) is not None:
            meeting.summary = unescape(summary).strip()
        if (notes := _tag_text(body, "private_notes", "notes")) is not None:
            meeting.notes = unescape(notes).strip()
        if not meeting.id:
            raise ReplyError("a meeting in the granola reply has no id: the reply is not one this connector reads")
        out.append(meeting)
    if not out and text and "meetings_data" not in text and not _NO_MEETINGS.search(text):
        raise ReplyError("the granola reply holds no meetings_data and no meetings that read")
    if not out and _OPEN_MEETING.search(text):
        raise ReplyError("the granola reply has meetings that do not read")
    return out


def parse_folders(text: str, structured: Any = None) -> list[tuple[str, str]]:
    """The folders of a `list_meeting_folders` reply, each as its id and title."""
    found = []
    for element in _FOLDER.finditer(text):
        attrs, body = attributes(element[1]), element[2] or ""
        found.append((_first(attrs.get("id", ""), attrs.get("folder_id", ""), _child(body, "id", "folder_id")),
                      _first(attrs.get("title", ""), attrs.get("name", ""), _child(body, "title", "name"))))
    top = _json(text, structured)
    items = top if isinstance(top, list) else top.get("folders") if isinstance(top, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            found.append((str(item.get("id") or ""), str(item.get("title") or "")))
    return found


def _speaker(label: str) -> str:
    """A speaker label without its audio source, as `System audio (Jane Doe)` or `Jane Doe (Microphone)`."""
    label = label.strip()
    if label.lower() in _AUDIO:
        return ""
    i = label.rfind(" (")
    if i > 0 and label.endswith(")"):
        inner, outer = label[i + 2:-1].strip(), label[:i].strip()
        if inner.lower() in _AUDIO:
            return outer
        if outer.lower() in _AUDIO:
            return inner
    return label


def _offset(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
        return int(seconds) if seconds >= 0 else None
    except ValueError:
        pass
    parts = value.strip("[]").split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _transcript_json(top: Any) -> list[Turn] | None:
    items = top if isinstance(top, list) else next(
        (top[k] for k in ("transcript", "turns", "blocks", "segments") if isinstance(top.get(k), list)), None)
    if items is None:
        return None
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        speaker = next((item[k] for k in ("speaker", "speaker_name", "name") if isinstance(item.get(k), str)
                        and item[k]), "")
        said = next((item[k] for k in ("text", "content") if isinstance(item.get(k), str)), "")
        at = next((item[k] for k in ("start", "start_time", "offset", "at")
                   if isinstance(item.get(k), (int, float)) and not isinstance(item.get(k), bool)), -1)
        if said.strip():
            out.append(Turn(_speaker(speaker), int(at), said.strip()))
    return out


def parse_transcript(text: str, structured: Any = None) -> list[Turn]:
    """The turns of a `get_meeting_transcript` reply. A turn no one is named for keeps no speaker."""
    top = _json(text, structured)
    if isinstance(top, (list, dict)) and (found := _transcript_json(top)) is not None:
        return found
    text = text.strip()
    if blocks := list(_BLOCK.finditer(text)):
        out = []
        for block in blocks:
            attrs, body = attributes(block[2]), block[3] or ""
            speaker = _first(*(attrs.get(k, "") for k in ("speaker", "speaker_name", "participant", "name")),
                             _child(body, "speaker", "speaker_name", "participant"))
            at = _offset(_first(*(attrs.get(k, "") for k in ("start", "start_time", "timestamp", "time", "offset")),
                                _child(body, "start", "start_time", "timestamp")))
            words = _tag_text(body, "text", "content", "words")
            said = unescape(_TAG.sub("", body) if words is None else words).strip()
            if said:
                out.append(Turn(_speaker(speaker), -1 if at is None else at, said))
        return out
    out = []
    for line in unescape(_TAG.sub("\n", text)).strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        at = -1
        if timed := _TURN_LINE.match(line):
            at = int(timed[1] or 0) * 3600 + int(timed[2]) * 60 + int(timed[3])
            line = timed[4].strip()
        speaker, sep, said = line.partition(": ")
        if sep and len(speaker) <= 60 and not any(c in speaker for c in "<>"):
            turn = Turn(_speaker(speaker), at, said.strip())
        else:
            turn = Turn("", at, line)
        if turn.text:
            out.append(turn)
    return out


def clock(seconds: int) -> str:
    """Seconds into a meeting as a transcript marks them: `m:ss`, or `h:mm:ss` from an hour on."""
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"
