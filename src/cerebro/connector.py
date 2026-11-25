"""Connectors read outside systems. They never write to one.

A connector lists its records cheaply, without bodies, from a cursor it gave before, so a pass lists only what
changed. It fetches one record whole on demand. A source may also answer a curator's questions with `ask`, read
only too, in rows that carry no body. It knows nothing of facts or articles: its records carry the keys
that can bind them to articles, and binding happens elsewhere. Third-party records are not kept by Cerebro, so a
connector is the copy of record and is asked again when a record is needed.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, ClassVar

import yaml

from .caller import Caller
from .errors import CerebroError

if TYPE_CHECKING:
    from pathlib import Path

    from .connectors.connecting import Need
    from .source import SourceFile

__all__ = ["MAX_RETRY_WAIT", "PUBLIC_MAIL", "Ask", "Choice", "Connector", "CursorError", "Listing", "Offer", "Ours",
           "QuestionError", "Record", "RecordNotFound", "address_keys", "clip_line", "event_key", "gone", "guard", "holds",
           "is_public_mail", "masked_first", "masking", "one_line", "outside_domain", "own_people", "registrable", "retry_after",
           "split_login", "with_list"]


@dataclass
class Record:
    """One record of a source, or one first-party record such as a note or a correction.

    `keys` are the values that can bind it to articles, such as `{"email": ["ada@example.com"]}`. `links` are the
    ids of related records. `body` is None in a listing. A first-party record is kept whole, with `by`, who made it.
    `extract` is what the record distills into, as its source file says, such as `facts`, and `into` the collection
    its records land in, when the source names one.

    `carried` names the keys the record only carries: values that point at something else, such as the addresses of
    a record's parties and their organisations' domains, and never name the record itself. A carried key never binds the
    record to an article, and an article admitted from the record does not take it, though the record's facts still
    link by it. Every other key names it.
    """

    id: str
    source: str
    revision: str
    at: str = ""
    url: str = ""
    keys: dict[str, list[str]] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    body: str | None = None
    by: Caller | None = None
    extract: str = ""
    into: str = ""
    carried: list[str] = field(default_factory=list)


@dataclass
class Listing:
    """What one listing gave: records without bodies, the ids of records the source says are gone since the cursor,
    the cursor to list from next time, whether more waits behind this page, and a line for each thing worth a
    person's notice, such as a file skipped. Listing again from the returned cursor gives the next page, then only
    what changed after.

    `removed` means deleted or cancelled at the source, never a record that only left the window or the source
    file's scope: the runner retracts a removed record's facts, and a record that aged out still exists. A connector
    reports `removed` only where its source can say a record is gone, as `gone` asks it.

    A listing from a cursor set for changes alone, as `Connector.cursor_for` sets one, proves nothing by absence: a
    record it does not name may still be at the source, unchanged, and in it `removed` names only what the source
    itself said is gone. Its records may repeat some the last listing gave, as an overlap does, which is safe.

    `again` says a record of this page was left out, so the page is listed again from the cursor it was listed from:
    the runner keeps that cursor, not this one, until a listing leaves nothing out."""

    records: list[Record]
    cursor: str | None
    removed: list[str] = field(default_factory=list)
    more: bool = False
    notices: list[str] = field(default_factory=list)
    again: bool = False


def gone(ids: Iterable[str], exists: Callable[[str], bool]) -> list[str]:
    """Of the ids a listing no longer found, those the source says are gone, asking `exists` of each: a record
    that only left the window still exists, and is dropped from the cursor without being called removed."""
    return sorted(id for id in ids if not exists(id))


def holds(get: Callable[[str], Any]) -> Callable[[str], bool]:
    """An `exists` for `gone` from a connector's `get`: a record exists unless `get` raises `RecordNotFound`."""

    def exists(id: str) -> bool:
        try:
            get(id)
        except RecordNotFound:
            return False
        return True

    return exists


