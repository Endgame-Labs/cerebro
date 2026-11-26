"""The store: storage hooks, and the SQLite store behind them.

All SQL and every schema live in this module. Other modules call the `Store` methods and never see a table. The
store holds everything Cerebro produces for a workspace, in one file:
- articles in pieces: an identity, a part of text for each heading or agent, and properties, each a row of its own;
- first-party records, whole;
- named append-only logs, such as `proposals` and `agent-runs`, and a small space of state by key;
- links from facts to articles, each saying how the fact is tied to the article;
- the facts of each record's earlier saves, superseded, up to a bound of saves per record.

Nothing is overwritten wholesale. A part is replaced only by another write of that heading, and a property only
by another write of that field, by the article's curator when it admits it or by one of the article's agents, each
kept with who wrote it and when.

A property value is JSON data: text, a number, true or false, null, or a list or map of those. A date is ISO 8601
text. A query names its collection and compares properties with `=`, `<`, `<=`, `>`, `>=`, and `in`. Numbers
compare as numbers, text compares as text ignoring case, and a list matches when any item does. Properties are
indexed by collection, so one name may hold a date in one collection and text in another, and a query is a lookup
on that index that never reads an article's text.

Which store a workspace uses is set in its `settings.yaml`, and `open_store` opens it. With no `store` block, it is
the SQLite store at `.cerebro/cerebro.sqlite` in the workspace folder:

    store:
      path: .cerebro/cerebro.sqlite    # the SQLite file; a relative path is under the workspace folder

A higher volume workspace plugs in its own store by naming its class, importable as `module:Class`. Every other key
of the block goes to its constructor as a keyword argument, or to its `from_settings`, with the workspace folder as
`root`, if it overrides that:

    store:
      class: example_stores:PostgresStore
      dsn: postgresql://localhost/cerebro

Such a class subclasses `Store` and passes the `Store` suite. The SQLite store stamps its file with a schema
version, and refuses a file of another version, which it never upgrades. Each write is one short transaction under the
file's write lock. One that cannot take the lock in time raises `StoreBusy`, and one the file or its disk refuses
raises `StoreUnwritable`.
"""

from __future__ import annotations

import atexit
import importlib
import inspect
import json
import math
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, astuple, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from .article import PROTECTED, Article, Citation, Compilation, Identity, Property, protected, stands
from .caller import Caller
from .connector import Record
from .errors import CerebroError
from .private import make_file, make_folder
from .search import DurableIndex, Hit

__all__ = [
    "DEFAULT_PATH", "LOGS", "MENTIONS", "OPS", "RECORD", "RELATIONS", "SCHEMA_VERSION", "SETTINGS_KEYS", "SUBJECT", "TIES", "UNKNOWN", "LogRow", "PropertyError", "SQLiteIndex", "SQLiteStore", "SchemaError", "Store",
    "StoreError", "StoreSettingsError", "Where", "check_id", "check_tie", "check_value", "check_where", "compares", "made_unique", "open_store", "order_key", "own_key",
    "scrubbed", "update_state",
]


# -- What every store keeps, and how a value compares
OPS = ("=", "<", "<=", ">", ">=", "in")

# How a fact is tied to an article: it is about the article, it names it, it came from a record bound to it, or the
# link says nothing of which.
SUBJECT, MENTIONS, RECORD, UNKNOWN = "subject", "mentions", "record", "unknown"

TIES = (SUBJECT, MENTIONS, RECORD, UNKNOWN)
RELATIONS = (SUBJECT, MENTIONS)  # what a fact says of an article, as a person or a caller is shown it

# The named logs Cerebro writes, each a query can read: proposals and merges, every agent run, every MCP call, the
# spend ledger, and the candidates curators left out. An app's own logs are named by the app.
LOGS = ("proposals", "merges", "agent-runs", "mcp-requests", "spend", "left-out")


class StoreError(CerebroError, ValueError):
    """A write names an article that is not there, or creates one that is."""


class PropertyError(StoreError):
    """A property holds a value no store can keep as written."""


class SchemaError(StoreError):
    """A store file is not a Cerebro store, or was written by a Cerebro of another schema version."""


class StoreBusy(StoreError):
    """Another process holds the store's write lock, as a pass or `serve` does while it writes: the store is sound,
    and a try a moment later goes through."""


class StoreUnwritable(StoreError):
    """The store cannot be written, for want of a permission, a writable disk or room on it, or for a disk error:
    reads answer from it where they can."""


class StoreSettingsError(StoreError):
    """The `store` block of `settings.yaml` names no store this workspace can open."""


@dataclass
class Where:
    """One predicate on a property: its field, an operator from `OPS`, and the value to compare with."""

    field: str
    op: str
    value: Any


@dataclass
class LogRow:
    """One row of a named log: its place in the store's history, and its data."""

    log: str
    seq: int
    data: dict[str, Any]


def _plain(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_plain(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _plain(item) for key, item in value.items())
    return False


def check_id(id: str) -> str:
    """Refuse an article id that could name a path outside its collection's folder: an empty part, `.` or `..`, a
    backslash, a colon or a control character. Gives the id. Every store checks the id it creates."""
    parts = id.split("/")
    if len(parts) < 2 or any(p in ("", ".", "..") for p in parts) or any(c in id for c in "\\:") or \
            any(ord(c) < 32 for c in id):
        raise StoreError(f"article id {id!r} is no collection and name such as things/a-thing: each part a "
                         "name, never empty, . or ..")
    return id


def check_tie(how: str) -> None:
    if how not in TIES:
        raise StoreError(f"a link ties a fact as one of {', '.join(TIES)}, and got {how!r}: pass one of them")


def check_value(where: str, name: str, value: Any) -> None:
    """Refuse a property named id, and a value that is not JSON data."""
    if name == "id":
        raise PropertyError(f"{where} has a property named id, which is the article's own: rename it")
    if not _plain(value):
        raise PropertyError(f"{where} {name} is a {type(value).__name__}: write text, a number, true or false, or "
                            "a list or map of those, and a date as ISO 8601 text")


def check_where(where: list[Where]) -> None:
    for predicate in where:
        if predicate.op not in OPS:
            raise StoreError(f"query operator {predicate.op!r} on {predicate.field} is not one of {', '.join(OPS)}")


def _scalar(value: Any) -> tuple[str, Any] | None:
    """A value as the store compares it: ("n", a float) or ("t", lower-case text), or None if it does not compare."""
    if isinstance(value, bool):
        return "t", "true" if value else "false"
    if isinstance(value, (int, float)):
        return "n", float(value)
    if isinstance(value, str):
        return "t", value.lower()
    return None


def compares(value: Any, op: str, want: Any) -> bool:
    """Whether a property value meets one predicate. A list meets it when any item does."""
    if op == "in":
        return any(compares(value, "=", item) for item in (want if isinstance(want, list) else [want]))
    goal = _scalar(want)
    for item in value if isinstance(value, list) else [value]:
        have = _scalar(item)
        if have is None or goal is None or have[0] != goal[0]:
            continue
        a, b = have[1], goal[1]
        if (op == "=" and a == b) or (op == "<" and a < b) or (op == "<=" and a <= b) or (op == ">" and a > b) \
                or (op == ">=" and a >= b):
            return True
    return False


def order_key(value: Any) -> tuple[int, Any]:
    """The key a store orders by: numbers before text, and no value last."""
    items = [s for s in (_scalar(v) for v in (value if isinstance(value, list) else [value])) if s is not None]
    if not items:
        return (2, 0)
    kind, low = min(items)
    return (0 if kind == "n" else 1, low)


def utc(at: str, article: str, field: str) -> str:
    """A property's time as fixed-width UTC text, so times compare as text. Refuses one with no offset."""
    try:
        moment = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        moment = None
    if moment is None or moment.tzinfo is None:
        raise StoreError(f"{field} of {article} has at {at!r}, which is not an ISO 8601 time with an offset: write "
                         "it as 2026-11-01T09:00:00Z")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _all_masked(value: Any, mask: Callable[[str], str]) -> Any:
    """`value` with each string in it, a map's keys too, masked as `clean.masked` says."""
    from .clean import masked

    if isinstance(value, str):
        return masked(value, mask)
    if isinstance(value, dict):
        return {_all_masked(k, mask): _all_masked(v, mask) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_all_masked(v, mask) for v in value]
    return value


def scrubbed(value: Any, scrub: Callable[[str], str] | None) -> Any:
    """A log row with each string in it, keys left alone, passed through `scrub`."""
    if scrub is None:
        return value
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrubbed(v, scrub) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrubbed(v, scrub) for v in value]
    return value


