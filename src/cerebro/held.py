"""The held list: what a curator's sources hold for its collection, given to the curator as evidence.

A record a source lists that is out of scope is held: the runner keeps its listing without a body under
`held:<source>` in the store's state, and tests it again each pass. A record a pass reads because another
collection's article reaches it, as its parent does, is kept there too while no article of its own collection binds
it. The `held` evidence line, `- held: {}` on a curator's file, gives the curator the records held for its
collection: those whose `into` is that collection and that no article of it in the working set binds, and those of no
collection whose keys bind only articles of it
that are out of the working set. Each is data, never the record's body: its
source, its id, its name as its keys or fields give it, its keys, `at`, the record's own time as its listing gives
it, `listed_at`, when a pass first held it, as the runner keeps it beside the listing, and last the fields its source
gives it that hold a value. Its source, id and name come first, so a line cut at `MAX_LINE` keeps what names the
record. The list is newest first
by the record's own time, and at most `LIST_MAX` long, or the line's `limit`, with a count of the rest.

A record leaves the list the moment it is bound to an article of its collection in the working set, as the curator's
admit does, so a second run in the same pass lists it no more, and when its source removes it, which the runner's next pass drops
from its held state. Each record counts in the digest by its source, id and revision, so a newly held record, or a
new revision of one, makes the curator due.

A record on the workspace's own side, one whose keys hold one of your addresses or an address or domain at the
workspace's own domains, as `profile.md` and `workspace.md` give them, or that marks its own name as yours, is listed
after the others, its name under `of_this_workspaces_own_side` with the names it marks as yours. It is labelled, never
left off: the curator decides what it gets.

A held record a curator left out with `leave_out` shows the reason it gave, under `left_out`, the latest one.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .agent import Agent, EvidenceLine
from .connector import NOBODY, Ours, Record, outside_domain, registrable
from .evidence import RECENT_DAYS, Evidence, EvidenceError, Item
from .identity import OWN_WEIGHED, bound_to, reached, working, would_bind
from .records import Proposal, proposals
from .store import Store, own_key, record_from, update_state

__all__ = [
    "FIRST_RUN_KEY",
    "FIRST_RUN_PASSED",
    "LIST_MAX",
    "OWN_SIDE",
    "OWN_WEIGHED",
    "Held",
    "HeldList",
    "StandIn",
    "before_cut",
    "check_line",
    "entry",
    "first_run_open",
    "first_run_since",
    "held_for",
    "keep_own",
    "one_of_ours",
    "own_held",
    "own_names",
    "own_record",
    "own_side",
    "pass_first_run",
    "passed_first_run",
    "pinned",
    "reweigh_own",
    "same_name",
    "spoken",
    "stand_in",
    "stand_ins",
    "waits_for_own",
    "weighed",
]

OWN_SIDE = "of_this_workspaces_own_side"  # the held line's mark for the names a record lists as the workspace's own
FIRST_RUN_KEY = own_key("first-run")  # the workspace's first run, as `first_run` keeps it: its status and its pile
# When a run went past the workspace's first run: kept apart from the session row,
# so a workspace that loses that row is still past it.
FIRST_RUN_PASSED = own_key("first-run-passed")
LIST_MAX = 500  # held records one run is shown; the rest are counted
NAME_FIELDS = ("name", "title", "subject")  # where a listing names its record, when its keys do not
DOMAIN = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}")  # a key's value that is a domain, such as alder.example
STOPS = frozenset(" \t\n\r<>()[]{},;\"'")  # what ends an address written with a name
BRACKETS = re.compile(r"[<>()\[\]{}]")


@dataclass(frozen=True)
class Held:
    """One held record as a curator reads it: never its body. `at` is the record's own time, as its listing gives
    it, `listed_at` when a pass first held it, and `revision` its source's revision, so a new revision reads as a
    change."""

    source: str
    id: str
    name: str
    keys: dict[str, list[str]] = field(default_factory=dict)
    at: str = ""
    revision: str = ""
    listed_at: str = ""
    links_to: list[dict[str, str]] = field(default_factory=list)  # the articles it sits under, or would on a first run
    proposed: dict[str, str] = field(default_factory=dict)  # on a first run: the proposal of the record itself
    outside: list[str] = field(default_factory=list)  # the domains of the parties it names outside your side
    ours: list[str] = field(default_factory=list)  # the names of the parties on it who are yours, as its source says
    carried: list[str] = field(default_factory=list)  # the keys it only carries, which an admit of it does not take
    own: bool = False  # whether the record itself is on the workspace's own side, as `own_record` says
    fields: dict[str, Any] = field(default_factory=dict)  # the fields its source gives it, but `ours`, held above

    def naming(self) -> dict[str, list[str]]:
        """The keys an article admitted from this record takes: those that name it, never one it only carries."""
        return {key: values for key, values in self.keys.items() if key not in self.carried}


@dataclass
class HeldList:
    """The held records of one collection, newest first and cut to the bound, and `total`, how many are held."""

    items: list[Held]
    total: int


@dataclass(frozen=True)
class StandIn:
    """A pending admit of a workspace's first run, which stands in for the article it would make until a person
    decides it, read beside the working set: the proposal's id, the collection
    and the article id it would make, its name and keys, the held record it came from as (source, id) or None, the
    curator that filed it."""

    id: str
    collection: str
    article: str
    name: str
    keys: dict[str, list[str]]
    record: tuple[str, str] | None
    by: str


def pass_first_run(store: Store, at: str) -> None:
    """Record, once, that a run went past the workspace's first run at `at`: a
    curator still in its own first run after it is late, and only its own admits wait for a person."""
    update_state(store, FIRST_RUN_PASSED, lambda kept: kept if kept is not None else {"passed_at": at})


def passed_first_run(store: Store) -> bool:
    """Whether a run went past the workspace's first run, as `pass_first_run` records it."""
    return store.state(FIRST_RUN_PASSED) is not None


