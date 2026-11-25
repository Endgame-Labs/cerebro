"""A folder of files as a source, read only.

Each `select:` entry of a `kind: files` source file names files by a glob, and each file it matches is one record:
its id and url are its path, relative to the workspace's folder or `~/` under the home folder, so no path that belongs
to one machine lands in a fact. Its revision is a hash of its bytes, so it changes when the content does and not
when a sync client touches the file. Its time is its `Date:` line, or the entry's `at:`, never the file's
modification time. Its keys are its header lines, such as `Topic: Apollo` near the top, each under its own word,
lower case, as written: they are what can bind it to an article. Its body is the file's text without those header
lines, since they give its time and keys and say nothing of their own: a fact is never made of them. A record moves
when its file moves.

An entry with `rows: csv`, `jsonl` or `json` makes one record of each row of each file it matches:
a csv file's first line names its columns, a jsonl file holds one JSON object a line, and a json file one array
of objects. Its templates fill `{row.<column>}`, a dotted column reaching into a nested object, and `{line}`, the line
the row starts on. A row's id is `{path}#L{line}` when the entry names none, and so is its url; its revision is a hash
of its columns and values, so a row keeps it when rows above it move; its text is one `column: value` line a column;
its time is the entry's `at:` alone; and it extracts into records unless the file says otherwise. A later row with
the same id wins, across files and entries in the order of `select:`, before the window applies. A row that does not
read, such as one with a byte that is not UTF-8, or whose id comes out empty, is left out with a notice;
so is a file whose header names a column twice or whose json is no
array. A column the header lacks stops the listing, since every row would miss it. A row is data: it is parsed,
never run.

Only text and markdown are read, and any file at all for an entry with `rows`. A file of another type, a file read
whole that is over the size cap, which a file read by rows has not, and a link that points outside the folder its
glob searches are skipped with a notice that names them. A hidden file or folder, one whose name starts with a dot,
does not count as a file. An id given to `get` is looked up among the files the globs match, so it can never reach
outside them.

The connector keeps no copy of a file's text. A listing's cursor carries what it needs to know a file changed,
each record's id and a short hash of its revision, so a pass over an unchanged folder lists nothing, a file gone
since is in `removed`, one that only aged out of the window is forgotten, and the cursor survives a restart because
nothing else is kept.

Notes that name one thing on one header line and what goes with it on another read as one row of that thing with
the values beside it: the `notes` question gives one row for each value of the header line `each`, with the values
of the header line `with` in the files that carry it. The source file names the two words, as `notes: {each: <word>,
with: <word>}`, and an ask may name others; no header word is known here.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import functools
import hashlib
import json
import os
import re
import stat
import zlib
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from ..clock import system_clock
from ..connector import (
    Connector,
    CursorError,
    Listing,
    Ours,
    QuestionError,
    Record,
    RecordNotFound,
    address_keys,
    gone,
)
from ..errors import CerebroError
from ..secrets import MARK
from ..source import Select, SourceError, SourceFile
from .connecting import Starter
from .questions import Text, asked

__all__ = ["CursorError", "FilesConnector", "content", "header_lines"]

SOURCE_KEYS = ("extract", "window", "select", "notes", "freshness")
REFUSED_KEYS = {"into": "a files record lands by its select entry's into, never by a top-level one: "
                        "move it onto each entry, as select: [{ glob: \"notes/*.md\", into: things }]"}
SELECT_KEYS = ("glob", "extract", "rows", "id", "url", "into", "identity", "fields", "at")
TEXT_TYPES = (".md", ".markdown", ".txt")
QUOTE_IN_VALUE = 'a quote in a value that is not quoted (quote the value and double the quote: "a ""b"" c")'
AFTER_QUOTE = "text after a value's closing quote (put a comma after the quote, or double a quote inside the value)"
NEVER_CLOSED = 'a quoted value with no closing quote (end it with ")'
MAX_BYTES = 1_000_000
PAGE_SIZE = 100
MAX_PAGES = 10  # a listing's pages at most, however many records it holds: each page carries the whole cursor
HEADER_LINES = 20
_CURSOR = "files1."
_HEADER = re.compile(r"^[*_]*([A-Za-z][A-Za-z0-9 _-]{0,40}?)[*_]*:[*_ \t]*(.*)$")
_TIME_KEYS = ("date",)
_TEMPLATE = re.compile(r"\{([^{}]*)\}")
_PATH_SAFE = "$&+:=@"  # what a path segment of a url keeps unescaped, past letters, digits and -._~
# The question a curator may ask the source with `ask`, with the params it takes.
QUESTIONS = {
    "header_values": "one row for each value of the header line `word`, such as topic, in the files the source "
                     "reads, the same in any case, with how many files carry it and their first and last date, the "
                     "most files first; params: word (required)",
    "notes": "one row for each value of the header line `each` in the files the source reads, the same in any case, "
             "with the values of the header line `with` in the files that carry it, the line as written when it "
             "gives an address beside a name, how many files carry it and their first and last date, the most files "
             "first; params: each and with, the source file's notes: words when left out",
}
TAKES = {"header_values": {"word": Text("topic", required=True, what="the header line to read")},
         "notes": {"each": Text("topic", what="the header line each row is a value of"),
                   "with": Text("owner", what="the header line whose values go with it")}}
NOTES = ("each", "with")  # what a source file's `notes:` names, each a header word


def _word(text: Any) -> str:
    """A header word as a note's header line is keyed: lower case, its spaces as underscores."""
    return "_".join(str(text or "").lower().split())


def _notes(source: SourceFile) -> dict[str, str]:
    """The two header words a source file's `notes:` names, or none. Refused when it is not `each` and `with`, each
    a word."""
    given = source.settings.get("notes")
    if given is None:
        return {}
    fix = f"write notes: {{each: <header word>, with: <header word>}} in sources/{source.name}.yaml"
    if not isinstance(given, dict) or set(given) != set(NOTES):
        raise SourceError(f"sources/{source.name}.yaml notes: takes each and with alone: {fix}")
    words = {key: _word(given[key]) for key in NOTES}
    if not all(words.values()) or not all(_HEADER.match(f"{w}: x") for w in words.values()):
        raise SourceError(f"sources/{source.name}.yaml notes: each and with are header words, such as a note's "
                          f"first word before its colon: {fix}")
    return words


