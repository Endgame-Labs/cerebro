"""What the connectors share to answer a curator's questions: the check of a question's params, the test of a
machine's address, and the rows a meeting, call or mail source answers with.

`asked` reads a question's params, each as its type and inside its bounds, before the source is asked anything, and
refuses a question, a param or a limit in one line that begins with the source's name. Every connector that answers
questions checks them with it, so each says the same thing of the same mistake.

A meeting or call source answers with one row for each outside domain, and never a title, a description, a note or a
word said.

A source reads its meetings or calls through its own read path and gives each as a `Conversation`: when it was,
the domains of the participants from outside as `address_keys` writes them, how someone on your side took part,
which sides took part, and who organized it. `rows` makes one row a domain:
how many records, how many someone on your side took part in and how, whether both sides took part, the
first and last day, whether a person or a machine organized them, and the keys and name another source keeps for
the one outside party a record names, when it has no other outside domain. A row is a `Record` with no body, the domain as its id and its key.
Calendar, Granola and Gong share `rows`, and Gmail and calendar `machine_address`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..connector import Listing, QuestionError, Record, one_line, registrable

__all__ = ["LATEST", "MOST_ENGAGED", "TOOK_PART_WORDS", "Conversation", "Days", "Text", "Word", "asked",
           "machine_address", "rows"]

# The ways someone on your side takes part, in the order a row lists them.
TOOK_PART_WORDS = ("replied", "accepted", "organized", "attended", "spoke", "posted", "owns")
LATEST, MOST_ENGAGED = "latest", "most_engaged"  # the latest record first; the most you took part in first
_EPOCH = datetime(1, 1, 1, tzinfo=UTC)

# The local parts a mail system or a calendar sends from, and the labels in front of an organization's domain one sends
# from.
_MACHINE_NAMES = frozenset((
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply", "mailer-daemon", "postmaster",
    "bounce", "bounces", "notification", "notifications", "notify", "alerts", "calendar-notification", "calendar",
    "unknownorganizer", "automated", "invoice", "invoices", "receipts", "billing-noreply"))
_MACHINE_HOSTS = frozenset(("bounce", "bounces", "calendar", "notifications", "noreply"))


def machine_address(email: str) -> bool:
    """Whether an address reads as a machine's: a no-reply, notification, bounce or calendar sender, by its local
    part or by a label of its host in front of the organization's domain."""
    local, at, host = email.strip().lower().partition("@")
    if not at:
        return False
    local = local.partition("+")[0]
    if local in _MACHINE_NAMES or local.startswith(("noreply", "no-reply", "donotreply", "bounce")):
        return True
    front = host.removesuffix(registrable(host)).strip(".")
    return any(label in _MACHINE_HOSTS for label in front.split("."))


@dataclass
class Conversation:
    """One meeting or call as a row reads it, never what was said: its time, its day as the source's records
    state it, the outside domains of its participants, how your side took part in the words of
    `TOOK_PART_WORDS`, which sides took part as `(ours, theirs)` or None when the source cannot tell, whether a
    person or a machine organized it, and as `party_keys` and `party_name` the keys and name another source keeps for
    the one outside party it names, when it names exactly one."""

    at: datetime | None = None
    day: str = ""
    outside: list[str] = field(default_factory=list)
    how: list[str] = field(default_factory=list)
    sides: tuple[bool, bool] | None = None
    person: bool = False
    machine: bool = False
    party_keys: dict[str, list[str]] = field(default_factory=dict)
    party_name: str = ""


def _row(source: str, domain: str, noun: str, group: list[Conversation]) -> Record:
    how = {w for c in group for w in c.how}
    timed = sorted((c for c in group if c.at is not None), key=lambda c: c.at or _EPOCH)
    fields: dict[str, Any] = {}
    keys = {"domain": [domain]}
    named = next((c for c in group if len(c.outside) == 1 and c.party_keys), None)
    if named is not None:
        keys |= named.party_keys
        if named.party_name:
            fields["name"] = named.party_name
    fields |= {"records": len(group), "noun": noun, "took_part": sum(1 for c in group if c.how)}
    if how:
        fields["how"] = [w for w in TOOK_PART_WORDS if w in how]
    told = [c.sides for c in group if c.sides is not None]
    if told:
        fields["directions"] = "both" if any(ours and theirs for ours, theirs in told) else "one"
    if timed:
        fields["first"], fields["last"] = timed[0].day, timed[-1].day
    person, machine = any(c.person for c in group), any(c.machine for c in group)
    if person or machine:
        fields["sender"] = "person and machine" if person and machine else "machine" if machine else "person"
    return Record(id=domain, source=source, revision="", keys=keys, fields=fields)