def made_unique(record: Record, unique: Sequence[str], others: Iterable[Mapping[str, Any]]) -> None:
    """Change the fields `unique` names in `record` so none holds a value one of `others` holds in it: the same `-n`,
    the least from 2 that frees them all, added to each. A store calls it inside the write that keeps the record."""
    taken: dict[str, set[str]] = {name: set() for name in unique}
    for held in others:
        for name in unique:
            if held.get(name) is not None:
                taken[name].add(str(held[name]))
    bases = {name: str(record.fields.get(name, "")) for name in unique}
    n = 1
    while any((base if n == 1 else f"{base}-{n}") in taken[name] for name, base in bases.items()):
        n += 1
    if n > 1:
        record.fields.update({name: f"{base}-{n}" for name, base in bases.items()})


# Cerebro's own state keys start here, where no key of a source's, `<what>:<source>`, reaches. A key on disk: it stays.
OWN = "cerebro:"


def own_key(*parts: str) -> str:
    """The state key of something Cerebro keeps for itself, under `cerebro:`, as `own_key("keep", digest)`."""
    return OWN + ":".join(parts)


LANDINGS_KEPT = 64  # parts a store remembers landing, so a part landed again costs no second cleaning


def _landing_key(compilation: Compilation) -> tuple[Any, ...]:
    """What landing a part reads, and so what a part landed before is found by."""
    return (compilation.heading, compilation.text, compilation.whole, tuple(map(astuple, compilation.citations)))


class Store:
    """The storage hooks. A store for a higher volume workspace implements these and passes the `Store` suite.

    A store is a context manager, and `close()` releases what it holds. `scrub`, which the app sets, masks secrets:
    every string in a log row, and a kept record's text and fields, goes through it before it is written. `masks`,
    which the app sets too, gives a mask of every value the secrets store holds, built anew for each write, in its
    place: `scrubbing` gives the one a write uses. `weights`, which the app sets too, is how strong each key its
    sources issue is, its own connectors' included.
    """

    scrub: Callable[[str], str] | None = None
    masks: Callable[[], Callable[[str], str]] | None = None
    weights: Mapping[str, int] | None = None

    def scrubbing(self) -> Callable[[str], str] | None:
        """The mask one write uses: of every value the secrets store holds when `masks` is set, else `scrub`."""
        return self.masks() if self.masks is not None else self.scrub

    def masked(self, record: Record) -> Record:
        """`record` with its text and fields masked as `scrubbing` gives, in place, so what the caller goes on to hand
        the facts layer is what was kept."""
        if (scrub := self.scrubbing()) is not None:
            record.fields = scrubbed(record.fields, scrub)
            record.body = scrub(record.body) if record.body is not None else None
        return record

    @classmethod
    def from_settings(cls, root: Path, **options: Any) -> Store:
        """Open the store a workspace's `settings.yaml` names. `options` are the other keys of its `store` block, and
        go to the constructor as keyword arguments. A store that needs the workspace folder, `root`, overrides this."""
        _fits(cls, cls.__name__, **options)
        return cls(**options)

    def close(self) -> None:
        """Release the store. A store with nothing to release does nothing."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- articles
    def create(self, identity: Identity) -> None:
        """Create an article's identity. Raises `StoreError` if the id is taken."""
        raise NotImplementedError

    def set_identity(self, identity: Identity) -> None:
        """Replace an article's identity whole: its title, keys, status, and admission. Its collection stays."""
        raise NotImplementedError

    def titled(self, identity: Identity) -> Identity:
        """`identity` as it is written: its title and keys masked as `clean.masked` says, with `scrubbing`. Never its
        id, which names the article."""
        if (mask := self.scrubbing()) is None:
            return identity
        from .clean import masked

        return replace(identity, title=masked(identity.title, mask), keys=_all_masked(identity.keys, mask))

    def valued(self, name: str, prop: Property) -> tuple[str, Property]:
        """A field as it is written: its name and each text in its value, a map's keys too, masked as `clean.masked`
        says, with `scrubbing`."""
        if (mask := self.scrubbing()) is None:
            return name, prop
        from .clean import masked

        return masked(name, mask), replace(prop, value=_all_masked(prop.value, mask))

    def landed(self, compilation: Compilation) -> tuple[Compilation, list[str], list[str]]:
        """`compilation` as it is written, a line for each thing cleaning changed, and one for each thing it noted and
        left as written: its text masked with `scrubbing`, then cleaned under its heading, its heading and each
        citation's quote likewise on one line, as `clean.land` says. A citation's id and source, which name a record,
        are left as they are: changed, they would name a record that does not exist. Landing a part already landed
        gives it back the same."""
        from .clean import label, land, land_heading, land_line

        mask = self.scrubbing()
        landing = getattr(self, "_landing", {})  # the parts last landed, by key, each with the mask it took
        if (done_before := landing.get(_landing_key(compilation))) is not None and done_before[0] is mask:
            before = done_before[1]
            return replace(compilation, heading=before.heading, text=before.text, citations=before.citations), [], []
        cited = tuple(c.id for c in compilation.citations)
        done = land(compilation.text, mask, top=1 if compilation.whole else 2, cited=cited)
        changed, noted = list(done.removed), list(done.noted)
        heading = compilation.heading
        if not compilation.whole:
            title = land_heading(compilation.heading, mask)
            heading = title.text
            changed += [f"its heading: {line}" for line in title.removed]
            noted += [f"its heading: {line}" for line in title.noted]
        citations = []
        for citation in compilation.citations:
            if citation.quote:
                quote = land_line(citation.quote, mask)
                changed += [f"the footnote of [^{label(citation.id)}]: {line}" for line in quote.removed]
                noted += [f"the footnote of [^{label(citation.id)}]: {line}" for line in quote.noted]
                citation = replace(citation, quote=quote.text)
            citations.append(citation)
        out = replace(compilation, heading=heading, text=done.text.rstrip("\n"), citations=citations)
        kept = list(landing.items())[-(LANDINGS_KEPT - 1):]  # the newest, leaving room for this one
        self._landing = {**dict(kept), _landing_key(out): (mask, out)}
        return out, list(dict.fromkeys(changed)), list(dict.fromkeys(noted))

    def write_compilation(self, compilation: Compilation) -> list[str]:
        """Write one part, replacing that heading's earlier text in its place, landed as `landed` says, and give
        the lines it says. A new heading goes last."""
        raise NotImplementedError

    def delete_compilation(self, article: str, heading: str) -> None:
        """Delete one part. Deleting one that is not there changes nothing."""
        raise NotImplementedError

    def set_field(self, article: str, name: str, prop: Property) -> None:
        """Write one property of an article, replacing that field's earlier value. Refuses what `check_value`
        refuses."""
        raise NotImplementedError

    def drop_field(self, article: str, name: str) -> None:
        """Take one property off an article, as its writer drops it. Dropping one it does not hold changes nothing."""
        raise NotImplementedError

    def article(self, id: str) -> Article | None:
        """The article assembled: identity, parts in order, and each property as last written."""
        raise NotImplementedError

    def query(self, collection: str, where: list[Where] | None = None, *, order_by: str | None = None,
              descending: bool = False, limit: int | None = None, offset: int = 0) -> list[Article]:
        """The articles of one collection whose properties meet every predicate, ordered by a property or by id."""
        raise NotImplementedError

    def bound(self, key: str, value: str) -> list[str]:
        """The ids of articles whose identity holds `value` under `key`, ignoring case."""
        raise NotImplementedError

    def named(self, name: str) -> list[str]:
        """The ids of articles whose title is `name`, ignoring case."""
        raise NotImplementedError

    def keyed(self, value: str) -> list[str]:
        """The ids of articles whose identity holds `value` under any key, ignoring case."""
        raise NotImplementedError

    def collections(self) -> list[str]:
        """Every collection that holds an article, in order."""
        raise NotImplementedError

    def linking_to(self, field: str, ids: list[str]) -> list[Identity]:
        """The identity of each article whose field `field`, or an item of it when it is a list, is one of `ids`,
        ignoring case, as everything reads its fields, in order of id."""
        raise NotImplementedError

    # -- first-party records
    def keep(self, record: Record, *, unique: Sequence[str] = ()) -> Record:
        """Keep a first-party record whole, body included, and give it back as kept. Each field `unique` names must
        hold a value no other record of its source holds in that field: when one does, the same `-2`, `-3` and so on,
        the least that frees them all, is added to each, in the same write, so two processes never both take a
        value. The record is changed in place to match."""
        raise NotImplementedError

    def record(self, id: str) -> Record | None:
        raise NotImplementedError

    def records(self, source: str) -> list[Record]:
        """The first-party records of one source, such as an app's kind, in the order they were kept."""
        raise NotImplementedError

    # -- logs and state
    def append(self, log: str, data: dict[str, Any]) -> LogRow:
        """Append a row of JSON data to a named log."""
        raise NotImplementedError

    def rows(self, log: str, where: dict[str, Any] | None = None, *, since: int = 0,
             limit: int | None = None) -> list[LogRow]:
        """The rows of a log after `since`, oldest first, whose fields equal every value in `where`."""
        raise NotImplementedError

    def latest(self, log: str, key: str = "id", where: dict[str, Any] | None = None) -> list[LogRow]:
        """The newest row of a log for each value of `key`, oldest first, whose fields equal every value in
        `where`. Rows with no `key` are left out. So a log of proposals reads as each proposal's latest state: a
        decision is a new row for the proposal's id, and a query of waiting proposals passes no decided one."""
        raise NotImplementedError

    def total(self, log: str, field: str, where: dict[str, Any] | None = None) -> float:
        """The sum of one numeric field over the rows of a log whose fields equal every value in `where`, as the
        spend ledger reads a day's spend. A row whose field is no number counts nothing."""
        raise NotImplementedError

    def state(self, key: str) -> Any:
        """The value kept under `key`, or None."""
        raise NotImplementedError

    def set_state(self, key: str, value: Any) -> None:
        raise NotImplementedError

    def exclusive(self) -> AbstractContextManager[None]:
        """A block in which every call on this store runs as one write: no other writer, in this process or
        another, lands between its reads and its writes, and a block that raises leaves nothing written. Blocks
        nest. `decide` reads a proposal's state and applies it inside one, so a proposal is applied once."""
        raise NotImplementedError

    # -- links
    def link(self, fact_id: str, article_ids: list[str], at: str = "", how: str = SUBJECT) -> None:
        """Link a fact to articles, tied as `how`, one of `TIES`. When the fact was learned is `at`, ISO 8601 with an
        offset, or empty for unknown, and a fact linked to an article already keeps the time it was first linked
        there. A known tie takes the place of an `unknown` one. Linking again changes nothing."""
        raise NotImplementedError

    def unlink(self, fact_id: str, article_ids: list[str], how: str | None = None) -> None:
        """Take links back: each tie, or only the one `how` names. Taking back a link that is not there changes
        nothing."""
        raise NotImplementedError

    def articles_of(self, fact_id: str) -> list[str]:
        """The ids of the articles a fact is linked to, each once, in the order first linked."""
        raise NotImplementedError

    def ties_of(self, fact_id: str) -> dict[str, list[str]]:
        """How a fact is tied to each article it is linked to, by article id in the order first linked, each tie in
        the order of `TIES`."""
        raise NotImplementedError

    def unknown_ties(self) -> int:
        """How many links say nothing of how their fact is tied."""
        raise NotImplementedError

    def facts_of(self, article_id: str, since: str | None = None) -> list[str]:
        """The ids of the facts linked to an article, each once, in the order first linked. With `since`, only the facts
        learned at or after it, as UTC: a link with no time is never after it."""
        raise NotImplementedError

    # -- earlier saves
    def supersede(self, source: str, record: str, revision: str, at: str, facts: list[dict[str, Any]],
                  keep: int) -> int:
        """Keep the facts one earlier revision of a record gave, each a map with its `id`, as superseded at `at`, the
        time a later revision replaced it. A fact kept already keeps its first row. Then keep only the `keep` latest
        saves of the record, and drop the facts of older ones, the oldest first. Gives how many facts it dropped."""
        raise NotImplementedError

    def superseded(self, *, source: str | None = None, record: str | None = None,
                   ids: list[str] | None = None) -> list[dict[str, Any]]:
        """The superseded facts kept that meet every filter given, the latest superseded first, each a map of
        `source`, `record`, `revision`, `at`, `status` (`superseded` or `retracted`) and `fact`, as it was kept."""
        raise NotImplementedError

    def retract_superseded(self, ids: list[str]) -> int:
        """Mark superseded facts retracted. They stay readable by id. Gives how many were superseded until now."""
        raise NotImplementedError

    # -- history
    def cursor(self) -> int:
        """A point in the store's history, to pass to `changed_since` later."""
        raise NotImplementedError

    def changed_since(self, cursor: int) -> tuple[list[tuple[str, str]], int]:
        """What was written after `cursor`, as (kind, id) pairs, each once in the order of its last write, and the
        cursor to pass next. Kinds are `article`, `record`, `log`, and `state`."""
        raise NotImplementedError