def _header(text: str, masked: bool = False) -> tuple[dict[str, list[str]], list[int]]:
    """The header lines of a note by word, and the numbers of the lines they stand on. In a `masked` text, a header
    line whose word held a stored value is no header line, so it stays in the content, and the header goes on."""
    found: dict[str, list[str]] = {}
    at: list[int] = []
    lines = text.split("\n", HEADER_LINES)[:HEADER_LINES]
    started = False
    for n, line in enumerate(lines):
        line = line.strip()
        if started and not line:
            break
        if not line or line == "---" or (line.startswith("#") and not started):
            continue
        match = _HEADER.match(line)
        if match is None and masked and MARK in line and _HEADER.match(line.replace(MARK, "x")):
            started = True
            continue
        if match is None:
            break
        started = True
        at.append(n)
        word, value = match.group(1).strip().lower().replace(" ", "_"), match.group(2).strip()
        if value:
            found.setdefault(word, []).append(value)
    return found, at


def header_lines(text: str, masked: bool = False) -> dict[str, list[str]]:
    """The header lines at the top of a note, each under its word in lower case, every value as written.

    The header is the run of `Word: value` lines among the first lines, after any front matter fence, title
    heading or blank line, and it ends at a blank line or the first line of anything else. Marks of bold or italic around a word
    are dropped, and each line is its own value, never split or joined. `masked` reads a masked text, as `_header` does.
    """
    return _header(text, masked)[0]


def content(text: str, masked: bool = False) -> str:
    """A note's text without its header lines, trimmed: what it says, for the facts layer to read. The header lines
    give the record its time and keys, and are no content. A note that is header lines and nothing else keeps them,
    since then they are all it says."""
    _, at = _header(text, masked)
    lines = text.split("\n")
    rest = "\n".join(line for n, line in enumerate(lines) if n not in set(at)).strip()
    return rest or text.strip()


def masked_note(text: str, mask: Callable[[str], str]) -> str:
    """A note's text masked before it is cut into header lines and content, its lines kept as lines: each header line
    is masked alone, as the line it is, and every other run of lines whole, with the header line before it. So a
    value wrapped from a header line onto a line that is no header line, as a front matter's `title:` wraps, is found
    whole, and no two header lines are read as one, so a line's end and the next line's word never spell a value."""
    _, at = _header(text)
    lines = text.split("\n")
    held = set(at)
    out, start = [], 0
    for cut in [*(n + 1 for n in at if n + 1 in held), len(lines)]:
        out.append(mask("\n".join(lines[start:cut])))
        start = cut
    return "\n".join(out)


DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
# A party on a header line: a name with its address in angle brackets, or a bare address.
PARTY = re.compile(r'(?:"?([^"<>,;]*?)"?\s*<\s*([^<>\s,;]+@[^<>\s,;]+)\s*>)|([^\s<>,;"]+@[^\s<>,;"]+)')


def parties(value: str, ours: Ours) -> tuple[str, list[str], list[str]] | None:
    """The parties a header line names by address, or None for a line that gives no address, which stays as written:
    the line with each address dropped, its parties' names joined; the addresses outside your side, which give the
    record its `email` and `domain` keys; and the names of the parties at your own addresses or domains, which are
    never keys. A bare address outside keeps its place on the line; a bare one of yours leaves it."""
    found = list(PARTY.finditer(value))
    if not found:
        return None
    names, outside, mine = [], [], []
    for match in found:
        name, address = (match.group(1) or "").strip(), (match.group(2) or match.group(3) or "").strip().lower()
        if ours.holds(address):
            mine.append(name or address)
            if name:
                names.append(name)
        else:
            outside.append(address)
            names.append(name or address)
    return ", ".join(dict.fromkeys(names)), outside, mine


def _when(text: str) -> datetime | None:
    """A time as a person or a mail client writes one. A time with no zone is read as UTC."""
    text = text.strip()
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _segments_match(pattern: list[str], name: list[str]) -> bool:
    """Whether a path's segments match a glob's, where `**` stands for any number of segments. Runs of `**`
    collapse, and each pair of places is tried once, so it takes time in proportion to their product."""
    pattern = [p for n, p in enumerate(pattern) if not (p == "**" and n and pattern[n - 1] == "**")]

    @functools.cache
    def match(i: int, j: int) -> bool:
        if i == len(pattern):
            return j == len(name)
        if pattern[i] == "**":
            return match(i + 1, j) or (j < len(name) and match(i, j + 1))
        return j < len(name) and fnmatch.fnmatchcase(name[j], pattern[i]) and match(i + 1, j + 1)

    return match(0, 0)


def _inside(folder: Path, path: Path) -> bool:
    return path == folder or folder in path.parents


@dataclass
class _File:
    """One file a glob matched: its record's id, the path it reads from, its `{path}`, its select entry and that
    entry's place under `select:`."""

    id: str
    real: Path
    path: str
    entry: Select
    index: int = 0

    @functools.cached_property
    def escaped(self) -> str:
        """Its `{path}` escaped for a url, a folder at a time."""
        return _escape_path(self.path)


class _Number(str):
    """A JSON number as the file writes it, so `1.50` stays `1.50`."""


@dataclass
class _Row:
    """One row: its values by column, its columns in order for csv and None for JSON, the line it starts on, why it
    does not read, when it does not, and where it stands in the file's text."""

    values: dict[str, Any]
    columns: list[str] | None
    line: int
    bad: str = ""
    start: int = 0  # where the row stands in the file's text, from its first character to the one after its last
    end: int = 0

    def order(self) -> list[str]:
        return self.columns if self.columns is not None else sorted(self.values)


class _NoColumn(Exception):
    """A template names a column a csv file's header lacks: a mistake in the entry, which every row would hit."""


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON value")


_DECODER = json.JSONDecoder(parse_float=_Number, parse_int=_Number, parse_constant=_no_constant)


def _json(value: Any) -> str:
    """A JSON value written compact, its numbers as the file wrote them and an object's keys in order."""
    if isinstance(value, _Number):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_json(v) for v in value) + "]"
    return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _json(value[k]) for k in sorted(value)) + "}"


def _scalar(value: Any) -> str:
    """A value as a template fills it: text as it is, a number as written, null as empty, anything else as JSON."""
    if value is None:
        return ""
    if isinstance(value, str) and not isinstance(value, _Number):
        return value
    return _json(value)


def _dig(values: dict[str, Any], column: str) -> tuple[Any, bool]:
    """A column's value, by its whole name or as a dotted path into nested objects, and whether the row holds it. A
    null on the way is a null value."""
    if column in values:
        return values[column], True
    here: Any = values
    for part in column.split("."):
        if here is None:
            return None, True
        if not isinstance(here, dict) or part not in here:
            return None, False
        here = here[part]
    return here, True




_SURROGATE = re.compile("[\ud800-\udfff]")


def _mended(value: Any) -> Any:
    """A JSON value with each lone surrogate, from a byte that is not UTF-8 or an escape such as `\\ud800`, read as
    U+FFFD."""
    if isinstance(value, _Number):
        return value
    if isinstance(value, str):
        return _SURROGATE.sub("�", value)
    if isinstance(value, list):
        return [_mended(v) for v in value]
    if isinstance(value, dict):
        return {_mended(k): _mended(v) for k, v in value.items()}
    return value


