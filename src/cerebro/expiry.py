"""Expires: a document a person marked out of date, and what that means wherever its facts are read.

An expire is a `record.expire` row of the `proposals` log. It names a record, a document, as its facts name it in
`record`, says why in its reason, and cites in `cites` the facts that show it. It is filed with `tell`, and it waits
for a person, whoever filed it. Accepting it changes no record and no fact: it writes
the accepted row, whose `applied` keeps the record's revision as it stood, `revision`, and the ids of the facts that
revision gave, `facts`. Those facts stay current, and every reader is shown each one with a line under it that says
a person marked its document out of date, so a writer leaves it out of what holds today. A waiting expire marks the
document's current facts with a line that says a person decides. A rejected one marks nothing, and as the newest
decision of the revision it ruled on, it lifts an older accept's mark from that revision's facts.

The mark binds to the revision. A new revision of the record from its source lifts it from the facts that revision
gives, and the document may be flagged again. A waiting expire follows its document to a new revision while that
revision still gives one of the facts it cites, and stands down when none is left: it marks nothing, and accepting
it changes nothing. A person takes an accepted expire back with `retract` and its id, which writes the row again as
withdrawn, and the document's facts count as before. Nothing is deleted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .connector import Record
from .facts import Extraction, Fact, saved_revision, title
from .records import Proposal, proposals
from .store import Store

__all__ = ["KIND", "Mark", "follows", "marks", "named", "record_title", "revision", "snapshot", "stood_down"]

KIND = "record.expire"
WAITING, ACCEPTED, REJECTED, WITHDRAWN = "waiting", "accepted", "rejected", "withdrawn"

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass
class Mark:
    """The expire that covers a fact's document: its id, `waiting` or `accepted`, and when a person accepted it."""

    proposal: str
    status: str
    decided_at: str = ""

    def line(self) -> str:
        """The line a reader is shown under the fact."""
        if self.status == ACCEPTED:
            return f"stale {self.proposal}: a person marked its document out of date on {_day(self.decided_at)}"
        return f"stale {self.proposal}: its document is flagged out of date, and a person decides"


def _day(at: str) -> str:
    try:
        moment = datetime.fromisoformat(at.strip())
    except ValueError:
        return at[:10]
    return f"{moment:%b} {moment.day}, {moment.year}"


class _Current:
    """The ids of each record's current facts, read once a call."""

    def __init__(self, extraction: Extraction | None):
        self.extraction, self.seen = extraction, {}

    def __call__(self, record: str) -> set[str]:
        return {f.id for f in self.facts(record)}

    def facts(self, record: str) -> list[Fact]:
        if record not in self.seen:
            self.seen[record] = self.extraction.facts(record=record) if self.extraction is not None else []
        return self.seen[record]

    def source(self, record: str) -> str:
        """The source a record's facts came from, its earlier saves' too, or "" when none."""
        facts = self.facts(record) or (self.extraction.facts(record=record, latest=False)
                                       if self.extraction is not None else [])
        return facts[0].source if facts else ""


def revision(store: Store, extraction: Extraction | None, record: str, current: _Current | None = None) -> str:
    """The revision of a record as an expire binds to it: the one the store keeps, as for a told record, or else the
    one its source gave when the facts layer last took it in, as `facts.Revisions` keeps it, a hash of its body when
    the source gives none. So a source that lists the same revision again leaves the expire where it was. A facts
    layer that keeps no saves leaves only a hash of the ids of the facts it gives now."""
    kept = store.record(record)
    if kept is not None:
        return kept.revision
    current = current or _Current(extraction)
    if saved := saved_revision(store, current.source(record), record):
        return saved
    ids = sorted(current(record))
    return "facts:" + hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16]


def named(extraction: Extraction | None, target: str) -> str:
    """The record an expire's target names, as its facts name it in `record`, in any case, or "" when no current
    fact names it."""
    wanted = target.strip()
    if extraction is None or not wanted:
        return ""
    if extraction.facts(record=wanted):
        return wanted
    folded = wanted.lower()
    return next((f.record for f in extraction.facts() if f.record.lower() == folded), "")


def record_title(store: Store, record: str) -> str:
    """The plain name a person knows a record by, as the facts layer names it: its title, or the last part of its
    id."""
    kept = store.record(record)
    return title(kept if kept is not None else Record(id=record, source="", revision=""))


def follows(store: Store, extraction: Extraction | None, p: Proposal, current: _Current | None = None) -> bool:
    """Whether a waiting expire still stands on its document: the record is at the revision it was filed on, or a
    newer revision still gives one of the facts it cites."""
    current = current or _Current(extraction)
    if revision(store, extraction, p.target, current) == str(p.payload.get("revision", "")):
        return True
    return bool(set(p.cites) & current(p.target))


def stood_down(store: Store, extraction: Extraction | None, p: Proposal) -> bool:
    """Whether an expire stands on nothing: one that waits and whose document changed past the facts it cites, or one
    decided while it was so."""
    if p.status == WAITING:
        return not follows(store, extraction, p)
    return bool(p.applied.get("stood_down"))


def snapshot(store: Store, extraction: Extraction | None, p: Proposal) -> dict[str, Any]:
    """What a decision of an expire covers, kept on its row: the record's revision now and the ids of the facts it
    gives now, or `stood_down` when the expire stands on nothing."""
    current = _Current(extraction)
    if not follows(store, extraction, p, current):
        return {"stood_down": True}
    return {"record": p.target, "revision": revision(store, extraction, p.target, current),
            "facts": sorted(current(p.target))}


def _covers(store: Store, p: Proposal, fact: Fact, current: _Current, extraction: Extraction | None) -> bool:
    if fact.record != p.target:
        return False
    if p.status == WAITING:
        return fact.id in current(p.target) and follows(store, extraction, p, current)
    if p.applied.get("stood_down"):
        return False
    ruled, given = str(p.applied.get("revision", "")), p.applied.get("facts")
    changed = revision(store, extraction, p.target, current) != ruled
    if not changed:  # the revision ruled on, whatever ids the facts layer gave it when it read it again
        return fact.id in current(p.target) or isinstance(given, list) and fact.id in given
    # a fact a later revision gives again is the document as it is now
    return isinstance(given, list) and fact.id in given and fact.id not in current(p.target)


def marks(store: Store, extraction: Extraction | None, facts: Iterable[Fact]) -> dict[str, Mark]:
    """The mark on each of `facts` whose document an expire covers, by fact id: the newest expire that covers it
    decides, and only one that waits or that a person accepted marks it, so a newer rejection lifts an older accept.
    One a person took back decides nothing, so an older expire they accepted marks the fact again."""
    rows = [p for p in proposals(store) if p.kind == KIND and p.status != WITHDRAWN]
    if not rows:
        return {}
    current = _Current(extraction)
    out: dict[str, Mark] = {}
    for fact in facts:
        mine = [p for p in rows if p.target == fact.record]
        covering = [p for p in mine if _covers(store, p, fact, current, extraction)]
        if not covering:
            continue
        newest = max(covering, key=lambda p: p.at)
        if newest.status in (WAITING, ACCEPTED):
            out[fact.id] = Mark(newest.id, newest.status, newest.decided_at)
    return out