def first_run_open(store: Store) -> bool:
    """Whether the workspace's first run has begun and is not decided yet."""
    session = store.state(FIRST_RUN_KEY)
    return isinstance(session, dict) and session.get("status") != "done"


def first_run_since(store: Store) -> str:
    """The first day the workspace's first run and its build read: `RECENT_DAYS` days before its session began, or ""
    with no session."""
    session = store.state(FIRST_RUN_KEY)
    began = session.get("at") if isinstance(session, dict) else None
    return (datetime.fromisoformat(began) - timedelta(days=RECENT_DAYS)).date().isoformat() if began else ""


def before_cut(at: str, since: str) -> bool:
    """Whether a record dated `at` is older than `since`, the first day a first run reads. An undated record never
    is, and with no `since` none is."""
    return bool(since and at and at[:10] < since)


def pinned(store: Store) -> set[str] | None:
    """The ids of the proposals that waited when the first run's round under way began, which every curator of that
    round reads, so all of them read the same queue; None when no round is under way, and everything waiting is read."""
    session = store.state(FIRST_RUN_KEY)
    if not isinstance(session, dict) or session.get("status") != "discovering":
        return None
    pin = session.get("pinned")
    return {str(id) for id in pin} if isinstance(pin, list) else None


def pinned_waiting(store: Store) -> list[Proposal]:
    """The proposals waiting, as the round under way pinned them, if one is."""
    pin = pinned(store)
    return [p for p in proposals(store, "waiting") if pin is None or p.id in pin]


def stand_in(proposal: Proposal) -> StandIn | None:
    """The stand-in a pending admit makes, or None for any other proposal."""
    if proposal.kind != "article.admit" or proposal.status != "waiting":
        return None
    payload = proposal.payload
    collection = str(payload.get("collection") or proposal.target.rpartition("/")[0])
    if not collection:
        return None
    slug = proposal.target.rpartition("/")[2]
    ref = payload.get("record")
    record = (str(ref["source"]), str(ref["id"])) if isinstance(ref, dict) and ref.get("source") and ref.get("id") \
        else None
    keys = {str(k): [str(v) for v in vs] for k, vs in dict(payload.get("keys") or {}).items() if isinstance(vs, list)}
    return StandIn(proposal.id, collection, f"{collection}/{slug}", str(payload.get("name") or slug), keys, record,
                   proposal.by.name)


def stand_ins(store: Store) -> list[StandIn]:
    """The first run's stand-ins, oldest first: every pending admit while the first run is under way, none once it is
    decided or when it never began. Nothing is made until the person approves the list, so these are what curators
    weigh beside the working set. During a round of discovery, only those waiting when the round began: what a curator
    proposes in a round, the others weigh in the next."""
    if not first_run_open(store):
        return []
    return [found for p in pinned_waiting(store) if (found := stand_in(p)) is not None]