def guard(text: str, own: re.Pattern[str]) -> str:
    """Text a source holds, such as an issue's body, with each line that could pass for one the connector writes
    itself, as `own` matches it, marked with a leading backslash, so a forged comment never reads as a real one."""
    return "\n".join("\\" + line if own.match(line.lstrip()) else line for line in text.split("\n"))


MAX_RETRY_WAIT = 600.0  # seconds: a source that asks for longer is asked again after ten minutes


def retry_after(value: str | None, now: datetime) -> float | None:
    """The seconds a `Retry-After` asks for, given as a count or as an HTTP date, from 0 to ten minutes. None when it
    is missing or reads as neither, so the caller waits as it would with none."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        wait = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        wait = ((when if when.tzinfo is not None else when.replace(tzinfo=UTC)) - now).total_seconds()
    if math.isnan(wait):
        return None
    return min(max(wait, 0.0), MAX_RETRY_WAIT)


_MASK: ContextVar[Callable[[str], str] | None] = ContextVar("cerebro_connector_mask", default=None)


@contextmanager
def masking(mask: Callable[[str], str]) -> Iterator[None]:
    """While it lasts, `masked_first` masks with `mask` in this thread. The app holds one around each call it makes
    to a connector, so a connector masks what a source gave before it cuts it."""
    token = _MASK.set(mask)
    try:
        yield
    finally:
        _MASK.reset(token)


def masked_first(text: str) -> str:
    """`text` masked with the mask the app holds around this call, or as it is outside one. A connector calls it on
    text a source gave before it cuts it: a cut first can leave part of a secret, which no mask finds."""
    mask = _MASK.get()
    return mask(text) if mask is not None else text


def one_line(text: object, limit: int = 200) -> str:
    """Text a source gave, such as an error, masked as `masked_first` masks it, as one line of at most about `limit`
    characters. A longer one is cut at a word's edge and the cut word dropped."""
    text = " ".join(masked_first(str(text)).split())
    if len(text) <= limit:
        return text
    head = text[:limit + 1]
    kept = head.rsplit(" ", 1)[0] if " " in head else ""
    return f"{kept} ..." if kept else "..."


def clip_line(text: object, limit: int) -> str:
    """A source's refusal, masked as `masked_first` masks it, as one line of at most `limit` bytes and `...`, cut
    on bytes, a split character dropped and the cut word kept. With no mask held,
    the word the cut falls in is dropped too, as `one_line` drops it, so no head of a value is kept that a later mask
    could not find."""
    line = " ".join(masked_first(str(text)).split())
    data = line.encode()
    if len(data) <= limit:
        return line
    head = data[:limit].decode("utf-8", "ignore")
    if _MASK.get() is not None:
        return head + "..."
    if not line[len(head):len(head) + 1].isspace():
        head = head.rsplit(" ", 1)[0] if " " in head else ""
    return f"{head.rstrip()} ..." if head.strip() else "..."


class RecordNotFound(CerebroError, LookupError):
    """A source has no record with that id."""


class CursorError(CerebroError, ValueError):
    """A cursor this connector did not give, one that does not read, or one the source refused or has forgotten:
    the one error that says a cursor is no good. The runner drops it, and lists a source that `lists_changes` in
    full in the same pass."""


class QuestionError(CerebroError, ValueError):
    """A question a source does not answer, or one asked with params it does not take. Its line names the source,
    and the questions it answers or the params the question takes."""


@dataclass(frozen=True)
class Ours:
    """Who is on our side: your own addresses, and the workspace's own domains, each covering the host itself and any
    host whose registrable domain it is. The app
    reads them from `profile.md` (`identity.email`) and `workspace.md` (`identity.domains`),
    and hands the same value to every connector that tells our side from theirs."""

    addresses: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()

    def holds(self, address: str) -> bool:
        """Whether an address is one of yours, with or without a `+tag`, or at one of the workspace's own domains: the
        host itself, or its registrable domain. So a host under a
        listed domain that is itself a subdomain, such as `mail.eu.ourco.example` for `eu.ourco.example`, is not."""
        address = address.strip().lower()
        mine = {_unplus(a.strip().lower()) for a in self.addresses}
        if address in mine or _unplus(address) in mine:
            return True
        _, at, host = address.partition("@")  # cut at the first @
        host = host.strip(".")
        domains = {d for d in (d.strip().strip(".").lower().lstrip("@") for d in self.domains) if d}
        return at and bool(host) and (host in domains or registrable(host) in domains)