class _Lines:
    """A text read a line at a time for the csv reader: each line keeps its `\\n`, a `\\r\\n` reads as
    `\\n`, a lone `\\r` is no line end, and a `\\r` the file ends on is dropped."""

    def __init__(self, text: str):
        self.text, self.at, self.number = text, 0, 0

    def read(self) -> str | None:
        if self.at >= len(self.text):
            return None
        end = self.text.find("\n", self.at)
        if end < 0:
            line, self.at = self.text[self.at:], len(self.text)
            line = line.removesuffix("\r")
        else:
            line, self.at = self.text[self.at:end + 1], end + 1
            line = line[:-2] + "\n" if line.endswith("\r\n") else line
        self.number += 1
        return line


def _csv_record(lines: _Lines) -> tuple[int, list[str] | None, str, int] | None:
    """The next record, the line it starts on, why it does not read and where in the text it starts, read
    strictly, or None at the end. An empty line is skipped."""
    at = lines.at
    line = lines.read()
    while line == "\n":
        at = lines.at
        line = lines.read()
    if line is None:
        return None
    start, fields = lines.number, []
    while True:
        if not line.startswith('"'):
            comma = line.find(",")
            field = line[:comma] if comma >= 0 else line.removesuffix("\n")
            if '"' in field:
                return start, None, QUOTE_IN_VALUE, at
            fields.append(field)
            if comma < 0:
                return start, fields, "", at
            line = line[comma + 1:]
            continue
        line, held = line[1:], []
        while True:
            quote_at = line.find('"')
            if quote_at >= 0:
                held.append(line[:quote_at])
                line = line[quote_at + 1:]
                if line.startswith('"'):
                    held.append('"')
                    line = line[1:]
                    continue
                if line.startswith(","):
                    fields.append("".join(held))
                    line = line[1:]
                    break
                if line in ("", "\n"):
                    fields.append("".join(held))
                    return start, fields, "", at
                return start, None, AFTER_QUOTE, at
            if not line:
                return start, None, NEVER_CLOSED, at
            held.append(line)
            line = lines.read() or ""


def _csv_row(record: tuple[int, list[str] | None, str, int], header: list[str], end: int) -> _Row:
    line, fields, why, start = record
    if fields is None:
        return _Row({}, header, line, why, start, end)
    if len(fields) != len(header):
        return _Row({}, header, line, f"{len(fields)} fields where the header has {len(header)}", start, end)
    return _Row(dict(zip(header, fields, strict=True)), header, line, "", start, end)


def _csv_rows(text: str) -> Iterator[_Row]:
    """A csv file's rows, one at a time, its first record naming the columns. A row with
    more or fewer fields than the header, or a stray quote, does not read alone; a quote that never closes ends the
    file there. A header that does not read raises before the first row."""
    lines = _Lines(text)
    first = _csv_record(lines)
    if first is None:
        return iter(())
    if first[1] is None:
        raise ValueError(f"its header does not read: line {first[0]}: {first[2]}")
    header = first[1]
    twice = next((c for n, c in enumerate(header) if c in header[:n]), None)
    if twice is not None:
        raise ValueError(f"the header names column {twice} twice")

    def rows() -> Iterator[_Row]:
        while (record := _csv_record(lines)) is not None:
            yield _csv_row(record, header, lines.at)

    return rows()


def _object(value: Any, line: int, start: int, end: int) -> _Row:
    if not isinstance(value, dict):
        return _Row({}, None, line, "a row must be a JSON object", start, end)
    return _Row(_mended(value), None, line, "", start, end)


_SPACE = " \t\r\n"
_WS = re.compile(r"[ \t\r\n]*")
_WS_COMMAS = re.compile(r"[ \t\r\n,]*")


def _first(text: str) -> Any:
    """The first JSON value of a text, after any space: what follows it is not read."""
    return _DECODER.raw_decode(text, _WS.match(text).end())[0]


def _jsonl_rows(text: str) -> Iterator[_Row]:
    """One JSON object a line, one at a time. A line that does not read does not read alone."""
    at, n = 0, 0
    while at <= len(text):
        end = text.find("\n", at)
        end = len(text) if end < 0 else end
        start, line, at, n = at, text[at:end], end + 1, n + 1
        if not line.strip(_SPACE):
            continue
        try:
            value = _first(line)
        except (ValueError, RecursionError) as error:
            yield _Row({}, None, n, str(error), start, end)
            continue
        yield _object(value, n, start, end)


def _json_rows(text: str) -> Iterator[_Row]:
    """A top-level array of objects, one at a time, each row on the line its object starts, read
    one element at a time: elements apart by one comma, an element that is not an object fails alone, and one
    that does not parse ends the file there. A file that is no array raises before the first row."""
    at = _WS.match(text).end()
    if not text.startswith("[", at):
        raise ValueError("rows: json wants a top-level array of objects")

    def rows(at: int) -> Iterator[_Row]:
        after, counted, lines = False, 0, 1
        while True:
            here = _WS.match(text, at).end()
            if here >= len(text) or text[here] == "]":
                return
            start = _WS_COMMAS.match(text, here).end()
            lines, counted = lines + text.count("\n", counted, start), start
            if after:
                if text[here] != ",":
                    yield _Row({}, None, lines, "expected comma after array element", start, start)
                    return
                here += 1
            value_at = _WS.match(text, here).end()
            try:
                value, at = _DECODER.raw_decode(text, value_at)
            except (ValueError, RecursionError) as error:
                yield _Row({}, None, lines, str(error), value_at, value_at)
                return
            yield _object(value, lines, value_at, at)
            after = True

    return rows(at + 1)


_PARSE = {"csv": _csv_rows, "jsonl": _jsonl_rows, "json": _json_rows}


def _one(kind: str, text: str, header: list[str] | None, line: int) -> _Row:
    """One row read again from its own text, the part of its file it stood on."""
    if kind == "csv":
        lines = _Lines(text)
        record = _csv_record(lines)
        row = _csv_row(record, header or [], lines.at) if record is not None else _Row({}, header, line, "gone")
    else:
        try:
            row = _object(_first(text), line, 0, len(text))
        except (ValueError, RecursionError) as error:
            row = _Row({}, None, line, str(error))
    row.line = line
    return row


