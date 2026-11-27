"""First-party records: what people tell the workspace, the proposals agents and people file, and how both are decided.

A tell is kept whole as a record, since it has no other copy, and then gives facts, proposals, both, or neither, each
citing it. It writes no article and no front matter: inputs are append-only, and only a run writes an article.

- **Sorting.** A tell sent with no `as` and no `kind` is sorted sentence by sentence through the decision model, its
  words handed over as data: what states something becomes facts, and what asks for a change becomes a proposal. A
  sentence the sort cannot place, or a tell with no decision model to sort it, writes nothing, and the reply asks for
  it again with `as`, `kind` and `target`.
- **Facts.** The facts layer draws a told record's facts, and each is linked to the tell's target, so its next run is
  due. The record waits on `RETRY` until its facts land, under a claim that keeps two processes from extracting it.
- **The proposals log.** A proposal is a row of the store's `proposals` log, and its state is its latest row by id: a
  decision is a new row, never an edit. A caller's is accepted as it is filed, and any other waits for a caller.
- **Applying.** An accepted admit, link, archive, merge or split is carried out by its collection's curator on its
  next run, through `curate.apply`, the one place articles change, and reads `applied` empty until then. A retract
  takes back a fact or a record here. An instruction, and an expire marking a document out of date, are the accepted
  row alone, which the writers and `expiry` read.
- **Taking back.** `retract` withdraws a told record or a fact, with the record's facts and the rows that cite it and
  were not applied, or an accepted proposal not carried out yet. An applied row stands, and the reply says what
  undoes it. The record itself stays kept.
- **Who is asking.** A caller decides any waiting row and takes back anything, and a source only what it said itself.
  An agent tells no fact and gives no instruction, and decides only the rows its file gives it. An app decides none,
  and neither takes back what a caller said. A source tells nothing. Each refusal is one line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import prompts
from .article import Article, fields
from .caller import KIND_WORDS, Caller, check_caller, describe
from .clock import Clock, stamp, system_clock
from .connector import Record
from .errors import CerebroError
from .identity import follow, link, link_to, redirects
from .models import DECISION_MAX_BYTES, DEFAULTS, Answer, Choice, Decider, DecisionError, ModelSpec, Question, YesNo
from .store import RECORD, Store, StoreBusy, StoreError, brief_wait, own_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from .budget import Ledger
    from .facts import Extraction, Fact

__all__ = [
    "CLAIMED",
    "KINDS",
    "MAX_DECIDE",
    "RETRY",
    "SORT_LAYER",
    "TOLD",
    "Claimed",
    "DecideResult",
    "Decision",
    "Gone",
    "InputError",
    "NotAllowed",
    "NothingStands",
    "Proposal",
    "RetractResult",
    "Review",
    "TellResult",
    "decide",
    "decided_by_caller",
    "extract_told",
    "finish_retracts",
    "instructions_on",
    "proposal_from",
    "proposals",
    "propose",
    "retract",
    "retracted_records",
    "review",
    "sentences",
    "settle_told",
    "tell",
    "told_waiting",
    "was_retracted",
]

KINDS = ("article.instruct", "article.admit", "article.link", "article.archive", "article.merge", "article.split",
         "fact.retract", "record.retract", "record.expire")
RETRACTS = ("fact.retract", "record.retract")
APPLIES = ("article.admit", "article.link", "article.archive", "article.merge",
           "article.split")  # rows a collection's curator carries out, through `curate.apply`
WAITING, ACCEPTED, REJECTED, WITHDRAWN = "waiting", "accepted", "rejected", "withdrawn"
TOLD = "tell"  # the source a told record is kept under


class InputError(CerebroError, ValueError):
    """A tell, a decide, or a retract that cannot be answered as given. The message says what to send instead."""


class NotAllowed(CerebroError, PermissionError):
    """The caller may not do what it asked: an agent telling a fact, or taking back what a person said."""


class NothingStands(InputError):
    """A retract of something already taken back. Nothing is written."""


class Claimed(CerebroError):
    """A told record's extraction is under way in a process that lives: another must not start it again. `until` is
    when its claim lapses, or "" when the record waits for no extraction now."""

    def __init__(self, message: str, until: str = "") -> None:
        super().__init__(message)
        self.until = until


class Gone(CerebroError, LookupError):
    """An article an accepted row names is gone, so nothing is left to apply and the row is withdrawn."""


@dataclass
class Proposal:
    """An ask that lands or waits for its owner: an admit, an archive, a merge, an instruction, and the like.

    `payload` holds what the ask needs, such as the keys of an admit or the article a merge folds in (`with`). `cites`
    names what it rests on, a told record first. The
    decision, once made, is `status` with who decided, when, and why, and `applied` says what an accept changed.
    """

    id: str
    kind: str
    target: str
    by: Caller
    at: str
    payload: dict[str, Any] = field(default_factory=dict)
    cites: list[str] = field(default_factory=list)
    reason: str = ""
    status: str = WAITING
    decided_by: Caller | None = None
    decided_at: str = ""
    decision_reason: str = ""
    applied: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """One entry of a `decide`: a proposal's id, and the reason, if one is given."""

    id: str
    reason: str = ""


@dataclass
class TellResult:
    """What a tell kept and gave: its record's id, the facts it gave, the proposals it filed, the parts handed back,
    and a line for each thing worth saying, such as what an accepted proposal changed."""

    record: str | None
    facts: list[str] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class DecideResult:
    """Each id's answer: `decided`, `already decided`, `unknown`, `not yours to decide`, `failed`, or `not decided`
    when another id of the call was not, and a line for each that says what happened."""

    answers: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)


@dataclass
class RetractResult:
    """What a retract took back: the id asked for, what it withdrew, the kind of row it filed and that row's id, a
    line for each applied row that stands, with what undoes it, and a line for each other thing worth saying."""

    id: str
    withdrew: list[str] = field(default_factory=list)
    kind: str = ""
    proposal: str = ""
    stands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Review:
    """One proposal waiting for a person: the row, what accepting it would change, and why it was asked."""

    proposal: Proposal
    change: str
    why: str
    title: str = ""  # the plain name of the record an expire names


# -- the proposals log ----------------------------------------------------------------------------------------------

def _who(value: Any) -> Caller | None:
    if isinstance(value, dict) and "name" in value:
        return Caller(str(value["name"]), str(value.get("kind", "agent")))
    if isinstance(value, str) and value:
        return Caller(value, "person" if value == "you" else "agent")
    return None


def _kind(kind: str) -> str:
    """A kind as the log keeps it: `admit` is `article.admit`, `expire` is `record.expire`, and an unknown kind stays
    as written."""
    plain = kind.strip().lower()
    if plain == "expire":
        return "record.expire"
    return f"article.{plain}" if f"article.{plain}" in KINDS else plain


def proposal_from(data: dict[str, Any]) -> Proposal:
    """A proposal from its latest row, which may hold fewer fields."""
    by = _who(data.get("by")) or Caller("", "agent")
    return Proposal(id=str(data["id"]), kind=_kind(str(data.get("kind", ""))), target=str(data.get("target", "")),
                    by=by, at=str(data.get("at", "")), payload=dict(data.get("payload") or {}),
                    cites=[str(c) for c in data.get("cites") or []],
                    reason=str(data.get("reason", "")), status=str(data.get("status") or WAITING),
                    decided_by=_who(data.get("decided_by")), decided_at=str(data.get("decided_at", "")),
                    decision_reason=str(data.get("decision_reason", "")),
                    applied=dict(data.get("applied") or {}))


def proposals(store: Store, status: str | None = None) -> list[Proposal]:
    """Every proposal at its latest row, oldest first, or those of one status."""
    found = [proposal_from(row.data) for row in store.latest("proposals")]
    return [p for p in found if status is None or p.status == status]


def propose(store: Store, proposal: Proposal) -> Proposal:
    """File a proposal as it stands, or record its new state: either way one new row of the `proposals` log."""
    store.append("proposals", asdict(proposal))
    return proposal


def _proposal(store: Store, id: str) -> Proposal | None:
    """The proposal `id` at its latest row, or None."""
    return next((p for p in proposals(store) if p.id == id), None)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _path(id: str) -> str:
    return id.strip().removeprefix("articles/").removesuffix(".md")


SLUG_MOST = 80  # the longest file name a slug gives


def _slug(name: str) -> str:
    """The file name a name gives: its letters of any script and its
    digits, in lower case, each run of anything else one dash, at most 80 long. Each letter is lowered on its own,
    one letter for one, so `İ` is `i`."""
    return _form(name)[:SLUG_MOST].rstrip("-")


def named_alike(one: str, two: str) -> bool:
    """Whether two names are one name as a path is made from a name: alike in letter case, spacing and the
    punctuation a path drops, whatever their length."""
    return _form(one) == _form(two)


def _form(name: str) -> str:
    out, dash = [], False
    for c in (letter.lower()[:1] for letter in name):
        if c.isalpha() or c.isdecimal():
            if dash and out:
                out.append("-")
            out.append(c)
            dash = False
        else:
            dash = True
    return "".join(out)


def _article(store: Store, target: str | None) -> Article | None:
    """The article a target names: by id or path, else by a title only one article has."""
    if not target or not target.strip():
        return None
    found = store.article(_path(target))
    if found is None and len(named := store.named(target.strip())) == 1:
        found = store.article(named[0])
    return found