def own_people(parties: Iterable[tuple[str, str]]) -> list[str]:
    """Who of `parties`, each a name and an address the source counts on the workspace's own side, is on a record,
    as its `ours` field lists them: each person once, by the address, else by the name; each by name, else by
    address, but two people of one name at two addresses each as `Name <address>`, so they stay apart."""
    people: dict[str, tuple[str, str]] = {}
    for name, address in parties:
        name, address = " ".join((name or "").split()), (address or "").strip()
        if key := _unplus(address.lower()) if address else name.casefold():
            people.setdefault(key, (name, address))
    names = [name for name, _ in people.values() if name]
    return [f"{name} <{address}>" if name and address and names.count(name) > 1 else name or address
            for name, address in people.values()]


def _unplus(address: str) -> str:
    """An address without its `+tag`, as mail services deliver `me+notes@` to `me@`."""
    local, at, host = address.rpartition("@")
    return f"{local.partition('+')[0]}@{host}" if at and "+" in local else address


# Public mail services, whose domain names no organization.
PUBLIC_MAIL = frozenset((
    "163.com", "aol.com", "fastmail.com", "gmail.com", "gmx.com", "gmx.net", "googlemail.com", "hotmail.co.uk",
    "hotmail.com", "icloud.com", "live.com", "mail.com", "me.com", "msn.com", "outlook.com", "pm.me", "proton.me",
    "protonmail.com", "qq.com", "ymail.com", "zoho.com", "yahoo.ca", "yahoo.co.in", "yahoo.co.jp", "yahoo.co.uk",
    "yahoo.com", "yahoo.com.ar", "yahoo.com.au", "yahoo.com.br", "yahoo.com.hk", "yahoo.com.mx", "yahoo.com.sg",
    "yahoo.com.tw", "yahoo.de", "yahoo.es", "yahoo.fr", "yahoo.ie", "yahoo.in", "yahoo.it"))
_SECOND_LEVEL = frozenset(("ac", "co", "com", "edu", "gov", "ltd", "net", "org", "plc"))


def registrable(domain: str) -> str:
    """The part of a domain an organization registers: the label before the suffix, with a
    `co` or `com` between them kept, so `mail.alder.example` gives `alder.example`. A guess from common shapes."""
    labels = domain.strip().strip(".").lower().split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def is_public_mail(email: str) -> bool:
    """Whether an address is at a public mail service, or a subdomain of one."""
    domain = email.strip().partition("@")[2].lower()
    return bool(domain) and (domain in PUBLIC_MAIL or registrable(domain) in PUBLIC_MAIL)


EVENT_INSTANCE = re.compile(r"^(.+)_(\d{8}(?:t\d{6}z)?)$")  # a provider's id of one date of a series, `<uid>_<start>`


def event_key(id: str, start: datetime | None) -> str:
    """A calendar event's id as every source that names the event writes it, so a recording and its event share one
    key: in lower case, without `@google.com`, and one date of an event as `<uid>#<when>`, `when` its start in UTC as
    `20261020t190000z` or a date as `20261020`, whether a source writes it so or as Google's `<uid>_<when>`. An id with
    no date of its own, which may name a whole series, takes the date in UTC of `start`, the record's own start, so
    each occurrence has its own key. "" for no id, or an id with no date and no start."""
    text = str(id or "").strip().lower()
    uid, hash, when = text.partition("#")
    uid = uid.removesuffix("@google.com")
    if not hash and (instance := EVENT_INSTANCE.match(uid)):
        uid, hash, when = instance.group(1), "#", instance.group(2)
    if not hash and start is not None:
        hash, when = "#", start.astimezone(UTC).strftime("%Y%m%d")
    return f"{uid}#{when}" if uid and hash else ""