def _bytes(text: str, rows: Iterable[_Row], lead: int) -> Iterator[tuple[_Row, int, int]]:
    """Each row, with where it stands in its file's bytes, from where it stands in the text read from them: `lead`
    bytes of a byte-order mark first, and every character one byte or more, a byte that is not UTF-8 one."""
    char, byte = 0, lead
    for row in rows:
        span = []
        for at in (row.start, row.end):
            byte += len(text[char:at].encode("utf-8", "surrogateescape"))
            char = at
            span.append(byte)
        yield row, span[0], span[1]


def _row_revision(row: _Row) -> str:
    """A hash of a row's columns and values, never its line, so a row keeps it when rows above it move."""
    digest = hashlib.sha256()
    for column in row.order():
        value = row.values[column]
        text = value if row.columns is not None or (isinstance(value, str) and not isinstance(value, _Number)) \
            else _json(value)
        for part in (column, text):
            digest.update(f"{len(part.encode())}\0{part}\0".encode())
    return "sha256:" + digest.hexdigest()


def _row_text(row: _Row) -> str:
    return "".join(f"{column}: {_scalar(row.values[column])}\n" for column in row.order())


def _not_utf8(row: _Row) -> str:
    """Which part of a csv row holds a byte that is not UTF-8, read in as a lone surrogate, or "" for none."""
    for column in row.order():
        if any("\udc80" <= c <= "\udcff" for c in column):
            return "a column name"
        if any("\udc80" <= c <= "\udcff" for c in _scalar(row.values[column])):
            return f"column {column}"
    return ""


def _escape_path(path: str) -> str:
    return "/".join(quote(part, safe=_PATH_SAFE) for part in path.split("/"))


def _short(revision: str) -> str:
    return revision.split(":")[-1][:16]


def _named(spots: list[tuple[str, int | None]]) -> str:
    """The first few of some rows or files, by path and line, as `a.csv lines 3, 4, 5 and more`."""
    shown, more = spots[:3], len(spots) > 3
    groups: list[tuple[str, list[int | None]]] = []
    for path, line in shown:
        if groups and groups[-1][0] == path:
            groups[-1][1].append(line)
        else:
            groups.append((path, [line]))
    words = []
    for n, (path, lines) in enumerate(groups):
        numbers = [str(line) for line in lines if line is not None]
        tail = " and more" if more and n == len(groups) - 1 else ""
        if not numbers:
            words.append(path + tail)
        elif len(numbers) == 1 and not tail:
            words.append(f"{path} line {numbers[0]}")
        else:
            joined = ", ".join(numbers) + tail if tail else ", ".join(numbers[:-1]) + " and " + numbers[-1]
            words.append(f"{path} lines {joined}")
    return ", ".join(words)


class _Said:
    """What one read of a source's files has to say: a line for each file it could not read whole, and each kind of
    trouble with rows said once, its first row in full and the next few named. `unread` is whether a file the globs
    match, or a row in one, did not read, so that nothing counts as removed."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.groups: dict[tuple[str, ...], list[tuple[str, int | None, str]]] = {}
        self.unread = False

    def line(self, text: str, *, unread: bool = False) -> None:
        self.lines.append(text)
        self.unread = self.unread or unread

    def bad(self, path: str, line: int | None, why: str) -> None:
        """A row, or a file read whole, that does not read."""
        self.groups.setdefault(("bad",), []).append((path, line, why))
        self.unread = True

    def time(self, path: str, line: int | None, written: str) -> None:
        self.groups.setdefault(("time",), []).append((path, line, written))

    def missing(self, path: str, line: int, id: str, part: str, key: str, columns: list[str]) -> None:
        self.groups.setdefault(("missing", part, key, ", ".join(columns)), []).append((path, line, id))

    def said(self, mask: Callable[[str], str]) -> list[str]:
        out = list(self.lines)
        for group, found in sorted(self.groups.items(), key=lambda item: item[0][0] != "bad"):
            path, line, what = found[0]
            where = f"{path} line {line}" if line is not None else path
            rest = len(found) - 1
            more = f" ({_named([(p, n) for p, n, _ in found[1:]])})"
            if group[0] == "bad":
                thing = "row" if line is not None else "file"
                said = f"{where}: {what}, so the {thing} is not read"
                said += f", nor are {rest} more{more}" if rest else ""
                said += (": they wait, and nothing this source gave counts as removed until they read" if rest else
                         ": it waits, and nothing this source gave counts as removed until it reads")
            elif group[0] == "time":
                said = f"{where}: the time {what!r} does not read, so the record has no time"
                said += f", nor have {rest} more{more}" if rest else ""
                said += ": write it as 2026-11-17 or 2026-11-17T09:00:00Z"
            else:
                _, part, key, columns = group
                said = (f"{what}: {part}.{key}: the row has no {columns}, so the "
                        f"{'key' if part == 'identity' else 'field'} is left out")
                said += f", and so in {rest} more row{'s' if rest > 1 else ''}{more}" if rest else ""
            out.append(said)
        return [mask(line) for line in out]


@dataclass(slots=True)
class _Spot:
    """Where one record is read again: its file, that file's change mark and size when it was read, and for a row
    the line it starts on, the bytes it stands on and, in csv, its header."""

    file: _File
    mark: tuple[int, int]
    line: int = 0
    start: int = -1
    end: int = -1
    header: list[str] | None = None


@dataclass
class _Index:
    """One read of every file the globs match, holding no record: each id's short revision, whether it is inside the
    window, and where to read it again, a later record of an id winning; what the read had to say; and, while a
    listing goes on page by page, the cursor it last gave, the ids still to list and what the cursor keeps."""

    entries: dict[str, tuple[str, bool, _Spot]]
    unread: bool
    notices: list[str]
    missing: list[str]
    cursor: str | None = None
    pending: list[str] = field(default_factory=list)
    kept: dict[str, str] = field(default_factory=dict)
    gone: list[str] = field(default_factory=list)
    size: int = PAGE_SIZE


class _Stale(Exception):
    """A file changed since the read that indexed it."""


class FilesChanged(CerebroError):
    """A source's files changed under every read of one listing. Its cursor stays, so the next pass goes on."""


def _masked_fields(fields: dict[str, Any], mask: Callable[[str], str]) -> dict[str, Any]:
    return {key: mask(value) if isinstance(value, str) else
            [mask(v) if isinstance(v, str) else v for v in value] if isinstance(value, list) else value
            for key, value in fields.items()}


# The source `connect local` adds when the workspace has none: the file init writes,
# over the local/ folder.
STARTER = Starter(
    "local", (Path(__file__).resolve().parents[1] / "base" / "sources" / "local.yaml").read_text(encoding="utf-8"),
    "local", "Added sources/{name}.yaml, which reads the notes, call notes and emails you drop into local/.",
    "Local files: sources/{name}.yaml reads {count} now", "file")


