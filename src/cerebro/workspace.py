"""The narrow handle a tool or an evidence function gets: `Workspace`.

It gives the six operations, and lets an app keep and read back first-party records of a kind it declared. It is
not the app object and exposes no store. A tool asks for it by declaring a parameter typed `Workspace`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from .caller import Caller, check_caller, describe
from .clock import Clock, stamp, system_clock
from .connector import Record
from .lookup import (
    DecideResult,
    Decision,
    FindResult,
    Lookup,
    OperationError,
    QueryResult,
    ReadResult,
    RetractResult,
    TellResult,
)
from .store import Store, scrubbed

__all__ = ["RECORD_MAX_BYTES", "Workspace"]

RECORD_MAX_BYTES = 58 * 1024  # an app's record's text and fields, so a query by its id gives it whole in one reply


class Workspace:
    """One workspace, as a tool sees it."""

    def __init__(self, lookup: Lookup, store: Store, kinds: list[str], clock: Clock = system_clock,
                 mask: Callable[[], Callable[[str], str]] | None = None):
        self._clock = clock
        self._mask = mask  # gives a mask of every value the secrets store holds, as of each keep
        self._lookup = lookup
        self._store = store
        self._kinds = list(kinds)

    def find(self, caller: Caller, name: str) -> FindResult:
        return self._lookup.find(caller, name)

    def query(self, caller: Caller, source: str, filters: dict[str, Any] | None = None, **options: Any) -> QueryResult:
        return self._lookup.query(caller, source, filters, **options)

    def read(self, caller: Caller, ids: list[str] | str, about: str | None = None) -> ReadResult:
        return self._lookup.read(caller, ids, about)

    def tell(self, caller: Caller, text: str, target: str | None = None, as_: str | None = None,
             kind: str | None = None) -> TellResult:
        return self._lookup.tell(caller, text, target, as_, kind)

    def decide(self, caller: Caller, accept: list[str | Decision] | None = None,
               reject: list[str | Decision] | None = None) -> DecideResult:
        return self._lookup.decide(caller, accept, reject)

    def retract(self, caller: Caller, id: str, reason: str = "") -> RetractResult:
        return self._lookup.retract(caller, id, reason)

    def keep(self, caller: Caller, kind: str, text: str, fields: dict[str, Any] | None = None, *,
             id: str | None = None, unique: Sequence[str] = ()) -> Record:
        """Keep a first-party record of an app's kind, whole, made by `caller`, and give it back. With `id`, keep the
        next revision of that record in its place, whole: the facts layer then replaces what the record gave. It
        refuses an id that names no record, one of another kind, one another caller made, and a record whose text and
        fields are over `RECORD_MAX_BYTES` together. Its text and fields are
        masked of every value the workspace's secrets store holds, as a source's records are. A field an app derives
        from text, such as a name or a slug, must be derived from `masked` text, since a changed form escapes the mask.
        A new record's fields that `unique` names take values no other record of the kind holds, chosen in the write
        that keeps it, as `Store.keep` says, so two processes never both take a name; a revision keeps its own."""
        check_caller(caller)
        if kind not in self._kinds:
            raise OperationError(f"keep: {kind} is not a kind an app declared: declare it in the app's kinds")
        revision = 1
        if id is not None:
            old = self._store.record(id)
            if old is None:
                raise OperationError(f"keep: there is no record {id}: leave id out to keep a new one")
            if old.source != kind:
                raise OperationError(f"keep: {id} is a record of {old.source}, not {kind}: revise it as its own kind")
            if old.by != caller:
                who = describe(old.by) if old.by is not None else "no one"
                raise OperationError(f"keep: {id} was kept by {who}, and only its maker revises it: keep a new one")
            revision = int(old.revision) + 1 if old.revision.isdigit() else 2
        if self._mask is not None:  # a kept record is masked as a source's is, before it reaches the store
            mask = self._mask()
            text, fields = mask(text), scrubbed(dict(fields or {}), mask)
        size = len(text.encode()) + len(json.dumps(fields or {}, ensure_ascii=False, default=str).encode())
        if size > RECORD_MAX_BYTES:
            raise OperationError(f"keep: the record's text and fields are {size} bytes, over the limit of "
                                 f"{RECORD_MAX_BYTES} bytes a record comes back whole in: keep a shorter one")
        record = Record(id=id or f"{kind}_{uuid.uuid4().hex[:12]}", source=kind, revision=str(revision),
                        fields=dict(fields or {}), at=stamp(self._clock()), body=text, by=caller)
        if unique and id is None:
            self._store.keep(record, unique=unique)
        else:
            self._store.keep(record)
        return record

    def masked(self, text: str) -> str:
        """`text` masked of every value the workspace's secrets store holds, as `keep` masks a record."""
        return self._mask()(text) if self._mask is not None else text

    def now(self) -> datetime:
        """The workspace's time, from its one clock, so a tool never reads the time itself."""
        return self._clock()

    def records(self, kind: str) -> list[Record]:
        """The first-party records of one kind an app declared, in the order they were kept."""
        if kind not in self._kinds:
            raise OperationError(f"records: {kind} is not a kind an app declared: declare it in the app's kinds")
        return self._store.records(kind)