def _name(keys: dict[str, list[str]], fields: dict[str, Any]) -> str:
    for value in keys.get("name", []):
        if str(value).strip():
            return str(value).strip()
    for key in NAME_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _for(store: Store, collection: str, into: str, bound: list[str]) -> bool:
    """Whether a held record waits for this collection. One that lands there waits until an article of the
    working set there binds it, whatever another collection's article binds. One that names no collection waits while what it binds lies
    there alone and out of the working set."""
    if into:
        return into == collection and not any(id.startswith(collection + "/") and working(store, id) for id in bound)
    return bool(bound) and all(id.startswith(collection + "/") for id in bound) and \
        not any(working(store, id) for id in bound)


def waits_for_own(store: Store, record: Record, curated: Collection[str]) -> bool:
    """Whether a record a pass reads waits for its own article all the same: it lands in a collection a curator
    keeps, and no article of the working set there binds it, though it is in scope through another, as a part is
    through its parent. The runner keeps it on the held list, so that curator weighs it."""
    return bool(record.into) and record.into in curated and _for(store, record.into, record.into,
                                                                  bound_to(store, record))


def _folded(value: Any) -> str:
    return str(value).strip().lower().lstrip("@")


def _ours_domain(text: str, ours: Ours) -> bool:
    return any(text == d.strip().lower().lstrip("@") or text.endswith("." + d.strip().lower().lstrip("@"))
               for d in ours.domains if d.strip())


def spoken(text: Any) -> tuple[str, str]:
    """A name as written, and the address it is written with, as in `Kim Vale <kim@birch.example>`, `Kim Vale
    (kim@birch.example)` or the address alone, or ""."""
    text = str(text).strip()
    at = text.find("@")
    if at < 0:
        return text, ""
    start, end = at, at + 1  # out from the first @, so a long value is read once
    while start > 0 and text[start - 1] not in STOPS:
        start -= 1
    while end < len(text) and text[end] not in STOPS:
        end += 1
    rest = " ".join(BRACKETS.sub(" ", text[:start] + " " + text[end:]).split()).strip(" ,;:")
    found = text[start:end]
    return rest, found[7:] if found.lower().startswith("mailto:") else found


def _address(text: str) -> str:
    """A folded value, or the address it is written with as `Name <address>`."""
    return spoken(text)[1] or text


def own_value(value: Any, ours: Ours) -> bool:
    """Whether one key value is the workspace's own: one of your addresses or an address at the workspace's own domains,
    alone or written with a name as `Name <address>`, or one of those domains, or a host under one."""
    text = _address(_folded(value))
    if "@" in text:
        return ours.holds(text)
    return "." in text and " " not in text and _ours_domain(text, ours)


def own_side(keys: dict[str, list[Any]], ours: Ours) -> bool:
    """Whether keys are someone's on the workspace's own side: a value that is one of your addresses or an address at
    the workspace's own domains, or a domain of theirs, under any key."""
    return any(own_value(value, ours) for values in keys.values() for value in values)


def _addresses(keys: dict[str, list[Any]]) -> set[str]:
    return {text for values in keys.values() for value in values if "@" in (text := _address(_folded(value)))}


def _outside(keys: dict[str, list[Any]], ours: Ours) -> bool:
    """Whether keys hold an address or a domain of their own that is not on the workspace's side."""
    for values in keys.values():
        for value in values:
            text = _address(_folded(value))
            if "@" in text and not ours.holds(text):
                return True
            if DOMAIN.fullmatch(text) and not _ours_domain(text, ours):
                return True
    return False


def one_of_ours(keys: dict[str, list[Any]], ours: Ours) -> bool:
    """Whether keys are about one person on the workspace's side, as a source's record of one of your users is: one address, ours, and
    nothing outside. A meeting or a message that only includes one of your addresses is not."""
    found = _addresses(keys)
    return len(found) == 1 and own_side(keys, ours) and not _outside(keys, ours)


def _folded_name(name: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(name)).casefold().split())