def update_state(store: Store, key: str, change: Callable[[Any], Any]) -> Any:
    """Change the value kept under `key` as one write: `change` is handed the value as it stands, read inside the
    store's exclusive block, and gives the new one, so a writer in another thread or process that lands between a read
    and a write is never lost. Gives the new value."""
    with store.exclusive():
        value = change(store.state(key))
        store.set_state(key, value)
        return value


def _caller(data: dict[str, Any] | None) -> Caller | None:
    return Caller(**data) if data else None


def identity_from(data: dict[str, Any]) -> Identity:
    return Identity(**(data | {"admitted_by": _caller(data.get("admitted_by"))}))


def compilation_from(data: dict[str, Any]) -> Compilation:
    return Compilation(**(data | {"citations": [Citation(**c) for c in data.get("citations", [])]}))


def property_from(data: dict[str, Any]) -> Property:
    return Property(data.get("value"), Caller(**data["by"]), str(data.get("at") or ""), list(data.get("cites") or []))


RECORD_FIELDS = frozenset(f.name for f in fields(Record))


def record_from(data: dict[str, Any]) -> Record:
    """A record from the dict `asdict` made of it. A key a record has no field for, such as the `listed_at` the
    runner keeps beside a held listing, is left out."""
    return Record(**({k: v for k, v in data.items() if k in RECORD_FIELDS} | {"by": _caller(data.get("by"))}))


# -- Which store a workspace opens
SETTINGS_KEYS = {"store": {"class": None, "path": None}}  # a class other than sqlite takes its own, as _fits checks
DEFAULT_PATH = ".cerebro/cerebro.sqlite"


def _fits(call: Any, name: str, *args: Any, **options: Any) -> None:
    """Refuse settings a store does not take, before it is called with them."""
    try:
        inspect.signature(call).bind(*args, **options)
    except TypeError as error:
        raise StoreSettingsError(f"settings.yaml store does not suit the {name} store ({error}): remove the key "
                                 "or check its spelling") from None


def _store_class(name: str) -> type[Store]:
    module, _, attr = name.partition(":") if ":" in name else name.rpartition(".")
    try:
        found = getattr(importlib.import_module(module), attr, None) if module and attr else None
    except ImportError as error:
        raise StoreSettingsError(f"settings.yaml store.class {name} does not import ({error}): install the package "
                                 "that holds it, or name it as module:Class") from None
    if not (isinstance(found, type) and issubclass(found, Store)):
        raise StoreSettingsError(f"settings.yaml store.class {name} is not a Store class: name a subclass of "
                                 "cerebro.store.Store as module:Class")
    return found


