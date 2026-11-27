"""In-memory fakes, one for each seam, for tests and for trying a workspace with no outside system. Each class's
docstring names the contract it stands in for.

Each fake passes its contract's conformance suite. None of them reaches the network or calls a model.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import re
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .agent import Agent, Budget
from .apps import App, ToolError, evidence, mcp_tool
from .article import Article, Compilation, Identity, Property, fields
from .caller import Caller
from .connector import Connector, CursorError, Listing, Record, RecordNotFound, gone
from .evidence import Evidence
from .facts import Extracted, Extraction, Fact, FactNotFound, Mention, NoBody, Quote
from .models import Answer, Choice, Decider, Decision, DecisionError, Question, Score, TokenCount, YesNo
from .run import AgentRun, Entry, Rewriter, Run, Usage
from .search import Hit, Index
from .secrets import Secrets, _check_value, _not_found, changed, check_name
from .store import (
    SUBJECT,
    TIES,
    LogRow,
    Store,
    StoreError,
    Where,
    check_id,
    check_tie,
    check_value,
    check_where,
    compares,
    made_unique,
    order_key,
    scrubbed,
    utc,
)
from .workspace import Workspace

__all__ = ["FakeChanges", "FakeConnector", "FakeDecider", "FakeExtraction", "FakeRewriter", "FakeSecrets", "MemoryIndex", "MemoryStore", "ScriptedRun",
           "notes_app"]


class FakeConnector(Connector):
    """Stands in for `connector.Connector`, a source. It holds its records in memory. It logs each change, and its cursor is how far into that log a
    listing read. A listing gives at most `page_size` records."""

    def __init__(self, name: str, records: list[Record], page_size: int = 1000):
        self.name = name
        self.page_size = page_size
        self.log: list[tuple[str, str]] = []
        self.records: dict[str, Record] = {}
        for record in records:
            self.add(record)

    def add(self, record: Record) -> str:
        """Add a record, or a new revision of one, as the source changing would. Gives its id."""
        self.records[record.id] = deepcopy(record)
        self.log.append(("add", record.id))
        return record.id

    def remove(self, id: str) -> None:
        """Delete a record, as the source deleting it would."""
        if self.records.pop(id, None) is not None:
            self.log.append(("remove", id))

    def list(self, since: str | None = None) -> Listing:
        start = int(since or 0) if (since or "0").isdigit() else -1
        if not 0 <= start <= len(self.log):
            raise CursorError(f"{self.name}: the cursor {since} is not one this source gave: list with no cursor")
        changed: list[str] = []
        removed: list[str] = []
        end = start
        while end < len(self.log) and len(changed) < self.page_size:
            what, id = self.log[end]
            end += 1
            for held in (changed, removed):
                if id in held:
                    held.remove(id)
            if what == "remove":
                removed.append(id)
            elif id in self.records:
                changed.append(id)
        listed = [deepcopy(self.records[id]) for id in changed]
        for record in listed:
            record.body = None
        return Listing(listed, str(end), removed, end < len(self.log))

    def get(self, id: str) -> Record:
        if id not in self.records:
            raise RecordNotFound(f"{self.name} has no record {id}: list the source for the ids it holds")
        return deepcopy(self.records[id])


class FakeChanges(Connector):
    """Stands in for `connector.Connector`, a source that lists changes, as `lists_changes` says. It stamps each
    record with its own time, `at`, which a test moves and the workspace's clock never touches. A listing from a cursor
    set for changes asks for what was stamped since the latest stamp the cursor saw, less `OVERLAP`, and names as
    removed only what the source says it deleted since; a listing in full reads every record in the window and asks
    after each one it no longer finds. `forget` makes the source refuse every cursor it gave, as an expired sync
    token is. `asked` says how each listing read: "full" or "changes"."""

    lists_changes = True
    OVERLAP = 300  # seconds a listing of changes reaches back before the latest stamp it saw

    def __init__(self, name: str, records: list[Record], at: str = "2026-11-01T09:00:00+00:00"):
        self.name, self.at = name, datetime.fromisoformat(at)
        self.records: dict[str, Record] = {}
        self.stamps: dict[str, datetime] = {}
        self.deleted: dict[str, datetime] = {}  # what the source says it deleted, and when
        self.away: set[str] = set()  # records that left the window and are still at the source
        self.epoch = 0
        self.asked: list[str] = []
        for record in records:
            self.add(record)

    def add(self, record: Record) -> str:
        """Add a record, or a new revision of one, stamped with the source's time now. Gives its id."""
        self.records[record.id], self.stamps[record.id] = deepcopy(record), self.at
        self.deleted.pop(record.id, None)
        return record.id

    def remove(self, id: str, *, says: bool = True) -> None:
        """Delete a record. With `says`, the source reports the delete to a listing of changes, as a recycle bin
        does; without, only a listing in full finds it gone."""
        if self.records.pop(id, None) is not None and says:
            self.deleted[id] = self.at

    def forget(self) -> None:
        """Refuse every cursor given so far, as a source that forgot them does."""
        self.epoch += 1

    def _state(self, cursor: str) -> dict[str, Any]:
        try:
            state = json.loads(cursor)
            if not isinstance(state, dict) or not isinstance(state.get("seen"), dict):
                raise TypeError
        except (ValueError, TypeError):
            raise CursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with no "
                              "cursor") from None
        if state.get("epoch") != self.epoch:
            raise CursorError(f"{self.name}: the source no longer knows the cursor it gave: list with no cursor")
        return state

    def cursor_for(self, cursor: str | None, *, full: bool) -> str | None:
        return None if cursor is None else json.dumps({**self._state(cursor), "full": full}, sort_keys=True)

    def list(self, since: str | None = None) -> Listing:
        state = self._state(since) if since is not None else {"seen": {}, "mark": None, "full": True}
        full, seen, mark = state.get("full", False), dict(state["seen"]), state.get("mark")
        self.asked.append("full" if full else "changes")
        floor = datetime.fromisoformat(mark) - timedelta(seconds=self.OVERLAP) if mark else None
        present = [id for id in sorted(self.records) if id not in self.away]
        if not full:
            present = [id for id in present if floor is None or self.stamps[id] >= floor]
        listed = [deepcopy(self.records[id]) for id in present if seen.get(id) != self.records[id].revision]
        if full:
            removed = gone([id for id in seen if id not in present], lambda id: id in self.records)
            seen = {id: seen[id] for id in seen if id in present}
        else:
            removed = sorted(id for id, when in self.deleted.items() if id in seen and (floor is None or when >= floor))
        for id in removed:
            seen.pop(id, None)
        for record in listed:
            record.body = None
            seen[record.id] = record.revision
        stamps = [self.stamps[id] for id in present] + [self.deleted[id] for id in removed if id in self.deleted]
        if stamps:
            mark = max([*stamps, *([datetime.fromisoformat(mark)] if mark else [])]).isoformat()
        cursor = json.dumps({"epoch": self.epoch, "seen": seen, "mark": mark, "full": False}, sort_keys=True)
        return Listing(listed, cursor, removed)

    def get(self, id: str) -> Record:
        if id not in self.records:
            raise RecordNotFound(f"{self.name} has no record {id}: list the source for the ids it holds")
        return deepcopy(self.records[id])