def _file_name(name: Any) -> str:
    """The file name a name makes: lower case, each run of letters or
    digits of any script kept and joined by one hyphen, at most 80 characters. Text with a slash is a path, so its
    last part counts, and a file name counts without `.md`. Its case is folded in
    full and its letters composed, so `Straße` is `strasse` and a letter written with a separate accent is the
    same letter."""
    text = unicodedata.normalize("NFC", str(name).strip()).rpartition("/")[2]
    text = text[:-3] if text.lower().endswith(".md") else text
    made: list[str] = []
    gap = False
    for char in text.casefold():
        if char.isalpha() or unicodedata.category(char) == "Nd":
            made += ["-", char] if gap and made else [char]
            gap = False
        else:
            gap = True
    return "".join(made)[:80].rstrip("-")


def same_name(name: str, mine: set[str]) -> bool:
    """Whether a name is one of `mine`, folded names: it folds to one, or makes the file name one makes, so a trailing
    mark or a hyphen in place of a space names the same one, in any
    script, and so does a path to their article."""
    made = _file_name(name)
    return _folded_name(name) in mine or bool(made) and made in {_file_name(n) for n in mine}


def _held(store: Store, sources: Iterable[str], only: Collection[tuple[str, str]] | None = None) \
        -> list[tuple[Any, dict[str, Any]]]:
    """Each record `sources` hold, with its row, or those of `only`, by source and id, read from no other row."""
    return [(record_from(row), row) for source in dict.fromkeys(sources) for row in store.state(f"held:{source}") or []
            if only is None or (row.get("source"), row.get("id")) in only]


def own_record(record: Record, ours: Ours) -> bool:
    """Whether a record is about someone on the workspace's own side: its keys hold one of your addresses or one at
    the workspace's own domains, or it marks its own name as yours under `ours`, as a 1:1 note named for one of
    your people does, whose own address its source keeps out of its keys."""
    keys = {str(k): [str(v) for v in vs] for k, vs in record.keys.items()}
    name = _folded_name(_name(keys, record.fields))
    return own_side(keys, ours) or bool(name) and name in own_of(record, ours)


def own_held(store: Store, sources: Iterable[str], ours: Ours) -> set[tuple[str, str]]:
    """The held records on the workspace's own side, as `own_record` tells them, by source and id."""
    return {(record.source, record.id) for record, _ in _held(store, sources) if own_record(record, ours)}


def own_of(record: Record, ours: Ours) -> list[str]:
    """The names, folded, a record gives as people on the workspace's own side: its own name when it is about one
    person on that side, and every name its source marks as the workspace's own under `ours`, such as the people
    of a note's attendees line whose addresses were ours. A meeting or a message that includes one of your
    addresses names nobody by its own name, since that is often the other side's."""
    keys = {str(k): [str(v) for v in vs] for k, vs in record.keys.items()}
    own = [_name(keys, record.fields)] if one_of_ours(keys, ours) else []
    marked = [str(n).partition(" <")[0] for n in record.fields.get("ours") or []]  # two of one name come with addresses
    return list(dict.fromkeys(n for n in map(_folded_name, [*own, *marked]) if n))


def keep_own(store: Store, source: str, records: Iterable[Record], ours: Ours, gone: Iterable[str] = ()) -> None:
    """Keep the names each record a source just listed gives as the workspace's own, under `own:<source>` by the
    record's id, and forget those of the records it removed. A pass calls this as soon as a source is listed,
    before any curator runs and before any record is held or read, so `own_names` holds them from the first
    pass on."""
    fresh = {record.id: own_of(record, ours) for record in records}
    drop = {*gone, *fresh}
    was = store.state(f"own:{source}") or {}
    now = {**{k: v for k, v in was.items() if k not in drop}, **{k: v for k, v in fresh.items() if v}}
    if now != was:
        update_state(store, f"own:{source}", lambda kept: {
            **{k: v for k, v in (kept or {}).items() if k not in drop}, **{k: v for k, v in fresh.items() if v}})


def weighed(ours: Ours) -> dict[str, list[str]]:
    """Who is on the workspace's own side, as `OWN_WEIGHED` keeps it: your addresses and its domains, sorted."""
    return {"addresses": sorted({a.strip().lower() for a in ours.addresses if a.strip()}),
            "domains": sorted({d.strip().lower().lstrip("@") for d in ours.domains if d.strip()})}