def open_store(root: Path, settings: dict[str, Any] | None = None) -> Store:
    """Open the store a workspace's settings name: the SQLite store under `root` by default, or the class named in
    `store.class`, given the block's other keys."""
    block = (settings or {}).get("store")
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise StoreSettingsError("settings.yaml store is not a map: write store: then path: or class: under it")
    options = dict(block)
    for key in options:
        if not isinstance(key, str):  # a keyword argument must be a string
            from .project import _shown  # the key as written, as on:, which YAML 1.1 reads as true

            raise StoreSettingsError(f"settings.yaml store: unknown key {_shown(key)}: remove the key or check its "
                                     "spelling")
    name = options.pop("class", "sqlite")
    if not isinstance(name, str) or not name:
        raise StoreSettingsError(f"settings.yaml store.class is {name!r}: write sqlite, or a store class as "
                                 "module:Class")
    cls: type[Store] = SQLiteStore if name == "sqlite" else _store_class(name)
    _fits(cls.from_settings, name, Path(root), **options)
    return cls.from_settings(Path(root), **options)


# -- The SQLite file: its schema, and the files it refuses
SCHEMA_VERSION = 1
APPLICATION_ID = 0x43455242  # "CERB" in the file's header, so a SQLite file of another program is told apart

# Every table, and an index for each way the store reads it. A property's values sit in `property_values`, one row
# per comparable item, keyed by collection and field: `values_num` and `values_txt` serve `=`, `in` and ranges, and
# `values_order` serves ordering without reading any other table.
SCHEMA = """
create table articles (id text primary key, collection text not null, title text not null, body text not null);
create index articles_collection on articles (collection, id);
create index articles_title on articles (title);
create table keys (article_id text not null, key text not null, value text not null);
create index keys_lookup on keys (key, value);
create index keys_article on keys (article_id);
create table compilations (article_id text not null, heading text not null, position integer not null,
    body text not null, primary key (article_id, heading));
create table properties (article_id text not null, field text not null, body text not null,
    primary key (article_id, field));
create table property_values (collection text not null, article_id text not null, field text not null, num real,
    txt text);
create index values_num on property_values (collection, field, num);
create index values_txt on property_values (collection, field, txt);
create index values_order on property_values (collection, field, article_id, num, txt);
create index values_article on property_values (article_id, field);
create index values_link on property_values (field, txt);
create table records (id text primary key, source text not null, seq integer not null, body text not null);
create index records_source on records (source, seq);
create table logs (seq integer primary key autoincrement, log text not null, body text not null);
create index logs_log on logs (log, seq);
create table state (key text primary key, value text not null);
create table links (fact_id text not null, article_id text not null, how text not null, at text not null,
    primary key (fact_id, article_id, how));
create index links_fact on links (fact_id);
create index links_article on links (article_id);
create table changes (seq integer primary key autoincrement, kind text not null, id text not null);
create table superseded (seq integer primary key autoincrement, save integer not null, source text not null,
    record text not null, revision text not null, at text not null, fact_id text not null unique,
    status text not null, body text not null);
create index superseded_record on superseded (source, record, save)
"""

PROTECTED_STAMP = "store/protected-fields"  # the state key naming the protected fields the index was last built for
OPEN_WAIT = 2.0  # seconds an open that must write waits for another process's write before it says the store is busy


def _not_a_store(path: Path | str, why: object) -> SchemaError:
    return SchemaError(f"{path} is not a cerebro store ({why}): point store.path in settings.yaml at a new file, or "
                       "move this one aside")


def _refused(path: Path | str, owner: int, version: int, tables: set[str]) -> SchemaError | None:
    """The refusal of a file holding `tables` under `owner` and `version`, or None for a new file or a store this
    cerebro reads. A store of another version is never upgraded."""
    if owner == 0 and not tables:
        return None
    if owner != APPLICATION_ID:
        return _not_a_store(path, "it holds another program's tables")
    if version != SCHEMA_VERSION:
        return SchemaError(f"{path} was made by a different version of Cerebro, which this one cannot read: move the "
                           "file aside and the next run starts a new store, which loses what people told, their "
                           "decisions and app records while sources are read again, or run the version that made it")
    return None


def _look(path: Path) -> None:
    """Refuse a file that is not a store from what it holds, read with no change to it, before anything writes it, so
    another program's file keeps its bytes and is never called busy. A file that cannot be read so is left to the
    open, which says what is wrong with it."""
    try:
        with path.open("rb") as file:
            head = file.read(100)
    except OSError:
        return
    if not head:
        return
    if not head.startswith(b"SQLite format 3\x00"):
        raise _not_a_store(path, "file is not a database")
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=OPEN_WAIT)
        try:
            owner = db.execute("pragma application_id").fetchone()[0]
            version = db.execute("pragma user_version").fetchone()[0]
            tables = {row[0] for row in db.execute("select name from sqlite_master where type = 'table'")}
        finally:
            db.close()
    except sqlite3.Error:
        # Unread, as while its program holds it exclusively: its header still names its owner, and a store is never
        # in a rollback journal, so a file in one that is not marked as a store is another program's.
        owner = int.from_bytes(head[68:72], "big")
        if owner == APPLICATION_ID or (owner == 0 and head[18] == 2):
            return
        raise _not_a_store(path, "it holds another program's tables") from None
    if (refused := _refused(path, owner, version, tables)) is not None:
        raise refused


# -- When SQLite refuses a write: another process holds the lock, or the file cannot be written
TRY_MS = 50  # one try for the write lock, in milliseconds: between tries other threads of the process read


def busy(error: sqlite3.Error) -> bool:
    """Whether SQLite refused because another connection holds the lock it needs, not because the file is wrong."""
    return getattr(error, "sqlite_errorname", "").startswith(("SQLITE_BUSY", "SQLITE_LOCKED"))


def busy_line(path: Path | None) -> str:
    """What a person is told of a store another process is writing: it is sound, and a try a moment later goes."""
    return (f"{path or 'the store'} is busy: another process, such as a pass or cerebro serve, is writing to it. "
            "Nothing is wrong with it: try again in a moment")


def unwritable(error: sqlite3.Error) -> bool:
    """Whether SQLite refused because the file or its folder cannot be written now, not because the file is wrong."""
    return getattr(error, "sqlite_errorname", "").startswith(
        ("SQLITE_READONLY", "SQLITE_CANTOPEN", "SQLITE_IOERR", "SQLITE_FULL", "SQLITE_PERM"))


def unwritable_line(path: Path | None, error: sqlite3.Error) -> str:
    """What a person is told of a store that cannot be written: why, and that reads still answer."""
    name = getattr(error, "sqlite_errorname", "")
    folder = path.parent if path is not None else None
    if name.startswith("SQLITE_FULL"):
        cause = "its disk is full"
    elif name.startswith("SQLITE_IOERR"):
        cause = f"its disk gave an I/O error ({error})"
    elif folder is not None and os.path.isdir(folder) and hasattr(os, "statvfs") and (
            os.statvfs(folder).f_flag & os.ST_RDONLY):
        cause = f"the disk {folder} is on is mounted read-only"
    elif folder is not None and not os.access(folder, os.W_OK):
        cause = f"this user may not write to {folder}"
    elif path is not None and path.exists() and not os.access(path, os.W_OK):
        cause = f"this user may not write to {path}"
    else:
        cause = str(error)
    return (f"{path or 'the store'} cannot be written, since {cause}. A command that only reads still answers from "
            "it, and one that changes it goes once that is fixed")


@contextmanager
def brief_wait(store: Store, seconds: float = OPEN_WAIT) -> Iterator[None]:
    """A block whose writes to `store` wait at most `seconds` for another process's write, then raise `StoreBusy`,
    for a read that writes only to keep a cache, such as the search index, and can go on without it. A store that is
    not SQLite is left as it is."""
    if not isinstance(store, SQLiteStore) or store.path is None:
        yield
        return
    wait = store._wait
    store._waits.seconds = min(seconds, wait)
    try:
        yield
    finally:
        store._waits.seconds = wait