def rows(source: str, noun: str, conversations: Iterable[Conversation], order: str, limit: int) -> Listing:
    """One row for each outside domain of the conversations, at most `limit` of them, as a `Listing` with no
    cursor whose `more` says rows were left. `LATEST` ranks the domain with the latest record first; `MOST_ENGAGED`
    the most records your side took part in first, then the most records, then the latest. A tie goes to the
    domain that sorts first."""
    by: dict[str, list[Conversation]] = {}
    for conversation in conversations:
        for domain in dict.fromkeys(conversation.outside):
            by.setdefault(domain, []).append(conversation)
    last = {d: max((c.at for c in group if c.at is not None), default=_EPOCH) for d, group in by.items()}
    found = [_row(source, d, noun, group) for d, group in by.items()]
    found.sort(key=lambda r: r.id)
    found.sort(key=lambda r: last[r.id], reverse=True)
    if order == MOST_ENGAGED:
        found.sort(key=lambda r: (r.fields["took_part"], r.fields["records"]), reverse=True)
    return Listing(found[:limit], None, [], len(found) > limit, [])


@dataclass(frozen=True)
class Days:
    """A whole number of days, `default` when left out, from `low` to `high`."""

    default: int
    low: int
    high: int


@dataclass(frozen=True)
class Word:
    """One of `words`, the first when left out."""

    words: tuple[str, ...]


@dataclass(frozen=True)
class Text:
    """A line of text, "" when left out, such as `example`. A `required` one refuses to be left out, saying what to
    give in `what`."""

    example: str
    required: bool = False
    what: str = "it"


def asked(say: str, questions: Mapping[str, str], takes: Mapping[str, Mapping[str, Days | Word | Text]],
          question: str, params: Mapping[str, Any] | None, limit: int) -> dict[str, Any]:
    """The params of a question this source answers, each read as its type, with its default for one left out, or
    `QuestionError` in one line that begins with `say`, which names the source."""
    if question not in questions:
        raise QuestionError(f"{say}: answers no question {one_line(question, 60)!r}: ask one of "
                            f"{', '.join(questions)}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise QuestionError(f"{say}: limit {one_line(repr(limit), 20)} is not a count of rows: give 1 or more")
    given, read = dict(params or {}), {}
    takes_here = takes[question]
    for name in given:
        if name not in takes_here:
            raise QuestionError(f"{say}: {question} takes no param {one_line(name, 40)!r}: give "
                                f"{', '.join(takes_here) or 'none'}")
    for name, kind in takes_here.items():
        value = given.get(name)
        if isinstance(kind, Days):
            if value is None or value == "":
                read[name] = kind.default
                continue
            text = str(value).strip()
            if isinstance(value, bool) or not (isinstance(value, int) or text.isdigit()):
                raise QuestionError(f"{say}: {question} {name} {one_line(value, 40)!r} is not a whole number of "
                                    f"days: give one from {kind.low} to {kind.high}, such as {kind.default}")
            days = int(value)
            if not kind.low <= days <= kind.high:
                raise QuestionError(f"{say}: {question} {name} {days} is outside {kind.low} to {kind.high}: give a "
                                    "number of days inside it")
            read[name] = days
        elif isinstance(kind, Word):
            word = str(value if value is not None else "").strip().lower() or kind.words[0]
            if word not in kind.words:
                raise QuestionError(f"{say}: {question} {name} {one_line(value, 40)!r} is not one it reads: say "
                                    f"{' or '.join(kind.words)}")
            read[name] = word
        else:
            if value is not None and (isinstance(value, bool) or not isinstance(value, str | int | float)):
                raise QuestionError(f"{say}: {question} {name} {one_line(value, 40)!r} is not text: give it as "
                                    f"text, such as {kind.example!r}")
            read[name] = str(value if value is not None else "").strip()
            if kind.required and not read[name]:
                raise QuestionError(f"{say}: {question} needs {name}: give {kind.what}, as "
                                    f"{{\"{name}\": \"{kind.example}\"}}")
    return read