def reweigh_own(store: Store, source: str, records: Iterable[Record], ours: Ours) -> None:
    """Read again who each record of a source's listing in full names as the workspace's own, by `ours`: the names
    `keep_own` keeps, and those the record's held listing and its record read already keep, under `held:<source>`
    and `ours:<source>`. So a domain made the workspace's own, or no longer its own, reaches the names of a record
    listed before the change."""
    fresh = {record.id: record for record in records}
    keep_own(store, source, fresh.values(), ours)
    marked = {id: [str(n) for n in record.fields.get("ours") or [] if str(n).strip()] for id, record in fresh.items()}
    if set(store.state(f"ours:{source}") or {}) & set(fresh):
        update_state(store, f"ours:{source}", lambda now: {
            k: marked.get(k, v) for k, v in (now or {}).items() if k not in fresh or marked[k]})
    if any(row.get("id") in fresh for row in store.state(f"held:{source}") or []):
        update_state(store, f"held:{source}", lambda now: [
            asdict(replace(fresh[row["id"]], body=None)) | {"listed_at": row.get("listed_at") or ""}
            if row.get("id") in fresh else row for row in now or []])


def own_names(store: Store, sources: Iterable[str], ours: Ours) -> set[str]:
    """The names, folded, of the people on the workspace's own side, which their labels read, as `own_of` reads
    them from a record: from each record a source listed, which a pass keeps under
    `own:<source>` as soon as it lists and before any curator runs, from each held record, and from each record
    read already, whose marked names the runner keeps under `ours:<source>`. So the set is whole before the
    first curator of any pass, a first run's discovery among them, and before any fact exists."""
    out: set[str] = set()
    for record, _ in _held(store, sources):
        out |= set(own_of(record, ours))
    for source in dict.fromkeys(sources):
        for kept in (store.state(f"own:{source}") or {}, store.state(f"ours:{source}") or {}):
            for names in kept.values():
                out |= {folded for n in names or [] if (folded := _folded_name(n))}
    return out


def linked(record: Record, ours: Ours = NOBODY) -> set[tuple[str, str]]:
    """What a record links to, as (key, value) pairs: each id `record.links` names under each key the record carries,
    as `identity.linked_keys` links a record's facts, and the domain of each address it holds outside your side."""
    pairs = {(key, _folded(link)) for key in record.keys for link in record.links if str(link).strip()}
    for address in _addresses(record.keys):
        if not ours.holds(address):
            pairs.add(("domain", registrable(address.rpartition("@")[2])))
    return pairs


def parties_outside(record: Record, ours: Ours = NOBODY) -> list[str]:
    """The domains of the parties a record names by address, outside your side and no public mail service, sorted:
    what ties a record with parties to an organisation before anything else does, whatever kind of record it is."""
    return sorted({domain for address in _addresses(record.keys) if not ours.holds(address)
                   if (domain := outside_domain(address, ours))})


def is_it(store: Store, standing: StandIn, record: Record) -> bool:
    """Whether a record is the thing a stand-in would make: its own record, when the admit came from one, else a
    record the article's keys or name would bind."""
    if standing.record is not None:
        return standing.record == (record.source, record.id)
    return would_bind(store, standing.keys, standing.name, record)


def links_to(standing: StandIn, pairs: set[tuple[str, str]]) -> bool:
    """Whether a stand-in's keys meet what a record links to, as `linked` gives it: no title is matched."""
    return any((key, _folded(value)) in pairs for key, values in standing.keys.items() for value in values)


def _by_pair(standing: list[StandIn]) -> dict[tuple[str, str], set[int]]:
    """The stand-ins by each (key, value) of their keys, folded as `links_to` folds them, each by its place in
    `standing`: whom a record's links meet, found without asking every stand-in."""
    out: dict[tuple[str, str], set[int]] = {}
    for n, s in enumerate(standing):
        for key, values in s.keys.items():
            for value in values:
                out.setdefault((key, _folded(value)), set()).add(n)
    return out