def same_event(one: Collection[str], other: Collection[str]) -> bool:
    """Whether two sets of event keys, as `event_key` writes them, may name one occurrence: by the keys at a time,
    when both hold one, else by the event and its day, a day apart at most, since a key that takes its day from a
    record's own start in UTC, as a recording that names no date, can fall on the day before or after its event's."""
    def timed(keys: Collection[str]) -> set[str]:
        return {k for k in keys if "t" in k.partition("#")[2]}

    if timed(one) and timed(other):
        return bool(timed(one) & timed(other))

    def days(keys: Collection[str]) -> set[tuple[str, date]]:
        out = set()
        for key in keys:
            uid, _, when = key.partition("#")
            try:
                out.add((uid, datetime.strptime(when[:8], "%Y%m%d").date()))
            except ValueError:
                continue
        return out

    return any(a == b and abs((d - e).days) <= 1 for a, d in days(one) for b, e in days(other))


def outside_domain(email: str, ours: Ours) -> str:
    """The domain of an organization outside that an address names, as the part the organization registers, or "":
    none for one of yours, a public mail service, a bare suffix or a host's address, or one at the workspace's own domains."""
    email = email.strip().lower()
    _, at, domain = email.rpartition("@")
    domain = domain.strip(".")
    labels = domain.split(".")
    if not at or ours.holds(email) or len(labels) < 2 or "" in labels or any(c in domain for c in "[]:") or (
            len(labels) == 2 and labels[0] in _SECOND_LEVEL) or is_public_mail(email):
        return ""
    try:
        ipaddress.ip_address(domain)
        return ""
    except ValueError:
        pass
    return registrable(domain)


def address_keys(addresses: Iterable[str], ours: Ours) -> dict[str, list[str]]:
    """The `email` and `domain` keys a record's addresses give, each once and sorted, in lower case: never an
    address `ours` holds, in any case or with a `+tag`, nor one at the workspace's own domains as it matches them; and a
    domain only for an organization outside, as `outside_domain` writes it, so never a public mail service's."""
    emails, domains = set(), set()
    for raw in addresses:
        address = (raw or "").strip().lower()
        if "@" not in address or ours.holds(address):
            continue
        emails.add(address)
        if domain := outside_domain(address, ours):
            domains.add(domain)
    return {k: sorted(v) for k, v in (("email", emails), ("domain", domains)) if v}


NOBODY = Ours()  # no address or domain is ours: what a connector assumes when it is told none


@dataclass(frozen=True)
class Choice:
    """One row a person may tick for a source file: `value` is what the file's list takes for it, `label` and
    `detail` what the row shows, `group` the heading it sits under, or "" for none, and `checked` whether it starts
    ticked while the file lists nothing yet, as a model that grouped the rows may suggest."""

    value: str
    label: str
    detail: str = ""
    group: str = ""
    checked: bool = False


@dataclass(frozen=True)
class Offer:
    """What a connector answers when asked what there is to choose for its source file: the list of the file the
    ticks fill (`key`), the word for one row and for many, the rows, what the file lists now, lines to show under the
    rows, such as a cap on what was read, and how many the login can see when rows group them, else 0."""

    key: str
    one: str
    many: str
    choices: tuple[Choice, ...]
    current: tuple[str, ...] = ()
    note: tuple[str, ...] = ()
    listed: int = 0
    head: str = ""  # the list's title in the connector's own words, as `<what> to read`; "" says `<Many> to read`
    verb: str = "Write"  # what Enter does with the ticked rows, as the legend says it