class FilesConnector(Connector):
    """A `kind: files` source over the workspace at `root`. `now` is the clock the window is read by. `masks`, when
    given, gives a mask of every value the secrets store holds, for each read."""

    title = "Local files"  # its name in connect's lobby

    starter = STARTER

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> FilesConnector:
        return cls(source, root, now=app.clock, ours=app.ours, masks=app.mask_every)

    def __init__(self, source: SourceFile, root: Path, *, page_size: int = PAGE_SIZE, max_bytes: int = MAX_BYTES,
                 now: Callable[[], datetime] | None = None, home: Path | None = None, ours: Ours | None = None,
                 masks: Callable[[], Callable[[str], str]] | None = None):
        if source.kind != "files":
            raise SourceError(f"sources/{source.name}.yaml is kind {source.kind}: a files connector reads kind "
                               f"files alone")
        self.name = source.name
        self.source = source
        self.root = Path(root).resolve()
        self.home = home
        self._ours = ours  # your addresses and domains, read from the workspace when not given
        self.masks = masks
        self.page_size = max(1, page_size)
        self.max_bytes = max_bytes
        self.now = now or system_clock
        self.behind = source.behind()
        self.notes = _notes(source)
        config = json.dumps([asdict(source.window), source.extract, [asdict(e) for e in source.select]],
                            sort_keys=True)
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]
        self.settle: timedelta | None = None  # under serve: a file is read once left alone this long
        self.unsettled_until: datetime | None = None  # after a listing: when the first file it left will have settled
        # Each file's change mark and size as last seen, when it was first seen so by the workspace's clock, and whether
        # it has settled. The disk's own times are only a mark of change here, never read as a time.
        self._sighted: dict[str, tuple[tuple[int, int], datetime, bool]] = {}
        self._index: _Index | None = None  # the last listing's read, which its next page and `get` go on from
        self._looked: _Index | None = None  # a read `get` made of every file, settled or not
        # By record id and header word, each line as the file writes it, by the line as its key keeps it: a person
        # named in a header line keeps the address written beside the name for the notes question
        self._written: dict[str, dict[str, dict[str, str]]] = {}

    def _mask(self) -> Callable[[str], str]:
        return self.masks() if self.masks is not None else (lambda text: text)

    # -- where files are
    def _home(self) -> Path:
        return (self.home or Path.home()).resolve()

    def _absolute(self, glob: str) -> str:
        if glob == "~" or glob.startswith("~/"):
            glob = str(self._home()) + glob[1:]
        path = Path(glob)
        return os.path.normpath(path if path.is_absolute() else self.root / path).replace(os.sep, "/")

    def _path(self, file: Path) -> str:
        """A file's `{path}`: from the workspace's folder, else `~/` and its path from home, else absolute."""
        for folder, lead in ((self.root, ""), (self._home(), "~/")):
            if _inside(folder, file) and file != folder:
                return lead + file.relative_to(folder).as_posix()
        return file.as_posix()

    def _fill(self, template: str, path: str, *, escape: bool = False) -> str:
        value = quote(path, safe="/~") if escape else path
        return template.replace("{path}", value).replace("{line}", "1")

    def _fill_row(self, template: str, file: _File, row: _Row, *, url: bool = False,
                  missing: list[str] | None = None, mask: Callable[[str], str] | None = None) -> str:
        """A template filled for one row. In a url a value that is the whole template is
        taken as a url already; any other is escaped as a query value after the first `?`, `{path}` one folder at a
        time elsewhere, and any other value as a part of a path. A column a JSON row lacks fills as empty and is
        added to `missing`. A url's values are masked by `mask` before they are escaped."""
        whole = _TEMPLATE.fullmatch(template) is not None
        query, fragment = template.find("?"), template.find("#")

        def one(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in ("path", "line"):
                value = file.path if name == "path" else str(row.line)
            else:
                found, held = _dig(row.values, name[4:])
                if not held and row.columns is not None:
                    raise _NoColumn(f"{{{name}}}: {file.path} has no column {name[4:]} in its header")
                if not held and missing is not None:
                    missing.append(name[4:])
                value = _scalar(found)
            if mask is not None:
                value = mask(value)
            if not url or whole:
                return value
            if query >= 0 and match.start() > query and (fragment < 0 or fragment < query or match.start() < fragment):
                return quote_plus(value, safe="")
            return _escape_path(value) if name == "path" else quote(value, safe=_PATH_SAFE)

        return _TEMPLATE.sub(one, template)

    def _matched(self, index: int, entry: Select, said: _Said) -> list[_File]:
        segments = self._absolute(entry.glob).split("/")
        first = next((i for i, s in enumerate(segments) if any(c in s for c in "*?[")), len(segments) - 1)
        base, pattern = Path("/".join(segments[:first]) or "/"), segments[first:]
        if not base.is_dir():
            self._missing.append(self._path(base))
            return []
        folder = base.resolve()
        found, any_file, matched = [], False, False
        for here, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(names):
                if name.startswith("."):
                    continue
                any_file = True
                file = Path(here, name)
                if not _segments_match(pattern, file.relative_to(base).as_posix().split("/")):
                    continue
                matched = True
                path = self._path(Path(os.path.normpath(file)))
                try:
                    real = file.resolve()
                    size = real.stat().st_size if real.is_file() else -1
                except (OSError, RuntimeError):  # gone since the walk, or a link that loops
                    continue
                if "\n" in name or "\r" in name:
                    said.line(f"{path!r}: a file name with a line break, not read: rename it")
                elif not _inside(folder, real) or any(p.startswith(".") for p in real.relative_to(folder).parts):
                    said.line(f"{path}: a link that points outside {self._path(folder)}, not read")
                elif size < 0:
                    continue
                elif not entry.rows and (file.suffix.lower() not in TEXT_TYPES
                                         or real.suffix.lower() not in TEXT_TYPES):
                    said.line(f"{path}: skipped, only text and markdown are read")
                elif not entry.rows and size > self.max_bytes:
                    said.line(f"{path}: skipped, larger than {self.max_bytes} bytes", unread=True)
                else:
                    found.append(_File(self._fill(entry.id, path) if entry.id and not entry.rows else path, real,
                                       path, entry, index))
        if any_file and not matched:
            said.line(f"select[{index}]: glob {entry.glob} matched no files")
        return found

    def _files(self, said: _Said) -> list[_File]:
        """The files to read, in the order of their entries and paths."""
        self._missing: list[str] = []
        return [file for index, entry in enumerate(self.source.select) for file in self._matched(index, entry, said)]

    # -- reading one file
    def _data(self, file: _File, cap: int | None) -> bytes | None:
        """The file's bytes, or None when it no longer reads as it did when it matched or passes `cap`."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            handle = os.open(file.real, flags)
        except OSError:
            return None
        with os.fdopen(handle, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (cap is not None and info.st_size > cap):
                return None
            data = stream.read() if cap is None else stream.read(cap + 1)
        return None if cap is not None and len(data) > cap else data

    def _read(self, file: _File) -> tuple[str, str] | None:
        """A file read one record a file: its text and revision, or None when it is not UTF-8 text or no longer
        reads as it did when it matched."""
        data = self._data(file, self.max_bytes)
        if data is None:
            return None
        try:
            text = data.decode("utf-8").removeprefix("﻿")
        except UnicodeDecodeError:
            return None
        return text, "sha256:" + hashlib.sha256(data).hexdigest()

    def _table(self, file: _File) -> tuple[str, int] | None:
        """A file read one record a row, with no size cap, and how many bytes of a byte-order mark it starts with. A
        byte that is not UTF-8 is kept, to fail only its row in csv and to read as U+FFFD in JSON."""
        data = self._data(file, None)
        if data is None:
            return None
        text = data.decode("utf-8", errors="surrogateescape")
        return (text[1:], 3) if text.startswith("﻿") else (text, 0)

    @property
    def ours(self) -> Ours:
        """Your own addresses and the workspace's own domains, as the workspace's `profile.md` and `workspace.md` give
        them, read once."""
        if self._ours is None:
            from ..project import read_ours  # here: the project module reads far more than a connector needs

            self._ours = read_ours(self.root)
        return self._ours

    def _parties(self, keys: dict[str, list[str]], fields: dict[str, Any], identity: Collection[str] = ()) -> list[str]:
        """Read the parties each header line names by address: the line keeps their names, each address outside
        your side adds its `email` and `domain` keys, and the names of your own people go to the `ours` field, so
        the record can say who on it is yours. A line of names alone stays as written: nothing is guessed from a
        name. Gives the keys the parties added that the record only carries, those the entry's `identity` does not
        name: they point at the parties, and never name the record."""
        outside: list[str] = []
        mine: list[str] = []
        for word, values in keys.items():
            kept = []
            for value in values:
                read = parties(value, self.ours)
                if read is None:
                    kept.append(value)
                    continue
                line, theirs, own = read
                outside += theirs
                mine += own
                if line:
                    kept.append(line)
            keys[word] = kept
        for word in [word for word, values in keys.items() if not values]:
            del keys[word]
        added = address_keys(outside, self.ours)
        for key, values in added.items():
            keys[key] = list(dict.fromkeys([*keys.get(key, []), *values]))
        if mine:
            fields["ours"] = list(dict.fromkeys(mine))
        return [key for key in added if key not in identity]

    def _record(self, file: _File, text: str, revision: str, said: _Said,
                mask: Callable[[str], str]) -> Record | None:
        """A file read whole as one record, or None when its id holds a value the secrets store keeps. The text is
        masked before it is cut into header lines and content, as `masked_note` masks it, so a value wrapped from a
        header line onto the next is found whole, and a header word never holds one."""
        if mask(file.id) != file.id:
            said.bad(file.path, None, "its id holds a value the secrets store keeps, and the id is never shown")
            return None
        text = masked_note(text, mask)
        headers = header_lines(text, masked=True)
        written = self._fill(file.entry.at, file.path) if file.entry.at else (headers.get("date") or [""])[0]
        keys = {word: values for word, values in headers.items() if word not in _TIME_KEYS}
        for key, template in file.entry.identity.items():
            keys.setdefault(key, []).append(self._fill(template, file.path))
        url = self._fill(file.entry.url, file.path, escape=True) if file.entry.url else quote(file.path, safe="/~")
        fields = {key: self._fill(template, file.path) for key, template in file.entry.fields.items()}
        self._written[file.id] = {word: {read[0]: " ".join(value.split()) for value in values
                                         if (read := parties(value, self.ours)) is not None and read[0]}
                                  for word, values in keys.items()}
        carried = self._parties(keys, fields, file.entry.identity)
        if file.entry.into:  # a file that lands in a collection: its header lines name what it hangs off, not it
            carried = [key for key in keys if key not in file.entry.identity]
        _, at = self._time(written, file.path, None, said, mask, midnight=False)
        return Record(id=file.id, source=self.name, revision=revision, at=at, url=mask(url),
                      keys={k: [mask(v) for v in vs] for k, vs in keys.items()}, fields=_masked_fields(fields, mask),
                      body=content(text, masked=True), extract=file.entry.extract or self.source.extract or "facts",
                      into=file.entry.into, carried=carried)

    @staticmethod
    def _time(written: str, path: str, line: int | None, said: _Said, mask: Callable[[str], str], *,
              midnight: bool) -> tuple[datetime | None, str]:
        """A record's time, from what its file writes, masked first. A date alone stays a date, unless it came from
        an entry's `at:`, which reads it at midnight UTC."""
        written = mask(written).strip()
        when = _when(written) if written else None
        if written and when is None:
            said.time(path, line, written)
        if when is None:
            return None, ""
        return when, written if not midnight and DATE_ONLY.fullmatch(written) else _stamp(when)

    def _row(self, file: _File, row: _Row, said: _Said, mask: Callable[[str], str], *, full: bool = True,
             body: bool = False, clean: bool = False, fill: bool = True) -> Record | None:
        """One row's record, its keys and fields as the file writes them, or None, said, when the row does not read.
        A mistake in the entry itself, a column the header lacks, stops the listing. Not `full`, it holds the id,
        revision and time alone, and fills the entry's other templates only with `fill`, to say what a row lacks;
        with `body`, its text. `clean` says the file holds no byte that is not UTF-8."""
        entry = file.entry
        bad = row.bad or ("" if clean or row.columns is None else
                          f"{part} is not valid UTF-8" if (part := _not_utf8(row)) else "")
        if bad:
            said.bad(file.path, row.line, bad)
            return None
        try:
            id = self._fill_row(entry.id, file, row) if entry.id else f"{file.path}#L{row.line}"
            if not id:
                said.bad(file.path, row.line, f"id {entry.id} is empty")
                return None
            if mask(id) != id:
                said.bad(file.path, row.line, "its id holds a value the secrets store keeps, and the id is never shown")
                return None
            keys: dict[str, list[str]] = {}
            fields: dict[str, Any] = {}
            for part, templates in (("identity", entry.identity), ("fields", entry.fields)) if full or fill else ():
                for key, template in templates.items():
                    missing: list[str] = []
                    value = self._fill_row(template, file, row, missing=missing)
                    if missing:
                        said.missing(file.path, row.line, id, part, key, missing)
                    elif part == "identity":
                        keys.setdefault(key, []).append(mask(value))
                    else:
                        fields[key] = mask(value)
            written = self._fill_row(entry.at, file, row) if entry.at else ""
            _, at = self._time(written, file.path, row.line, said, mask, midnight=True)
            url = self._fill_row(entry.url, file, row, url=True, mask=mask) if entry.url else \
                f"{file.escaped}#L{row.line}"  # filled even for the index, so a row that indexes also lists
            into = mask(self._fill_row(entry.into, file, row)) if entry.into else ""
            if not full:
                return Record(id=id, source=self.name, revision=_row_revision(row), at=at)
            return Record(id=id, source=self.name, revision=_row_revision(row), at=at, url=mask(url), keys=keys,
                          fields=fields, body=mask(_row_text(row)) if body else None,
                          extract=entry.extract or self.source.extract or "records", into=into)
        except _NoColumn as error:
            raise SourceError(f"sources/{self.name}.yaml select[{file.index}]: {error}: name a column it has, or "
                              f"fix the file") from None
        except Exception as error:  # noqa: BLE001 - a row that does not read fails alone
            said.bad(file.path, row.line, f"{type(error).__name__}: {error}")
            return None

    def _settled(self, file: _File, now: datetime) -> bool | None:
        """Whether a file may be read now, under serve's settle time: None when it is gone, and False while a change
        to it has not settled, which `unsettled_until` then says when it will have."""
        if not self.settle:
            return True
        try:
            info = file.real.stat()
        except OSError:
            return None
        mark = (info.st_mtime_ns, info.st_size)
        key = file.real.as_posix() + "\0" + file.id
        last = self._sighted.get(key)
        if last is None:  # first seen: read at once, mid-save or not, and read again once a change settles
            self._sighted[key] = (mark, now, True)
        elif last[0] != mark:
            self._sighted[key] = (mark, now, False)
        elif not last[2] and last[1] + self.settle <= now:
            self._sighted[key] = (mark, last[1], True)
        if self._sighted[key][2]:
            return True
        until = self._sighted[key][1] + self.settle
        self.unsettled_until = min(self.unsettled_until or until, until)
        return False

    def _each(self, said: _Said, mask: Callable[[str], str], now: datetime | None = None, *,  # type: ignore[no-untyped-def]
              full: bool = False):
        """Every record the files give, without its body, and where to read it again, in the order of the entries
        and their paths, each file parsed once. A row's record holds its id, revision and time alone unless `full`.
        With `now`, a file whose change has not settled is left for a later pass."""
        for file in self._files(said):
            if now is not None and not self._settled(file, now):
                said.unread = True
                continue
            try:
                info = file.real.stat()
            except OSError:
                said.unread = True
                continue
            mark = (info.st_mtime_ns, info.st_size)
            if not file.entry.rows:
                read = self._read(file)
                if read is None:
                    said.line(f"{file.path}: skipped, it is not UTF-8 text or changed while it was read", unread=True)
                    continue
                record = self._record(file, *read, said, mask)
                if record is not None:
                    record.body = None
                    yield record, _Spot(file, mark)
                continue
            table = self._table(file)
            if table is None:
                said.line(f"{file.path}: skipped, it changed while it was read", unread=True)
                continue
            text, lead = table
            try:
                rows = _PARSE[file.entry.rows](text)
            except (ValueError, RecursionError) as error:
                said.line(f"{file.path}: skipped, {error}", unread=True)
                continue
            clean, first = not _SURROGATE.search(text), True
            for row, start, end in _bytes(text, rows, lead):
                record = self._row(file, row, said, mask, full=full, clean=clean,
                                   fill=first or row.columns is None)  # a column a csv header lacks, every row lacks
                first = first and (record is None)
                if record is not None:
                    yield record, _Spot(file, mark, row.line, start, end, row.columns)

    def _inside(self, record: Record, floor: datetime | None) -> bool:
        return floor is None or not record.at or _when(record.at) >= floor  # type: ignore[operator]

    def _read_all(self, now: datetime | None) -> _Index:
        """Read every file once into an index, the window read by the clock and, with `now`, the settle time."""
        said, mask = _Said(), self._mask()
        clock = self.now()
        floor = clock - self.behind if self.behind is not None else None
        if now is not None:
            self.unsettled_until = None
        entries: dict[str, tuple[str, bool, _Spot]] = {}
        for record, spot in self._each(said, mask, now):
            entries.pop(record.id, None)
            entries[record.id] = (_short(record.revision), self._inside(record, floor), spot)
        return _Index(entries, said.unread, said.said(mask), list(self._missing))

    def _again(self, ids: list[str], index: _Index, *, body: bool = False) -> list[Record]:
        """The records of `ids` read again from where the index found them, each file opened once and the part its
        rows stand on read in one piece. Raises `_Stale` when a file changed since."""
        spots = [index.entries[id][2] for id in ids]
        blocks: dict[Path, tuple[int, bytes]] = {}
        for real in dict.fromkeys(spot.file.real for spot in spots):
            mine = [spot for spot in spots if spot.file.real == real]
            try:
                info = real.stat()
            except OSError:
                raise _Stale from None
            if any((info.st_mtime_ns, info.st_size) != spot.mark for spot in mine):
                raise _Stale
            rows = [spot for spot in mine if spot.start >= 0]
            if rows:
                low, high = max(min(s.start for s in rows) - 1, 0), max(s.end for s in rows)
                try:
                    with os.fdopen(os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
                        stream.seek(low)
                        blocks[real] = (low, stream.read(high - low))
                except OSError:
                    raise _Stale from None
        said, mask, out = _Said(), self._mask(), []
        for id, spot in zip(ids, spots, strict=True):
            file = spot.file
            if spot.start < 0:
                read = self._read(file)
                record = self._record(file, *read, said, mask) if read is not None else None
                if record is not None and not body:
                    record.body = None
            else:
                low, block = blocks[file.real]
                if file.entry.rows != "json" and spot.start > 3 and block[spot.start - low - 1:spot.start - low] != b"\n":
                    raise _Stale  # a csv or jsonl row starts a line: a file put back the same size cuts it elsewhere
                text = block[spot.start - low:spot.end - low].decode("utf-8", errors="surrogateescape")
                row = _one(file.entry.rows, text, spot.header, spot.line)
                record = self._row(file, row, said, mask, body=body)
            if record is None or record.id != id or _short(record.revision) != index.entries[id][0]:
                raise _Stale  # a file put back with the same size and time is caught by its row's revision
            out.append(record)
        return out

    # -- the cursor
    def _cursor(self, seen: dict[str, str]) -> str:
        state = json.dumps({"config": self._config, "seen": seen}, sort_keys=True, separators=(",", ":"))
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(state.encode())).decode()

    def _seen(self, cursor: str) -> tuple[dict[str, str], bool]:
        """The revisions a cursor saw, and whether the source file read the same then."""
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
        """A page of what changed since `since`. The files are read once for a listing, and its next pages go on
        from that read, each record read again from where it stands. While a file or a row the globs match does not
        read, nothing counts as removed, and what it gave before stays in the cursor."""
        for _ in range(3):
            index = self._index
            try:
                if index is None or since is None or since != index.cursor or not index.pending:
                    index = self._index = self._read_all(self.now() if self.settle else None)
                    if index.missing and since is not None:
                        notice = (f"{', '.join(index.missing)} is missing, so nothing is listed and nothing counts "
                                  f"as removed until it is back: restore it, or edit sources/{self.name}.yaml")
                        return Listing([], since, [], False, [*index.notices, notice])
                    self._begin(index, since)
                return self._page(index)
            except _Stale:
                self._index = None
        raise FilesChanged(f"{self.name}: its files kept changing while they were read, so it is listed again next "
                           f"pass from where it was")

    def _begin(self, index: _Index, since: str | None) -> None:
        seen, same = self._seen(since) if since is not None else ({}, True)
        current = {id: rev for id, (rev, inside, _) in index.entries.items() if inside}
        left = [id for id in seen if id not in current]
        index.gone, held = [], {}
        if index.unread:  # absence proves nothing while something does not read: what it gave before stays
            held = {id: seen[id] for id in left if id not in index.entries}
        elif not any(entry.rows for entry in self.source.select):  # a row gone is forgotten
            index.gone = gone(left, lambda id: id in index.entries)  # aged out is not gone
        if not same:
            seen = {}  # the source file changed, so every record is listed anew
        index.pending = sorted(id for id, rev in current.items() if seen.get(id) != rev)
        index.size = max(self.page_size, -(-len(index.pending) // MAX_PAGES))  # each page writes the whole cursor
        index.kept = {id: rev for id, rev in seen.items() if id in current} | held

    def _page(self, index: _Index) -> Listing:
        page, index.pending = index.pending[:index.size], index.pending[index.size:]
        records = self._again(page, index)
        index.kept.update({record.id: _short(record.revision) for record in records})
        removed, index.gone = index.gone, []
        index.cursor = self._cursor(index.kept)
        more = bool(index.pending)
        return Listing(records, index.cursor, removed, more, [] if more else index.notices)

    # -- a curator's question
    questions = QUESTIONS

    def _current(self) -> list[Record]:
        """Every record inside the window, without its body, a later record of an id winning."""
        floor = self.now() - self.behind if self.behind is not None else None
        records: dict[str, Record] = {}
        for record, _ in self._each(_Said(), self._mask(), full=True):
            records.pop(record.id, None)
            records[record.id] = record
        return [record for record in records.values() if self._inside(record, floor)]

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """A curator's question, as `QUESTIONS` lists it: one row for each value of a header line in the files the
        source reads within its window, its key that header word, so an admit with it binds every file that
        carries the line. It reads the files as `list` does, and keeps nothing."""
        read = asked(f"files source {self.name}", QUESTIONS, TAKES, question, params, limit)
        if question == "notes":
            return self._notes_of(read, limit)
        word = "_".join(read["word"].lower().split())
        found: dict[str, dict[str, Any]] = {}
        for record in self._current():
            for value in dict.fromkeys(record.keys.get(word, [])):
                row = found.setdefault(" ".join(value.split()).lower(), {"name": value, "files": set(), "dates": [],
                                                                         "into": {}})
                row["files"].add(record.id)
                row["dates"] += [record.at[:10]] if record.at else []
                row["into"][record.into] = row["into"].get(record.into, 0) + 1
        rows = sorted(found.values(), key=lambda r: (-len(r["files"]), r["name"].lower()))
        answer = [Record(id=row["name"], source=self.name, revision="", keys={word: [row["name"]]},
                         fields={"name": row["name"], "files": len(row["files"]),
                                 "first": min(row["dates"], default=""), "last": max(row["dates"], default="")},
                         into=max(row["into"], key=lambda into: (row["into"][into], into)))
                  for row in rows[:limit]]
        return Listing(answer, None, [], len(rows) > limit, [])

    def _notes_of(self, read: dict[str, Any], limit: int) -> Listing:
        """The `notes` question: one row for each value of the header line `each`, with the values of `with` in the
        files that carry it, its key that header word, so an admit with it binds every file that carries the line. A
        person the line names keeps the address written beside the name: a line that gives one is under `written` as
        the file writes it, since the line as its key keeps it holds names alone."""
        each, with_ = (_word(read[key]) or self.notes.get(key, "") for key in NOTES)
        if not each or not with_:
            raise QuestionError(f"files source {self.name}: notes needs each and with, two header words: give them, "
                                f"or name them in sources/{self.name}.yaml as notes: {{each: <word>, with: <word>}}")
        found: dict[str, dict[str, Any]] = {}
        for record in self._current():
            beside = list(dict.fromkeys(record.keys.get(with_, [])))
            for value in dict.fromkeys(record.keys.get(each, [])):
                row = found.setdefault(" ".join(value.split()).lower(), {"name": value, "files": set(), "dates": [],
                                                                         "with": [], "into": {}, "written": []})
                row["files"].add(record.id)
                written = self._written.get(record.id, {}).get(each, {}).get(value)
                row["written"] += [written] if written and written not in row["written"] else []
                row["dates"] += [record.at[:10]] if record.at else []
                row["with"] += [v for v in beside if v not in row["with"]]
                row["into"][record.into] = row["into"].get(record.into, 0) + 1
        rows = sorted(found.values(), key=lambda r: (-len(r["files"]), r["name"].lower()))
        answer = [Record(id=row["name"], source=self.name, revision="", keys={each: [row["name"]]},
                         fields={"name": row["name"], with_: row["with"],
                                 **({"written": row["written"]} if row["written"] else {}),
                                 "files": len(row["files"]),
                                 "first": min(row["dates"], default=""), "last": max(row["dates"], default="")},
                         into=max(row["into"], key=lambda into: (row["into"][into], into)))
                  for row in rows[:limit]]
        return Listing(answer, None, [], len(rows) > limit, [])

    def get(self, id: str) -> Record:
        """One record whole, read again from where the last read found it, or from a new read of every file when
        its file changed since or that read did not find it."""
        for index in (self._index, self._looked):
            if index is not None and id in index.entries:
                try:
                    return self._again([id], index, body=True)[0]
                except _Stale:
                    pass
        self._looked = self._read_all(None)
        if id in self._looked.entries:
            try:
                return self._again([id], self._looked, body=True)[0]
            except _Stale:
                pass
        raise RecordNotFound(f"{self.name} has no record {id}: list the source for the ids it holds")