def _first_run_marks(store: Store, record: Record, standing: list[StandIn], ours: Ours,
                     bound: list[str], by_pair: dict[tuple[str, str], set[int]] | None = None) \
        -> tuple[list[dict[str, str]], dict[str, str]]:
    """On a first run, the articles a held record would sit under, from the working set and from the stand-ins, each
    proposed one with its proposer, and the stand-in of the record itself. `by_pair` is
    `_by_pair(standing)`, made once for a list of many records."""
    pairs = linked(record, ours)
    by_pair = _by_pair(standing) if by_pair is None else by_pair
    met = {n for pair in pairs for n in by_pair.get(pair, ())}  # the stand-ins `links_to` finds the record meets
    found = [{"id": id} for id in dict.fromkeys(id for key, value in sorted(pairs) for id in store.bound(key, value))
             if id not in bound and working(store, id)]
    # A stand-in by name that would bind the record is the record's own when it is for the collection the record
    # lands in, and else what the record names, as a note names its parent on a header line: it sits under that one.
    binds = [s for s in standing if s.record is None and is_it(store, s, record)]
    itself = next((s for s in standing if s.record is not None and is_it(store, s, record)), None) or next(
        (s for s in binds if not record.into or s.collection == record.into), None)
    seen = {item["id"] for item in found}
    for n, s in sorted(enumerate(standing), key=lambda ns: (ns[1].article, ns[1].by)):  # not by who filed first
        if s is not itself and s.article not in seen and (n in met or s in binds):
            found.append({"id": s.article, "proposed_by": f"agents/{s.by}.md"})
            seen.add(s.article)
    return found, ({"id": itself.article, "by": f"agents/{itself.by}.md"} if itself is not None else {})


def held_for(store: Store, collection: str, sources: Iterable[str], *, limit: int | None = LIST_MAX,
             ours: Ours = NOBODY, only: Collection[tuple[str, str]] | None = None) -> HeldList:
    """The records `sources` hold for `collection`, newest first, at most `limit`, or every one with None, read from each source's held
    state. A record a key binds to an article of the working set is in scope, so it is not listed, whatever the state
    still says before the next pass. A record on the workspace's own side, by `ours`, comes after the others. `only`,
    by source and id, weighs those records alone, each as the whole list would give it, so a call that names a few
    records does not weigh every one held."""
    found: list[Held] = []
    sources = list(sources)
    standing = stand_ins(store) if first_run_open(store) else None
    by_pair = _by_pair(standing) if standing is not None else None
    for record, row in _held(store, sources, only):
        bound = bound_to(store, record)
        if not _for(store, collection, record.into, bound):
            continue
        keys = {str(k): [str(v) for v in vs] for k, vs in record.keys.items()}
        name = _name(keys, record.fields)
        # what it sits under in the working set, as its keys reach them, those it only carries too: the parent a
        # curator admits a held record of, after the first run as on it
        links, itself = (_first_run_marks(store, record, standing, ours, bound, by_pair) if standing is not None else
                         ([{"id": id} for id in reached(store, record) if id not in bound and working(store, id)], {}))
        found.append(Held(record.source, record.id, name, keys, record.at, record.revision,
                          str(row.get("listed_at") or ""), links, itself, parties_outside(record, ours),
                          [str(n) for n in record.fields.get("ours") or [] if str(n).strip()],
                          [k for k in record.carried or () if k in keys], own_record(record, ours),
                          {k: v for k, v in record.fields.items() if k != "ours" and v not in (None, "", [], {})}))
    found.sort(key=lambda held: (held.source, held.id))
    found.sort(key=lambda held: held.at, reverse=True)  # newest first; one time apart, by source and id
    found.sort(key=lambda held: held.own)
    return HeldList(found[:limit], len(found))


def check_line(args: Any, curator: bool, fail: Callable[[str], Exception]) -> dict[str, Any]:
    """The arguments of a `held` line as an agent file writes it, checked: none, or a `limit` of 1 to `LIST_MAX`,
    and only on a curator's file. Raises what `fail` makes of the problem."""
    args = {} if args is None else args
    if not isinstance(args, dict) or set(args) - {"limit"}:
        raise fail("it takes limit alone, or nothing: write - held: {} or - held: { limit: 200 }")
    limit = args.get("limit", LIST_MAX)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIST_MAX:
        raise fail(f"limit {limit!r} is not a whole number from 1 to {LIST_MAX}: write limit: 200")
    if not curator:
        raise fail("it lists the records held for a curator's collection, and this file is no curator.md: move "
                   "the line to the collection's curator.md, or delete it")
    return args


MAX_REASON = 200  # characters of a left-out reason, as leave_out bounds it
MAX_LINE = 2000  # characters one held record takes of the list, whatever its keys and fields hold
FIRST = ("source", "id", "name")  # what admit and leave_out name a held record by, first on its line