def _one_at_a_time[StoreClass: type[Store]](cls: StoreClass) -> StoreClass:
    """Every public method of `cls` takes the lock its `exclusive` holds, so a call from another thread waits for an
    exclusive block to end, as on the real store. Else a write lands while `exclusive` copies the store."""
    def held(method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def call(self: Any, *args: Any, **kwargs: Any) -> Any:
            with self._exclusive:
                return method(self, *args, **kwargs)
        return call

    for name, method in list(vars(cls).items()):
        if inspect.isfunction(method) and not name.startswith("_") and name != "exclusive":
            setattr(cls, name, held(method))
    return cls


@_one_at_a_time
class MemoryStore(Store):
    """Stands in for `store.Store`: a store in a few dicts and lists."""

    def __init__(self) -> None:
        self.identities: dict[str, Identity] = {}
        self.compilations: dict[str, list[Compilation]] = {}
        self.props: dict[str, dict[str, Property]] = {}
        self.kept: dict[str, Record] = {}
        self.logs: list[LogRow] = []
        self.values: dict[str, Any] = {}
        self.links: list[tuple[str, str, str]] = []
        self.learned: dict[tuple[str, str], str] = {}
        self.history: list[tuple[str, str]] = []
        self.earlier: list[dict[str, Any]] = []
        self._exclusive = threading.RLock()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._exclusive:
            kept = {k: deepcopy(v) for k, v in vars(self).items() if k not in ("_exclusive", "scrub", "masks")}
            try:
                yield
            except BaseException:
                vars(self).update(kept)
                raise

    def _known(self, id: str) -> None:
        if id not in self.identities:
            raise StoreError(f"article {id} does not exist: create its identity first")

    def create(self, identity: Identity) -> None:
        check_id(identity.id)
        if identity.id in self.identities:
            raise StoreError(f"article {identity.id} exists already: write its parts or set its fields")
        self.identities[identity.id] = deepcopy(self.titled(identity))
        self.history.append(("article", identity.id))

    def set_identity(self, identity: Identity) -> None:
        self._known(identity.id)
        if self.identities[identity.id].collection != identity.collection:
            raise StoreError(f"article {identity.id} cannot change collection: create one in the other collection")
        self.identities[identity.id] = deepcopy(self.titled(identity))
        self.history.append(("article", identity.id))

    def write_compilation(self, compilation: Compilation) -> list[str]:
        compilation, changed, noted = self.landed(compilation)
        self._known(compilation.article)
        compilations = self.compilations.setdefault(compilation.article, [])
        found = [n for n, s in enumerate(compilations) if s.heading == compilation.heading]
        if found:
            compilations[found[0]] = deepcopy(compilation)
        else:
            compilations.append(deepcopy(compilation))
        self.history.append(("article", compilation.article))
        return [*changed, *noted]

    def delete_compilation(self, article: str, heading: str) -> None:
        compilations = self.compilations.get(article, [])
        kept = [s for s in compilations if s.heading != heading]
        if len(kept) != len(compilations):
            self.compilations[article] = kept
            self.history.append(("article", article))

    def drop_field(self, article: str, name: str) -> None:
        self._known(article)
        if self.props.get(article, {}).pop(name, None) is not None:
            self.history.append(("article", article))

    def set_field(self, article: str, name: str, prop: Property) -> None:
        self._known(article)
        check_value(article, name, prop.value)
        name, prop = self.valued(name, prop)
        self.props.setdefault(article, {})[name] = Property(deepcopy(prop.value), prop.by, utc(prop.at, article, name),
                                                            list(prop.cites))
        self.history.append(("article", article))

    def article(self, id: str) -> Article | None:
        if id not in self.identities:
            return None
        return deepcopy(Article(self.identities[id], self.compilations.get(id, []),
                                dict(sorted(self.props.get(id, {}).items()))))  # as the real store reads them

    def query(self, collection: str, where: list[Where] | None = None, *, order_by: str | None = None,
              descending: bool = False, limit: int | None = None, offset: int = 0) -> list[Article]:
        check_where(where or [])
        found = []
        for id in sorted(self.identities):
            article = self.article(id)
            if article is None or article.identity.collection != collection:
                continue
            values = {name: prop.value for name, prop in fields(article).items()}  # as the real store indexes
            if all(p.field in values and compares(values[p.field], p.op, p.value) for p in where or []):
                found.append(article)
        if order_by is not None:
            def key(article: Article) -> tuple[int, Any]:
                prop = fields(article).get(order_by)
                return order_key(prop.value if prop else None)

            present = sorted((a for a in found if key(a)[0] < 2), key=key, reverse=descending)
            found = present + [a for a in found if key(a)[0] == 2]
        return found[offset:None if limit is None else offset + limit]

    def bound(self, key: str, value: str) -> list[str]:
        return sorted(id for id, who in self.identities.items()
                      if str(value).lower() in [str(v).lower() for v in who.keys.get(key, [])])

    def keyed(self, value: str) -> list[str]:
        return sorted(id for id, who in self.identities.items()
                      if any(str(value).lower() in [str(v).lower() for v in vs] for vs in who.keys.values()))

    def collections(self) -> list[str]:
        return sorted({who.collection for who in self.identities.values()})

    def linking_to(self, field: str, ids: list[str]) -> list[Identity]:
        wanted = {str(id).lower() for id in ids}
        return [deepcopy(self.identities[id]) for id in sorted(self.identities)
                if (prop := fields(self.article(id)).get(field)) is not None  # type: ignore[arg-type]
                and any(isinstance(v, str) and v.lower() in wanted
                        for v in (prop.value if isinstance(prop.value, list) else [prop.value]))]

    def named(self, name: str) -> list[str]:
        return sorted(id for id, who in self.identities.items() if who.title.lower() == name.lower())

    def keep(self, record: Record, *, unique: Sequence[str] = ()) -> Record:
        self.masked(record)
        made_unique(record, unique, [r.fields for r in self.kept.values()
                                     if r.source == record.source and r.id != record.id])
        self.kept.pop(record.id, None)
        self.kept[record.id] = deepcopy(record)
        self.history.append(("record", record.id))
        return record

    def record(self, id: str) -> Record | None:
        return deepcopy(self.kept.get(id))

    def records(self, source: str) -> list[Record]:
        return [deepcopy(r) for r in self.kept.values() if r.source == source]

    def append(self, log: str, data: dict[str, Any]) -> LogRow:
        check_value(f"log {log}", "row", data)
        self.history.append(("log", log))
        row = LogRow(log, len(self.history), deepcopy(scrubbed(data, self.scrubbing())))
        self.logs.append(row)
        return deepcopy(row)

    def rows(self, log: str, where: dict[str, Any] | None = None, *, since: int = 0,
             limit: int | None = None) -> list[LogRow]:
        found = [deepcopy(r) for r in self.logs if r.log == log and r.seq > since
                 and all(compares(r.data.get(k), "=", v) for k, v in (where or {}).items())]
        return found if limit is None else found[:limit]

    def latest(self, log: str, key: str = "id", where: dict[str, Any] | None = None) -> list[LogRow]:
        newest: dict[str, LogRow] = {}
        for row in self.logs:
            if row.log == log and row.data.get(key) is not None:
                newest.pop(json.dumps(row.data[key]), None)
                newest[json.dumps(row.data[key])] = row
        return [deepcopy(r) for r in sorted(newest.values(), key=lambda r: r.seq)
                if all(compares(r.data.get(k), "=", v) for k, v in (where or {}).items())]

    def total(self, log: str, field: str, where: dict[str, Any] | None = None) -> float:
        return float(sum(row.data[field] for row in self.rows(log, where)
                         if isinstance(row.data.get(field), (int, float)) and not isinstance(row.data[field], bool)))

    def state(self, key: str) -> Any:
        return deepcopy(self.values.get(key))

    def set_state(self, key: str, value: Any) -> None:
        check_value(f"state {key}", "value", value)
        self.values[key] = deepcopy(value)
        self.history.append(("state", key))

    def link(self, fact_id: str, article_ids: list[str], at: str = "", how: str = SUBJECT) -> None:
        check_tie(how)
        learned = utc(at, fact_id, "learned") if at else ""
        for article_id in article_ids:
            first = (fact_id, article_id) not in self.learned
            if (fact_id, article_id, how) not in self.links:
                self.links.append((fact_id, article_id, how))
            if first:
                self.learned[(fact_id, article_id)] = learned
                self.history.append(("article", article_id))

    def unlink(self, fact_id: str, article_ids: list[str], how: str | None = None) -> None:
        for article_id in article_ids:
            gone = [link for link in self.links if link[:2] == (fact_id, article_id) and how in (None, link[2])]
            for link in gone:
                self.links.remove(link)
            if not any(link[:2] == (fact_id, article_id) for link in self.links):
                self.learned.pop((fact_id, article_id), None)
            if gone:
                self.history.append(("article", article_id))

    def articles_of(self, fact_id: str) -> list[str]:
        return list(dict.fromkeys(article for fact, article, _ in self.links if fact == fact_id))

    def ties_of(self, fact_id: str) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for fact, article, how in self.links:
            if fact == fact_id:
                found.setdefault(article, []).append(how)
        return {id: sorted(hows, key=TIES.index) for id, hows in found.items()}

    def facts_of(self, article_id: str, since: str | None = None) -> list[str]:
        floor = utc(since, article_id, "since") if since is not None else None
        return list(dict.fromkeys(fact for fact, article, _ in self.links if article == article_id
                                  and (floor is None or "" < self.learned[(fact, article)] >= floor)))

    def supersede(self, source: str, record: str, revision: str, at: str, facts: list[dict[str, Any]],
                  keep: int) -> int:
        save, scrub = max((row["save"] for row in self.earlier), default=0) + 1, self.scrubbing()
        held = {row["fact"]["id"] for row in self.earlier}
        self.earlier += [{"save": save, "source": source, "record": record, "revision": revision, "at": at,
                          "status": "superseded", "fact": deepcopy(scrubbed(fact, scrub))}
                         for fact in facts if fact["id"] not in held]
        saves = sorted({row["save"] for row in self.earlier if (row["source"], row["record"]) == (source, record)},
                       reverse=True)
        old = set(saves[max(keep, 0):])
        kept = [row for row in self.earlier if (row["source"], row["record"]) != (source, record) or row["save"] not in old]
        dropped, self.earlier = len(self.earlier) - len(kept), kept
        return dropped

    def superseded(self, *, source: str | None = None, record: str | None = None,
                   ids: list[str] | None = None) -> list[dict[str, Any]]:
        found = [row for row in self.earlier if (source is None or row["source"] == source)
                 and (record is None or row["record"] == record) and (ids is None or row["fact"]["id"] in ids)]
        return [deepcopy({k: v for k, v in row.items() if k != "save"})
                for row in sorted(found, key=lambda row: -row["save"])]

    def retract_superseded(self, ids: list[str]) -> int:
        found = [row for row in self.earlier if row["fact"]["id"] in ids and row["status"] == "superseded"]
        for row in found:
            row["status"] = "retracted"
        return len(found)

    def cursor(self) -> int:
        return len(self.history)

    def changed_since(self, cursor: int) -> tuple[list[tuple[str, str]], int]:
        later = self.history[cursor:]
        return [pair for n, pair in enumerate(later) if pair not in later[n + 1:]], len(self.history)


def _locked(write: Callable[..., Any]) -> Callable[..., Any]:
    def held(self: MemoryStore, *args: Any, **kwargs: Any) -> Any:
        with self._exclusive:
            return write(self, *args, **kwargs)
    return held


# every write holds the lock `exclusive` holds, as the real store runs every write under one lock: a write from another
# thread then never lands while `exclusive` copies the dicts it would roll back to
for _write in (MemoryStore.create, MemoryStore.set_identity, MemoryStore.write_compilation, MemoryStore.delete_compilation,
               MemoryStore.set_field, MemoryStore.drop_field, MemoryStore.keep, MemoryStore.append, MemoryStore.set_state,
               MemoryStore.link, MemoryStore.unlink, MemoryStore.supersede, MemoryStore.retract_superseded):
    setattr(MemoryStore, _write.__name__, _locked(_write))


class MemoryIndex(Index):
    """Stands in for `search.Index`: it ranks by how many of the query's words a text holds."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict[str, Any]]] = {}

    def upsert(self, namespace: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self.rows.setdefault(namespace, {})[row["id"]] = deepcopy(row)

    def delete(self, namespace: str, ids: list[str]) -> None:
        for id in ids:
            self.rows.get(namespace, {}).pop(id, None)

    def search(self, namespace: str, text: str, top_k: int, filters: dict[str, Any] | None = None) -> list[Hit]:
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        scored = []
        for id, row in self.rows.get(namespace, {}).items():
            if not all(compares(row.get(key), "=", want) for key, want in (filters or {}).items()):
                continue
            score = len(words & set(re.findall(r"[a-z0-9]+", str(row.get("text", "")).lower())))
            if score:
                attributes = {k: v for k, v in row.items() if k not in ("id", "text")}
                scored.append(Hit(id, float(score), deepcopy(attributes)))
        return sorted(scored, key=lambda hit: (-hit.score, hit.id))[:top_k]


CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
HEADER = re.compile(r"(?:[A-Za-z][\w-]*: [^\n]*\n?)+")


def fact_id(seed: str) -> str:
    """An id in the shape Factoid gives a fact, `fact_` then 18 characters of Crockford base32, here from a hash of
    `seed` so it holds from run to run. A stand-in whose ids look like no real one's hides a change of shape."""
    value, chars = int(hashlib.sha1(seed.encode()).hexdigest(), 16), []
    for _ in range(18):
        value, rem = divmod(value, 32)
        chars.append(CROCKFORD[rem])
    return "fact_" + "".join(chars)


class FakeExtraction(Extraction):
    """Stands in for `facts.Extraction`, the facts layer. It makes one fact
    of each paragraph of a record, quoting it whole, but for a block of header
    lines such as `Date: 2026-11-15`. A fact's subject is the record's `subject` field, if it has one, and its time
    is the record's.

    `kinds` are the kinds of fact it gives, as a workspace's `facts/` folder would define them. With them, a paragraph
    takes the record's `kind` field if that is one of them, or else the first kind a word of whose name it holds, as
    `risk` for `risk_objection`, or else the first kind. Without them, a fact's kind is the record's `kind` field, or
    `note`, and it does not say its kinds ahead of time. With `searches_retracted`, its search still finds a retracted
    fact, as a facts layer whose index keeps its words may, so a caller must leave it out.

    With `names`, it gives mentions as Factoid does instead: each of those names a paragraph holds, the one it holds
    first as the fact's `subject` and each other as `about`, and no subject when the paragraph holds none of them."""

    def __init__(self, kinds: list[str] | None = None, *, searches_retracted: bool = False,
                 names: list[str] | None = None) -> None:
        self.searches_retracted = searches_retracted  # as an index that still holds a retracted fact's words
        self.by_record: dict[str, list[Fact]] = {}
        self.field_of: dict[str, str] = {}  # the field each fact of a record's fields says
        self._kinds = list(kinds or [])
        self._names = list(names or [])

    def _mentions(self, record: Record, paragraph: str) -> list[Mention]:
        if not self._names:
            return [Mention(str(record.fields["subject"]), "subject", dict(record.keys))] \
                if record.fields.get("subject") else []
        held = sorted((paragraph.find(name), name) for name in self._names if name in paragraph)
        return [Mention(name, "subject" if n == 0 else "about") for n, (_, name) in enumerate(held)]

    def _kind(self, record: Record, paragraph: str) -> str:
        given = str(record.fields.get("kind", ""))
        if not self._kinds:
            return given or "note"
        if given in self._kinds:
            return given
        words = set(re.findall(r"[a-z0-9]+", paragraph.lower()))
        return next((k for k in self._kinds if words & set(k.split("_"))), self._kinds[0])

    def kinds(self) -> list[str]:
        return list(self._kinds)

    def extract(self, record: Record) -> Extracted:
        if record.body is None:
            raise NoBody(f"record {record.id} has no body: fetch it whole with the connector's get")
        paragraphs = [p.strip() for p in record.body.split("\n\n") if p.strip() and not HEADER.fullmatch(p.strip())]
        facts = [Fact(id=fact_id(f"{record.id}:{n}:{p}"), text=p, subject="", kind=self._kind(record, p),
                      at=record.at, source=record.source, record=record.id, quotes=[Quote(p, at=record.at)],
                      mentions=self._mentions(record, p))
                 for n, p in enumerate(paragraphs)]
        for fact in facts:
            fact.subject = next((m.text for m in fact.mentions if m.role == "subject"), "")
        earlier = [f for f in self.by_record.pop(record.id, []) if f.id not in {g.id for g in facts}]
        for fact in earlier:
            fact.status = "retracted"  # a fact the new revision no longer gives stays readable by its id
        self.by_record[record.id] = facts + earlier
        return Extracted(deepcopy(facts), input_tokens=len(record.body) // 4, output_tokens=len(facts) * 20)

    def extract_fields(self, record: Record) -> Extracted:
        """One fact a field, as Factoid's structured document gives them: a value given again is the fact held, a
        changed one is a new fact, the old one retracted, and a field gone retracts its fact. Kept apart from the
        record's text, under `<id>#fields`."""
        from .facts import FIELD_KIND, field_rows, field_sentence

        held = self.by_record.setdefault(f"{record.id}#fields", [])
        made, rows = [], field_rows(record)
        learned = record.at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # with no time of its own, when learned
        for old in [f for f in held if f.status == "current" and self.field_of.get(f.id) not in {r["key"] for r in rows}]:
            old.status = "retracted"  # its field is gone from the record
        for row in rows:
            text = field_sentence(row)
            standing = [f for f in held if f.status == "current" and self.field_of.get(f.id) == row["key"]]
            if any(f.text == text for f in standing):
                continue
            for old in standing:
                old.status = "retracted"  # a changed value replaces the old fact
            fact = Fact(id=fact_id(f"{record.id}#fields:{row['key']}:{text}"), text=text, subject="",  # as Factoid's
                        kind=FIELD_KIND, at=learned, source=record.source, record=record.id,
                        quotes=[Quote(row["value"], at=learned)], field=row["key"])
            self.field_of[fact.id] = row["key"]
            held.append(fact)
            made.append(fact)
        return Extracted(deepcopy(made))

    def facts(self, *, ids: list[str] | None = None, source: str | None = None, record: str | None = None,
              since: str | None = None, kinds: list[str] | None = None, limit: int | None = None,
              latest: bool = True) -> list[Fact]:
        found = [f for facts in self.by_record.values() for f in facts
                 if (f.id in ids if ids is not None else f.status == "current") and (source is None or f.source == source)
                 and (record is None or f.record == record) and (since is None or f.at >= since)
                 and (kinds is None or f.kind in kinds)]
        found = sorted(found, key=lambda f: f.at, reverse=True)
        return deepcopy(found if limit is None else found[:limit])

    def retract(self, fact_id: str) -> None:
        found = [f for facts in self.by_record.values() for f in facts if f.id == fact_id]
        if not found:
            raise FactNotFound(f"there is no fact {fact_id}: query facts for their ids")
        found[0].status = "retracted"

    def search(self, text: str, top_k: int = 10, *, source: str | None = None) -> list[Hit]:
        index = MemoryIndex()
        held = [f for facts in self.by_record.values() for f in facts if source is None or f.source == source] \
            if self.searches_retracted else self.facts(source=source)
        index.upsert("facts", [{"id": f.id, "text": f.text} for f in held])
        return index.search("facts", text, top_k)


class ScriptedRun(AgentRun):
    """Stands in for `run.AgentRun`, the agent run, with no loop. It makes
    `calls`, each a tool's name and arguments, then hands in the text `write(prompt)` gives, or the parts of it as
    heading to text, joined as `run.joined` joins them. Empty text hands in nothing.
    `prompt` is what a model would read: the run's instructions, the agent's own words, then its evidence as data.
    Each call counts as one tool call of the budget, and one past it is not made. Times come from `clock`, and a
    prompt ejected under `root/prompts/` takes the place of the bundled one."""

    def __init__(self, write: Callable[[str], dict[str, str] | str],
                 calls: list[tuple[str, dict[str, Any]]] | None = None, clock: Callable[[], Any] | None = None,
                 root: Path | None = None):
        self.write = write
        self.calls = calls or []
        self.clock = clock
        self.root = root

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        from .clock import stamp, system_clock
        from .run import HAND_INS, NO_CALLS, WROTE_NOTHING, Result, answered, hand_in, instructions, joined, opening

        for tool in tools:
            if (name := getattr(tool, "__name__", "")) in HAND_INS:
                raise ToolError(f"a tool is named {name}, which is how a run hands in what it wrote: rename that tool")
        clock = self.clock or system_clock
        text = opening(agent, evidence, clock(), self.root)
        run = Run(id="run_scripted", agent=agent.id, target=agent.target, status="done",
                  transcript=[Entry("user", text)], usage=Usage(model="scripted", requests=1),
                  started_at=stamp(clock()))
        by_name = {tool.__name__: tool for tool in tools}
        for name, args in self.calls:
            run.transcript.append(Entry("call", tool=name, args=dict(args)))
            if run.usage.tool_calls >= budget.tool_calls:
                result: Any = NO_CALLS
            else:
                run.usage.tool_calls += 1
                result = answered(by_name[name], **args) if name in by_name else f"no tool named {name}"
            run.transcript.append(Entry("tool", str(result), tool=name))
        written = self.write(f"{instructions(agent, self.root)}\n\n{text}")
        handed = written if isinstance(written, str) else joined(agent, written)
        if handed.strip():
            run.result = Result(handed)
            run.transcript.append(Entry("call", tool=hand_in(agent), args={"text": handed}))
        elif agent.role != "curator":
            run.status, run.note = WROTE_NOTHING, "it said there is nothing to write: the stand-in wrote nothing"
        run.ended_at = stamp(clock())
        return run


def notes_app() -> App:
    """Stands in for an `apps.App`: a small app shaped like Notes. A `note` tool keeps a note as a first-party record, and `notes` evidence reads
    them back. Both reach the workspace through the `Workspace` handle."""

    @mcp_tool
    def note(text: str, caller: Caller, workspace: Workspace, collection: str = "notes") -> dict[str, str]:
        """Write a note into a collection. A note is the record a session leaves for the next one."""
        record = workspace.keep(caller, "note", text, {"collection": collection})
        return {"id": record.id, "collection": collection}

    @evidence("notes")
    def notes(workspace: Workspace, collection: str = "notes") -> list[dict[str, str]]:
        """The notes of one collection, oldest first."""
        return [{"id": r.id, "text": r.body or "", "source": "note"} for r in workspace.records("note")
                if r.fields.get("collection") == collection]

    return App(name="notes", kinds=["note"], functions=[note, notes])


class FakeDecider(Decider):
    """Stands in for `models.Decider`, the decision model. It answers from `answers`, by question name: a number for a yes or no (the probability of
    yes) or a score, and an option's name for a choice, each with full confidence. A question it was not told
    about gets yes, the first option, or the highest level. It keeps each `(state, questions)` it was asked.

    `strict` is for a test whose point is which questions were asked: a question it was not told about fails the
    call, and each call reports the tokens a real one would, an estimate of what it was sent, never zero."""

    def __init__(self, answers: dict[str, Any] | None = None, *, strict: bool = False):
        self.answers = dict(answers or {})
        self.strict = strict
        self.asked: list[tuple[Any, dict[str, Question]]] = []

    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        self.asked.append((deepcopy(state), dict(questions)))
        if self.strict and (unknown := sorted(set(questions) - set(self.answers))):
            raise DecisionError(f"the strict stand-in was asked {', '.join(unknown)}, which it was not told about: "
                                "tell it the answer, or ask less")
        out: dict[str, Answer] = {}
        for name, question in questions.items():
            given = self.answers.get(name)
            if isinstance(question, YesNo):
                out[name] = Answer(yes=1.0 if given is None else float(given))
            elif isinstance(question, Choice):
                pick = given if given is not None else question.options[0][0]
                out[name] = Answer(choice=pick, confidence=1.0,
                                   probabilities={k: float(k == pick) for k, _ in question.options})
            elif isinstance(question, Score):
                level = len(question.levels) - 1.0 if given is None else float(given)
                out[name] = Answer(score=level, confidence=1.0, probabilities={
                    str(i): float(i == round(level)) for i in range(len(question.levels))})
        if self.strict:
            sent = len(json.dumps(state, default=str)) + sum(len(str(q)) for q in questions.values())
            return Decision(out, model="fake", usage=TokenCount(input_tokens=max(1, sent // 4),
                                                                output_tokens=len(questions)))
        return Decision(out, model="fake")


class FakeRewriter(Rewriter):
    """Stands in for `run.Rewriter`, a writing model asked one request. It answers each request, read as JSON, with
    what `reply(request)` gives, as the model's text, and keeps every request it was asked."""

    def __init__(self, reply: Callable[[dict[str, Any]], str]):
        self.reply = reply
        self.asked: list[dict[str, Any]] = []

    def rewrite(self, system: str, request: str) -> tuple[str, TokenCount]:
        data = json.loads(request)
        self.asked.append(data)
        return self.reply(data), TokenCount(100, 20)


def every_item_ticked(request: str) -> str:
    """A reply to the `first-list` prompt that stands in for the model: every item a group of its own, in the order
    given, each ticked."""
    ids = [item["id"] for item in json.loads(request)["items"]]
    return json.dumps({"same": [], "order": ids, "tick": ids})


class FakeSecrets(Secrets):
    """A secrets store in a dict, for tests: it never touches a keychain or a file. Its `repr` shows names alone."""

    def __init__(self, values: dict[str, str] | None = None):
        self._values: dict[str, str] = {}
        for name, value in (values or {}).items():
            self.set(name, value)

    def where(self) -> str:
        return "the fake secrets store"

    def get(self, name: str) -> str:
        if check_name(name) not in self._values:
            raise _not_found(name, self.where())
        return self._values[name]

    def set(self, name: str, value: str) -> None:
        self._values[check_name(name)] = _check_value(name, value)
        changed(self)

    def delete(self, name: str) -> bool:
        gone = self._values.pop(check_name(name), None) is not None
        changed(self)
        return gone

    def names(self) -> list[str]:
        return sorted(self._values)

    def __repr__(self) -> str:
        return f"<FakeSecrets {', '.join(self.names()) or 'empty'}>"