class _Later:
    """Log rows written after the answer they log, so a row never holds a reply: at once on the store's own
    connection when the store is free, else, in order, on a thread with a connection of its own that waits `seconds`
    at most for another process's write. Rows that still cannot land are dropped, and the count is said in one line
    on standard error, the server's own output."""

    def __init__(self, store: SQLiteStore, seconds: float):
        self.store, self.seconds = store, seconds
        self.rows: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self.lock, self.waiting, self.dropped = threading.Lock(), 0, 0
        self.thread: threading.Thread | None = None

    def append(self, log: str, data: dict[str, Any]) -> None:
        data = scrubbed(data, self.store.scrubbing())  # masked as the store masks, whichever connection writes it
        with self.lock:
            if not self.waiting:
                try:
                    with brief_wait(self.store, 0):
                        self.store.append(log, data)
                    return
                except StoreBusy:
                    pass
                except StoreUnwritable as error:
                    self._drop(1, str(error))
                    return
            self.waiting += 1
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._write, name="cerebro-log-rows", daemon=True)
                self.thread.start()
            self.rows.put((log, data))

    def _drop(self, rows: int, why: str) -> None:
        self.dropped += rows
        print(f"cerebro: {self.dropped} request log row{'s' if self.dropped != 1 else ''} dropped so far: {why}, and "
              "the answers went out as they were", file=sys.stderr, flush=True)

    def _write(self) -> None:
        own: SQLiteStore | None = None
        while True:
            batch = [self.rows.get()]
            while not self.rows.empty():
                batch.append(self.rows.get_nowait())
            rows = [row for row in batch if row is not None]
            try:
                if rows:
                    own = own or SQLiteStore(self.store.path, timeout=self.seconds)
                    with own.exclusive():
                        for log, data in rows:
                            own.append(log, data)
            except Exception as error:  # noqa: BLE001 - a row is never worth the answers after it
                with self.lock:
                    self._drop(len(rows), f"another process held the store past {self.seconds:g} seconds"
                               if isinstance(error, StoreBusy) else f"{type(error).__name__}: {error}")
            with self.lock:
                self.waiting -= len(rows)
            for _ in batch:
                self.rows.task_done()
            if len(rows) < len(batch):
                if own is not None:
                    own.close()
                return

    def finish(self) -> None:
        """Let rows still waiting land in their wait, as the process ends."""
        if self.thread is not None and self.thread.is_alive():
            self.rows.put(None)
            self.thread.join(self.seconds + 1)


_later: dict[int, _Later] = {}
_later_lock = threading.Lock()


def append_later(store: Store, log: str, data: dict[str, Any]) -> None:
    """Append a row that logs an answer already given: it never waits behind another process's write, as `_Later`
    says. A store that is not a SQLite file is written at once."""
    if not isinstance(store, SQLiteStore) or store.path is None:
        store.append(log, data)
        return
    with _later_lock:
        later = _later.get(id(store))
        if later is None or later.store is not store:
            later = _later[id(store)] = _Later(store, OPEN_WAIT)
    later.append(log, data)


def _finish_later() -> None:
    for later in list(_later.values()):
        later.finish()


atexit.register(_finish_later)