def _capped(text: str, cap: int) -> str:
    """Text folded to one line and cut at `cap` characters, with how long it was."""
    one = " ".join(str(text).split())
    return one if len(one) <= cap else f"{one[:cap]}… ({len(one)} characters)"


def _line(data: dict[str, Any]) -> str:
    """One held record as the list shows it: its data as JSON, cut at `MAX_LINE` characters, `FIRST` first, then the
    rest sorted, its fields last, so a cut never takes what names the record."""
    data = json.loads(json.dumps(data, sort_keys=True))  # every level sorted, an order json.loads keeps
    order = sorted(data, key=lambda key: FIRST.index(key) if key in FIRST else len(FIRST) + (key == "fields"))
    return "- " + _capped(json.dumps({key: data[key] for key in order}), MAX_LINE)


def _data(held: Held) -> dict[str, Any]:
    data = asdict(held)
    data.pop("revision")  # counted in the item's key, so the digest moves, and of no use to the run
    data.pop("carried")  # the source's own knowledge: an admit of the record leaves those keys off its article
    for mark in ("links_to", "proposed", "outside", "fields"):
        if not data[mark]:
            data.pop(mark)  # proposed is set on a first run alone; the others when a record has them
    # named as a fact's label is: a bare "ours" beside a record's other names read as all of them
    own = [held.name] if data.pop("own") and held.name else []
    if own := list(dict.fromkeys(own + data.pop("ours"))):
        data[OWN_SIDE] = own
    return data


def _counted(held: Held, agent: str) -> dict[str, Any]:
    """A held line as the digest counts it: never `proposed`, since a proposal wakes no curator on a first run, and
    no article this curator proposed itself among `links_to`, so its own proposals never wake it again. An article
    another curator proposed that the record would sit under counts, and wakes it."""
    data = _data(held)
    data.pop("proposed", None)
    if "links_to" in data:
        mine = f"agents/{agent}.md"
        data["links_to"] = [item for item in data["links_to"] if item.get("proposed_by") != mine]
        if not data["links_to"]:
            data.pop("links_to")
    return data


def entry(agent: Agent, store: Store | None, sources: Iterable[str], n: int, line: EvidenceLine,
          ours: Ours = NOBODY) -> Evidence:
    """The evidence a `held` line gathers for a curator: its collection's held records, each one line of data."""
    name = f"agents/{agent.id}.md"
    if agent.role != "curator":
        raise EvidenceError(f"{name}: evidence {n}, held: it lists the records held for a curator's collection, and "
                            f"this agent curates none: move the line to a curator.md")
    if store is None:
        raise EvidenceError(f"{name}: evidence {n}, held: the lookup it was gathered with holds no store, so no held "
                            "record can be read: gather it through the workspace's own lookup")
    args = line.args if isinstance(line.args, dict) else {}
    found = held_for(store, agent.target, sources, limit=int(args.get("limit", LIST_MAX)), ours=ours)
    left = {(row.data.get("source"), row.data.get("id")): str(row.data.get("reason", ""))
            for row in store.rows("left-out", {"collection": agent.target}) if row.data.get("id")}
    out = Evidence(f"Held for {agent.target}: records your sources hold that no article in the working set binds, "
                   "newest first", spelled=f"held for {agent.target}")
    for held in found.items:
        shown = _line(_data(held))
        counted = _line(_counted(held, agent.id))
        reason = left.get((held.source, held.id))
        text = _line(_data(held) | {"left_out": _capped(reason, MAX_REASON)}) if reason else shown
        # the reason is the curator's own words, so it is shown and never counted: a reworded one wakes no run
        out.items.append(Item(f"held:{held.source}:{held.id}:{held.revision}", text, at=held.at,
                              left=f"{held.source}:{held.id}", counts=counted if text != counted else None))
    if first_run_open(store):
        out.notes.append("This is the workspace's first run: links_to names the articles a record would sit under, and "
                         "one with proposed_by is only proposed so far, by that curator, since nothing is made until "
                         "the person approves the list. proposed says the record itself is on the list already.")
    if found.total > len(found.items):
        out.notes.append(f"{found.total - len(found.items)} more held records are not listed here, the oldest: "
                         "admit or leave out these, and a later run lists the rest")
    if not out.items:
        out.notes.append(f"no record is held for {agent.target}")
        out.counted = []  # an empty list is counted by its items alone, whatever its notes say of the first run
    return out