@dataclass(frozen=True)
class Ask:
    """One question to the workspace's model, held to its cap. `ask(prompt, request)` puts the bundled prompt named
    `prompt`, or the workspace's copy of it, with `request` read as data, and gives the reply's text. `room(prompt)`
    is how many bytes a request may hold beside that prompt in one call."""

    call: Callable[[str, str], str]
    room: Callable[[str], int]

    def __call__(self, prompt: str, request: str) -> str:
        return self.call(prompt, request)


def _item(value: str, flow: bool) -> str:
    """value as one item of a YAML list, in a flow list `[a, b]` or as a block list's `- a`, quoted only when a plain
    scalar would read as something else."""
    try:
        plain = "\n" not in value and yaml.safe_load(f"[{value}]" if flow else f"- {value}") == [value]
    except yaml.YAMLError:
        plain = False
    return value if plain else json.dumps(value)


class ListKept(ValueError):
    """A source file whose list cannot be written in place with every other line and comment as it was: the line
    says why, and nothing is written."""


def _comment_of(line: str, after: int) -> str:
    """The comment on `line` from column `after` on, such as `  # picked by hand`, or "" when it has none."""
    rest = line.rstrip("\r\n")[after:]
    for i, c in enumerate(rest):
        if c == "#" and (i == 0 or rest[i - 1] in " \t"):
            return "  " + rest[i:]
    return ""