class SQLiteStore(Store):
    """The basic store for one person: a SQLite file, or memory when `path` is None.

    The CLI may write while `serve` runs, so a file is opened in write-ahead logging mode, readers never wait, and a
    writer waits its turn for up to `timeout` seconds. Each write is one short `BEGIN IMMEDIATE` transaction, so two
    processes on one file never see half a write, and every number a write derives, a record's place,
    a part's position, is read inside it. Each read of several tables reads one snapshot.
    """

    def __init__(self, path: Path | None = None, *, timeout: float = 30.0):
        self.path, self.timeout = path, timeout
        self._lock = threading.RLock()
        self._depth = 0
        self._waits = threading.local()  # a `brief_wait`'s seconds, for the thread inside it
        self._unwritable: str | None = None  # the line a write is refused with, for a store opened to read alone
        if path is None:
            self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        else:
            _look(path)
            make_folder(path.parent)
            make_file(path)  # SQLite gives the files it makes beside it this file's mode
            self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=timeout)
        try:
            if path is not None:
                self.db.execute("pragma journal_mode=wal")
                self.db.execute("pragma synchronous=normal")  # durable at each checkpoint, and safe in WAL mode
            self._stamp()
        except sqlite3.DatabaseError as error:
            self.db.close()
            if busy(error):
                raise StoreBusy(busy_line(path)) from None
            if path is not None and unwritable(error):
                line = unwritable_line(path, error)
                if getattr(error, "sqlite_errorname", "").startswith(("SQLITE_IOERR", "SQLITE_FULL")):
                    raise StoreUnwritable(line) from None  # a disk fault, not read alone: opened again later
                self._read_alone(path, line)
                return
            raise _not_a_store(path, error) from None
        except BaseException:
            self.db.close()
            raise

    @classmethod
    def from_settings(cls, root: Path, *, path: str = DEFAULT_PATH) -> SQLiteStore:
        """The SQLite store at `path`, which is under the workspace folder `root` unless it is absolute."""
        if not isinstance(path, str) or not path.strip():
            raise StoreSettingsError(f"settings.yaml store.path is {path!r}: write a file path, such as "
                                     f"{DEFAULT_PATH}")
        file = Path(path).expanduser()
        return cls(file if file.is_absolute() else Path(root) / file)

    def _stamp(self) -> None:
        """Create the schema in a new file, and refuse a file of another program or another schema version. A store
        that needs nothing written is only read, so opening it never waits behind another process's write. One that
        needs a write, a new file or a new index, waits for the write lock at most `OPEN_WAIT`
        seconds before it says the store is busy."""
        with self._snapshot() as db:
            if self._stamped(db):
                return
        with brief_wait(self, OPEN_WAIT):
            self._stamp_now()

    def _stamped(self, db: sqlite3.Connection) -> bool:
        """Whether the store holds this schema version and its protected fields indexed for today's list, so opening
        it writes nothing. Refuses a file of another program or version."""
        owner = db.execute("pragma application_id").fetchone()[0]
        version = db.execute("pragma user_version").fetchone()[0]
        if owner == 0:
            return False  # a new file, which `_stamp_now` makes, or another program's, which it refuses
        if owner != APPLICATION_ID:
            raise _not_a_store(self.path or "the store", "it holds another program's tables")
        if version != SCHEMA_VERSION:
            return False  # refused under the write lock, in its own words
        row = db.execute("select value from state where key = ?", (PROTECTED_STAMP,)).fetchone()
        return row is not None and row[0] == json.dumps(sorted(PROTECTED))

    def _stamp_now(self) -> None:
        """`_stamp`'s write, under the store's write lock."""
        with self._tx() as db:
            owner = db.execute("pragma application_id").fetchone()[0]
            version = db.execute("pragma user_version").fetchone()[0]
            if owner == 0 and not db.execute("select count(*) from sqlite_master").fetchone()[0]:
                for statement in SCHEMA.split(";"):
                    db.execute(statement)
                db.execute(f"pragma application_id = {APPLICATION_ID}")
                db.execute(f"pragma user_version = {SCHEMA_VERSION}")
            else:
                name = self.path or "the store"
                tables = {row[0] for row in db.execute("select name from sqlite_master where type = 'table'")}
                if owner == 0 and not tables:  # not empty, yet no table: views or triggers alone, another program's
                    raise _not_a_store(name, "it holds another program's tables")
                if (refused := _refused(name, owner, version, tables)) is not None:
                    raise refused
            self._index_protected(db)  # in a new file, nothing to index yet: it stamps the list

    def _index_protected(self, db: sqlite3.Connection) -> None:
        """Index each protected field as it stands, once for each list of them, so no writer's value of a protected
        field stays indexed. The stamp in `state` names the list it was last indexed for."""
        stamp = json.dumps(sorted(PROTECTED))
        row = db.execute("select value from state where key = ?", (PROTECTED_STAMP,)).fetchone()
        if row is not None and row[0] == stamp:
            return
        for collection, field in db.execute("select distinct collection, field from properties join articles on "
                                            "articles.id = properties.article_id").fetchall():
            if protected(field):
                for (article,) in db.execute("select distinct article_id from properties join articles on "
                                             "articles.id = properties.article_id where collection = ? and field = ?",
                                             (collection, field)).fetchall():
                    self._reindex(db, collection, article, field)
        db.execute("insert or replace into state values (?,?)", (PROTECTED_STAMP, stamp))

    def _read_alone(self, path: Path, line: str) -> None:
        """Open a store that cannot be written to read it alone, so reads answer and every write gives `line`. In a
        folder this user cannot write, with no write in progress, it is read as it stands when it is opened."""
        uri = path.resolve().as_uri()
        wal = path.with_name(path.name + "-wal")
        for query in ("?mode=ro", "?mode=ro&immutable=1"):
            if "immutable" in query and wal.exists():
                break
            try:
                self.db = sqlite3.connect(uri + query, uri=True, check_same_thread=False, isolation_level=None,
                                          timeout=self.timeout)
            except sqlite3.Error:
                continue
            try:
                with self._snapshot() as db:
                    stamped = self._stamped(db)
            except sqlite3.OperationalError:
                self.db.close()
                continue
            except BaseException:
                self.db.close()
                raise
            if stamped:
                self._unwritable = line
                return
            self.db.close()
            break
        raise StoreUnwritable(line) from None

    def ensure(self, schema: str) -> None:
        """Run `schema`, statements that each create a table or index if it is not there, only when one of them is
        missing, so a store that has them all is only read."""
        names = re.findall(r"if not exists (\w+)", schema)
        with self._snapshot() as db:
            if set(names) <= {row[0] for row in db.execute("select name from sqlite_master")}:
                return
        with brief_wait(self), self._tx() as db:  # a first open's write, which waits as `_stamp`'s does
            for statement in schema.split(";"):
                db.execute(statement)

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # -- transactions: a write under the file's write lock, and a read of one snapshot
    @property
    def _wait(self) -> float:
        """How long this thread's next write waits for another process's: a `brief_wait`'s seconds, else `timeout`."""
        return getattr(self._waits, "seconds", self.timeout)

    def _begin(self) -> None:
        """Take the file's write lock for this thread, which holds `_lock`, waiting up to `_wait` seconds for another
        process's write. `_lock` is let go between tries, so this process's other threads read meanwhile."""
        if self._unwritable is not None:
            raise StoreUnwritable(self._unwritable)
        deadline = time.monotonic() + self._wait
        while True:
            left = max(0.0, deadline - time.monotonic())
            self.db.execute(f"pragma busy_timeout = {int(min(TRY_MS, left * 1000))}")
            try:
                self.db.execute("begin immediate")
                return
            except sqlite3.OperationalError as error:
                if unwritable(error):
                    raise StoreUnwritable(unwritable_line(self.path, error)) from None
                if not busy(error):
                    raise
                if time.monotonic() >= deadline:
                    raise StoreBusy(busy_line(self.path)) from None
            finally:
                self.db.execute(f"pragma busy_timeout = {int(self.timeout * 1000)}")
            self._lock.release()
            try:
                time.sleep(0.005)
            finally:
                self._lock.acquire()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write: it waits for the file's write lock first, so it never fails halfway for want of it. Inside
        another, it is part of that one, under a savepoint, so a nested write that raises is undone even when the
        outer one catches its error and goes on."""
        with self._lock:
            if self.db.in_transaction:
                self._depth += 1
                name = f"nested_{self._depth}"
                self.db.execute(f"savepoint {name}")
                try:
                    yield self.db
                except BaseException:
                    self.db.execute(f"rollback to {name}")
                    self.db.execute(f"release {name}")
                    raise
                else:
                    self.db.execute(f"release {name}")
                finally:
                    self._depth -= 1
                return
            self._begin()
            try:
                yield self.db
                self.db.execute("commit")
            except sqlite3.OperationalError as error:
                if self.db.in_transaction:  # a full disk or an I/O error may have rolled it back already
                    self.db.execute("rollback")
                if unwritable(error):
                    raise StoreUnwritable(unwritable_line(self.path, error)) from None
                raise
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("rollback")
                raise

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._tx():
            yield

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        """One read of several tables, which sees no write that lands while it reads."""
        with self._lock:
            if self.db.in_transaction:
                yield self.db
                return
            self.db.execute("begin")
            try:
                yield self.db
            finally:
                self.db.execute("commit")

    def _all(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self.db.execute(sql, params).fetchall()

    # -- articles
    @staticmethod
    def _collection(db: sqlite3.Connection, id: str) -> str:
        row = db.execute("select collection from articles where id = ?", (id,)).fetchone()
        if row is None:
            raise StoreError(f"article {id} does not exist: create its identity first")
        return row[0]

    @staticmethod
    def _changed(db: sqlite3.Connection, kind: str, id: str) -> int:
        """Note that `id` of `kind` changed, for `changed_since`, and give the change's place in the store's history."""
        return db.execute("insert into changes (kind, id) values (?, ?)", (kind, id)).lastrowid or 0

    def create(self, identity: Identity) -> None:
        check_id(identity.id)
        identity = self.titled(identity)
        with self._tx() as db:
            if db.execute("select 1 from articles where id = ?", (identity.id,)).fetchone():
                raise StoreError(f"article {identity.id} exists already: write its parts or set its fields")
            db.execute("insert into articles values (?,?,?,?)", (identity.id, identity.collection,
                                                                 identity.title.lower(), json.dumps(asdict(identity))))
            self._index_keys(db, identity)
            self._changed(db, "article", identity.id)

    def set_identity(self, identity: Identity) -> None:
        identity = self.titled(identity)
        with self._tx() as db:
            collection = self._collection(db, identity.id)
            if collection != identity.collection:
                raise StoreError(f"article {identity.id} is in {collection}, and its collection cannot change: "
                                 "create an article in the other collection")
            db.execute("update articles set title = ?, body = ? where id = ?",
                       (identity.title.lower(), json.dumps(asdict(identity)), identity.id))
            self._index_keys(db, identity)
            self._changed(db, "article", identity.id)

    @staticmethod
    def _index_keys(db: sqlite3.Connection, identity: Identity) -> None:
        db.execute("delete from keys where article_id = ?", (identity.id,))
        for key, values in identity.keys.items():
            db.executemany("insert into keys values (?,?,?)", [(identity.id, key, str(v).lower()) for v in values])

    def write_compilation(self, compilation: Compilation) -> list[str]:
        compilation, changed, noted = self.landed(compilation)
        with self._tx() as db:
            self._collection(db, compilation.article)
            found = db.execute("select position from compilations where article_id = ? and heading = ?",
                               (compilation.article, compilation.heading)).fetchone()
            last = db.execute("select coalesce(max(position), -1) from compilations where article_id = ?",
                              (compilation.article,)).fetchone()[0]
            db.execute("insert or replace into compilations values (?,?,?,?)", (
                compilation.article, compilation.heading, found[0] if found else last + 1,
                json.dumps(asdict(compilation))))
            self._changed(db, "article", compilation.article)
        return [*changed, *noted]

    def delete_compilation(self, article: str, heading: str) -> None:
        with self._tx() as db:
            if db.execute("delete from compilations where article_id = ? and heading = ?", (article, heading)).rowcount:
                self._changed(db, "article", article)

    def set_field(self, article: str, name: str, prop: Property) -> None:
        check_value(article, name, prop.value)
        name, prop = self.valued(name, prop)
        body = {"value": prop.value, "by": asdict(prop.by), "at": utc(prop.at, article, name), "cites": prop.cites}
        with self._tx() as db:
            collection = self._collection(db, article)
            db.execute("insert or replace into properties values (?,?,?)", (article, name, json.dumps(body)))
            self._reindex(db, collection, article, name)
            self._changed(db, "article", article)

    def drop_field(self, article: str, name: str) -> None:
        with self._tx() as db:
            collection = self._collection(db, article)
            if db.execute("delete from properties where article_id = ? and field = ?", (article, name)).rowcount:
                self._reindex(db, collection, article, name)
                self._changed(db, "article", article)

    @staticmethod
    def _reindex(db: sqlite3.Connection, collection: str, article: str, field: str) -> None:
        """Index a field's value: one row per item that compares. A written value of a protected field never stands,
        as `article.stands` says, so it is not indexed and a query reads the system's value of it."""
        db.execute("delete from property_values where article_id = ? and field = ?", (article, field))
        value = None
        row = db.execute("select body from properties where article_id = ? and field = ?", (article, field)).fetchone()
        if row is not None:
            value = json.loads(row[0])["value"]
            if not stands(field):
                value = None
        for item in value if isinstance(value, list) else [value]:
            if (scalar := _scalar(item)) is not None:
                num, txt = (scalar[1], None) if scalar[0] == "n" else (None, scalar[1])
                db.execute("insert into property_values values (?,?,?,?,?)", (collection, article, field, num, txt))

    @staticmethod
    def _assemble(db: sqlite3.Connection, id: str) -> Article | None:
        row = db.execute("select body from articles where id = ?", (id,)).fetchone()
        if row is None:
            return None
        compilations = db.execute("select body from compilations where article_id = ? order by position", (id,)).fetchall()
        props = db.execute("select field, body from properties where article_id = ? order by field", (id,)).fetchall()
        return Article(identity_from(json.loads(row[0])), [compilation_from(json.loads(b)) for (b,) in compilations],
                       {name: property_from(json.loads(body)) for name, body in props})

    def article(self, id: str) -> Article | None:
        with self._snapshot() as db:
            return self._assemble(db, id)

    @staticmethod
    def _predicate(collection: str, where: Where) -> tuple[str, list[Any]]:
        """A predicate as a lookup on `values_num` or `values_txt`, or on both for an `in` that mixes the two."""
        wants = where.value if where.op == "in" and isinstance(where.value, list) else [where.value]
        op = "=" if where.op == "in" else where.op
        by_column: dict[str, list[Any]] = {}
        for want in wants:
            if (scalar := _scalar(want)) is not None:
                by_column.setdefault("num" if scalar[0] == "n" else "txt", []).append(scalar[1])
        if not by_column:
            return " and 0", []
        lookups, params = [], []
        for column, values in by_column.items():
            test = f"{column} {op} ?" if len(values) == 1 else f"{column} in ({','.join('?' * len(values))})"
            lookups.append(f"select article_id from property_values where collection = ? and field = ? and {test}")
            params += [collection, where.field, *values]
        return f" and a.id in ({' union '.join(lookups)})", params

    def query(self, collection: str, where: list[Where] | None = None, *, order_by: str | None = None,
              descending: bool = False, limit: int | None = None, offset: int = 0) -> list[Article]:
        check_where(where or [])
        sql, params = "select a.id from articles a", []
        if order_by is not None:
            # A list orders by its least item, a number before any text: the key `order_key` gives.
            sql += (" left join (select article_id, min(num) as n, min(txt) as t from property_values"
                    " where collection = ? and field = ? group by article_id) o on o.article_id = a.id")
            params += [collection, order_by]
        sql += " where a.collection = ?"
        params.append(collection)
        for predicate in where or []:
            clause, more = self._predicate(collection, predicate)
            sql += clause
            params += more
        if order_by is not None:
            way = " desc" if descending else ""
            sql += (f" order by (o.n is null and o.t is null), (o.n is null){way}, coalesce(o.n, o.t){way}, a.id")
        else:
            sql += " order by a.id"
        sql += " limit ? offset ?"
        params += [-1 if limit is None else limit, offset]
        with self._snapshot() as db:
            found = [self._assemble(db, id) for (id,) in db.execute(sql, params).fetchall()]
        return [a for a in found if a is not None]

    def bound(self, key: str, value: str) -> list[str]:
        return [id for (id,) in self._all("select distinct article_id from keys where key = ? and value = ? "
                                          "order by article_id", (key, str(value).lower()))]

    def named(self, name: str) -> list[str]:
        return [id for (id,) in self._all("select id from articles where title = ? order by id", (name.lower(),))]

    def keyed(self, value: str) -> list[str]:
        return [id for (id,) in self._all("select distinct article_id from keys where value = ? order by article_id",
                                          (str(value).lower(),))]

    def collections(self) -> list[str]:
        return [c for (c,) in self._all("select distinct collection from articles order by collection")]

    def linking_to(self, field: str, ids: list[str]) -> list[Identity]:
        wanted = sorted({str(id).lower() for id in ids})
        rows = self._all("select distinct a.id, a.body from property_values v join articles a on a.id = v.article_id"
                         f" where v.field = ? and v.txt in ({','.join('?' * len(wanted))}) order by a.id",
                         [field, *wanted]) if wanted else []
        return [identity_from(json.loads(body)) for _, body in rows]

    # -- first-party records
    def keep(self, record: Record, *, unique: Sequence[str] = ()) -> Record:
        self.masked(record)
        with self._tx() as db:
            if unique:
                made_unique(record, unique, [json.loads(body).get("fields") or {} for (body,) in db.execute(
                    "select body from records where source = ? and id != ?", (record.source, record.id))])
            seq = self._changed(db, "record", record.id)
            db.execute("insert or replace into records values (?,?,?,?)",
                       (record.id, record.source, seq, json.dumps(asdict(record), default=str)))
        return record

    def record(self, id: str) -> Record | None:
        row = self._all("select body from records where id = ?", (id,))
        return record_from(json.loads(row[0][0])) if row else None

    def records(self, source: str) -> list[Record]:
        return [record_from(json.loads(body)) for (body,) in
                self._all("select body from records where source = ? order by seq", (source,))]

    # -- logs and state
    def append(self, log: str, data: dict[str, Any]) -> LogRow:
        check_value(f"log {log}", "row", data)
        data = scrubbed(data, self.scrubbing())
        with self._tx() as db:
            seq = db.execute("insert into logs (log, body) values (?,?)", (log, json.dumps(data))).lastrowid
            self._changed(db, "log", log)
        return LogRow(log, seq or 0, data)

    def rows(self, log: str, where: dict[str, Any] | None = None, *, since: int = 0,
             limit: int | None = None) -> list[LogRow]:
        found: list[LogRow] = []
        if limit is not None and limit <= 0:
            return found
        with self._lock:
            for seq, body in self.db.execute("select seq, body from logs where log = ? and seq > ? order by seq",
                                             (log, since)):
                data = json.loads(body)
                if all(compares(data.get(k), "=", v) for k, v in (where or {}).items()):
                    found.append(LogRow(log, seq, data))
                    if len(found) == limit:
                        break
        return found

    def latest(self, log: str, key: str = "id", where: dict[str, Any] | None = None) -> list[LogRow]:
        found = []
        path = "$." + json.dumps(key)
        with self._lock:
            for seq, body in self.db.execute(
                    "select seq, body from logs where seq in (select max(seq) from logs where log = ? and "
                    "json_extract(body, ?) is not null group by json_extract(body, ?)) order by seq",
                    (log, path, path)):
                data = json.loads(body)
                if all(compares(data.get(k), "=", v) for k, v in (where or {}).items()):
                    found.append(LogRow(log, seq, data))
        return found

    def total(self, log: str, field: str, where: dict[str, Any] | None = None) -> float:
        sql, params = "select json_extract(body, ?), body from logs where log = ?", ["$." + json.dumps(field), log]
        for key, value in (where or {}).items():  # narrowed in SQL, then checked by the store's own rule
            if isinstance(value, str):
                sql += " and lower(json_extract(body, ?)) = ?"
                params += ["$." + json.dumps(key), value.lower()]
        total = 0.0
        with self._lock:
            for amount, body in self.db.execute(sql, params):
                if isinstance(amount, (int, float)) and not isinstance(amount, bool) and \
                        all(compares(json.loads(body).get(k), "=", v) for k, v in (where or {}).items()):
                    total += amount
        return total

    def state(self, key: str) -> Any:
        row = self._all("select value from state where key = ?", (key,))
        return json.loads(row[0][0]) if row else None

    def set_state(self, key: str, value: Any) -> None:
        check_value(f"state {key}", "value", value)
        with self._tx() as db:
            db.execute("insert or replace into state values (?,?)", (key, json.dumps(value)))
            self._changed(db, "state", key)

    # -- links
    def link(self, fact_id: str, article_ids: list[str], at: str = "", how: str = SUBJECT) -> None:
        check_tie(how)
        learned = utc(at, fact_id, "learned") if at else ""
        with self._tx() as db:
            for article_id in article_ids:
                first = db.execute("select at from links where fact_id = ? and article_id = ? order by rowid limit 1",
                                   (fact_id, article_id)).fetchone()
                db.execute("insert or ignore into links values (?,?,?,?)",
                           (fact_id, article_id, how, learned if first is None else first[0]))
                if how != UNKNOWN:
                    db.execute("delete from links where fact_id = ? and article_id = ? and how = ?",
                               (fact_id, article_id, UNKNOWN))
                if first is None:
                    self._changed(db, "article", article_id)

    def unlink(self, fact_id: str, article_ids: list[str], how: str | None = None) -> None:
        with self._tx() as db:
            for article_id in article_ids:
                if db.execute("delete from links where fact_id = ? and article_id = ? and (? is null or how = ?)",
                              (fact_id, article_id, how, how)).rowcount:
                    self._changed(db, "article", article_id)

    def articles_of(self, fact_id: str) -> list[str]:
        return [id for (id,) in self._all("select article_id from links where fact_id = ? group by article_id "
                                          "order by min(rowid)", (fact_id,))]

    def ties_of(self, fact_id: str) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for article_id, how in self._all("select article_id, how from links where fact_id = ? order by rowid",
                                         (fact_id,)):
            found.setdefault(article_id, []).append(how)
        return {id: sorted(hows, key=TIES.index) for id, hows in found.items()}

    def unknown_ties(self) -> int:
        return self._all("select count(*) from links where how = ?", (UNKNOWN,))[0][0]

    def facts_of(self, article_id: str, since: str | None = None) -> list[str]:
        if since is not None:
            return [id for (id,) in self._all("select fact_id from links where article_id = ? and at != '' and at >= ? "
                                              "group by fact_id order by min(rowid)",
                                              (article_id, utc(since, article_id, "since")))]
        return [id for (id,) in self._all("select fact_id from links where article_id = ? group by fact_id "
                                          "order by min(rowid)", (article_id,))]

    # -- earlier saves
    def supersede(self, source: str, record: str, revision: str, at: str, facts: list[dict[str, Any]],
                  keep: int) -> int:
        scrub = self.scrubbing()
        with self._tx() as db:
            save = db.execute("select coalesce(max(save), 0) + 1 from superseded").fetchone()[0]
            for fact in facts:
                db.execute("insert or ignore into superseded (save, source, record, revision, at, fact_id, status, "
                           "body) values (?,?,?,?,?,?,?,?)", (save, source, record, revision, at, fact["id"],
                                                              "superseded", json.dumps(scrubbed(fact, scrub))))
            saves = [s for (s,) in db.execute("select distinct save from superseded where source = ? and record = ? "
                                              "order by save desc", (source, record))]
            old = saves[max(keep, 0):]
            if not old:
                return 0
            marks = ",".join("?" * len(old))
            return db.execute(f"delete from superseded where source = ? and record = ? and save in ({marks})",
                              (source, record, *old)).rowcount

    def superseded(self, *, source: str | None = None, record: str | None = None,
                   ids: list[str] | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        for column, value in (("source", source), ("record", record)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if ids is not None:
            if not ids:
                return []
            where.append(f"fact_id in ({','.join('?' * len(ids))})")
            params += ids
        rows = self._all("select source, record, revision, at, status, body from superseded"
                         + (" where " + " and ".join(where) if where else "") + " order by save desc, seq", params)
        return [{"source": s, "record": r, "revision": v, "at": a, "status": st, "fact": json.loads(b)}
                for s, r, v, a, st, b in rows]

    def retract_superseded(self, ids: list[str]) -> int:
        if not ids:
            return 0
        with self._tx() as db:
            return db.execute(f"update superseded set status = 'retracted' where status = 'superseded' and fact_id in "
                              f"({','.join('?' * len(ids))})", ids).rowcount

    # -- history
    def cursor(self) -> int:
        return int(self._all("select coalesce(max(seq), 0) from changes")[0][0])

    def changed_since(self, cursor: int) -> tuple[list[tuple[str, str]], int]:
        with self._snapshot() as db:  # the cursor it gives is the last change it read, never a later one
            rows = db.execute("select kind, id from changes where seq > ? group by kind, id order by max(seq)",
                              (cursor,)).fetchall()
            later = db.execute("select coalesce(max(seq), 0) from changes").fetchone()[0]
        return [(kind, id) for kind, id in rows], max(int(later), cursor)


# The search index's tables, made when an index first opens a store, beside the store's own. A row's text sits in
# `search_text`, under the row's `seq`, so a search is one BM25 match joined to the row's id and attributes.
SEARCH_SCHEMA = """
create table if not exists search_rows (seq integer primary key autoincrement, namespace text not null,
    id text not null, attributes text not null, unique (namespace, id));
create virtual table if not exists search_text using fts5(text, tokenize = 'porter unicode61 remove_diacritics 2');
create table if not exists search_synced (namespace text primary key, cursor integer not null)
"""


class SQLiteIndex(DurableIndex):
    """The search index in a SQLite store's own file, ranked by BM25, or in memory of its own with no store.

    A search matches any of its words, a word by its stem, so `moved` finds `moves`, and ranks a row that holds
    more of them, and rarer ones, first. The cursor `sync` last reached sits in the same file as the rows, so the
    two never disagree.
    """

    def __init__(self, store: SQLiteStore | None = None):
        self.store = store or SQLiteStore()
        self.store.ensure(SEARCH_SCHEMA)

    @staticmethod
    def _drop(db: sqlite3.Connection, namespace: str, ids: list[str]) -> None:
        for id in ids:
            row = db.execute("select seq from search_rows where namespace = ? and id = ?", (namespace, id)).fetchone()
            if row is not None:
                db.execute("delete from search_text where rowid = ?", row)
                db.execute("delete from search_rows where seq = ?", row)

    def upsert(self, namespace: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            check_value(f"index {namespace}", str(row.get("id")), row)
        with self.store._tx() as db:
            self._drop(db, namespace, [str(row["id"]) for row in rows])
            for row in rows:
                attributes = {k: v for k, v in row.items() if k not in ("id", "text")}
                seq = db.execute("insert into search_rows (namespace, id, attributes) values (?,?,?)",
                                 (namespace, str(row["id"]), json.dumps(attributes))).lastrowid
                db.execute("insert into search_text (rowid, text) values (?,?)", (seq, str(row.get("text", ""))))

    def delete(self, namespace: str, ids: list[str]) -> None:
        with self.store._tx() as db:
            self._drop(db, namespace, ids)

    def search(self, namespace: str, text: str, top_k: int, filters: dict[str, Any] | None = None) -> list[Hit]:
        words = list(dict.fromkeys("".join(c if c.isalnum() else " " for c in text.lower()).split()))
        if not words or top_k <= 0:
            return []
        found: list[Hit] = []
        with self.store._lock:
            for id, attributes, score in self.store.db.execute(
                    "select r.id, r.attributes, -bm25(search_text) from search_text join search_rows r "
                    "on r.seq = search_text.rowid where search_text match ? and r.namespace = ? "
                    "order by bm25(search_text), r.id", (" OR ".join(f'"{w}"' for w in words), namespace)):
                kept = json.loads(attributes)
                if all(compares(kept.get(key), "=", want) for key, want in (filters or {}).items()):
                    found.append(Hit(id, score, kept))
                    if len(found) == top_k:
                        break
        return found

    def ids(self, namespace: str, filters: dict[str, Any]) -> list[str]:
        sql, params = "select id, attributes from search_rows where namespace = ?", [namespace]
        for key, value in filters.items():  # narrowed in SQL, then checked by the store's own rule
            if isinstance(value, str):
                sql += " and (json_type(attributes, ?) = 'array' or lower(json_extract(attributes, ?)) = ?)"
                params += ["$." + json.dumps(key)] * 2 + [value.lower()]
        rows = self.store._all(sql + " order by seq", params)
        return [id for id, attributes in rows
                if all(compares(json.loads(attributes).get(k), "=", v) for k, v in filters.items())]

    def synced(self, namespace: str) -> int:
        row = self.store._all("select cursor from search_synced where namespace = ?", (namespace,))
        return int(row[0][0]) if row else 0

    def mark(self, namespace: str, cursor: int) -> None:
        with self.store._tx() as db:  # never back: a slower sync that read less does not undo a faster one
            db.execute("insert into search_synced values (?, ?) on conflict (namespace) do update set "
                       "cursor = max(cursor, excluded.cursor)", (namespace, cursor))