def _admitted(target: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    """The id, collection, and title an admit names: `things/a-thing`, or `things/A Thing` by its name."""
    collection, _, name = _path(target).rpartition("/")
    slug, collection = _slug(name), "/".join(_slug(part) for part in collection.split("/"))
    if not collection or not slug or "" in collection.split("/"):
        raise InputError(f"admit of {target!r} names no article: give its collection and name, as things/a-thing")
    title = str(payload.get("title") or (name if name != slug else ""))
    return f"{collection}/{slug}", collection, title


def _refuse_secret(store: Store, id: str) -> None:
    """Refuse an admit whose article id would hold a value the secrets store keeps."""
    if store.masks is not None and store.masks(slug=_slug)(id) != id:
        raise InputError(f"admit into {id.rpartition('/')[0]}: its name holds a value the secrets store keeps, which "
                         "its article's id would hold, so nothing is admitted: give a name without the value, or "
                         "change the stored value")


# -- who may do what ------------------------------------------------------------------------------------------------

OWNER = Caller("you", "person")  # the workspace's owner at its terminal: no token may take this name


def _may_decide(store: Store, caller: Caller, proposal: Proposal, root: Path | None = None) -> bool:
    """A caller decides any waiting row, and a curator, by its file under the workspace's `root`, the rows of its
    collection `curate.may_answer` gives it: an app and a source decide nothing."""
    if caller.kind != "agent" or root is None:
        return caller.kind == "person"
    from .agent import AgentFileError, load_agent  # imported here: agent files are read only for this
    from .curate import cited, may_answer

    try:
        agent = load_agent(root / "agents" / f"{caller.name}.md", root / "agents")
    except (AgentFileError, OSError):
        return False
    return agent.role == "curator" and may_answer(agent, proposal, cited(proposals(store, WAITING)))


def _reaches(caller: Caller, by: Caller | None) -> bool:
    """Whether a caller may take back what `by` said: a caller anything, a source only what it said itself, and an
    agent or an app nothing a caller said."""
    if caller.kind == "person":
        return True
    if caller.kind == "source":
        return by == caller
    return by is None or by.kind != "person"


def _may_tell(caller: Caller, lane: str, kind: str | None) -> None:
    """Refuse what a caller may not tell, one part at a time: an agent tells no fact, and only a caller gives an
    instruction."""
    if lane == FACT and caller.kind == "agent":
        raise NotAllowed(f"tell: {caller.name} is an agent, and an agent's tell files proposals only: send as: "
                         "proposal with a kind, and cite the fact")
    if kind == "article.instruct" and caller.kind != "person":
        raise NotAllowed("instruct is for a caller: say what you want changed in prose and cite the fact")


def _may_take_back(store: Store, extraction: Extraction | None, caller: Caller, id: str, verb: str) -> str:
    """What `id` names, as `_said_by` gives it, once the caller may take it back: `retract` and a told retract refuse
    alike, each in its own `verb`'s name."""
    what, by = _said_by(store, extraction, id)
    if what and not _reaches(caller, by):
        said = describe(by) if by is not None else "a source"
        raise NotAllowed(f"{verb}: {caller.name} is {KIND_WORDS.get(caller.kind, caller.kind)}, and {id} was said "
                         f"by {said}: only a caller, or the one that said it, can take it back")
    return what


def _may_file(store: Store, caller: Caller, kind: str, planned: tuple[str, dict[str, Any]]) -> None:
    """Refuse an agent's or an app's told change to which articles exist that repeats its own waiting ask with
    nothing new, so an app does not file the same ask on every pass."""
    if kind not in APPLIES or caller.kind == "person":
        return
    from .curate import repeated  # imported here, as in `_apply`

    if why := repeated(store, caller, kind, *planned):
        raise InputError(f"tell: {why}")


# -- applying an accepted row ---------------------------------------------------------------------------------------

def instructions_on(store: Store, article: str) -> list[Proposal]:
    """The instructions that stand on an article for its writers, oldest first: each accepted one a person or an
    outside agent gave it, and the one a curator's admit gave it, which a person's takes the place of."""
    return [p for p in proposals(store, ACCEPTED) if p.kind == "article.instruct" and p.target == article]


def _apply(store: Store, extraction: Extraction | None, proposal: Proposal, at: str) -> tuple[dict[str, Any], str]:
    """Make the change an accepted row asks for, and say what it did. An admit, an archive, a merge or a split goes
    to `curate.apply`, the one place articles change, and a retract takes back what it names."""
    from .curate import apply  # imported here: curate runs its agents through compile and lookup, which reach here

    kind, target = proposal.kind, proposal.target
    if kind in APPLIES:
        return apply(store, proposal, at)
    if kind in RETRACTS:
        withdrew, _ = _take_back(store, extraction, proposal)
        return {"at": at, "withdrew": withdrew}, f"Withdrew {', '.join(withdrew)}."
    if kind == "article.instruct":
        if proposal.by.kind == "person":
            for row in proposals(store, ACCEPTED):  # a caller's word takes the place of a curator's, for good
                if row.kind == "article.instruct" and row.target == target and row.by.kind == "agent":
                    propose(store, replace(row, status=WITHDRAWN, decided_by=proposal.by, decided_at=at,
                                           decision_reason=f"{proposal.id}, a caller's instruction, took its "
                                                           "place"))
        return {}, f"{target} carries the instruction for its writers from their next run."
    if kind == "record.expire":
        from .expiry import snapshot  # imported here: expiry reads the proposals log through this module

        covered = snapshot(store, extraction, proposal)
        if covered.get("stood_down"):
            return covered, (f"{target} changed after the expire and the facts it cited no longer hold, so accepting "
                             "it changes nothing.")
        proposal.payload = {**proposal.payload, "revision": covered["revision"]}
        return covered, (f"{target} is marked out of date as it stands: its facts are shown marked to every reader "
                         "until it changes, and retract with this id takes the mark back.")
    raise InputError(f"proposal {proposal.id} has kind {kind!r}, which is not one of {', '.join(KINDS)}: reject it")


# -- taking back ----------------------------------------------------------------------------------------------------

UNDO = {"article.admit": "archive it to take it out of the working set again",
        "article.link": "tell a link that puts back what it changed",
        "article.archive": "admit it to bring it back",
        "article.merge": "split the article merged away to bring it back",
        "article.split": "merge the two articles again"}


def decided_by_caller(proposal: Proposal) -> bool:
    """Whether a caller decided `proposal`, and not a curator, an agent or an app."""
    return (proposal.decided_by or proposal.by).kind == "person"


def retracted_records(store: Store) -> dict[str, tuple[str, str]]:
    """When the latest accepted `record.retract` of each record it names was decided, and when the latest a caller
    decided was, or "" when no caller decided one, by record id."""
    found: dict[str, tuple[str, str]] = {}
    for p in proposals(store, ACCEPTED):
        if p.kind == "record.retract":
            latest, by_caller = found.get(p.target, ("", ""))
            found[p.target] = (max(latest, p.decided_at), max(by_caller, p.decided_at if decided_by_caller(p) else ""))
    return found


def _taken_back(by: Caller, kept: str, latest: str, by_caller: str) -> bool:
    """Whether a record's retracts take back what `by` kept at `kept`: what was kept at or before the latest of them,
    and, once a caller retracted the record, all that no caller made, since a caller's decision outranks an app's or
    an agent's. Times go to the second, so what is kept in a retract's own second is taken back."""
    return kept <= latest or (bool(by_caller) and by.kind != "person")


def was_retracted(record: Record, retracted: dict[str, tuple[str, str]]) -> bool:
    """Whether the retracts `retracted_records` gives took back this revision of `record`, as `_taken_back` says."""
    return record.id in retracted and _taken_back(record.by, record.at, *retracted[record.id])  # type: ignore[arg-type]


def _said_by(store: Store, extraction: Extraction | None, id: str) -> tuple[str, Caller | None]:
    """What an id names, `record` or `fact`, and who said it, or ("", None) for nothing."""
    if (record := store.record(id)) is not None:
        return "record", record.by
    found = extraction.facts(ids=[id]) if extraction is not None else []
    if found:
        record = store.record(found[0].record)
        return "fact", record.by if record is not None else None
    return "", None


def _take_back(store: Store, extraction: Extraction | None, proposal: Proposal) -> tuple[list[str], list[str]]:
    """Withdraw the record or the fact an accepted retract names, and give what was withdrawn and a line for each
    applied row that stands. A record's facts, and its unapplied rows that cite it, go with it. Raises `NothingStands`
    when nothing is left."""
    id, at = proposal.target, proposal.decided_at
    what, by = _said_by(store, extraction, id)
    if what == "fact":
        fact = extraction.facts(ids=[id])[0] if extraction is not None else None
        if fact is None or fact.status not in ("current", "superseded"):
            raise NothingStands(f"retract: fact {id} is retracted already: query facts for those that stand")
        extraction.retract(id)  # type: ignore[union-attr]
        return [id], []
    if what != "record":
        raise InputError(f"retract: nothing has the id {id!r}: pass a fact's fact_ id, a record's "
                         "r_ id as tell gave it, or an accepted proposal's p_ id")
    if id in (store.state(RETRY) or []):  # taken back before its facts came out: no pass extracts it now
        with store.exclusive():
            store.set_state(RETRY, [w for w in store.state(RETRY) or [] if w != id])
            claims = store.state(CLAIMED) or {}
            if id in claims and not _lives(claims[id], datetime.fromisoformat(at)):  # its facts are retracted below
                store.set_state(CLAIMED, {k: v for k, v in claims.items() if k != id})
    withdrew: list[str] = []
    taken = retracted_records(store)
    first_by_caller = decided_by_caller(proposal) and not taken.get(id, ("", ""))[1]  # it holds back later saves
    if first_by_caller or not was_retracted(store.record(id), taken):  # type: ignore[arg-type]
        propose(store, proposal)  # filed before its facts are read: a pass that saves them later sees it as it checks
        withdrew.append(id)
    for fact in extraction.facts(record=id, latest=False) if extraction is not None else []:  # every save of it
        if _taken_back(by, fact.at, at, at if decided_by_caller(proposal) else ""):  # type: ignore[arg-type]
            extraction.retract(fact.id)  # type: ignore[union-attr]
            withdrew.append(fact.id)
    stands: list[str] = []
    for row in proposals(store):
        if id not in row.cites or row.status in (REJECTED, WITHDRAWN) or row.kind in RETRACTS:
            continue
        if row.applied and row.kind in APPLIES:
            stands.append(f"{row.id} {row.kind.partition('.')[2]} of {row.target} was applied and stands: "
                          f"{UNDO[row.kind]}")
            continue
        propose(store, replace(row, status=WITHDRAWN, decided_at=at, decision_reason=f"its record {id} was retracted"))
        withdrew.append(row.id)
    if not withdrew:
        raise NothingStands(f"retract: nothing stands on {id}: it is taken back already")
    return withdrew, stands


def retract(store: Store, extraction: Extraction | None, caller: Caller, id: str, reason: str = "", *,
            clock: Clock = system_clock) -> RetractResult:
    """Take back a told record, a fact, an accepted expire, or an accepted proposal its curator has not carried out
    yet, by its id, with a reason, and say what was withdrawn.
    The row that records it, a `record.retract` or a `fact.retract`, is filed accepted in the caller's name."""
    check_caller(caller)
    id = (id or "").strip()
    if not id:
        raise InputError("retract needs an id: pass a fact's fact_ id, a record's r_ id, or an accepted proposal's p_ id")
    row = _proposal(store, id)
    if row is not None and row.kind == "record.expire":
        return _take_back_expire(store, caller, row, reason, stamp(clock()))
    if row is not None and row.kind in APPLIES:
        return _take_back_accepted(store, caller, row, reason, stamp(clock()))
    what = _may_take_back(store, extraction, caller, id, "retract")
    at = stamp(clock())
    row = Proposal(_new_id("p"), "record.retract" if what == "record" else "fact.retract", id, caller, at,
                   cites=[id] if what == "record" else [], reason=reason, status=ACCEPTED, decided_by=caller,
                   decided_at=at, decision_reason=reason)
    try:
        withdrew, stands = _take_back(store, extraction, row)
    except Exception as error:
        if _proposal(store, row.id) is None:  # nothing was filed, so the error says it all
            raise
        raise CerebroError(f"retract: {id} is retracted, but withdrawing what it gave stopped partway: {error}. "
                           f"Retract {id} again to withdraw the rest.") from error
    propose(store, replace(row, applied={"at": at, "withdrew": withdrew}))
    return RetractResult(id, withdrew, row.kind, row.id, stands)


def finish_retracts(store: Store, extraction: Extraction, at: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Finish each accepted retract of a record whose take-back stopped partway, as one whose withdrawal raised or
    whose process ended before it filed what it withdrew: withdraw what still stands of what it took back, and file
    its row as applied, so no retract stays half done. Gives the facts withdrawn, by record id, and a line for each
    retract that failed again."""
    withdrawn: dict[str, list[str]] = {}
    failed: dict[str, str] = {}
    for row in proposals(store, ACCEPTED):
        if row.kind != "record.retract" or row.applied:
            continue
        standing = {f.id for f in extraction.facts(record=row.target, latest=False)}
        try:
            withdrew, _ = _take_back(store, extraction, row)
        except NothingStands:
            withdrew = []
        except Exception as error:  # noqa: BLE001 - one retract's failure is reported, and the rest go on
            failed[f"retracts/{row.id}"] = f"{type(error).__name__}: {error}"
            continue
        propose(store, replace(row, applied={"at": at, "withdrew": withdrew}))
        if gone := [id for id in withdrew if id in standing]:
            withdrawn[row.target] = gone
    return withdrawn, failed


def _take_back_accepted(store: Store, caller: Caller, row: Proposal, reason: str, at: str) -> RetractResult:
    """Take back an accepted admit, link, archive, merge or split its curator has not carried out yet: its row again,
    withdrawn. One carried out stands, and the refusal says what undoes it."""
    if caller.kind != "person":
        raise NotAllowed(f"retract: {caller.name} is {KIND_WORDS.get(caller.kind, caller.kind)}, and {row.id} is "
                         "accepted: only a caller can take back a yes")
    with store.exclusive():
        row = next(p for p in proposals(store) if p.id == row.id)
        if row.status == WAITING:
            raise InputError(f"retract: {row.id} waits for a caller: reject it with decide, or cerebro review")
        if row.status != ACCEPTED:
            raise NothingStands(f"retract: {row.id} is {row.status}, so nothing stands to take back")
        if row.applied:
            raise InputError(f"retract: {row.id} was carried out and stands: {UNDO[row.kind]}")
        propose(store, replace(row, status=WITHDRAWN, decided_by=caller, decided_at=at,
                               decision_reason=reason.strip() or f"taken back by {caller.name}"))
    return RetractResult(row.id, [row.id], row.kind, row.id,
                         notes=[f"Took back {row.id}: its curator does not carry it out."])


def _take_back_expire(store: Store, caller: Caller, row: Proposal, reason: str, at: str) -> RetractResult:
    """Take back an accepted expire: its row again, withdrawn, so its document's facts count as before. A caller
    decides an expire, so only a caller takes one back."""
    if caller.kind != "person":
        raise NotAllowed(f"retract: {caller.name} is {KIND_WORDS.get(caller.kind, caller.kind)}, and {row.id} is an "
                         "expire, which a caller decides: ask a caller to take it back")
    with store.exclusive():
        row = next(p for p in proposals(store) if p.id == row.id)
        if row.status == WAITING:
            raise InputError(f"retract: {row.id} waits for a caller: reject it with decide, or cerebro review")
        if row.status != ACCEPTED:
            raise NothingStands(f"retract: {row.id} is {row.status}, so no mark stands to take back")
        propose(store, replace(row, status=WITHDRAWN, decided_by=caller, decided_at=at,
                               decision_reason=reason.strip() or f"taken back by {caller.name}",
                               applied={**row.applied, "taken_back": at}))
    return RetractResult(row.id, [row.id], row.kind, row.id,
                         notes=[f"Took back {row.id}: {row.target} counts as current again."])


# -- sorting a tell into what it states and what it asks ------------------------------------------------------------

# The sorting of a tell sent with no `as` and no `kind`. A sentence is a fact part when the decision model gives its
# yes at FACT_LINE or above, and a proposal part when it picks a change at ASK_LINE or above: both, when both. An
# answer from UNSURE up to its line hands the sentence back whole, so none is half guessed, but for an unsure fact
# beside a sure ask, where the ask wins. Under UNSURE an answer counts as no. A sort reads at most `MAX_PARTS`
# sentences, a few decision calls, and hands the rest back.
MAX_PARTS = 100
SORT_LAYER = "extraction"  # the ledger layer a sort's calls count toward: it decides which words become facts
FACT_LINE, ASK_LINE, UNSURE = 0.6, 0.6, 0.4
FACT, SEVERAL, OTHER, NONE = "fact", "several", "other", "none"
ASKS = {  # the changes a sorted sentence may ask for, each with what it means; the retracts go through retract
    "article.instruct": "asks how this article reads from now on: what to leave out, what to call someone, what to lead "
                        "with, including when it names a part of the article",
    "article.admit": "asks to bring this article into the wiki's working set, or back into it",
    "article.link": "asks to add, fix or take off a key of this article, or a link between it and another article",
    "article.archive": "asks to close or retire this article now",
    "article.merge": "asks to treat this article and another it names as one thing, now",
    "article.split": "asks to undo a merge of this article, now",
    SEVERAL: "asks for two or more different changes in the one sentence",
    OTHER: "asks for a change to an article other than this one",
    NONE: "asks for no change, as when it only describes",
}
FACT_QUESTION = "Sentence {}: does it describe the world, something that happened or is so?"
ASK_QUESTION = "Sentence {}: what change to the wiki does it ask for, if any?"
SENTENCE_END = re.compile(r"\n|[.!?][\"')\]]*(?=\s|$)")
ABBREVIATIONS = ("inc", "corp", "co", "ltd", "llc", "mr", "ms", "mrs", "dr", "jr", "sr", "st", "vs", "etc", "e.g",
                 "i.e", "approx", "dept")


@dataclass
class _Part:
    """A sentence of a sorted tell, where it sits in the text, and what it became: a fact, a kind of proposal, or
    nothing, with the line that hands it back."""

    start: int
    end: int
    text: str
    kind: str = ""
    line: str = ""


class _Unsorted(CerebroError):
    """A tell the sort did not read: its cap is spent, or its call failed. Nothing of it is kept, and the reply asks
    for it again with `as`."""


def sentences(text: str) -> list[tuple[int, int]]:
    """Cut text into sentences, each `text[start:end]` with no white space at either end. One ends at a line break,
    or at a period, an exclamation or a question mark before white space or the end, with any closing quote or
    bracket after it. A period after an abbreviation or an initial ends nothing."""
    out: list[tuple[int, int]] = []
    start = 0
    for match in SENTENCE_END.finditer(text):
        if match[0].startswith(".") and _abbreviated(text[start:match.start()]):
            continue
        _trimmed(out, text, start, match.start() if match[0] == "\n" else match.end())
        start = match.end()
    _trimmed(out, text, start, len(text))
    return out


def _trimmed(out: list[tuple[int, int]], text: str, start: int, end: int) -> None:
    piece = text[start:end]
    if piece.strip():
        out.append((start + len(piece) - len(piece.lstrip()), start + len(piece.rstrip())))


def _abbreviated(before: str) -> bool:
    """Whether the words before a period end in an abbreviation, as `Inc`, or an initial, as the `J` of `J. Doe`."""
    words = before.split()
    if not words:
        return False
    last = words[-1].lower().lstrip("\"'([")
    return len(last) == 1 and last.isalpha() and last not in "ia" or last in ABBREVIATIONS


def _material(contract: str, article: Article | None, collection: str, said: dict[str, str]) -> dict[str, Any]:
    """The `state` of a sort's request: the contract, what the tell is about, and its sentences by id. The tell's
    words sit in `sentences` alone, as data: no question's instructions ever hold them."""
    state: dict[str, Any] = {"contract": contract}
    if collection:
        state["collection"] = collection
    if article is not None:
        state["article"] = article.identity.title or article.identity.id
        state["fields"] = sorted(fields(article))  # as the system reads them: no writer's value of a protected field
    state["sentences"] = [{"id": id, "text": words} for id, words in said.items()]
    return state


def _questions(ids: list[str]) -> dict[str, Question]:
    """Two questions for each sentence, which name it by its id alone: does it describe the world, and what change
    does it ask for."""
    asked: dict[str, Question] = {}
    for id in ids:
        asked[f"{id}.fact"] = YesNo(FACT_QUESTION.format(id))
        asked[f"{id}.ask"] = Choice.of(ASK_QUESTION.format(id), ASKS)
    return asked


def _size(model: ModelSpec, state: dict[str, Any], questions: dict[str, Question]) -> int:
    """The bytes of the request `HttpDecider` sends for these, which the decision model bounds."""
    wire = {q.key(): q.wire() for q in questions.values()}
    return len(json.dumps({"model": model.wire_id, "state": state, "questions": wire}).encode())


def _batches(costs: list[int], base: int, most: int) -> tuple[list[list[int]], list[int]]:
    """The sentences, by index, in requests that each fit `DECISION_MAX_BYTES` and hold at most `most` sentences, and
    those too large to ask about even alone. `base` is the bytes of a request with no sentence, and each cost what
    one sentence adds, its two questions with it. JSON parts each item of a list or a map from the next with two
    bytes, so a request of k sentences, with 2k questions, weighs `base + sum(costs) + 6k - 4`."""
    batches: list[list[int]] = []
    alone: list[int] = []
    batch: list[int] = []
    total = 0
    for i, cost in enumerate(costs):
        if base + cost + 2 > DECISION_MAX_BYTES:
            alone.append(i)
            continue
        if batch and (len(batch) >= most or base + total + cost + 6 * (len(batch) + 1) - 4 > DECISION_MAX_BYTES):
            batches.append(batch)
            batch, total = [], 0
        batch.append(i)
        total += cost
    return [*batches, batch] if batch else batches, alone


def _read(fact: Answer | None, ask: Answer | None) -> list[str]:
    """What one sentence's two answers make of it: a fact, a kind of proposal, both, `several`, a change to another
    article handed back beside any fact it states, or nothing, which hands it back."""
    if fact is None or ask is None or fact.yes is None:
        return []
    sure = ask.confidence or 0.0
    choice = ask.choice if ask.choice in ASKS and sure >= UNSURE else NONE
    if choice == SEVERAL:
        return [SEVERAL]
    if choice == OTHER and sure >= ASK_LINE:
        return ([FACT] if fact.yes >= FACT_LINE else []) + [OTHER]
    asks = choice != NONE
    if asks and sure < ASK_LINE or not asks and UNSURE <= fact.yes < FACT_LINE:
        return []
    return ([FACT] if fact.yes >= FACT_LINE else []) + ([choice] if asks else [])


def _neither(fact: Answer | None, ask: Answer | None) -> bool:
    """Whether the sort is sure one sentence neither says something of the world nor asks for a change: then there
    is nothing in it to take in, which no `as` would change."""
    return fact is not None and ask is not None and fact.yes is not None and fact.yes < UNSURE and \
        ask.choice == NONE and (ask.confidence or 0.0) >= ASK_LINE


ASK_BACK = ("send it again with as: fact to put it on the record, or as: proposal with a kind to ask for a change, "
            "and a target to name the article it is about")


def _quoted(text: str) -> str:
    return json.dumps(text if len(text) <= 160 else text[:157] + "...", ensure_ascii=False)


def _sort(decider: Decider, ledger: Ledger | None, model: ModelSpec, text: str, article: Article | None,
          collection: str, run: str, clock: Clock, root: Path | None = None) -> list[_Part]:
    """Sort a tell sentence by sentence through the decision model, in as few requests as its bounds allow, each
    held to the ledger's `SORT_LAYER` as `budget.Held` holds it. Writes nothing to the store. Raises `_Unsorted` when
    the cap is spent or a call fails. A workspace at `root` that ejected the `tell-sort` prompt sorts by its own copy."""
    from .budget import BudgetSpent, Held  # imported here: the ledger reaches the facts layer, which this module names

    found = [_Part(start, end, text[start:end]) for start, end in sentences(text)]
    unread = found[MAX_PARTS:]
    found = found[:MAX_PARTS]
    for part in unread:
        part.line = (f"Sorted the first {MAX_PARTS} sentences of this tell and handed the other {len(unread)} back "
                     "unread: send them in another tell.")
    ids = [f"s{i + 1}" for i in range(len(found))]
    contract = prompts.load("tell-sort", root)

    def state(batch: list[int]) -> dict[str, Any]:
        return _material(contract, article, collection, {ids[i]: found[i].text for i in batch})

    def size(batch: list[int]) -> int:
        return _size(model, state(batch), _questions([ids[i] for i in batch]))

    base = size([])
    limit = getattr(decider, "max_questions", None)
    most = (limit() if callable(limit) else 0) // 2 or len(found)
    batches, alone = _batches([size([i]) - base - 2 for i in range(len(found))], base, max(most, 1))
    decider = decider if ledger is None else Held(decider, ledger, SORT_LAYER, model, clock, run)
    answers: dict[str, Answer] = {}
    for batch in batches:
        try:
            decision = decider.decide(state(batch), _questions([ids[i] for i in batch]))
        except (BudgetSpent, DecisionError) as error:
            raise _Unsorted(str(error)) from error
        answers.update(decision.answers)
    parts: list[_Part] = []
    for i, part in enumerate(found):
        kinds = [] if i in alone else _read(answers.get(f"{ids[i]}.fact"), answers.get(f"{ids[i]}.ask"))
        if i in alone:
            part.line = (f"Filed nothing for {_quoted(part.text)}: alone it is over the {DECISION_MAX_BYTES}-byte bound "
                         f"of one decision, so {ASK_BACK}.")
        elif kinds == [SEVERAL]:
            part.line = (f"Filed nothing for {_quoted(part.text)}: it asks for more than one change, so say them as "
                         "two sentences.")
        elif OTHER in kinds:
            back = replace(part, line=f"Handed back the change in {_quoted(part.text)}: it asks for a change to another "
                                      "article, so tell it again with that article as the target.")
            parts += [replace(part, kind=FACT)] if FACT in kinds else []
            parts.append(back)
            continue
        elif not kinds and _neither(answers.get(f"{ids[i]}.fact"), answers.get(f"{ids[i]}.ask")):
            part.line = (f"Took in nothing of {_quoted(part.text)}: it says nothing of the world and asks for no "
                         "change.")
        elif not kinds:
            part.line = f"Didn't know what to do with {_quoted(part.text)}: {ASK_BACK}."
        parts += [replace(part, kind=kind) for kind in kinds if kind != SEVERAL] or [part]
    return parts + unread


# -- a told record's facts, extracted under a claim -----------------------------------------------------------------

RETRY = own_key("failed", "tell")  # the state key of told records whose facts did not come out yet, which each pass tries again
CLAIMED = own_key("extracting", "tell")  # the told records whose extraction is under way: by whom, and until when
CLAIM_LAPSE = 1800  # seconds a claim holds past its last renewal, as a pass's lease does
CLAIM_RENEW = 300.0  # seconds between two renewals of a claim, while its holder extracts
_HELD: set[str] = set()  # the claims this process holds


def _fact_words(record: Record) -> str:
    """The words of a told record that go to the facts layer: the whole of it, or, when a sort cut it into parts, its
    fact parts in their order, each kept apart from the next as the text had it, by a blank line, a line or a space."""
    body = record.body or ""
    if "parts" not in record.fields:
        return body
    out: list[str] = []
    last = -1
    for part in record.fields.get("parts") or []:
        if part.get("kind") != FACT:
            continue
        start, end = int(part["start"]), int(part["end"])
        if last >= 0:
            gap = body[last:start]
            out.append("\n\n" if "\n\n" in gap else "\n" if "\n" in gap else " ")
        out.append(body[start:end])
        last = end
    return "".join(out)


def _holder(token: str, now: datetime) -> dict[str, Any]:
    """A claim of `token`'s, renewed at `now`. Its time is kept to the second, rounded up, so it never lapses before
    `CLAIM_LAPSE` has passed."""
    until = now + timedelta(seconds=CLAIM_LAPSE)
    if until.microsecond:
        until = until.replace(microsecond=0) + timedelta(seconds=1)
    return {"by": token, "host": socket.gethostname(), "pid": os.getpid(), "until": stamp(until)}


def _lives(claim: Any, now: datetime) -> bool:
    """Whether the process that took a claim may still be extracting: while it renews the claim, so until the claim
    lapses. This process holds a claim exactly while it extracts. One on this machine whose pid is gone has ended,
    so its claim is taken over at once; a pid that answers proves nothing, since another program may hold it now."""
    try:
        if claim.get("host") == socket.gethostname() and os.name == "posix":
            if claim.get("pid") == os.getpid():
                return claim.get("by") in _HELD
            try:
                os.kill(int(claim["pid"]), 0)
            except ProcessLookupError:
                return False
            except PermissionError:  # a process of another user holds the pid: the lapse decides
                pass
        return datetime.fromisoformat(str(claim["until"])) > now
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        return False


@contextmanager
def _renewing(store: Store, id: str, token: str, clock: Clock) -> Iterator[None]:
    """Renew the claim `token` holds on a told record every `CLAIM_RENEW` seconds while the block runs, by `clock`, so
    it lapses only once its holder stops."""
    stop = threading.Event()

    def renew() -> None:
        while not stop.wait(CLAIM_RENEW):
            try:
                with store.exclusive():
                    claims = store.state(CLAIMED) or {}
                    if (claims.get(id) or {}).get("by") == token:
                        store.set_state(CLAIMED, {**claims, id: _holder(token, clock())})
            except Exception:
                logging.getLogger(__name__).exception("renewing the claim on a told record failed")

    keeper = threading.Thread(target=renew, name="cerebro-tell-claim", daemon=True)
    keeper.start()
    try:
        yield
    finally:
        stop.set()
        keeper.join(timeout=5)


def _take(store: Store, id: str, now: datetime) -> str:
    """Claim the extraction of a told record for this process and give the claim, or raise `Claimed` when a process
    that lives holds it, or when the record no longer waits, as when its facts landed since the list was read. A
    claim whose process is gone is taken over."""
    token = uuid.uuid4().hex
    with store.exclusive():
        if id not in (store.state(RETRY) or []):
            raise Claimed(f"{id} waits for no extraction now")
        claims = store.state(CLAIMED) or {}
        if id in claims and _lives(claims[id], now):
            raise Claimed(f"{id} is being extracted by another process now", str(claims[id].get("until") or ""))
        _HELD.add(token)
        store.set_state(CLAIMED, {**claims, id: _holder(token, now)})
    return token


def _let_go(store: Store, id: str, token: str) -> None:
    """Give back a claim this process holds, in the write the caller is in, if any."""
    with store.exclusive():
        claims = store.state(CLAIMED) or {}
        if (claims.get(id) or {}).get("by") == token:
            store.set_state(CLAIMED, {k: v for k, v in claims.items() if k != id})
    _HELD.discard(token)


def _holds(store: Store, id: str, token: str) -> None:
    """Raise `Claimed` unless the claim on a told record is still `token`'s."""
    if ((store.state(CLAIMED) or {}).get(id) or {}).get("by") != token:
        raise Claimed(f"{id} was taken over by another process, as this one's claim lapsed while it extracted")


def _withdraw(extraction: Extraction, id: str, facts: list[Fact]) -> None:
    """Retract every fact of a record taken back that still stands: those its extraction gave, and any the layer
    holds of it. A retract of a fact already retracted changes nothing."""
    for fact in [*facts, *extraction.facts(record=id, latest=False)]:
        if fact.status in ("current", "superseded", "contradicted"):
            extraction.retract(fact.id)


def _drop_own(store: Store, extraction: Extraction, facts: list[Fact]) -> None:
    """Retract the facts a holder's own extraction gave once its claim was taken over, as one stopped or asleep past
    its lapse: the record's facts are the new holder's, or none when it was taken back meanwhile. A fact another
    holder linked, as a layer that gives the same fact the same id may give, is that holder's and stands."""
    mine = [f.id for f in facts if not store.articles_of(f.id)]
    for fact in extraction.facts(ids=mine) if mine else []:
        if fact.status in ("current", "superseded", "contradicted"):
            extraction.retract(fact.id)


def extract_told(store: Store, extraction: Extraction, record: Record, at: str, held: str = "",
                 clock: Clock = system_clock) -> list[str]:
    """Turn a told record into facts, link each to the record's target and to what it names, and give their ids. A
    sorted record gives facts of its fact parts alone, so what it asked for is never read as something it stated.

    The record waits on `RETRY` from the write that kept it, and leaves it in the write that links its facts, so a
    process that ends at any step between leaves it for a later pass. Its extraction is claimed for the process first,
    or `held` is the claim the caller took, so no two extract it at once: `Claimed` when another holds it or it waits
    no more. The claim is renewed by `clock` while the extraction runs. A holder whose claim lapsed, as one stopped or
    asleep, pays for no extraction once it knows, and one that learns it after its extraction retracts the facts that
    extraction gave and raises `Claimed`: the record is the new holder's, or taken back. Whether the claim is still
    its own and whether the record still waits are read in the one write that links, so no takeover lands between.
    Only a retract takes a claimed record off `RETRY`: its facts are retracted before its claim is given back, so a
    process cut between leaves the claim for `settle_told`."""
    token, settled, taken_back = held or _take(store, record.id, datetime.fromisoformat(at)), False, False
    try:
        article = _article(store, str(record.fields.get("target") or ""))
        words = _fact_words(record)
        with _renewing(store, record.id, token, clock):
            _holds(store, record.id, token)
            facts = extraction.extract(record if words == record.body else replace(record, body=words)).facts
        with store.exclusive():
            lost = ((store.state(CLAIMED) or {}).get(record.id) or {}).get("by") != token
            taken_back = not lost and record.id not in (store.state(RETRY) or [])  # a retract that came since
            if not lost and not taken_back:
                for fact in facts:
                    if article is not None:
                        link_to(store, fact.id, [article.identity.id], at, how=RECORD)
                    link(store, fact, record, at=at)
                store.set_state(RETRY, [w for w in store.state(RETRY) or [] if w != record.id])
                _let_go(store, record.id, token)
                settled = True
        if lost:
            _drop_own(store, extraction, facts)
            raise Claimed(f"{record.id} was taken over by another process, as this one's claim lapsed while it "
                          "extracted")
        if taken_back:
            _withdraw(extraction, record.id, facts)
            _let_go(store, record.id, token)
            settled = True
    finally:
        if not settled and record.id in (store.state(RETRY) or []):
            try:
                with brief_wait(store):
                    _let_go(store, record.id, token)
            except StoreBusy:
                pass  # a claim not in `_HELD` is taken over at once, so a busy store need not hold up the tell
        _HELD.discard(token)
    return [] if taken_back else [fact.id for fact in facts]


def settle_told(store: Store, extraction: Extraction, now: datetime) -> dict[str, str]:
    """Finish what a process cut while it extracted a record taken back left: retract the facts of the record and
    give back the claim. A holder that lives finishes it itself. Gives a line for each that failed."""
    failed = {}
    for id, claim in (store.state(CLAIMED) or {}).items():
        if id in (store.state(RETRY) or []) or _lives(claim, now):
            continue
        try:
            _withdraw(extraction, id, [])
            with store.exclusive():
                claims = store.state(CLAIMED) or {}
                if claims.get(id) == claim:
                    store.set_state(CLAIMED, {k: v for k, v in claims.items() if k != id})
        except Exception as error:  # noqa: BLE001 - one record's failure is reported, and the rest go on
            failed[f"tell/{id}"] = f"{type(error).__name__}: {error}"
    return failed


def told_waiting(store: Store) -> list[str]:
    """The ids of the told records that wait for a pass to extract their facts: those whose extraction failed, was
    cut, or is under way now."""
    return [str(id) for id in store.state(RETRY) or []]


def _keep(store: Store, record: Record, at: str, filed: Callable[[], None] | None = None) -> str:
    """Keep a told record with the proposals `filed` writes of it, and when it has words to extract, its place on
    `RETRY` and this process's claim on its extraction, all in one write, so no end of the process leaves it kept and
    unread, and a tell refused as busy left nothing and sent again is filed once. Gives the claim, or "" for a record
    with nothing to extract."""
    with store.exclusive():
        store.keep(record)
        if filed is not None:
            filed()
        if record.extract != "facts":
            return ""
        token = uuid.uuid4().hex
        _HELD.add(token)
        try:
            store.set_state(RETRY, [*(store.state(RETRY) or []), record.id])
            held = _holder(token, datetime.fromisoformat(at))
            store.set_state(CLAIMED, {**(store.state(CLAIMED) or {}), record.id: held})
        except BaseException:
            _HELD.discard(token)
            raise
    return token


def _keep_facts(store: Store, extraction: Extraction | None, record: Record, at: str, result: TellResult,
                token: str, clock: Clock) -> None:
    """Give a told record's facts: the facts layer turns its words into facts now, or a later pass does when it fails,
    is cut, or there is no facts layer yet."""
    try:
        if extraction is None:
            _let_go(store, record.id, token)
            why = "this workspace has no facts layer"
        else:
            try:
                result.facts = extract_told(store, extraction, record, at, held=token, clock=clock)
                return
            except Claimed:
                try:
                    if record.id in (store.state(RETRY) or []):
                        said = "so another process took it over, and it or a later pass extracts its facts"
                    elif extraction.facts(record=record.id):
                        said = "so another process took it over and gave its facts"
                    else:
                        said = "and it was taken back meanwhile, so none of its facts stand"
                except Exception:  # noqa: BLE001 - what stands cannot be read now: say only what this tell did
                    said = "so this tell gave up the facts it extracted to the process that holds the record now"
                result.notes.append(f"Kept {record.id}; its extraction was held up past its claim, {said}.")
                return
            except StoreBusy as error:
                if record.id in (store.state(RETRY) or []):
                    result.notes.append(f"Kept {record.id}; its facts follow at the next pass, since another "
                                        "process, such as a pass, is writing the store now.")
                    return
                why = f"{type(error).__name__}: {error}"
            except Exception as error:  # noqa: BLE001 - the words are kept, and a later pass extracts them
                why = f"{type(error).__name__}: {error}"
        if record.id in (store.state(RETRY) or []):
            result.notes.append(f"Kept {record.id}; its facts did not come out yet ({why}), and a later pass "
                                "extracts them.")
        else:
            result.notes.append(f"Kept {record.id}; it was taken back while its facts came out ({why}), and a later "
                                "pass retracts any it gave.")
    finally:
        _HELD.discard(token)


# -- tell -----------------------------------------------------------------------------------------------------------

# A tell holds a long note, such as a meeting's notes, at about 8,000 words; anything longer is a document, which the
# files source reads.
MAX_TELL_CHARS = 50_000
MERGE_WORDS = re.compile(r"^(?:please\s+)?merge\s+(?:this|it)\s+(?:with|into)\s+", re.IGNORECASE)


CURATED = {"article.admit": "admitted", "article.link": "linked", "article.archive": "archived",
           "article.merge": "merged", "article.split": "split"}  # the acts a collection's curator carries out


def _curated(root: Path | None, collection: str, kind: str) -> None:
    """Refuse an act into a collection of the workspace at `root` that no curator keeps, since none would carry it
    out. With no `root`, the store alone, nothing is refused."""
    if root is not None and not (root / "agents" / collection / "curator.md").is_file():
        raise InputError(f"tell: articles/{collection} has no curator, so nothing is {CURATED[kind]} there")


def _planned(store: Store, kind: str, target: str | None, text: str, extraction: Extraction | None = None,
             root: Path | None = None) -> tuple[str, dict[str, Any]]:
    """The target and payload of a proposal a tell files, checked before anything is written."""
    target = (target or "").strip()
    if kind == "article.instruct":
        article = _article(store, target)
        if not target:
            raise InputError("tell: an instruction needs a target: name the article it is for, or a collection")
        if article is None and target not in store.collections():
            raise InputError(f"tell: target {target} is no article or collection: find the article first")
        return (article.identity.id if article else target), {"text": text}
    if not target:
        raise InputError(f"tell: {kind} needs a target: name the article, or the record or fact it is about")
    if kind == "article.admit":
        article = _article(store, target)
        if article is not None:
            _curated(root, article.identity.collection, kind)
            return article.identity.id, {}
        id, collection, title = _admitted(target, {})
        _curated(root, collection, kind)
        _refuse_secret(store, id)
        return id, {"title": title} if title else {}
    if kind in ("article.archive", "article.merge", "article.split", "article.link"):
        article = _article(store, target)
        if article is None:
            raise InputError(f"tell: target {target} is no article: find it first")
        _curated(root, article.identity.collection, kind)
        if kind == "article.link":
            return article.identity.id, {"text": text}
        if kind == "article.split" and article.identity.id not in redirects(store):
            raise InputError(f"tell: {article.identity.id} is not merged into another article, so there is nothing to split")
        if kind == "article.split":
            return article.identity.id, {"from": redirects(store)[article.identity.id]}
        if kind == "article.merge":
            other = _other(store, article, text)
            return article.identity.id, {"with": other}
        return article.identity.id, {}
    if kind == "record.expire":
        from .expiry import named, revision

        record = target if store.record(target) is not None else named(extraction, target)
        if not record:
            raise InputError(f"tell: an expire names a document as its facts name it in record, and no fact names "
                             f"{target}: query facts for the record")
        return record, {"text": text, "revision": revision(store, extraction, record)}
    return target, {"text": text}


def _other(store: Store, article: Article, text: str) -> str:
    """The article a merge folds into `article`: an id or path its words give, else a title one article of its
    collection holds, as in "merge this with Alder Works"."""
    for word in re.findall(r"[a-z0-9][a-z0-9_-]*/[^\s,;]+", text):
        if (other := store.article(_path(word.rstrip(".")))) is not None and other.identity.id != article.identity.id:
            return other.identity.id
    name = MERGE_WORDS.sub("", text.strip()).rstrip(".")
    same = [id for id in store.named(name) if id != article.identity.id and id.startswith(article.identity.collection + "/")]
    if len(same) == 1:
        return same[0]
    if same:
        raise InputError(f"tell: {name!r} names {' and '.join(same)}: give one by its path")
    raise InputError(f"tell: no other article of {article.identity.collection} is named {name!r}: give the article to merge "
                     "by its path")


def _file(store: Store, extraction: Extraction | None, caller: Caller, kind: str, planned: tuple[str, dict[str, Any]],
          record: Record, at: str, result: TellResult) -> None:
    """File one proposal a tell gives, citing its record. A caller's is accepted as it is sent: its admit, link,
    archive, merge or split is carried out by the collection's curator on its next run, and anything else is applied
    at once. An agent's or an app's waits for a caller."""
    proposal = Proposal(_new_id("p"), kind, planned[0], caller, at, planned[1], [record.id])
    if caller.kind != "person":
        result.notes.append(f"Filed {proposal.id}, {kind} of {proposal.target}: awaiting review by a caller.")
    elif kind in APPLIES:
        proposal = replace(proposal, status=ACCEPTED, decided_by=caller, decided_at=at)
        result.notes.append(f"Accepted {proposal.id}, {kind.partition('.')[2]} of {proposal.target}: its curator "
                            "carries it out on its next run.")
    else:
        proposal = replace(proposal, status=ACCEPTED, decided_by=caller, decided_at=at)
        proposal.applied, note = _apply(store, extraction, proposal, at)
        result.notes.append(f"Filed and accepted {proposal.id}. {note}")
    propose(store, _landing(extraction, proposal))
    result.proposals.append(proposal.id)


def admit_by_hand(store: Store, extraction: Extraction | None, caller: Caller, collection: str, name: str, *,
                  clock: Clock = system_clock) -> TellResult:
    """A caller's admit of `name` into `collection`, as `cerebro admit` files it: its target is the slug of the name,
    and its title the name as the caller wrote it, slashes and all. It is filed as a tell's admit is, on a record of
    what the caller said, accepted, and its curator finds which thing the name means and carries it out. Only this
    command and a tell's admit of a target written as a name, such as `things/Pat Quill`, set the title so."""
    check_caller(caller)
    if caller.kind != "person":
        raise NotAllowed(f"admit by hand is a caller's: {caller.name} files an admit with tell")
    name = name.strip()
    id = f"{collection}/{_slug(name)}"
    if not _slug(name) or not collection:
        raise InputError(f"admit {collection}/{name}: it names nothing an article could be called")
    _refuse_secret(store, id)
    payload: dict[str, Any] = {"title": name}
    at = stamp(clock())
    record = Record(id=_new_id("r"), source=TOLD, revision="1", at=at, body=f"admit {collection}/{name}", by=caller,
                    fields={"target": id, "as": "proposal", "kind": "article.admit"})
    store.keep(record)
    result = TellResult(record.id)
    _file(store, extraction, caller, "article.admit", (id, payload), record, at, result)
    return result


def archive_by_hand(store: Store, extraction: Extraction | None, caller: Caller, article: str, *,
                    clock: Clock = system_clock, root: Path | None = None) -> TellResult:
    """A caller's archive of `article`, by its id, as `cerebro archive` files it. It is filed as a tell's archive is,
    on a record of what the caller said, accepted, and its curator carries it out on its next run: the article
    leaves the working set, and its file, facts and parts stay."""
    check_caller(caller)
    if caller.kind != "person":
        raise NotAllowed(f"archive by hand is a caller's: {caller.name} files an archive with tell")
    found = store.article(_path(article))
    if found is None:
        raise InputError(f"archive {article}: there is no article {article}")
    try:
        _curated(root, found.identity.collection, "article.archive")
    except InputError as error:
        raise InputError(f"archive {article}: {str(error).removeprefix('tell: ')}") from error
    at = stamp(clock())
    id = found.identity.id
    record = Record(id=_new_id("r"), source=TOLD, revision="1", at=at, body=f"archive {id}", by=caller,
                    fields={"target": id, "as": "proposal", "kind": "article.archive"})
    store.keep(record)
    result = TellResult(record.id)
    _file(store, extraction, caller, "article.archive", (id, {}), record, at, result)
    return result


def tell(store: Store, extraction: Extraction | None, caller: Caller, text: str, target: str | None = None,
         as_: str | None = None, kind: str | None = None, *, clock: Clock = system_clock,
         decider: Decider | None = None, ledger: Ledger | None = None,
         decision_model: ModelSpec | None = None, root: Path | None = None) -> TellResult:
    """Tell the workspace something. It becomes a first-party record, then facts, proposals, or both, and edits no
    article.

    `as_` is `fact` or `proposal`, and a `kind` alone makes it a proposal: the caller's word stands, and nothing is
    sorted. With neither, the decision model sorts the tell sentence by sentence (`decider`, priced on
    `decision_model` and held by `ledger` to `SORT_LAYER`): what describes the world becomes facts, as a fact tell's
    words do, and what asks for a change becomes a proposal, and one sentence may be both. Who may tell what applies
    to each part, and a part refused, or one the sort was unsure of, is handed back in `returned` with a line. A tell
    holds at most `MAX_TELL_CHARS` characters, and a sort reads its first `MAX_PARTS` sentences. With
    no decider, its cap spent, or its call failed, nothing is kept, and the reply asks for it again with `as`. `root` is
    the workspace's folder, where an ejected `tell-sort` prompt takes the bundled one's place.

    A fact's record goes to the facts layer, and its facts link to `target`. A proposal files one row of `kind`, `article.instruct` when none is
    given, and a caller's is accepted as it is sent. Everything is checked before the record is written, so a
    refused tell leaves nothing behind.
    """
    check_caller(caller)
    text = (text or "").strip()
    if not text:
        raise InputError("tell needs text: say what the wiki should know, or what to change")
    if len(text) > MAX_TELL_CHARS:
        raise InputError(f"tell: the text is {len(text)} characters, past the {MAX_TELL_CHARS} a tell takes: split it "
                         "into several tells, or put a long document where the files source reads it")
    if as_ and as_ not in (FACT, "proposal"):
        raise InputError(f"tell: as is {as_!r}: pass fact or proposal, or leave it out")
    if caller.kind == "source":
        raise NotAllowed(f"tell: {caller.name} is a source, and a source speaks through its connector: list the "
                         "record there")
    if as_ or kind:
        return _tell_as(store, extraction, caller, text, target, as_ or "proposal", kind, clock, root)
    why = "this workspace has no decision model to sort a tell into facts and proposals"
    if decider is not None:
        article = _article(store, target)
        collection = article.identity.collection if article else (target or "").strip()
        collection = collection if collection in store.collections() else ""
        record = _new_id("r")
        try:
            parts = _sort(decider, ledger, decision_model or DEFAULTS["decision"], text, article, collection, record,
                          clock, root)
        except _Unsorted as error:
            why = f"the sort did not run: {error}"
        else:
            return _tell_sorted(store, extraction, caller, text, target, parts, record, clock, root)
    return TellResult(None, returned=[text], notes=[f"Kept nothing, since {why}: {ASK_BACK}."])


def _tell_as(store: Store, extraction: Extraction | None, caller: Caller, text: str, target: str | None, lane: str,
             kind: str | None, clock: Clock, root: Path | None) -> TellResult:
    """A tell whose lane is given: a fact, or one proposal of `kind`."""
    _may_tell(caller, lane, None)
    if lane == FACT and kind:
        raise InputError(f"tell: kind {kind} names a proposal, and as is fact: send as: proposal, or leave kind out")
    article: Article | None = None
    planned: tuple[str, dict[str, Any]] = ("", {})
    if lane == "proposal":
        kind = _kind(kind or "article.instruct")
        if kind not in KINDS:
            raise InputError(f"tell: kind {kind} is not one of {', '.join(KINDS)}: pass one of those")
        _may_tell(caller, lane, kind)
        planned = _planned(store, kind, target, text, extraction, root)
        if kind in RETRACTS:
            _may_take_back(store, extraction, caller, planned[0], "tell")
        _may_file(store, caller, kind, planned)
    elif target:
        article = _article(store, target)
        if article is None:
            raise InputError(f"tell: target {target} is no article: find it first, or leave target out")
    at = stamp(clock())
    fields: dict[str, Any] = {"target": article.identity.id if article else planned[0] or (target or ""), "as": lane}
    if lane == "proposal":
        fields["kind"] = kind
    if article is not None and article.identity.title:
        fields["subject"] = article.identity.title
    record = Record(id=_new_id("r"), source=TOLD, revision="1", at=at, body=text, by=caller, fields=fields,
                    extract="facts" if lane == FACT else "")
    result = TellResult(record.id)
    if lane == FACT:
        token = _keep(store, record, at)
        _keep_facts(store, extraction, record, at, result, token, clock)
    else:
        held = _HeldFacts(extraction) if extraction is not None else None
        _keep(store, record, at, lambda: _file(store, held, caller, str(kind), planned, record, at, result))
        _released(store, held)
    return result


def _count(n: int) -> str:
    return "no sentence" if n == 0 else "1 sentence" if n == 1 else f"{n} sentences"


def _tell_sorted(store: Store, extraction: Extraction | None, caller: Caller, text: str, target: str | None,
                 parts: list[_Part], id: str, clock: Clock, root: Path | None) -> TellResult:
    """File what a sort made of a tell, each part checked as a tell of its own lane would be, before anything is
    written. A part refused is handed back with its line, and the record is kept whole when any part stands. When
    none does, nothing is written."""
    article = _article(store, target)
    stated: list[_Part] = []
    asked: list[tuple[_Part, tuple[str, dict[str, Any]]]] = []
    for part in parts:
        if part.line:
            continue
        try:
            _may_tell(caller, part.kind, part.kind)
            if part.kind == FACT:
                if target and article is None:
                    raise InputError(f"tell: target {target} is no article: find it first, or leave target out")
                stated.append(part)
            else:
                planned = _planned(store, part.kind, target, part.text, extraction, root)
                if part.kind in RETRACTS:
                    _may_take_back(store, extraction, caller, planned[0], "tell")
                _may_file(store, caller, part.kind, planned)
                asked.append((part, planned))
        except (InputError, NotAllowed) as error:
            part.line = f"Filed nothing for {_quoted(part.text)}: {str(error).removeprefix('tell: ')}."
    back = [p for p in parts if p.line]
    returned = list(dict.fromkeys(p.text for p in back))
    lines = list(dict.fromkeys(p.line for p in back))
    if not stated and not asked:
        return TellResult(None, returned=returned, notes=["Kept nothing: no sentence of it could be filed.", *lines])
    at = stamp(clock())
    fields: dict[str, Any] = {
        "target": article.identity.id if article else asked[0][1][0] if asked else (target or "").strip(), "as": "sorted",
        "parts": [{"start": p.start, "end": p.end, "kind": p.kind} for p in parts if not p.line]}
    if article is not None and article.identity.title:
        fields["subject"] = article.identity.title
    record = Record(id=id, source=TOLD, revision="1", at=at, body=text, by=caller, fields=fields,
                    extract="facts" if stated else "")
    result = TellResult(record.id, returned=returned)
    result.notes.append(f"Sorted {record.id} sentence by sentence: {_count(len(stated))} to facts, "
                        f"{_count(len(asked))} to proposals, {_count(len(returned))} handed back.")

    held = _HeldFacts(extraction) if extraction is not None else None

    def filed() -> None:
        for part, planned in asked:
            _file(store, held, caller, part.kind, planned, record, at, result)

    token = _keep(store, record, at, filed)
    if stated:
        _keep_facts(store, extraction, record, at, result, token, clock)
    result.notes += lines
    _released(store, held)
    return result


# -- decide ---------------------------------------------------------------------------------------------------------

MAX_DECIDE = 500


class _NotApplied(CerebroError):
    """An accept whose change could not be made: nothing of it is written."""


class _Refused(CerebroError):
    """A decide one id of which could not be decided as asked: raised to undo the rest."""


class _HeldFacts:
    """The facts layer as a decide or a tell sees it while its write lands: reads go to `inner`, and a fact it
    withdraws reads as withdrawn and is held in `withdrawn`, to be withdrawn once the store has kept the whole write,
    since the store's undo does not reach the facts layer. `rows` holds each record retract the write filed, as it is
    filed once its facts are withdrawn."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.withdrawn: list[str] = []
        self.rows: list[Proposal] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def facts(self, *, ids: list[str] | None = None, **filters: Any) -> list[Any]:
        found = self.inner.facts(ids=ids, **filters)
        if ids is None:
            return [f for f in found if f.id not in self.withdrawn]
        return [replace(f, status="retracted") if f.id in self.withdrawn else f for f in found]

    def retract(self, fact_id: str) -> None:
        self.withdrawn.append(fact_id)


def _landing(extraction: Any, row: Proposal) -> Proposal:
    """`row` as a write files it: a record retract whose facts are held goes in without `applied`, which `_release`
    files once they are withdrawn, so a pass finishes one that stops between the two."""
    if isinstance(extraction, _HeldFacts) and row.kind == "record.retract":
        extraction.rows.append(row)
        return replace(row, applied={})
    return row


def _release(store: Store, held: _HeldFacts) -> dict[str, Exception]:
    """Withdraw the facts `held` holds, now the store has kept the write, and file as applied each record retract whose
    facts all went. Gives each fact the facts layer kept, with its error: its retract waits for a pass to finish."""
    kept: dict[str, Exception] = {}
    for fact in held.withdrawn:
        try:
            held.inner.retract(fact)
        except Exception as error:  # noqa: BLE001 - the store kept the write: the rest go on
            kept[fact] = error
    for row in held.rows:
        if not kept.keys() & set(row.applied.get("withdrew", [])):
            propose(store, row)
    return kept


def _released(store: Store, held: _HeldFacts | None) -> None:
    """`_release` for a tell, which says so when a fact stayed, as `retract` does."""
    kept = _release(store, held) if held is not None else {}
    for fact, error in kept.items():
        id = next((r.target for r in held.rows if fact in r.applied.get("withdrew", [])), fact)  # type: ignore[union-attr]
        raise CerebroError(f"retract: {id} is retracted, but withdrawing what it gave stopped partway: {error}. "
                           f"Retract {id} again to withdraw the rest.") from error


def _fault(store: Store, caller: Caller, id: str, row: Proposal | None, root: Path | None,
           accepting: bool) -> tuple[str, str] | None:
    """Why `caller` may not decide `row` now, as an answer and its line, or None."""
    if row is None:
        return "unknown", f"there is no proposal {id!r}: query proposals for ids"
    if row.status != WAITING:
        return "already decided", f"{id} is already {row.status}"
    if not _may_decide(store, caller, row, root) or accepting and caller.kind == "agent":  # a curator only rejects
        return "not yours to decide", (
            f"{id} is not one you decide: answer the proposals your opening lists under Proposals waiting on your "
            "collection" if caller.kind == "agent" else f"{id} waits for a caller to decide it")
    return None


def _accept(store: Store, extraction: Extraction | None, caller: Caller, decision: Decision, row: Proposal,
            at: str) -> str:
    """Accept one row, with the waiting rows it cites, and say what it did. An admit, link, archive, merge or split is
    left for its collection's curator to carry out, its `applied` empty, and anything else is applied now."""
    decided = replace(row, status=ACCEPTED, decided_by=caller, decided_at=at, decision_reason=decision.reason)
    note = "its curator carries it out on its next run."
    if row.kind not in APPLIES:
        try:
            decided.applied, note = _apply(store, extraction, decided, at)
        except CerebroError as error:
            raise _NotApplied(str(error)) from error
    propose(store, _landing(extraction, decided))
    for p in _cited(store, row):
        propose(store, replace(p, status=ACCEPTED, decided_by=caller, decided_at=at,
                               decision_reason=decision.reason or row.reason, applied=decided.applied))
    return f"Accepted {row.id}: {note}" if row.kind in APPLIES else f"Accepted {row.id}. {note}"


def _reject(store: Store, extraction: Extraction | None, caller: Caller, decision: Decision, row: Proposal,
            at: str) -> str:
    """Reject one row, and say what it did."""
    id = decision.id
    decided = replace(row, status=REJECTED, decided_by=caller, decided_at=at, decision_reason=decision.reason)
    if row.kind == "record.expire":
        from .expiry import snapshot

        covered = snapshot(store, extraction, row)
        if covered.get("stood_down"):
            decided.applied = covered
            note = (f"Rejected {id}. It had stood down: the document changed after it and the facts it cited no "
                    "longer hold, so nothing is held back.")
        else:  # what it ruled on, kept as an accept keeps it, so the newest decision of the document is this one
            decided.payload, decided.applied = {**row.payload, "revision": covered["revision"]}, covered
            note = (f"Rejected {id}. This revision of {row.target} is not marked, and a later change to it can be "
                    "flagged.")
    elif row.kind == "article.merge" and not row.payload.get("distinct"):  # kept apart in either order
        other = str(row.payload.get("with", ""))
        store.append("merges", {"at": at, "distinct_from": [row.target, other], "by": caller.name, "proposal": id,
                                "reason": decision.reason})
        note = f"Rejected {id}. The proposer will not raise it again until something new about them arrives."
    else:
        note = f"Rejected {id}. The proposer will not raise it again."
    propose(store, decided)
    for p in _cited(store, row) if caller.kind != "agent" else []:
        propose(store, replace(p, status=REJECTED, decided_by=caller, decided_at=at, decision_reason=decision.reason))
    return note


def _cited(store: Store, row: Proposal) -> list[Proposal]:
    """The waiting proposals a curator's act names, which wait with it and are decided with it."""
    return [p for p in proposals(store, WAITING) if p.id in row.cites]


def _decided(store: Store, extraction: Extraction | None, caller: Caller, entries: list[tuple[Decision, bool]],
             at: str, root: Path | None) -> DecideResult:
    """Decide each entry in turn, on the store as the ones before it leave it, and give each id's answer: run inside
    one exclusive block, which the caller undoes when one is not decided."""
    result = DecideResult()
    start = {p.id: p for p in proposals(store)}
    for decision, yes in entries:
        id = decision.id
        row, was = _proposal(store, id), start.get(id)
        if was is not None and was.status == WAITING and row is not None and row.status != WAITING:
            result.answers[id], result.notes[id] = "failed", (
                f"{id} was not decided: an earlier decision of this decide leaves it {row.status}: "
                f"{row.decision_reason}")
            continue
        if fault := _fault(store, caller, id, row, root, yes):
            result.answers[id], result.notes[id] = fault
            continue
        try:
            with store.exclusive():
                note = _accept(store, extraction, caller, decision, row, at) if yes else \
                    _reject(store, extraction, caller, decision, row, at)  # type: ignore[arg-type]
        except _NotApplied as error:
            result.answers[id], result.notes[id] = "failed", f"{id} was not decided: {error}"
            continue
        if decision.reason.strip():
            note += " Your reason is on the row, for the proposer to read on its next run."
        result.answers[id], result.notes[id] = "decided", note
    return result


def _sunk(store: Store, result: DecideResult, entries: list[tuple[Decision, bool]]) -> list[str]:
    """The ids of a decide that were not decided as asked: any but one decided, or one that stood already as
    asked."""
    asked = {d.id: ACCEPTED if yes else REJECTED for d, yes in entries}
    state = {p.id: p.status for p in proposals(store) if p.id in asked}
    return [id for id, said in result.answers.items()
            if said != "decided" and not (said == "already decided" and state.get(id) == asked[id])]


def _land(store: Store, extraction: Extraction | None, caller: Caller, entries: list[tuple[Decision, bool]],
          clock: Clock, curators: Path | None,
          measure: Callable[[Callable[[], None]], None] | None = None) -> DecideResult:
    """Decide `entries` in one write that lands whole or not at all, run by `measure` when given, then withdraw the
    facts its retracts took back."""
    at = stamp(clock())
    held = _HeldFacts(extraction) if extraction is not None else None
    result = DecideResult()

    def write() -> None:
        nonlocal result
        with store.exclusive():
            result = _decided(store, held, caller, entries, at, curators)  # type: ignore[arg-type]
            if sunk := _sunk(store, result, entries):
                raise _Refused(", ".join(sunk))

    try:
        write() if measure is None else measure(write)
    except _Refused as refused:
        for id, said in result.answers.items():
            if said == "decided":
                result.answers[id], result.notes[id] = "not decided", (
                    f"{id} was not decided, since {refused} could not be, and a decide lands whole or not at all")
        return result
    except Exception as error:
        if isinstance(error, CerebroError) and not isinstance(error, StoreError):
            raise
        cause = error  # a failed undo of SQLite's raises over the error that ended the write
        while isinstance(cause.__context__, Exception):
            cause = cause.__context__
        raise CerebroError(f"decide stopped as it landed, and the store kept none of it: {cause}. Nothing was "
                           "applied.") from error
    for fact, error in (_release(store, held) if held is not None else {}).items():  # say what the facts layer kept
        by = next((id for id, note in result.notes.items() if result.answers[id] == "decided" and fact in note),
                  next(iter(result.notes)))
        said = result.notes[by].replace(f", {fact}", "").replace(f"Withdrew {fact}, ", "Withdrew ")
        result.notes[by] = said.replace(f" Withdrew {fact}.", "") + (
            f" The facts layer kept {fact}: {error}. So {fact} still stands, though {by} is accepted: deciding "
            f"{by} again changes nothing, and the retract tool takes {fact} back.")
    return result


def decide(store: Store, caller: Caller, accept: list[str | Decision] | None = None,
           reject: list[str | Decision] | None = None, *, extraction: Extraction | None = None,
           clock: Clock = system_clock, root: Path | None = None, curators: Path | None = None,
           measure: Callable[[Callable[[], None]], None] | None = None) -> DecideResult:
    """Accept or reject proposals, each by its id or by a `Decision` with a reason, in one write that lands whole or
    not at all: when one id cannot be decided as asked, none is, and the answers say which and why. An id that
    stands already as asked changes nothing and sinks nothing. Each decision is a new row of the `proposals` log for
    that id. An accepted admit, link, archive, merge or split is carried out by its collection's curator on its next
    run, and any other is applied now. An id in both lists, or more than 500 ids, refuses the whole call before
    anything is written. Which rows a curator decides is read from the curator files under `curators`, the
    workspace's root, or `root` when it is not given, as `_may_decide` reads them. `measure` runs the write."""
    check_caller(caller)
    entries = [(d if isinstance(d, Decision) else Decision(str(d)), True) for d in accept or []]
    entries += [(d if isinstance(d, Decision) else Decision(str(d)), False) for d in reject or []]
    entries = list({(d.id.strip(), yes): (replace(d, id=d.id.strip()), yes) for d, yes in entries}.values())
    if not entries:
        raise InputError("decide needs an id in accept or reject, such as p_0a1f")
    if len(entries) > MAX_DECIDE:
        raise InputError(f"decide takes at most {MAX_DECIDE} ids a call, and this one has {len(entries)}: decide "
                         "them in more than one call")
    accepted = {d.id for d, yes in entries if yes}
    both = list(dict.fromkeys(d.id for d, yes in entries if not yes and d.id and d.id in accepted))
    if both:
        raise InputError(f"decide: {', '.join(both)} is in both accept and reject, so nothing was decided: put each "
                         "id in one list")
    return _land(store, extraction, caller, entries, clock, curators or root, measure=measure)


# -- review ---------------------------------------------------------------------------------------------------------

def _change(store: Store, extraction: Extraction | None, p: Proposal) -> str:
    """What accepting a proposal would change, in one line."""
    if p.kind == "article.admit":
        try:
            id = _admitted(p.target, p.payload)[0]
        except InputError as error:
            return str(error)
        found, into = store.article(id), follow(id, redirects(store))
        if found is not None and found.identity.status == "merged" or found is None and into != id:
            now = store.article(into)  # it leads where the merge, or the list that grouped it, does
            said = (f"{id} was merged into {into}, " if found is not None else f"{id} leads to {into}, ") + (
                "which is in the working set already" if now is not None and now.identity.status == "active" else
                f"so {into} joins the working set again" if now is not None else
                f"so {into} becomes a new article in the working set")
        else:
            said = f"{id} joins the working set again" if found else f"{id} becomes a new article in the working set"
        if words := str(p.payload.get("instructions") or ""):
            said += f', and {p.by.name} instructs its writers, until a person gives theirs: "{words}"'
        return said
    if p.kind == "article.archive":
        from .curate import moot_archive  # imported here, as in `_apply`

        if why := moot_archive(store, p.target):
            return f"nothing: {why}, so accepting closes this row"
        return f"{p.target} leaves the working set: its status becomes archived, and a read by id still serves it"
    if p.kind == "article.merge":
        from .curate import brought_back, moot_archive  # imported here, as in `_apply`

        gone = next((why for why in (moot_archive(store, id) for id in (p.target, str(p.payload.get("with", ""))))
                     if "merged into" in why or why.endswith("is gone")), "")
        if gone:
            return f"nothing: {gone}, so accepting closes this row"
        if p.payload.get("distinct"):
            return (f"{p.target} and {p.payload.get('with', '?')} are kept apart: neither is merged into the other until "
                    "something new about them arrives")
        back = brought_back(store, p.target, str(p.payload.get("with", "")))
        return (f"{p.target} and {p.payload.get('with', '?')} become one article: {p.target}, the first named, stays, "
                "and the other's keys and facts lead there" + (f"; {back}" if back else ""))
    if p.kind == "article.link" and p.payload.get("text"):
        return f'{p.target}: its curator works out the change from "{p.payload["text"]}"'
    if p.kind == "article.link":
        keys, wrong = dict(p.payload.get("keys") or {}), dict(p.payload.get("wrong") or {})
        done = [f"{k} {v} added" for k, vs in keys.items() for v in vs or []] + \
            [f"{k} taken off" for k, vs in keys.items() if vs is None] + \
            [f"{k} {v} taken off" for k, vs in wrong.items() for v in vs] + \
            [f"link {x} added" for x in p.payload.get("linked") or []] + \
            [f"link {x} taken off" for x in p.payload.get("unlinked") or []]
        return f"{p.target}: {', '.join(done) or 'nothing changes'}"
    if p.kind == "article.split":
        into = redirects(store).get(_path(p.target), "the article it was merged into")
        return f"{p.target} is split back out of {into}"
    if p.kind == "article.instruct":
        return f'{p.target} gets an instruction for its writers: "{p.payload.get("text", "")}"'
    if p.kind == "fact.retract":
        found = extraction.facts(ids=[p.target]) if extraction is not None else []
        return f'fact {p.target} is withdrawn: "{found[0].text}"' if found else f"{p.target} is withdrawn"
    if p.kind == "record.retract":
        return f"record {p.target} is withdrawn, with every fact it gave"
    if p.kind == "record.expire":
        from .expiry import record_title, stood_down

        if stood_down(store, extraction, p):
            return (f"nothing: {p.target} changed after this expire and the facts it cited no longer hold, so it "
                    "stood down")
        return (f'"{record_title(store, p.target)}" ({p.target}) is marked out of date: its facts are shown marked to '
                "every reader, and writers leave them out of what holds today, until it changes")
    return f"{p.kind} of {p.target}, a kind this workspace does not apply"


def review(store: Store, caller: Caller | None = None, *, extraction: Extraction | None = None,
           root: Path | None = None) -> list[Review]:
    """The proposals waiting for `caller` to decide, a person when none is given, oldest first: each row, what
    accepting it would change, and why it was asked, with who asked and what it cites. With the workspace's `root`,
    a curator is given the rows `_may_decide` reads its file to give it."""
    deciding = caller or OWNER
    found = []
    for p in proposals(store, WAITING):
        if not _may_decide(store, deciding, p, root):
            continue
        why = f"{p.by.name or 'unknown'} ({p.by.kind}) asks: {p.reason or 'no reason given'}"
        if p.cites:
            why += f"; cites {', '.join(p.cites)}"
        title = ""
        if p.kind == "record.expire":
            from .expiry import record_title

            title = record_title(store, p.target)
        found.append(Review(p, _change(store, extraction, p), why, title))
    return found