def with_list(text: str, key: str, values: list[str]) -> str:
    """A source file's text with its top-level list `key` set to `values`, and every other line and every comment as
    it was.

    A flow list, `key: [a, b]`, is replaced between its brackets. A block list keeps each item it still holds on its
    own line, in place, with the comments among them; an item no longer held leaves its comment behind as a comment
    line, and an added one goes under the last item, indented as the first. An empty key, `key:`, takes the values
    on one line, its comment kept. A file with no such key gets `key: [a, b]` at the end. The file's line endings
    are kept. A form it cannot keep, such as a key given twice, a key not at the start of its line, an item that is
    not one plain value on one line, or a list another key leans on, raises `ListKept` with the reason, and nothing
    is written."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    try:
        doc = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        raise ListKept("it does not read as YAML") from None
    if doc is not None and not isinstance(doc, yaml.MappingNode):
        raise ListKept("it is not a mapping of keys")
    found = [(k, v) for k, v in (doc.value if doc is not None else [])
             if isinstance(k, yaml.ScalarNode) and k.value == key]
    flow = "[" + ", ".join(_item(v, True) for v in values) + "]"
    if len(found) > 1:
        raise ListKept(f"the key {key} is given twice")
    if not found:
        written = text + ("" if not text or text.endswith("\n") else newline) + f"{key}: {flow}{newline}"
    else:
        name, value = found[0]
        row = name.start_mark.line
        if name.start_mark.column != 0:
            raise ListKept(f"the {key} key is not at the start of its line")
        if isinstance(value, yaml.SequenceNode) and any(not isinstance(i, yaml.ScalarNode) for i in value.value):
            raise ListKept(f"{key} holds more than plain values")
        if isinstance(value, yaml.SequenceNode) and value.flow_style:
            first, last = value.start_mark.line, value.end_mark.line
            if any(_comment_of(lines[i], 0) for i in range(first + 1, last + 1)) or (
                    first != last and _comment_of(lines[first], value.start_mark.column)):
                raise ListKept(f"the {key} list runs over lines with comments among its values")
            written = text[:value.start_mark.index] + flow + text[value.end_mark.index:]
        elif isinstance(value, yaml.SequenceNode):
            written = _block(lines, key, row, value.value, values, newline)
        elif isinstance(value, yaml.ScalarNode) and value.tag == "tag:yaml.org,2002:null":
            end = lines[row][-2:] if lines[row].endswith("\r\n") else lines[row][-1:] if lines[row].endswith("\n") \
                else newline
            if value.end_mark.line != row and value.start_mark.line != value.end_mark.line:
                raise ListKept(f"the {key} key's value runs over lines")
            lines[row] = f"{key}: {flow}{_comment_of(lines[row], name.end_mark.column + 1)}{end}"
            written = "".join(lines)
        else:
            raise ListKept(f"{key} is not a list")
    try:
        after = yaml.safe_load(written)
    except yaml.YAMLError:
        raise ListKept(f"another key leans on the {key} list") from None
    got = after.get(key) if isinstance(after, dict) else None
    if not isinstance(got, list) or [str(v).strip() for v in got] != [v.strip() for v in values]:
        raise ListKept(f"the {key} list would not read back as written")
    return written


def _block(lines: list[str], key: str, row: int, items: list[Any], values: list[str], newline: str) -> str:
    """A block list's lines with only `values` held: each kept item where it stands, each other item's line left as
    its comment alone, and the new values under the last item."""
    want = {v.strip().casefold() for v in values}
    kept: set[str] = set()
    out = list(lines)
    first = items[0]
    dash = lines[first.start_mark.line][:first.start_mark.column]
    if not re.fullmatch(r"[ \t]*-[ \t]+", dash):
        raise ListKept(f"the {key} list is not one item to a line")
    for item in items:
        line = item.start_mark.line
        if item.end_mark.line != line or not isinstance(item.value, str):
            raise ListKept(f"an item of the {key} list runs over lines")
        word = item.value.strip().casefold()
        if word in want and word not in kept:
            kept.add(word)
            continue
        comment = _comment_of(lines[line], item.end_mark.column)
        indent = dash[:len(dash) - len(dash.lstrip())]
        out[line] = f"{indent}{comment.lstrip()}{newline}" if comment else ""
    added = [f"{dash}{_item(v, False)}{newline}" for v in values if v.strip().casefold() not in kept]
    last = max(item.start_mark.line for item in items)
    if added and out[last] and not lines[last].endswith("\n"):  # the file ends on the last item, with no line end
        out[last] += newline
    out[last + 1:last + 1] = added
    if not kept and not added:  # the list holds none now: the key says so on its line, its comment kept
        end = newline if lines[row].endswith("\n") else ""
        out[row] = f"{key}: []{_comment_of(lines[row], lines[row].index(':') + 1)}{end}"
    return "".join(out)


def split_login(value: str, what: str, fix: str) -> tuple[str, str]:
    """A login kept as `<first>:<second>`, the first colon ending the first part."""
    from .secrets import SecretsError  # here: the secrets module reads source files, which read this one

    first, colon, second = value.partition(":")
    if not colon or not first.strip() or not second.strip():
        raise SecretsError(f"the stored {what} does not read as two parts joined by a colon: {fix}")
    return first.strip(), second.strip()


class Connector:
    """The seam every source implements. `name` is the source's name, as its file under `sources/` gives it."""

    name: str = ""
    # Why a source lists nothing by design, such as one an agent reads by a query of its own, or empty for one that
    # lists. Cerebro says it once when it loads, and a pass does not list such a source.
    lists_nothing: ClassVar[str] = ""
    # True for a source whose listing from a cursor can ask the source for only what changed since it. The runner
    # then hands each cursor through `cursor_for` before it lists, lists the source in full every `full_every:` of its
    # file, 24 hours by default, and at once when the source refuses its cursor.
    lists_changes: ClassVar[bool] = False
    # The keys a curator may give by hand through admit, and any key that weighs when two articles fold or merge, each
    # with that weight; at 0, one value may bind to several articles. A key that only comes with a record needs none.
    IDENTITY_KEYS: ClassVar[Mapping[str, int]] = {}
    # A regular expression that matches the label at the start of a line of a record's body that says who speaks, with
    # the name in a group named `speaker`; empty for a source whose lines mark no speaker.
    speaker_pattern: ClassVar[str] = ""
    # The address of the provider's own MCP server, for a kind that reads one; a source's url may only move it on its host.
    mcp_server: ClassVar[str] = ""
    # The one read-only tool call that proves a login on that server, its arguments, and the tools a session may call.
    mcp_probe: ClassVar[tuple[str, Mapping[str, Any], tuple[str, ...]] | tuple[()]] = ()

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> Connector:
        """The connector of `source`, a file of the workspace at `root`, built with what `app`, the workspace's
        `Cerebro`, gives every source: its HTTP client, logins, clock, zone, own side and masks. Each kind the
        registry lists builds its own."""
        raise NotImplementedError

    @classmethod
    def needs(cls, source: SourceFile, item: str) -> Need | None:
        """What login a source of this kind needs, kept under `item`, for connect, or None when the engine's own rule
        says: a provider's MCP server, an MCP source, a database, or nothing to connect."""
        return None

    @classmethod
    def login_name(cls, source: SourceFile) -> str | None:
        """The name a source of this kind keeps its login under when its file names none, or None for a kind that
        keeps none."""
        return None

    def cursor_for(self, cursor: str | None, *, full: bool) -> str | None:
        """The cursor `cursor`, set so the next listing from it reads the source in full, when `full`, or asks it
        only for what changed since, when not. A full listing from a cursor still lists only what changed against
        what the cursor saw, and calls removed what the source says is gone, so it is what catches an edit or a
        delete a listing of changes cannot see. With no cursor, the one a first listing in full starts from, or None
        when listing from none is that already. Raises `CursorError` for a cursor this connector did not give. A
        source that does not list changes gives the cursor back as it is, as this one does."""
        return cursor

    def list(self, since: str | None = None) -> Listing:
        """The records new or changed since the cursor `since`, or every record when it is None, without bodies."""
        raise NotImplementedError

    def take_ours(self, ours: Ours) -> None:
        """Tell your side from theirs by `ours` from now on, as the workspace's files give it after a change, so
        Cerebro, running on, needs no restart. A connector keeps who is on your side as `ours`, or as `_ours` behind a
        property of that name, and one that keeps neither tells no side."""
        for kept in ("ours", "_ours"):
            if kept in vars(self):
                setattr(self, kept, ours)

    def get(self, id: str) -> Record:
        """One record whole, body included. Raises `RecordNotFound` for an id the source does not hold."""
        raise NotImplementedError

    # The questions `ask` answers, each by its name with one line that says what it gives and the params it takes.
    # A source that answers none lists none.
    questions: ClassVar[Mapping[str, str]] = {}

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """Optional, and read only like `list` and `get`: a curator's question to the source, one of `questions` by
        its name, with its params, such as the records the login owns. It gives at most `limit` rows as a `Listing`
        with no cursor: each row a `Record` with no body, the source's own id, its name under `fields`, the keys
        that would bind the source's records to an article, the collection it would land in as `into`, and what
        else the source says of it, as fields. `more` says rows were left past the limit. Nothing it gives is kept
        or becomes a fact. Raises `QuestionError` for a question the source does not answer, or params the
        question does not take; a source that answers no question raises it for every one, as this one does."""
        raise QuestionError(f"source {self.name} answers no questions: it lists its records for a pass, and a "
                            "curator admits from its evidence instead")

    # The list of the source's file a person picks for, with the word for one entry and for many, such as ("repos",
    # "repository", "repositories"), for a connector that answers `offer`; empty for one whose file a person writes.
    picks: ClassVar[tuple[str, str, str]] = ()

    def offer(self, ask: Ask | None = None) -> Offer | None:
        """Optional, and read only: what there is to choose for this source's file, read with its login, such as the
        rows a list of the file may name, or None when the connector does not answer, and a person writes the file
        by hand. `ask` puts one question to the workspace's model, held to its cap, for a connector that groups its
        rows with one; without it the rows come ungrouped. Nothing it gives is kept or becomes a fact."""
        return None

    def written(self, text: str, offer: Offer, values: list[str]) -> str:
        """The source file's `text` with the ticked `values` as the list `offer` fills, every other line as it was."""
        return with_list(text, offer.key, values)
