"""Curation: which articles a collection holds, and so which records are in the working set.

The working set is every article whose status is `active`. A record is in scope, and so extracted, when one of its
keys binds it to an article in the working set, and held when it binds only to articles out of it. A record bound to
none is held when it names a collection a curator keeps, and extracted otherwise, so a workspace with no curator
extracts what it lists.

A curator, `<collection>/curator.md`, runs on its collection through `compile.run_agent`, held to the compilation cap
and recorded like any run. Its tools, on every run:
- `admit` puts an article in the collection with the keys that bind records to it, or brings an archived one back;
- `link` changes the keys and links of an article of the collection, and `instruct` its note to the article's writers,
  each on the curator's own word;
- `archive` takes an article out of the working set and keeps it, marked `archived`;
- `merge` folds one article into another behind a redirect, or says two articles are distinct;
- `split` takes back the last merge into an article;
- `reject` turns down, with a reason, a proposal of another curator's on the collection that it may decide;
- `fetch`, where one of the workspace's sources answers questions, as `fetch` has it, and every tool an app gives.

`admit`, `link`, `archive`, `merge` and `split` take `collection`: the act on another curated collection waits there
as a proposal, for that collection's curator or a person.

Who decides: each call files a row of the `proposals` log. A kind the curator's `authority` holds true is accepted and
applied at once, and any other waits for a caller. `link` lands on the curator's own word. A proposal a caller accepted
is carried out by the collection's curator on its next run: its act names the proposal by id and lands, whatever its
authority, as the curator's own act, and the yes stays that caller's one decision. `apply` makes the change, the one
place articles change. Merges, redirects, splits and `distinct_from` pairs are rows of the `merges` log, and every
write goes through the store's own methods.

A curator may not undo what a caller decided, yes or no, until something new about it arrives: `turned_down` refuses
the act, and its deferred list shows the admits a caller turned down. On the workspace's first run a curator runs in
proposal mode, as `proposing` says: every admit it makes, into its collection or another, waits for a caller, so what
it would admit is one list a caller decides.
"""

from __future__ import annotations

import copy
import functools
import json
import re
import unicodedata
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent import Agent, AgentFileError, Authority, load_agent
from .apps import bind
from .article import Identity, Property
from .budget import Ledger
from .caller import Caller
from .clock import Clock, stamp, system_clock
from .compile import agent_surface, drop_material, field_refused, keep_judge, run_agent, unmirrored
from .connector import NOBODY, Connector, Ours, Record, registrable, same_event
from .connectors import CONNECTORS
from .errors import CerebroError
from .evidence import Evidence, EvidenceError, Item, KeepJudge, Starter, _item, fingerprint, gather
from .expiry import KIND as EXPIRE
from .facts import SAVED, Extraction
from .fetch import askable, fetch_tool
from .held import (
    FIRST_RUN_KEY,
    OWN_SIDE,
    Held,
    held_for,
    own_names,
    passed_first_run,
    pinned_waiting,
)
from .identity import ADMITTED_FROM, follow, reached, redirects, working, would_bind
from .lookup import LINKED, Lookup, OperationError
from .models import Decider, ModelSpec
from .project import read_ours
from .records import (
    APPLIES,
    TOLD,
    Decision,
    Gone,
    Proposal,
    _proposal,
    _refuse_secret,
    _slug,
    named_alike,
    proposals,
    propose,
)
from .run import WROTE_NOTHING, AgentRun, Run
from .store import Store, record_from, update_state
from .workspace import Workspace

if TYPE_CHECKING:
    from .app import Cerebro
    from .records import DecideResult

__all__ = [
    "APPLIES",
    "CurateError",
    "Curated",
    "address_form",
    "apply",
    "curator_order",
    "in_scope",
    "own_values",
    "proposing",
    "run_curator",
    "unbuilt",
    "working_set",
]


COLLECTION = re.compile(r"[a-z0-9][a-z0-9-]*")  # a folder under articles/, as an admit's collection names it
MAX_PER_CALL = 500  # records one call admits or leaves out
LEFT_OUT = "left-out"  # the log of the candidates a curator weighed and did not admit, each with its reason


class CurateError(CerebroError, ValueError):
    """A curator's call cannot land as asked. Its tool replies with the line, and the run goes on."""


@dataclass
class Curated:
    """What one curator run did: articles admitted and archived, proposals filed for a person, by id, and its run.
    `merged` and `split` name the articles folded away and brought back, and `notes` what the run went without. With
    nothing it reads moved since its last run, `skipped` is true and there is no run."""

    admitted: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)
    run: Run | None = None
    merged: list[str] = field(default_factory=list)
    split: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    carried: list[str] = field(default_factory=list)  # the accepted proposals it carried out, by id
    notes: list[str] = field(default_factory=list)
    skipped: bool = False
    read: int = 0  # what its evidence returned, every line's items: none when it had nothing to read yet


# -- the working set, and the order curators run in --------------------------------------------------------------------

def working_set(store: Store) -> list[Identity]:
    """Every article in the working set, in any collection: those `identity.working` holds, the same test `in_scope`
    binds a record by, so what the app hands a connector as its bounds is what a pass extracts."""
    return [article.identity for collection in store.collections() for article in store.query(collection)
            if working(store, article.identity.id)]


def in_scope(store: Store, record: Record, *, curated: Collection[str] = ()) -> bool:
    """Whether a listed record is extracted. It is when a key binds it to an article in the working set, and not
    when it binds only to articles out of it. One bound to none is held when its collection, `into`, is one of
    `curated`, the collections a curator keeps, and extracted otherwise. A key the record only carries counts here,
    as the domain of a part's parties puts the part in scope with its parent, though it binds the part to nothing."""
    ids = reached(store, record)
    if ids:
        return any(working(store, id) for id in ids)
    return not (record.into and record.into in curated)


def unbuilt(agents: list[Agent]) -> list[str]:
    """A line for each thing an agent file asks for that the workspace skips, naming the file, so nothing is skipped
    in silence: a curator that declares no evidence, which reads no held record."""
    lines = []
    for agent in agents:
        name = f"agents/{agent.id}.md"
        if agent.role == "curator" and not agent.evidence:
            lines.append(f"{name} declares no evidence, so it reads no held record and runs on its "
                         "tools alone: add - held: {} under evidence: to read what its sources hold for "
                         f"{agent.target}")
    return lines


def _reads(agent: Agent) -> set[str]:
    """The collections an agent's evidence reads by name: a `query` or `left` from one, and an `articles` line's
    paths and folders, each by its first part."""
    read: set[str] = set()
    for line in agent.evidence:
        args = line.args if isinstance(line.args, dict) else {}
        if line.kind in ("query", "left") and isinstance(args.get("from"), str):
            read.add(args["from"].removeprefix("articles/"))
        elif line.kind == "articles":
            paths = line.args if isinstance(line.args, list) else [line.args]
            read |= {str(p).removeprefix("articles/").split("/")[0] for p in paths if isinstance(p, str)}
    return read


def curator_order(curators: list[Agent]) -> list[Agent]:
    """The order curators run in, one at a time, as a person reads it from the workspace's folder: a curator whose
    evidence reads another curator's collection runs after that curator, so it weighs what that one admitted in the
    same pass, and otherwise in the order of their files' paths. Curators that read each other run in path order."""
    left = sorted(curators, key=lambda agent: agent.id)
    by_collection = {agent.target: agent.id for agent in left}
    after = {agent.id: {by_collection[c] for c in _reads(agent) if c in by_collection} - {agent.id} for agent in left}
    order: list[Agent] = []
    while left:
        placed = {agent.id for agent in order}
        ready = next((agent for agent in left if after[agent.id] <= placed), None)
        if ready is None:  # a cycle: its first by path goes first, and the rest follow as they can
            ready = left[0]
        order.append(ready)
        left.remove(ready)
    return order


def nothing_to_weigh(agent: Agent, *, lookup: Lookup, store: Store, functions: dict[str, Callable[..., Any]],
                     workspace: Workspace | None, root: Path, clock: Clock, sources: Mapping[str, Connector],
                     ours: Any = None) -> bool:
    """Whether the curator `agent` has nothing to propose from: no source answers its questions, no other curator's
    proposal waits on its collection, its sources hold no record for its collection, whether or not its file reads
    them, and its other evidence lines gather nothing. A line that does not read is its run's to say, so the curator
    is asked."""
    if askable(sources) or _on_yours(store, agent):
        return False
    if held_for(store, agent.target, sorted(sources), ours=ours).total:
        return False
    lines = [line for line in agent.evidence if line.kind != "held"]
    try:
        gathered = gather(replace(agent, evidence=lines), lookup, functions, workspace, writers=[agent.id], root=root,
                          now=clock(), sources=sources)
    except Exception:  # noqa: BLE001 - the run says what the gather could not read
        return False
    return not any(entry.items or entry.files for entry in gathered)


# -- who decides: proposal mode, and what a person decided -------------------------------------------------------------

def proposing(store: Store, agent: str) -> bool:
    """Whether the curator `agent`, by its id, runs in proposal mode: each admit it makes waits for a person, whatever
    its authority says. That holds for every curator while the workspace's first run, as `first_run`
    keeps it, is undecided, and after it for a curator no admit of which a person has decided since it began. With no
    first run kept, a curator is in proposal mode while it is late: its own first run is not over
    (`in_own_first_run`), and the workspace's is, by `pass_first_run` or `left_first_run`. A workspace that never began
    a first run and that nothing went past has no proposal mode."""
    session = store.state(FIRST_RUN_KEY)
    if not isinstance(session, dict):
        return (passed_first_run(store) or left_first_run(store)) and in_own_first_run(store, agent)
    if session.get("status") != "done":
        return True
    since = str(session.get("at") or "")
    return not any(p.kind == "article.admit" and p.by.name == agent and p.status in ("accepted", "rejected")
                   and p.decided_by is not None and p.decided_by.kind == "person" and (p.decided_at or "") >= since
                   for p in proposals(store))


def for_a_person(store: Store, waiting: Iterable[Proposal]) -> set[str]:
    """The ids of the `waiting` proposals a person decides, whatever collection they are for, so no curator holds
    them: each a list showed a person who left it, as the first run's session keeps it offered, and each admit a
    curator in proposal mode filed, a row of its late list."""
    session = store.state(FIRST_RUN_KEY)
    offered = {str(id) for id in session.get("offered") or []} if isinstance(session, dict) else set()
    admits = [p for p in waiting if p.kind == "article.admit" and p.by.kind == "agent"]
    listing = {name for name in {p.by.name for p in admits} if name.endswith("/curator") and proposing(store, name)}
    return offered | {p.id for p in admits if p.by.name in listing}


def in_own_first_run(store: Store, agent: str) -> bool:
    """Whether the curator `agent`, by its id, is still in its own first run: no decided proposal
    ends it. One the curator filed ends it, whoever decided it, and so does an archive
    or a merge of an article of its collection, or an admit into it by a caller that is no agent, other than a
    person's own admit by hand. A correction or an instruction ends nothing."""
    collection = agent.split("/")[0] + "/"
    for p in proposals(store):
        if p.status == "waiting" or p.kind in ("fact.retract", "record.retract", "article.instruct"):
            continue
        if p.by.name == agent:
            return False
        if p.kind == "article.admit" and p.target.startswith(collection) and p.by.kind not in ("agent", "person"):
            return False
        if p.kind in ("article.archive", "article.merge") and p.target.startswith(collection):
            return False
    return True


def left_first_run(store: Store) -> bool:
    """Whether some curator's collection has left its first run, which puts the workspace past its own: a decided
    proposal of a curator's own, or a decided archive or merge."""
    return any(p.status != "waiting" and p.kind not in ("fact.retract", "record.retract", "article.instruct")
               and (p.by.kind == "agent" and p.by.name.endswith("/curator")
                    or p.kind in ("article.archive", "article.merge")) for p in proposals(store))


def renewed(store: Store, sources: Collection[str], payload: Mapping[str, Any], since: str) -> bool:
    """Whether something new names what a deferred admit named: a record one of `sources` holds, dated after `since`,
    that the admit's keys or name would bind. A deferral holds until then."""
    keys = {str(k): list(v) for k, v in dict(payload.get("keys") or {}).items() if isinstance(v, list)}
    name = str(payload.get("name") or payload.get("title") or "")
    for source in sources:
        for row in store.state(f"held:{source}") or []:
            record = record_from(row)
            if record.at and since and record.at > since and would_bind(store, keys, name, record):
                return True
    return False


OPPOSITE = {"article.admit": "article.archive", "article.archive": "article.admit",
            "article.merge": "article.split", "article.split": "article.merge"}
KEEP = "article.keep"  # a merge that keeps its two articles apart, told from a merge


def _act(p: Proposal) -> str:
    return KEEP if p.kind == "article.merge" and p.payload.get("distinct") else p.kind


def _thing(moved: dict[str, str], kind: str, target: str, payload: Mapping[str, Any]) -> Any:
    """What an act is of: a merge's, keep-apart's or split's pair, in either order, or the article a path leads to,
    by the redirects `moved`."""
    if kind in ("article.merge", "article.split", KEEP):
        return frozenset({target, str(payload.get("with") or payload.get("from") or "")})
    return follow(target, moved)


def _when(p: Proposal) -> str:
    """When a caller's decision was made: for a yes carried out, when it was carried out."""
    return str(p.applied.get("at") or p.decided_at) if p.status == "accepted" else p.decided_at


def _latest(rows: Iterable[Proposal]) -> Proposal | None:
    """The latest decision of `rows`: of two at one time, the one the other names, which it carried out."""
    rows = list(rows)
    named = {id for p in rows for id in p.cites}
    return max(rows, key=lambda p: (_when(p), p.id in named), default=None)


def record_at(store: Store, source: str, id: str) -> str:
    """A record's own time, as its source or teller gave it, or "" when it gave none: a told record's, or what the
    facts layer kept when it took the record in."""
    if (told := store.record(id)) is not None:
        return told.at or ""
    return str((store.state(f"{SAVED}{source}/{id}") or {}).get("dated") or "")


def _news(store: Store, extraction: Extraction | None, sources: Collection[str], articles: Iterable[str],
          payload: Mapping[str, Any], since: str) -> bool:
    """Whether something new arrived about these articles after `since`: a fact tied to one whose record's own time
    is later, or a record held for its collection, dated later, that its keys or name would bind. A fact's tie time
    is no measure: a pass ties old facts again, and an admit brings old held records in."""
    ids = [f for a in articles for f in store.facts_of(a)]
    for fact in extraction.facts(ids=ids) if extraction is not None and ids else []:
        if (at := record_at(store, fact.source, fact.record)) and at > since:
            return True
    for a in articles:
        found = store.article(a)
        named = {"keys": found.identity.keys, "name": found.identity.title} if found is not None else payload
        if renewed(store, sources, named, since):
            return True
    return False


def _undoes(act: str, p: Proposal) -> bool:
    """Whether the act undoes the caller's decision `p` on the same thing: the opposite of what it accepted, or what
    it rejected. A merge undoes a keep-apart too."""
    if p.status == "rejected":
        return _act(p) == act
    return _act(p) == OPPOSITE.get(act) or act == "article.merge" and _act(p) == KEEP


def _link_values(store: Store, p: Proposal | None, payload: Mapping[str, Any], target: str) -> tuple[set[Any], set[Any]]:
    """The values a link adds and takes off, as (key name as `word` folds it, value in lower case): from what a row
    carried out, or from what it asks when it was not carried out, as one rejected."""
    if p is not None and p.applied:
        keys, links = p.applied.get("keys") or {}, p.applied.get("linked") or {}
        added, removed = ({*((word(k), str(v).lower()) for k, vs in (keys.get(part) or {}).items() for v in vs),
                           *(("linked", str(v).lower()) for v in links.get(part) or [])} for part in ("added", "removed"))
        return added, removed
    found = store.article(target)
    held = found.identity.keys if found is not None else {}
    adds = {(word(k), str(v).lower()) for k, vs in dict(payload.get("keys") or {}).items() for v in vs or []}
    offs = {(word(k), str(v).lower()) for k, vs in dict(payload.get("wrong") or {}).items() for v in vs}
    offs |= {(word(k), str(v).lower()) for k, vs in dict(payload.get("keys") or {}).items() if vs is None
             for v in held.get(k, [])}
    adds |= {("linked", str(v).lower()) for v in payload.get("linked") or []}
    offs |= {("linked", str(v).lower()) for v in payload.get("unlinked") or []}
    return adds, offs


def turned_down(store: Store, extraction: Extraction | None, sources: Collection[str], kind: str, target: str,
                payload: Mapping[str, Any], named: Iterable[Proposal] = ()) -> str:
    """Why a curator's act would undo what a caller decided, or "": the caller's latest word on the act and its
    opposite for this thing, when the act would undo it and nothing new has arrived since. The accepted proposals
    the act carries out, `named`, are their caller's latest word on their own things, so they never refuse it."""
    named = list(named)
    act = KEEP if kind == "article.merge" and payload.get("distinct") else kind
    rows = [replace(p, target=str(p.applied.get("article") or p.target))  # a yes carried out is of the article it landed on
            for p in proposals(store) if p.decided_by is not None and p.decided_by.kind == "person"
            and p.status in ("accepted", "rejected") and p.kind in (kind, OPPOSITE.get(kind, kind))
            and all(p.id != q.id for q in named)]
    if kind == "article.link":
        return _link_turned_down(store, extraction, sources, target, payload, rows, named)
    moved = redirects(store)
    thing = _thing(moved, act, target, payload)
    if any(_thing(moved, _act(q), q.target, q.payload) == thing for q in named):
        return ""
    same = [p for p in rows if _thing(moved, _act(p), p.target, p.payload) == thing]
    latest = _latest(same)
    if latest is None or not _undoes(act, latest) or _news(store, extraction, sources, sorted(thing) if isinstance(
            thing, frozenset) else [thing], payload, _when(latest)):
        return ""
    asks = "" if not named else (f". The ask this act carries out, {', '.join(q.id for q in named)}, stays accepted "
                                 f"until then, or until a caller asks for {thing} itself")
    return _refusal(store, latest, act) + asks


def _link_turned_down(store: Store, extraction: Extraction | None, sources: Collection[str], target: str,
                      payload: Mapping[str, Any], rows: list[Proposal], named: list[Proposal]) -> str:
    """Why a link would undo a value a caller added or took off, or "": checked value by value, and refused whole."""
    moved = redirects(store)
    here = follow(target, moved)
    adds, offs = _link_values(store, None, payload, here)
    asked = {(v, way == 0) for q in named for way, side in enumerate(_link_values(store, None, q.payload, here))
             for v in side}  # each value a named ask gives, with whether it adds it
    words = {w.strip(".,;:!?\"'()[]<>") for q in named for w in str(q.payload.get("text") or "").lower().split()}
    for value, adding in [*((v, True) for v in sorted(adds)), *((v, False) for v in sorted(offs))]:
        if (value, adding) in asked or value[1] in words:  # a value a caller's yes gives, whole, is its latest word
            continue
        said = []
        for p in rows:
            if follow(p.target, moved) != here:
                continue
            put, took = _link_values(store, p, p.payload, here)
            if value in put or value in took:
                said.append((p, value in put))
        if (p := _latest(q for q, _ in said)) is None:
            continue
        put = {q.id: added for q, added in said}[p.id]
        undoes = put != adding if p.status == "accepted" else put == adding
        if not undoes or _news(store, extraction, sources, [here], payload, _when(p)):
            continue
        return _refusal(store, p, "article.link", value, adding)
    return ""


def _refusal(store: Store, p: Proposal, act: str, value: tuple[str, str] | None = None, adding: bool = True) -> str:
    """The line that names the caller's decision an act would undo, its reason, and what lifts it. It says "a caller",
    never a name: a curator reads its own refusal."""
    told = next((r.body for id in p.cites if (r := store.record(id)) is not None), "")
    why = " ".join((p.decision_reason or p.reason or told).split())
    why = f": {why}" if why else ""
    pair = " and ".join(sorted(_thing({}, _act(p), p.target, p.payload))) if p.kind in (
        "article.merge", "article.split") else p.target
    if p.status == "rejected":
        verb = "keep-apart" if _act(p) == KEEP else p.kind.partition(".")[2]
        return (f"a caller rejected {verb} of {pair} in {p.id}{why}: it is not done again until a new fact or record "
                "about it arrives")
    if value is not None:
        did = f"{'added' if not adding else 'took off'} {value[0]} {value[1]} {'to' if not adding else 'from'} {p.target}"
        undo = "taking it off" if not adding else "adding it again"
    else:
        did = {"article.admit": f"admitted {p.target}", "article.archive": f"archived {p.target}",
               "article.merge": f"merged {pair}", KEEP: f"kept {pair} apart",
               "article.split": f"split {p.target} from {p.payload.get('from') or 'the article it was merged into'}"}[
            _act(p)]
        undo = {"article.admit": "an admit of it", "article.archive": "an archive of it",
                "article.merge": "a merge of them", "article.split": "a split of them"}[act]
    return f"a caller {did} in {p.id}{why}: {undo} is not done until a new fact or record about it arrives"


def waiting_item(store: Store, target: str, payload: Mapping[str, Any] | None = None) -> list[Proposal]:
    """The admits that wait for a person to make the article `target` leads to: one item, decided as one. With
    `payload`, only those that may be the same thing as it, as `one_thing` says."""
    moved, weights = redirects(store), weights_of(store)
    here = follow(target, moved)
    return [p for p in proposals(store, "waiting") if p.kind == "article.admit"
            and follow(p.target, moved) == here
            and (payload is None or one_thing(p.payload, payload, weights))]


def waits_alike(store: Store, collection: str, payload: Mapping[str, Any], curator: str) -> bool:
    """Whether an admit into `collection` waits for a person that names the record an ask names, or one of its key
    values: the person decides whether the two are one thing, so the ask waits beside it. A curator, by its id,
    passes over another curator's: its own act answers that one."""
    def values(of: Mapping[str, Any]) -> set[tuple[str, str]]:
        return {(k, str(v).strip().lower()) for k, vs in dict(of.get("keys") or {}).items() if k != "alias"
                and isinstance(vs, list) for v in vs}

    return any(p.kind == "article.admit" and not another_curators(p, curator)
               and p.target.startswith(collection + "/") and (payload.get("record") and p.payload.get("record") ==
                                                              payload.get("record") or values(p.payload) & values(payload))
               for p in proposals(store, "waiting"))


def another_curators(p: Proposal, curator: str) -> bool:
    """Whether a curator other than `curator`, by its id, filed the row: never a caller's."""
    return p.by.kind == "agent" and p.by.name.count("/") == 1 and p.by.name.endswith("/curator") \
        and p.by.name != curator


def cited(rows: Iterable[Proposal]) -> set[str]:
    """The ids the waiting rows among `rows` name, and those not carried out yet: each waits, or is carried out, with
    the act that names it."""
    return {id for q in rows if q.status == "waiting" or not carried_out(q) for id in q.cites}


def carried_out(p: Proposal) -> bool:
    """Whether a row's act is done: any but an accepted admit, link, archive, merge or split with `applied` empty,
    which its collection's curator carries out."""
    return not (p.status == "accepted" and p.kind in APPLIES and not p.applied)


def may_answer(agent: Agent, p: Proposal, named: Collection[str]) -> bool:
    """Whether the curator `agent` answers the row `p` of its collection: an accepted one not carried out yet, whoever
    filed it and whatever act names it, or a waiting one another curator filed that is not among `named`, the rows a
    waiting act names, as `cited` gives them."""
    if not p.target.startswith(agent.target + "/"):
        return False
    return not carried_out(p) or p.id not in named and p.status == "waiting" and another_curators(p, agent.id)


def one_thing(one: Mapping[str, Any], other: Mapping[str, Any], weights: Mapping[str, int]) -> bool:
    """Whether two asks for an article may be one thing, whatever their names: by the strongest key that identifies
    a thing which both carry, by `weights`, one when they share a value of it, whatever their records, as an event and
    its recording do, and else two; two when each carries such keys and none is carried by both. When neither carries
    one, two when they name different records, else one."""
    def record(payload: Mapping[str, Any]) -> tuple[str, str] | None:
        ref = payload.get("record")
        return (str(ref.get("source")), str(ref.get("id"))) if isinstance(ref, dict) else None

    keys = [dict(p.get("keys") or {}) for p in (one, other)]
    if (same := same_thing(*keys, weights)) is not None:
        return same
    if all(any(weights.get(k, 0) > 0 and vs for k, vs in held.items()) for held in keys):
        return False
    return not (record(one) and record(other) and record(one) != record(other))


def same_thing(one: Mapping[str, Iterable[Any]], other: Mapping[str, Iterable[Any]],
               weights: Mapping[str, int]) -> bool | None:
    """Whether two sets of keys name one thing, by the strongest key that names one thing, one `weights` puts above 0,
    which both carry: one when they share a value of it, as an event and its recording do, and two when they share
    none. None when they carry no such key in common. A weaker key shared, as a domain, makes no two things one.
    Values are read as an admit writes them, so `www.alder.example` is `alder.example`."""
    mine, theirs = ({k: _lower(vs) for k, vs in address_form({k: list(vs) for k, vs in keys.items()}).items()
                     if weights.get(k, 0) > 0} for keys in (one, other))
    if not (both := [k for k in mine if k in theirs]):
        return None
    top = max(weights[k] for k in both)
    return any(same_event(mine[k], theirs[k]) if k == "calendar_event_id" else bool(mine[k] & theirs[k])
               for k in both if weights[k] == top)


def free_path(store: Store, base: str) -> str:
    """`base`, or the first of `base-2`, `base-3` and on that no article holds."""
    id, n = base, 1
    while store.article(id) is not None:
        n += 1
        id = f"{base}-{n}"
    return id


def repeated(store: Store, by: Caller, kind: str, target: str, payload: dict[str, Any]) -> str:
    """Why an ask is not filed, or "": the same asker's own ask of the same kind, target and payload waits already, so
    it says nothing new. One that differs in any of them is filed beside it."""
    same = next((p for p in proposals(store, "waiting") if p.by == by and p.kind == kind and p.target == target
                 and p.payload == payload), None)
    return f"{kind.partition('.')[2]} of {target} waits already, in {same.id}, with nothing new: it is not filed " \
        "again" if same is not None else ""


def moot_archive(store: Store, target: str) -> str:
    """Why an archive filed earlier has nothing left to do with the article as it stands, or "": it was merged away
    or archived since."""
    found = store.article(target)
    if found is None:
        return f"{target} is gone"
    article = found.identity
    if article.status == "merged":
        return f"{article.id} was merged into {redirects(store).get(article.id, 'another article')} since"
    if article.status == "archived":
        return f"{article.id} is archived already"
    return ""


def moot(store: Store, p: Proposal) -> str:
    """Why an accepted proposal not carried out yet has nothing left to do, or "": its target is gone, or its change
    is made already."""
    if p.kind == "article.admit":
        found = store.article(follow(p.target, redirects(store)))
        if found is None or found.identity.status != "active":
            return ""
        made = [q.payload for q in proposals(store, "accepted") if q.kind == "article.admit"
                and q.applied.get("article") == found.identity.id]  # another thing of its name is made at a free path
        weights = weights_of(store)
        return f"{found.identity.id} is in the working set already" if all(one_thing(m, p.payload, weights)
                                                                            for m in made) else ""
    if p.kind == "article.archive":
        return moot_archive(store, p.target)
    if p.kind == "article.merge":
        return next((why for id in (p.target, str(p.payload.get("with", ""))) if "merged into" in (
            why := moot_archive(store, id)) or why.endswith("is gone")), "")
    if p.kind == "article.split":
        found = store.article(p.target)
        return "" if found and found.identity.status == "merged" else f"{p.target} is not merged into another article"
    return "" if store.article(p.target) else f"{p.target} is gone"


def yes_left(store: Store, collection: str) -> list[str]:
    """The ids of the accepted proposals of `collection` its curator has not carried out yet, oldest first."""
    return [p.id for p in proposals(store, "accepted") if not carried_out(p) and p.target.startswith(collection + "/")]


def new_yes(store: Store, agent: Agent) -> bool:
    """Whether a proposal of the curator's collection was accepted since its last run, and waits to be carried
    out: a new yes is new work, whatever the curator's heartbeat."""
    since, left = store.state(f"told:{agent.id}") or 0, set(yes_left(store, agent.target))
    return any(row.seq > since and row.data.get("id") in left and row.data.get("status") == "accepted"
               for row in store.rows("proposals"))


def close_moot(store: Store, collections: Collection[str]) -> list[str]:
    """Withdraw each accepted proposal of `collections` not carried out yet that has nothing left to do, with why,
    so no curator's run is paid for it. Gives a line for each."""
    lines = []
    for p in proposals(store, "accepted"):
        if not carried_out(p) and p.target.split("/")[0] in collections and (why := moot(store, p)):
            propose(store, replace(p, status="withdrawn", decision_reason=f"{why}, so nothing is left to do"))
            lines.append(f"{p.id} {p.kind.partition('.')[2]} of {p.target}: withdrawn, as {why}")
    return lines


def brought_back(store: Store, a: str, b: str) -> str:
    """What a merge of `a` and `b` would undo, or "": an archive of one while the other is in the working set, as
    the merged article stays in it, with the row that archived it and who decided it."""
    found = [store.article(id) for id in (a, b)]
    status = [f.identity.status if f is not None else "" for f in found]
    if sorted(status) != ["active", "archived"]:
        return ""
    gone = (a, b)[status.index("archived")]
    row = next((p for p in reversed(proposals(store, "accepted")) if p.kind == "article.archive" and p.target == gone),
               None)
    by = f", archived in {row.id} by {row.decided_by.name if row.decided_by else row.by.name}" if row else ""
    return f"{gone} is archived{by}, and it comes back into the working set in the merged article"


# -- keys: as an admit writes them ------------------------------------------------------------------------------------

# How strong a key is as an identity, which tells two asks for an article apart: the engine's address keys here, and a key a source issues
# as its connector's `IDENTITY_KEYS`. A key nobody declares weighs 0.
ADDRESS_WEIGHTS = {"email": 100, "domain": 10}


class KeyWeightError(CerebroError):
    """Two connectors declare different weights for one key, so a merge could not tell which holds."""


def key_weights(connectors: Mapping[str, type[Connector]] | None = None) -> dict[str, int]:
    """The weight of each key: the engine's address keys, and each key a connector of `connectors` declares, by
    default every kind the app builds, whether or not a workspace has a source of it, since a key can arrive through
    another source. Two connectors that declare one key at different weights are refused."""
    connectors = CONNECTORS if connectors is None else connectors
    table = dict(ADDRESS_WEIGHTS)
    said: dict[str, str] = {key: "the engine's address keys" for key in table}
    for kind, built in sorted(connectors.items()):
        for key, weight in dict(getattr(built, "IDENTITY_KEYS", {}) or {}).items():
            key, weight, who = str(key), int(weight), f"{built.__name__} (kind {kind})"
            if key in table and table[key] != weight:
                raise KeyWeightError(f"the key {key} weighs {table[key]} by {said[key]} and {weight} by {who}: "
                                     "declare one weight for it, on the connector that issues it")
            table[key], said[key] = weight, said.get(key, who)
    return table


def workspace_weights(connectors: Iterable[Connector]) -> dict[str, int]:
    """The built-in table, and each key only a workspace's own connectors declare, at the weight they agree on. A
    built-in weight stands whatever a workspace's connector says of the key, so no source's name decides it."""
    built, said = key_weights(), dict[str, set[int]]()
    for connector in connectors:
        for key, weight in connector.IDENTITY_KEYS.items():
            said.setdefault(str(key), set()).add(int(weight))
    return {**{key: weights.pop() for key, weights in said.items() if len(weights) == 1}, **built}


def weights_of(store: Store) -> Mapping[str, int]:
    """How strong each key is as an identity in a workspace: as the app set it on the store from its sources, else
    the table every kind of source declares."""
    return store.weights if store.weights is not None else key_weights()


def holder(store: Store, collection: str, key: str, value: str, but: str = "") -> str | None:
    """The article of `collection`, but `but`, that holds a key value, or None. One value binds one article only where
    its key names the thing: a key a source issues at weight 0 is shared, and may sit on many."""
    if weights_of(store).get(key) == 0:
        return None
    return next((id for id in store.bound(key, value) if id != but and id.startswith(collection + "/")), None)


def address_form(keys: dict[str, list[Any]]) -> dict[str, list[str]]:
    """Keys as a connector's `address_keys` writes them, so an admit binds what a source lists: an `email` in lower
    case, and a `domain` as the part an organization registers, so `www.Alder.example` is `alder.example`."""
    out: dict[str, list[str]] = {}
    for key, values in keys.items():
        written = [str(v).strip() for v in values if str(v).strip()]
        if key == "email":
            written = [v.lower() for v in written]
        elif key == "domain":
            written = [registrable(v) for v in written]
        if written:
            out[str(key)] = list(dict.fromkeys(written))
    return out


def word(name: str) -> str:
    """A key name folded for case and spacing, so `Thing Code` and `thing_code` read alike."""
    return "_".join(name.casefold().replace("_", " ").split())


def read_keys(given: Iterable[tuple[Any, list[Any]]], spellings: Sequence[str]) -> dict[str, list[Any]]:
    """Keys as a curator gives them, read as it means them. A name that is one of `spellings` is itself. Any other is
    read as the first of them that spells it in another case or spacing, so `Thing Code` is a source's `thing_code`,
    else as it was first given, so two spellings of one name are one key."""
    spelled: dict[str, str] = {}
    for name in spellings:
        spelled.setdefault(word(name), name)
    out: dict[str, list[Any]] = {}
    for name, values in given:
        name = str(name).strip()
        key = name if name in spellings else spelled.setdefault(word(name), name)
        out[key] = [*out.get(key, []), *values]
    return out


def own_values(keys: dict[str, list[str]], name: str) -> dict[str, list[str]]:
    """Keys as one article holds them: a value that lists several names between commas, as a meeting note's
    `Attendees: Nessa Torvik, Sam Okoye` line, is the article's own only in its own name, so a person admitted by such a
    line binds by their own name and not by the line, which would bind every record of the people beside them. A value
    that is the name whole, as `Okoye, Sam`, stays as it is, and a list that does not hold the name is dropped."""
    out: dict[str, list[str]] = {}
    want = " ".join(name.lower().split())
    for key, values in keys.items():
        kept = []
        for value in values:
            parts = [" ".join(p.split()) for p in value.split(",") if p.strip()]
            if "," not in value or " ".join(value.lower().split()) == want:
                kept.append(value)
            else:
                kept += [p for p in parts if p.lower() == want]
        if kept:
            out[key] = list(dict.fromkeys(kept))
    return out


# -- applying an accepted proposal: the one place admit, archive, merge and split change articles ----------------------

def apply(store: Store, proposal: Proposal, at: str) -> tuple[dict[str, Any], str]:
    """Make the change an accepted admit, link, archive, merge or split asks for, through the store's own methods, and
    give what it changed, for the row's `applied`, and one line that says so. A curator's act lands here, its own on
    its authority, or one that carries out what a caller accepted. Raises `Gone` when an article the row names is
    gone, and `CurateError` when the row cannot land as asked."""
    kind, target, payload = proposal.kind, proposal.target, proposal.payload
    if kind == "article.admit":
        return _admit(store, target, payload, proposal.by, at, proposal.reason)
    if kind == "article.archive":
        return _archive(store, target, at)
    if kind == "article.merge":
        return _merge(store, target, payload, proposal, at)
    if kind == "article.split":
        return _split(store, target, proposal, at)
    if kind == "article.link":
        return _link(store, proposal, at)
    raise CurateError(f"proposal {proposal.id} has kind {kind!r}, which curate does not apply: use one of "
                      f"{', '.join(APPLIES)}")


def _admit(store: Store, target: str, payload: dict[str, Any], by: Caller, at: str,
           reason: str) -> tuple[dict[str, Any], str]:
    """Admit the article an accepted admit names, binding the held record it was made of, and stand its
    instructions on it."""
    applied, note = _landed(store, target, payload, by, at, reason)
    ref = payload.get("record")
    if isinstance(ref, dict) and _lands_in(store, ref, applied["article"].rpartition("/")[0]):
        # the article binds the record of its own collection it was made of
        update_state(store, ADMITTED_FROM, lambda kept: {**(kept or {}),
                                                         f"{ref['source']}:{ref['id']}": applied["article"]})
    said = instruct(store, applied["article"], str(payload["instructions"]), by, at) \
        if payload.get("instructions") else ""
    if said == "written":
        note += " Its instructions stand on it for its writers."
    elif said == "superseded":
        note += " Its instructions were " + SUPERSEDED.removeprefix("not ")
    return applied, note


def _landed(store: Store, target: str, payload: dict[str, Any], by: Caller, at: str,
            reason: str) -> tuple[dict[str, Any], str]:
    """Make the article an admit names, or bring it back into the working set, with the keys no other article of its
    collection holds; one a merge folded away is admitted as the article it was merged into. An article in the
    working set already, as one an ask by name finds, changes in nothing but what the admit adds."""
    found = store.article(target)
    keys, left = _held_elsewhere(store, target, address_form(dict(payload.get("keys") or {})))
    collection, _, name = target.rpartition("/")
    title = str(payload.get("name") or payload.get("title") or name.replace("-", " ").capitalize())
    _refuse_secret(store, target, title, keys, CurateError)
    if found is None:
        store.create(Identity(target, collection, title.strip(), keys, "active", by, at, reason.strip()))
        if target in redirects(store):  # a path a fold led elsewhere names this article from now on
            store.append("merges", {"at": at, "redirect": target, "path": target})
        unmirrored(store, target, True)  # its copy is written as the pass ends, so a link to it opens unwritten
        set_ = _set_fields(store, target, payload, by, at)
        return {"at": at, "article": target, "created": True} | set_, \
            f"{target} is a new article in the working set.{left}"
    article, merged = found.identity, ""
    if article.status == "merged":  # its id leads where the merge does
        into = store.article(follow(article.id, redirects(store)))
        if into is None or into.identity.status == "merged":
            raise CurateError(f"{article.id} was merged into another article: admit that one")
        merged, article = f"{article.id} was merged into {into.identity.id}, so the admit is of it. ", into.identity
        keys, left = _held_elsewhere(store, article.id, address_form(dict(payload.get("keys") or {})))
    added = {k: [v for v in vs if v.lower() not in _lower(article.keys.get(k, []))] for k, vs in keys.items()}
    added = {k: vs for k, vs in added.items() if vs}
    for key, values in added.items():
        article.keys[key] = article.keys.get(key, []) + values
    back = article.status != "active"
    if back:
        article.status, article.admitted_by, article.admitted_at, article.admitted_reason = "active", by, at, reason.strip()
    article.title = article.title or str(payload.get("name") or payload.get("title") or "")
    store.set_identity(article)
    if back:
        unmirrored(store, article.id, True)  # its copy no longer says archived as the pass ends
    set_ = _set_fields(store, article.id, payload, by, at)
    if not back and not added:
        return {"at": at, "article": article.id, "created": False} | set_, \
            f"{merged}{article.id} is in the working set already.{left}"
    return ({"at": at, "article": article.id, "created": False, "keys": added} | ({"back": True} if back else {}) | set_,
            f"{merged}{article.id} joins the working set{' again' if back else ''}.{left}")


def _held_elsewhere(store: Store, target: str, keys: dict[str, list[str]]) -> tuple[dict[str, list[str]], str]:
    """The keys an admit adds to its article, and a line for each one left off: one another article of the target's
    collection holds, since an admit that waited may find its key taken meanwhile and one key binds one article."""
    kept, said = {}, []
    for key, values in keys.items():
        for value in values:
            if other := holder(store, target.rpartition("/")[0], key, value, target):
                said.append(_stays(key, value, other))
            else:
                kept.setdefault(key, []).append(value)
    return kept, "".join(f" {line}" for line in said)


def _stays(key: str, value: str, other: str) -> str:
    return f"The key {key} {value} stays on {other}, which holds it, so it is not added."


def _lands_in(store: Store, ref: dict[str, Any], collection: str) -> bool:
    """Whether the held record `ref` names, by source and id, lands in `collection`, as its listing says."""
    return any(row.get("id") == ref.get("id") and row.get("into") == collection
               for row in store.state(f"held:{ref.get('source')}") or [])


def _set_fields(store: Store, article: str, payload: dict[str, Any], by: Caller, at: str) -> dict[str, Any]:
    """Write the front matter fields an admit gives on its article, as its curator wrote them, each under the guard an
    article's agents write under: a field the guard refuses is left as it is. Gives the names it set, for `applied`."""
    given = payload.get("fields") or {}
    set_ = [name for name, value in given.items() if not field_refused(article, name, value)]
    for name in set_:
        store.set_field(article, name, Property(given[name], by, at))
    return {"fields": set_} if set_ else {}


def _link(store: Store, proposal: Proposal, at: str) -> tuple[dict[str, Any], str]:
    """Change the keys and links of the article an accepted link names, as it stands now: each value of `wrong`, and
    each key `keys` gives none, off; each other value of `keys` it lacks on, but one another article of its
    collection holds; `unlinked` out of its links, and `linked` in. Gives what changed, and a line naming each value
    it passed over."""
    article, payload, said = _article_of(store, proposal.target), proposal.payload, []
    removed: dict[str, list[str]] = {}
    for key, values in [*((k, None) for k, v in (payload.get("keys") or {}).items() if v is None),
                        *(payload.get("wrong") or {}).items()]:
        held = article.keys.get(key, [])
        off = held if values is None else [v for v in held if v.lower() in _lower(values)]
        said += [f" {key} {v} was not on it." for v in values or [] if v.lower() not in _lower(held)]
        said += [f" {key} was not on it."] if values is None and not held else []
        if off:
            removed[key] = off
            article.keys[key] = [v for v in held if v not in off]
    added: dict[str, list[str]] = {}
    for key, values in (payload.get("keys") or {}).items():
        for value in values or []:
            if value.lower() in _lower(article.keys.get(key, [])):
                said.append(f" {key} {value} is on it already.")
            elif other := holder(store, article.collection, key, value, article.id):
                said.append(f" {_stays(key, value, other)}")
            else:
                article.keys[key] = [*article.keys.get(key, []), value]
                added.setdefault(key, []).append(value)
    article.keys = {k: vs for k, vs in article.keys.items() if vs}
    store.set_identity(article)
    found = store.article(article.id)
    was = list((found.properties[LINKED].value or []) if found and LINKED in found.properties else [])
    links = [p for p in was if p not in payload.get("unlinked", [])]
    links += [p for p in dict.fromkeys(payload.get("linked", [])) if p not in links]
    if links != was:
        store.set_field(article.id, LINKED, Property(links, proposal.by, at))
    keys = {"added": added, "removed": removed}
    linked = {"added": [p for p in links if p not in was], "removed": [p for p in was if p not in links]}
    applied = {"at": at, "article": article.id} | {part: {k: v for k, v in changes.items() if v}
                                                   for part, changes in (("keys", keys), ("linked", linked))
                                                   if any(changes.values())}
    done = [f"{k} {v} added" for k, vs in added.items() for v in vs] + \
        [f"{k} {v} taken off" for k, vs in removed.items() for v in vs] + \
        [f"link {p} added" for p in linked["added"]] + [f"link {p} taken off" for p in linked["removed"]]
    return applied, f"{article.id}: {', '.join(done) or 'nothing changed'}.{''.join(said)}"


def _proposal_id() -> str:
    """A new row's id, random so two rows filed alike at one instant differ."""
    return f"p_{uuid.uuid4().hex[:12]}"


FIRST_RUN = ("not kept: on the first run only what a caller approves lands, so give them when you admit the article "
             "again on a later run")
SUPERSEDED = ("not written: a caller gave the article an instruction of their own in place of these words, and "
              "theirs stands")


def standing(store: Store, article: str) -> Proposal | None:
    """The curator's instruction that stands on an article: its newest from an agent that no person's instruction
    took the place of."""
    agents = [p for p in proposals(store, "accepted") if p.kind == "article.instruct" and p.target == article
              and p.by.kind == "agent"]
    return agents[-1] if agents else None


def superseded(store: Store, article: str, text: str, by: Caller) -> bool:
    """Whether a caller's instruction took the place of these words of this curator on the article: a curator that
    repeats them never undoes a caller's word."""
    return any(p.kind == "article.instruct" and p.target == article and p.by == by and p.status == "withdrawn"
               and p.decided_by is not None and p.decided_by.kind == "person"
               and p.payload.get("text") == text for p in proposals(store))


def instruct(store: Store, article: str, text: str, by: Caller, at: str) -> str:
    """Stand a curator's instructions on an article for its writers, as an accepted `article.instruct` row in the
    curator's name, in place of any it gave before. Gives what became of them: `written`, `standing` when the same
    words stand already, or `superseded` when a person's instruction took the place of these words."""
    if superseded(store, article, text, by):
        return "superseded"
    now = standing(store, article)
    if now is not None and now.by == by and now.payload.get("text") == text:
        return "standing"
    if now is not None and now.by == by:
        propose(store, replace(now, status="withdrawn", decided_at=at, decision_reason="its curator gave other words"))
    propose(store, Proposal(id=_proposal_id(), kind="article.instruct", target=article, by=by, at=at,
                            payload={"text": text}, status="accepted", decided_by=by, decided_at=at))
    return "written"


def _archive(store: Store, target: str, at: str) -> tuple[dict[str, Any], str]:
    if why := moot_archive(store, target):
        raise Gone(f"{why}, so nothing is left to apply")
    article = _article_of(store, target)
    article.status = "archived"
    store.set_identity(article)
    drop_material(store, article.id)
    unmirrored(store, article.id, True)  # its copy says it is archived as the pass ends
    return {"at": at, "article": article.id}, (f"{article.id} is archived: out of the working set, queries leave it out, "
                                            "and a read by id still serves it.")


def _merge(store: Store, target: str, payload: dict[str, Any], proposal: Proposal,
           at: str) -> tuple[dict[str, Any], str]:
    a, b = _article_of(store, target), _article_of(store, str(payload.get("with", "")))
    if a.id == b.id:
        raise CurateError(f"{a.id} and {b.id} are one article: name two articles")
    if "merged" in (a.status, b.status):
        raise Gone(f"{a.id if a.status == 'merged' else b.id} was merged away already, so nothing is left to apply")
    if payload.get("distinct"):
        store.append("merges", {"at": at, "distinct_from": [a.id, b.id], "by": proposal.by.name,
                                "proposal": proposal.id, "reason": proposal.reason})
        return {"at": at, "article": a.id, "distinct": [a.id, b.id]}, f"{a.id} and {b.id} are kept apart."
    back = brought_back(store, a.id, b.id)
    into, gone = a, b  # the first article named stays
    moved = [f for f in store.facts_of(gone.id) if f not in store.facts_of(into.id)]
    shared = {f: [how for how in store.ties_of(f).get(gone.id, []) if how not in store.ties_of(f).get(into.id, [])]
              for f in store.facts_of(gone.id) if f not in moved}
    shared = {f: hows for f, hows in shared.items() if hows}
    store.append("merges", {"at": at, "merge": [into.id, gone.id], "into": into.id, "by": proposal.by.name,
                            "proposal": proposal.id, "reason": proposal.reason, "facts": moved, "ties": shared,
                            "identity": {into.id: asdict(into), gone.id: asdict(gone)}})
    for key, values in gone.keys.items():
        into.keys[key] = into.keys.get(key, []) + [v for v in values if v.lower() not in
                                                   _lower(into.keys.get(key, []))]
    names = [n for n in [gone.title, *gone.keys.get("alias", [])] if n and n.lower() != into.title.lower()]
    if names:
        into.keys["alias"] = list(dict.fromkeys(into.keys.get("alias", []) + names))
    into.status = "active" if "active" in (into.status, gone.status) else into.status
    store.set_identity(into)
    store.set_identity(Identity(gone.id, gone.collection, gone.title, {}, "merged", gone.admitted_by,
                                gone.admitted_at, gone.admitted_reason))
    for fact in [*moved, *shared]:
        for how in shared.get(fact) or store.ties_of(fact).get(gone.id, []):
            store.link(fact, [into.id], at, how)
    store.append("merges", {"at": at, "redirect": gone.id, "to": into.id, "path": gone.id})
    drop_material(store, gone.id, into.id)
    return ({"at": at, "article": into.id, "merged": gone.id},
            f"{gone.id} is merged into {into.id}, and its keys and facts lead there." + (f" {back[0].upper()}{back[1:]}."
                                                                                       if back else ""))


def _split(store: Store, target: str, proposal: Proposal, at: str) -> tuple[dict[str, Any], str]:
    gone = _article_of(store, target)
    into = redirects(store).get(gone.id)
    if gone.status != "merged" or into is None:
        raise Gone(f"{gone.id} is not merged into another article, so nothing is left to split")
    line = next(row.data for row in reversed(store.rows("merges"))
                if row.data.get("into") == into and gone.id in row.data.get("merge", []))
    before, kept = _identity(line["identity"][gone.id]), _identity(line["identity"][into])
    now = store.article(into)
    assert now is not None  # a merge's survivor stays an article
    held = now.identity
    brought = {k: _lower(vs) - _lower(kept.keys.get(k, [])) for k, vs in before.keys.items()}
    brought["alias"] = (_lower(before.keys.get("alias", [])) | {before.title.lower()}) - \
        _lower(kept.keys.get("alias", []))
    held.keys = {k: [v for v in vs if v.lower() not in brought.get(k, set())] for k, vs in held.keys.items()}
    held.keys = {k: vs for k, vs in held.keys.items() if vs}
    if kept.status != "active" and before.status == "active":
        held.status = kept.status  # the admit the merged article brought goes back with it
    store.set_identity(held)
    store.set_identity(before)
    for fact in line.get("facts", []):
        store.unlink(fact, [into])
    for fact, hows in (line.get("ties") or {}).items():
        for how in hows:
            store.unlink(fact, [into], how)
    for row in store.rows("merges"):  # what was told or linked to the article while it was merged goes back to it
        if row.data.get("followed") == gone.id and row.data.get("to") == into:
            store.unlink(row.data["fact"], [into], row.data["how"])
            store.link(row.data["fact"], [gone.id], at, row.data["how"])
    store.append("merges", {"at": at, "split": gone.id, "from": into, "path": gone.id, "by": proposal.by.name,
                            "proposal": proposal.id, "reason": proposal.reason, "redirect": gone.id, "to": ""})
    store.append("merges", {"at": at, "distinct_from": [gone.id, into], "by": proposal.by.name})
    drop_material(store, gone.id, into)
    return {"at": at, "article": gone.id, "from": into}, f"{gone.id} is split out of {into}."


def _identity(data: dict[str, Any]) -> Identity:
    by = data.get("admitted_by")
    return Identity(**(data | {"admitted_by": Caller(**by) if by else None}))


def _lower(values: list[str]) -> set[str]:
    return {str(v).lower() for v in values}


def _article_of(store: Store, id: str) -> Identity:
    found = store.article(id.strip().removeprefix("articles/").removesuffix(".md"))
    if found is None:
        raise Gone(f"{id} is gone, so nothing is left to apply")
    return found.identity


# -- one curator run: its tools and what they file ---------------------------------------------------------------------

def _new_path(collection: str, name: str) -> str:
    """The path a new article of `collection` takes from its name."""
    return f"{collection}/{_slug(name) or 'article'}"


def _ref(ref: Any) -> tuple[str, str]:
    """The source and id a call names a held record by, each trimmed, or two empty strings."""
    if not isinstance(ref, dict):
        return "", ""
    return str(ref.get("source", "")).strip(), str(ref.get("id", "")).strip()


def _label(ref: Any) -> str:
    """A held record as a reply names it: `source:id`, as the call gave them."""
    return f"{ref.get('source', '')}:{ref.get('id', '')}" if isinstance(ref, dict) else str(ref)


def _refusing(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """A tool's reply: what the call gives, or the line that says why it cannot land."""
    try:
        return call()
    except (CurateError, Gone) as error:
        return {"refused": str(error), "note": f"refused: {error}"}


def _with_record(keys: dict[str, list[str]], found: Held) -> dict[str, list[str]]:
    """An admit's keys with those its held record names it by. A key the record only carries binds nothing, whoever
    gives it."""
    out = {key: values for key, values in keys.items() if key not in found.carried}
    for key, values in found.naming().items():
        out[key] = list(dict.fromkeys([*out.get(key, []), *values]))
    return out


ANSWERS = ("Name in `proposals` the ids this act answers or carries out: a waiting proposal of another curator waits "
           "with it or lands with it, and an accepted proposal is carried out by it.")
ELSEWHERE = "Set collection to propose it for another collection: that collection's curator or a caller decides it."


class _Curation:
    """One curator run's writes, as its tools make them."""

    def __init__(self, agent: Agent, store: Store, clock: Clock, done: Curated, extraction: Extraction | None = None):
        self.agent, self.store, self.clock, self.done = agent, store, clock, done
        self.extraction = extraction
        self.collection = agent.target
        self.caller = Caller(agent.id, "agent")
        self.authority = agent.authority or Authority()
        self.ours: Ours = NOBODY  # who is on the workspace's own side
        self.sources: list[str] = []  # the workspace's sources by name, whose held records an admit names
        self.shared: list[str] = []  # the keys its sources issue at weight 0, which the admit tool names
        self.wake = "hand"  # first, a schedule word, change, or hand: what woke this run
        self.own: set[str] = set()  # the folded names of the people on that side, as `own_names` gives them
        self.proposing = False  # whether every admit waits for a person, as on the workspace's first run
        self.measure: _Measure | None = None  # how each act of a run is measured as it lands
        self.lookup: Lookup | None = None  # what `reject` decides through
        self.answers: list[str] = []  # the waiting proposals the act under way answers, which each row it files cites
        self.taken: list[str] = []  # those its act took

    # -- what every tool shares

    def now(self) -> str:
        return stamp(self.clock())

    def file(self, kind: str, target: str, payload: dict[str, Any], reason: str, lands: bool) -> tuple[str, str]:
        """A row of the `proposals` log, and a line that says what it did. One that lands is accepted and applied at
        once, as the curator's own act, and a caller's yes it carries out stays that caller's one decision. Any other
        waits for a caller. An act that would undo what a caller decided, with nothing new since, is refused."""
        named = [p for p in proposals(self.store) if p.id in self.answers]
        moved = redirects(self.store)
        if kind != "article.admit" and (off := [p.id for p in named if _thing(moved, kind, p.target, p.payload)
                                                != _thing(moved, kind, target, payload)]):
            raise CurateError(f"{_either(off, 'and')} {'asks' if len(off) == 1 else 'ask'} about another article than "
                              f"{target}: turn {'it' if len(off) == 1 else 'them'} down with reject and a reason")
        yes = [p for p in named if p.status == "accepted"]
        if yes and not lands:  # a yes is carried out by an act that lands, never left to wait behind one
            raise CurateError(f"{_either([p.id for p in yes], 'and')} {'is' if len(yes) == 1 else 'are'} accepted by a "
                              f"caller: carry {'it' if len(yes) == 1 else 'them'} out with an act in {self.collection}")
        if why := turned_down(self.store, self.extraction, self.sources, kind, target, payload, yes):
            raise CurateError(why)
        if not yes and (why := repeated(self.store, self.caller, kind, target, payload)):  # a yes is carried out
            raise CurateError(why)
        if kind in ("article.admit", "article.link"):
            _refuse_secret(self.store, target, str(payload.get("name") or ""), payload.get("keys"), CurateError)
        at = self.now()
        proposal = Proposal(id=_proposal_id(), kind=kind, target=target, by=self.caller, at=at, payload=payload,
                            reason=reason, cites=list(self.answers))
        note = "it waits for a caller"
        if lands:
            proposal.applied, note = apply(self.store, proposal, at)
            proposal.status, proposal.decided_by, proposal.decided_at = "accepted", self.caller, at
            if yes and kind == "article.admit" and (proposal.applied.get("created") or proposal.applied.get("back")):
                # an article reads as admitted by whoever filed what it was made from: a caller's own ask first
                made = self.store.article(proposal.applied["article"]).identity  # type: ignore[union-attr]
                made.admitted_by = next((p.by for p in yes if p.by.kind == "person"), yes[0].by)
                self.store.set_identity(made)
        else:
            self.done.proposed.append(proposal.id)
        propose(self.store, proposal)
        if proposal.status == "accepted":
            self.done.carried += [p.id for p in yes]
            for p in named:
                kept = p.status == "accepted"
                propose(self.store, replace(p, status="accepted", decided_by=p.decided_by if kept else self.caller,
                                            decided_at=p.decided_at if kept else at,
                                            decision_reason=p.decision_reason if kept else reason,
                                            applied=proposal.applied))
            for p in [q for q in proposals(self.store, "accepted") if not carried_out(q)
                      and any(q.id in n.cites for n in yes)]:  # what a caller's yes accepted with it
                propose(self.store, replace(p, applied=proposal.applied))
        self.taken = self.answers
        return proposal.id, note

    def yes(self) -> list[Proposal]:
        """The proposals a caller accepted that the act under way names, and so carries out."""
        return [p for p in proposals(self.store, "accepted") if p.id in self.answers]

    def spellings(self) -> list[str]:
        """The key names a key given in another case or spacing is read as, each once, in the order that decides
        between two that fold alike: those the engine and the sources issue, the engine's `alias` and `group`, then
        those of the records its sources hold."""
        held = (key for source in self.sources for row in self.store.state(f"held:{source}") or []
                for key in row.get("keys") or {})
        return list(dict.fromkeys([*key_weights(), *(self.store.weights or {}), "alias", "group", *held]))

    def answering(self, ids: list[str] | None, kind: str, act: Callable[[], dict[str, Any]],
                  distinct: bool = False) -> dict[str, Any]:
        """Run an act of `kind`, a keep-apart when `distinct`, that answers the proposals `ids`, each one this curator
        may answer that asks for the same act, checked before anything is filed: each row the act files cites them, and
        they are accepted with it or wait with it. Its reply gives the ids it took."""
        rows = {p.id: p for p in proposals(self.store)}
        named = cited(rows.values())
        if wrong := [id for id in ids or [] if id not in rows or not may_answer(self.agent, rows[id], named)]:
            raise CurateError(f"{_either(wrong, 'and')} {'is not one' if len(wrong) == 1 else 'are not ones'} you "
                              f"decide: answer the proposals your opening lists under {ON_YOURS}")
        if other := [id for id in ids or [] if rows[id].kind != kind
                     or bool(rows[id].payload.get("distinct")) != distinct]:
            raise CurateError(f"{_either(other, 'and')} {'asks' if len(other) == 1 else 'ask'} for another act than "
                              f"{'keeping two apart' if distinct else kind.partition('.')[2]}: turn "
                              f"{'it' if len(other) == 1 else 'them'} down with reject and a reason")
        self.answers, self.taken = list(ids or []), []
        try:
            out = act()
        finally:
            self.answers = []
        status = {p.id: p.status for p in proposals(self.store) if p.id in self.taken}
        took = {"carried_out": [id for id in self.taken if status.get(id) == "accepted"],
                "waits_with_it": [id for id in self.taken if status.get(id) == "waiting"]}
        return out | {k: v for k, v in took.items() if v}

    def waits(self, id: str, what: dict[str, Any], other: str = "") -> dict[str, Any]:
        """The reply to a call filed as a proposal, with whom it waits for: a caller, or the curator of `other`."""
        return {"proposed": id, **what, "note": f"waits for {f'the curator of {other}' if other else 'a caller'}, in {id}"}

    # -- the article a name gives, in any spelling

    def find(self, name: str, *, merged: bool = False, collection: str | None = None) -> Identity:
        """An article of the collection, or of `collection` when given, by its id, its path in the collection in any
        spelling, or its title or an alias in any spelling an admit finds it by, as `_named` reads one."""
        if collection is not None and collection != self.collection:
            there = copy.copy(self)
            there.collection = collection
            return there.find(name, merged=merged)
        wanted = name.strip()
        ids = [wanted, f"{self.collection}/{wanted}", f"{self.collection}/{_slug(wanted)}"]
        found = next((a for a in map(self.store.article, dict.fromkeys(ids)) if a is not None), None)
        if found is None:
            named = [id for id in self.store.named(wanted) if id.startswith(self.collection + "/")]
            if len(named) > 1:
                raise CurateError(f"{wanted} names {', '.join(named)}: give one by its path")
            found = self.store.article(named[0]) if named else None
        if found is None and (at := self._at_path(wanted, merged=True)) is not None:
            found = self.store.article(at.id)  # a path in any spelling, as an admit reads it
        if found is None:  # a name in any spelling an admit finds it by
            named = self._named(wanted, {})
            if len(named) > 1:
                raise CurateError(f"{wanted} names {', '.join(named)}: give one by its path")
            found = self.store.article(named[0]) if named else None
        if found is None or found.identity.collection != self.collection:
            raise CurateError(f"{wanted} is no article of {self.collection}: give a path, an id or a title from your "
                              "evidence")
        if found.identity.status == "merged" and not merged:
            found = self.store.article(follow(found.identity.id, redirects(self.store))) or found
        return found.identity

    def _at_path(self, name: str, *, merged: bool = False) -> Identity | None:
        """The article of this collection a name holding a slash spells the path of, in any spelling `_spelled_path`
        reads, archived or not. One a merge folded away leads to the article it was folded into."""
        if "/" not in name and "\\" not in name:
            return None
        rel = _spelled_path(name)
        found = self.store.article(rel)
        if found is None and rel.casefold().startswith(self.collection + "/"):
            found = next((a for a in self.store.query(self.collection) if a.identity.id.casefold() == rel.casefold()),
                         None)
        if found is None or found.identity.collection != self.collection:
            return None
        if found.identity.status == "merged" and not merged:
            found = self.store.article(follow(found.identity.id, redirects(self.store))) or found
        return found.identity

    def _read_name(self, name: str) -> str:
        """The name of an admit that names no article by its path: a leading `articles/` is
        cut, then a leading collection, this curator's own, and the rest is the name, while another's collection is
        refused. Any other text is the name as it stands."""
        text = name.strip().removeprefix("articles/")
        first, slash, rest = text.partition("/")
        agents = self.agent.path.parent.parent
        collections = {self.collection, *self.store.collections(), *(p.parent.name for p in agents.glob("*/curator.md"))}
        if not slash or rest != rest.lstrip() or first.casefold() not in {c.casefold() for c in collections}:
            return name.strip()
        if first.casefold() != self.collection.casefold():
            raise CurateError(f"admit: an admit of {text} is for {first}, not {self.collection}")
        return rest.strip()

    def _existing(self, name: str, keys: dict[str, list[str]], at_path: Identity | None = None,
                  record: tuple[str, str] | None = None) -> Identity | None:
        """The article an admit names, archived or not: one a key that names the thing binds, under any name, but not
        one it binds only by keys that name no one thing, as a name, that `same_thing` tells apart from it, as binding
        does; else `at_path`, the one at the path it gave; else the one its name or an alias it gives finds, as
        `_named` reads them, unless an admit by name alone is another thing by `one_thing`. So an admit finds an
        archived article by every name it answers to."""
        moved, weights = redirects(self.store), weights_of(self.store)
        held = [(key, id) for key, values in keys.items() for value in values
                if (id := holder(self.store, self.collection, key, value))]
        strong = {id for key, id in held if weights.get(key, 0) > 0}
        by_key = list(dict.fromkeys(id for _, id in held if id in strong
                                    or same_thing(keys, self.find(id).keys, weights) is not False))
        if len(by_key) > 1:
            raise CurateError(f"the keys of {name} bind {', '.join(by_key)}: admit one article, or merge them first")
        if by_key:
            return self.find(by_key[0])
        if at_path is not None:
            return at_path
        named: list[str] = []  # the name and each alias it gives are its names
        for wanted in dict.fromkeys(v.strip() for v in [name, *keys.get("alias", [])] if v.strip()):
            named += [id for id in self._named(wanted, moved) if id not in named]
        # a held record admitted under an article's name pairs with it, as a recording with its meeting, unless
        # the two hold values of a key that identifies and share none, as two dates of one weekly meeting
        named = [id for id in named if one_thing({"keys": keys}, {"keys": self.find(id).keys}, weights)]
        if len(named) > 1:
            raise CurateError(f"{name} names {', '.join(named)}: give keys that tell them apart")
        return self.find(named[0]) if named else None

    def _named(self, wanted: str, moved: dict[str, str]) -> list[str]:
        """The articles of this collection a name finds, each one a merge folded away leading to the article it was
        merged into: those whose title, alias or file name is its words, whatever the case, the width of the letters,
        the spacing and the punctuation."""
        mine = self.collection + "/"
        words = _same(wanted, accents=False)
        found = [*self.store.named(wanted), *self.store.bound("alias", wanted)]
        if words:
            found += [a.identity.id for a in self.store.query(self.collection) if words in {
                _same(a.identity.title, accents=False), *(_same(alias, accents=False) for alias in
                                                          a.identity.keys.get("alias", []))}]
        return [id for id in dict.fromkeys(follow(id, moved) for id in found) if id.startswith(mine)]

    # -- admit

    def admit(self, name: str, keys: dict[str, list[str]], reason: str, instructions: str = "",
              group: str = "", record: tuple[str, str] | None = None,
              fields: dict[str, Any] | None = None) -> dict[str, Any]:
        """Admit an article into this collection: it lands on the curator's authority or a caller's yes it carries
        out, or waits for a caller."""
        if not name.strip():
            raise CurateError("admit names no article: give the name of the thing, as its records write it")
        keys = own_values(address_form(keys), name)
        # a name with no record may give a path or a collection, unless it is a value of a key the admit binds, such
        # as a name holding a slash: that one is only looked for at its exact path
        read = record is None and not any(v.strip().casefold() == name.strip().casefold()
                                          for values in keys.values() for v in values)
        at_path = self._at_path(name) if record is None else None
        if read and at_path is None and not (name := self._read_name(name)):
            raise CurateError("admit names no article: give the name of the thing, as its records write it")
        existing = self._existing(name, keys, at_path, record)
        yes = self.yes()
        if existing is not None and existing.status == "active" and not yes:
            raise CurateError(f"admit: {existing.id} is in the working set already: to change its keys or links, use "
                              "link, and to change your note to its writers, use instruct")
        if existing is None and read and _lower_path(_link_path(name)):
            raise CurateError(f"admit: {name.strip()} reads as an article's path, and {self.collection} holds no such "
                              "article: give a name, with a space on each side of a slash in it, or name its record")
        fields = {str(k).strip(): v for k, v in (fields or {}).items()}
        if refused := [why for k, v in fields.items() if (why := field_refused(name, k, v))]:
            raise CurateError(f"admit: {'; '.join(refused)}")
        payload: dict[str, Any] = {"name": name.strip(), "keys": keys} | ({"fields": fields} if fields else {})
        if record is not None:  # what the first run's list folds it by, and what joins a thin article on accept
            payload["record"] = {"source": record[0], "id": record[1]}
        dropped = ""
        if instructions and self.wake == "first" and self.authority.admit and not self.proposing:
            dropped = FIRST_RUN  # on a first run only what a person approves carries them
        elif instructions:
            payload["instructions"] = instructions
        asked = existing.id if existing else _new_path(self.collection, name)
        if not instructions and (words := next((p.payload["instructions"] for p in yes
                                                if p.payload.get("instructions")), "")):
            payload["instructions"] = words  # approving the proposal that carried them writes them
        if own := [p for p in yes if p.by == self.caller]:  # this curator's own proposals, approved: made as asked
            moved = redirects(self.store)
            path = follow(own[0].target, moved)
            lead = next((q for q in own if q.target == path), own[0])  # a first-run group's lead row
            called = str(lead.payload.get("name") or lead.payload.get("title") or "")
            for q in own:
                if asked != follow(q.target, moved) or at_path is None and not named_alike(name, called):
                    there = follow(q.target, moved)
                    said = called if there == path else str(q.payload.get("name") or q.payload.get("title") or "")
                    raise CurateError(f"{q.id} was approved as {said} at {there}: admit it there")
            payload["name"] = called  # as approved, though the curator gave its path or another spelling
            made = [p.payload for p in proposals(self.store, "accepted") if p.kind == "article.admit"
                    and p.applied.get("article") == path]  # another thing made there takes the next free path
            weights = weights_of(self.store)
            asked = path if all(one_thing(m, own[0].payload, weights) for m in made) else free_path(self.store, path)
        if not yes and (not self.authority.admit or self.proposing
                        or [p for p in waiting_item(self.store, asked) if not another_curators(p, self.agent.id)]
                        or waits_alike(self.store, self.collection, payload, self.agent.id)):
            id, _ = self.file("article.admit", asked, payload, reason, lands=False)
            carried = {"instructions": "carried by the proposal: approving it writes them, and rejecting it writes "
                                       "none"} if instructions else {}
            return self.waits(id, {"article": asked, **carried})
        target = asked if own else existing.id if existing is not None else free_path(self.store, asked)
        _, note = self.file("article.admit", target, payload, reason, lands=True)
        self.done.admitted.append(target)
        return {"admitted": target, "note": note} | ({"instructions": dropped} if dropped else {})

    # -- admit by a held record, or several

    def held_map(self, refs: list[Any]) -> dict[tuple[str, str], Held]:
        """The records held for this collection that `refs` name, by source and id, read once for a call however
        many it names, and weighing no other held record."""
        named = {_ref(ref) for ref in refs}
        return {(h.source, h.id): h for h in held_for(self.store, self.collection, self.sources, limit=None,
                                                       ours=self.ours, only=named).items}

    def held(self, ref: Any, held: dict[tuple[str, str], Held] | None = None) -> Held:
        """The held record of this collection an admit names by its source and id, from `held` when given."""
        source, id = _ref(ref)
        if not source or not id:
            raise CurateError("admit: a record names its source and its id, as your held list gives them")
        found = (self.held_map([ref]) if held is None else held).get((source, id)) or self.told(source, id)
        if found is None:
            raise CurateError(f"admit: no record of {source} with id {id} is held for {self.collection}: give one your "
                              "held list shows, or admit by name and keys")
        return found

    def told(self, source: str, id: str) -> Held | None:
        """The told record an accepted admit of this collection cites, as a held record an admit is made from, or
        None. It never sits on the held list."""
        record = self.store.record(id) if source == TOLD else None
        row = next((p for p in proposals(self.store, "accepted") if p.kind == "article.admit" and not carried_out(p)
                    and id in p.cites and p.target.startswith(self.collection + "/")), None)
        if record is None or row is None:
            return None
        return Held(TOLD, id, str(row.payload.get("name") or row.payload.get("title") or record.body), at=record.at)

    def held_there(self, collection: str, ref: Any) -> Held:
        """The held record of another collection a proposal names by its source and id."""
        source, id = _ref(ref)
        found = next((h for h in held_for(self.store, collection, self.sources, limit=None, ours=self.ours,
                                          only={(source, id)}).items if (h.source, h.id) == (source, id)), None)
        if found is None:
            raise CurateError(f"admit: no record of {source or 'a source'} with id {id or '?'} is held for {collection}: "
                              f"admit it by name and keys with collection {collection}")
        return found

    def admit_records(self, records: list[Any], reason: str, group: str) -> dict[str, Any]:
        """Admit several held records in one call, each with its own reason or the call's, under one group, and
        say in one reply what became of each."""
        if len(records) > MAX_PER_CALL:
            raise CurateError(f"admit: at most {MAX_PER_CALL} records in one call")
        out: dict[str, Any] = {"admitted": [], "proposed": [], "refused": {}}
        held = self.held_map(records)
        for ref in records:
            own = str(ref.get("reason") or "").strip() if isinstance(ref, dict) else ""
            said = own or reason
            label = _label(ref)
            try:
                found = self.held(ref, held)
                if not said.strip():
                    raise CurateError("admit: give a one-line reason, for the call or for this record")
                done = self.admit(found.name, found.naming(), f"{said} ({group})" if group else said, group=group,
                                  record=(found.source, found.id))
            except (CurateError, Gone) as error:
                out["refused"][label] = str(error)
                continue
            if "admitted" in done:
                out["admitted"].append(done["admitted"])
            if "proposed" in done:
                out["proposed"].append(f"{label}: {done['note']}")
        counts = {k: len(v) for k, v in out.items() if v}
        out["note"] = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in counts.items()) or "nothing admitted"
        return {k: v for k, v in out.items() if v}

    # -- leave_out: a candidate weighed and not admitted, with its reason

    def leave_out(self, reason: str, name: str, keys: dict[str, list[str]], record: Any,
                  records: list[Any]) -> dict[str, Any]:
        """Keep a row of the `left-out` log for each candidate this run weighed and did not admit, with its reason,
        so a caller sees why and the curator's later runs show it again. It admits and defers nothing."""
        reason = " ".join(reason.split())
        if not reason:
            raise CurateError("leave_out: give a short reason, such as named once in one record")
        if records and (record or name.strip() or keys):
            raise CurateError("leave_out: give records alone, or one record or a name")
        if len(records) > MAX_PER_CALL:
            raise CurateError(f"leave_out: at most {MAX_PER_CALL} records in one call")
        refs = records or ([record] if record else [])
        rows, refused = [], {}
        held = self.held_map(refs) if refs else {}
        for ref in refs:
            try:
                found = self.held(ref, held)
            except CurateError as error:
                refused[_label(ref)] = str(error).replace("admit:", "leave_out:", 1)
                continue
            rows.append({"name": found.name, "source": found.source, "id": found.id})
        if not refs:
            if not name.strip():
                raise CurateError("leave_out: name the candidate, or give a record your held list shows")
            rows.append({"name": name.strip(), "keys": address_form(keys)})
        at = self.now()
        for row in rows:
            said = [r.data for r in self.store.rows(LEFT_OUT, {"agent": self.agent.id, "name": row["name"]})
                    if r.data.get("source") == row.get("source") and r.data.get("id") == row.get("id")]
            if said and said[-1].get("reason") == reason:
                continue  # the reason it stands with already: a repeat writes no row
            self.store.append(LEFT_OUT, {"at": at, "agent": self.agent.id, "collection": self.collection, **row,
                                         "reason": reason})
        out: dict[str, Any] = {"left_out": [row["name"] for row in rows] if records else
                               (rows[0]["name"] if rows else ""),
                               "note": "done: listed again with its reason on your later runs"}
        if refused:
            out["refused"] = refused
        return out

    # -- an act on another collection: a proposal there

    def other(self, collection: str | None) -> str | None:
        """The other collection an act names, once a curator keeps it, or None for this curator's own."""
        wanted = (collection or "").strip().removeprefix("articles/").strip("/")
        if not wanted or wanted == self.collection:
            return None
        if not COLLECTION.fullmatch(wanted):
            raise CurateError(f"collection {wanted!r} is not the name of a folder under articles/: give the name of "
                              f"another collection's folder, or leave collection out to act in {self.collection}")
        self.keeper(wanted)
        return wanted

    def elsewhere(self, kind: str, target: str, payload: dict[str, Any], reason: str, other: str) -> dict[str, Any]:
        """File an act on another collection as a proposal there, for its curator or a person."""
        id, _ = self.file(kind, target, payload, reason, lands=False)
        return self.waits(id, {"article": target}, other)

    def admit_elsewhere(self, collection: str, name: str, keys: dict[str, list[str]], reason: str,
                        record: tuple[str, str] | None = None,
                        fields: dict[str, Any] | None = None) -> dict[str, Any]:
        """Propose an admit for another curated collection, for its curator or a caller: never an article this curator
        makes, whatever its own authority says. `record` is the held record of that collection it came from, if
        any. A proposal of the same article that waits already is no bar: the two are one item for the person."""
        if not name.strip():
            raise CurateError(f"admit: a proposal for {collection} names no article: give the name of the thing, as "
                              "its records write it")
        if not reason.strip():
            raise CurateError(f"admit: a proposal for {collection} gives no reason: say in one line why it belongs "
                              "there")
        keys = own_values(address_form(keys), name)
        target = _new_path(collection, name)
        there = [id for id in dict.fromkeys([*self.store.named(name.strip()), *(
            id for key, values in keys.items() for value in values for id in self.store.bound(key, value))])
            if id.startswith(collection + "/") and working(self.store, id)]
        if there:
            raise CurateError(f"admit: {there[0]} is in the working set already: to change its keys or links, use link "
                              f"with collection {collection}")
        fields = {str(k).strip(): v for k, v in (fields or {}).items()}
        if refused := [why for k, v in fields.items() if (why := field_refused(name, k, v))]:
            raise CurateError(f"admit: {'; '.join(refused)}")
        payload = {"name": name.strip(), "keys": keys} | ({"record": {"source": record[0], "id": record[1]}}
                                                          if record is not None else {}) | (
            {"fields": fields} if fields else {})
        out = self.elsewhere("article.admit", target, payload, reason.strip(), collection)
        return {"proposed": out["proposed"], "proposed_article": target, "note": out["note"]}

    def keeper(self, collection: str) -> Agent:
        """The curator of another collection of the workspace, which an act there waits for, or the line that says
        why there is none."""
        agents = self.agent.path.parent.parent
        if not (agents / collection).is_dir() and collection not in self.store.collections():
            raise CurateError(f"articles/{collection} is no collection of this workspace: name one it has, or leave "
                              f"collection out to act in {self.collection}")
        keeper, why, file = None, "", agents / collection / "curator.md"
        if file.is_file():
            try:
                keeper = load_agent(file, agents)
            except AgentFileError as error:
                why = f" ({error})"
        if keeper is None:
            raise CurateError(f"articles/{collection} has no curator, so it takes no proposal{why}: act in "
                              f"{self.collection}, or name a collection a curator keeps")
        return keeper

    # -- archive

    def archive(self, article: str, reason: str, collection: str | None = None) -> dict[str, Any]:
        if not reason.strip():
            raise CurateError(f"archive of {article} gives no reason: say why it leaves the working set")
        other = self.other(collection)
        who = self.find(article, collection=other)
        if who.status == "archived":
            return {"archived": who.id, "note": "archived already: nothing changed"}
        if other is not None:
            return self.elsewhere("article.archive", who.id, {}, reason, other)
        if not self.authority.archive and not self.yes():
            id, _ = self.file("article.archive", who.id, {}, reason, lands=False)
            return self.waits(id, {"article": who.id})
        _, note = self.file("article.archive", who.id, {}, reason, lands=True)
        self.done.archived.append(who.id)
        return {"archived": who.id, "note": note}

    # -- merge and split

    def merge(self, article: str, with_: str, reason: str, distinct: bool,
              collection: str | None = None) -> dict[str, Any]:
        other = self.other(collection)
        a, b = self.find(article, collection=other), self.find(with_, collection=other)
        if a.id == b.id:
            raise CurateError(f"{article} and {with_} are one article, {a.id}: name two articles")
        yes = self.yes()
        if kept := next((p for p in yes if {p.target, p.payload.get("with")} == {a.id, b.id}), None):
            a, b = (a, b) if kept.target == a.id else (b, a)  # the article the caller named first stays
        payload = {"with": b.id} | ({"distinct": True} if distinct else {})
        if other is not None:
            return self.elsewhere("article.merge", a.id, payload, reason, other)
        if not self.authority.merge and not yes:
            id, _ = self.file("article.merge", a.id, payload, reason, lands=False)
            return self.waits(id, {"articles": [a.id, b.id]})
        _, note = self.file("article.merge", a.id, payload, reason, lands=True)
        if distinct:
            return {"distinct": [a.id, b.id], "note": note}
        self.done.merged.append(b.id)
        return {"merged": b.id, "into": a.id, "note": note}

    def split(self, article: str, reason: str, collection: str | None = None) -> dict[str, Any]:
        other = self.other(collection)
        gone = self.find(article, merged=True, collection=other)
        into = redirects(self.store).get(gone.id)
        if gone.status != "merged" or into is None:
            raise CurateError(f"{gone.id} was never merged, so there is nothing to split")
        if other is not None:
            return self.elsewhere("article.split", gone.id, {"from": into}, reason, other)
        if not self.authority.split and not self.yes():
            id, _ = self.file("article.split", gone.id, {"from": into}, reason, lands=False)
            return self.waits(id, {"article": gone.id})
        _, note = self.file("article.split", gone.id, {"from": into}, reason, lands=True)
        self.done.split.append(gone.id)
        return {"split": gone.id, "from": into, "note": note}

    # -- link and instruct: an article of the collection that exists

    def link(self, article: str, reason: str, keys: dict[str, list[str] | None], wrong: dict[str, list[str]],
             linked: list[str], unlinked: list[str], collection: str | None = None) -> dict[str, Any]:
        """Change an article's keys and links. It lands on the curator's own word, whatever its authority, and on
        another collection waits there."""
        other = self.other(collection)
        who = self.find(article, collection=other)

        def off(name: Any) -> list[str]:  # a key comes off as named, else off each spelling the article holds it in
            name = str(name).strip()
            return [name] if name in who.keys else [k for k in who.keys if word(k) == word(name)] or [name]

        def holding(name: Any, value: Any) -> list[str]:  # a value comes off the spellings that hold it, else the first
            names = off(name)
            return [k for k in names if str(value).lower() in _lower(who.keys.get(k, []))] or names[:1]

        # a key goes on as the sources write it
        adds = address_form(read_keys(((k, v) for k, v in keys.items() if v is not None),
                                      [*(self.spellings() if keys else []), *who.keys]))
        cleared = [n for k, v in keys.items() if v is None for n in off(k)]
        wrong = read_keys(((n, [v]) for k, vs in wrong.items() for v in vs for n in holding(k, v)), list(who.keys))
        payload: dict[str, Any] = {
            "keys": {k: None for k in cleared} | adds,
            "wrong": address_form(wrong),
            "linked": [p.strip() for p in linked if p.strip()], "unlinked": [p.strip() for p in unlinked if p.strip()]}
        payload = {k: v for k, v in payload.items() if v}
        if other is not None:
            return self.elsewhere("article.link", who.id, payload, reason, other)
        id, note = self.file("article.link", who.id, payload, reason, lands=True)
        self.done.linked.append(who.id)
        applied = _proposal(self.store, id).applied  # type: ignore[union-attr]  # filed just above
        keys, links, out = applied.get("keys", {}), applied.get("linked", {}), {"linked": who.id}
        for part in ("added", "removed"):
            if changed := {**keys.get(part, {}), **({"linked": links[part]} if part in links else {})}:
                out[part] = changed
        return out | {"note": note}

    def instruct(self, article: str, text: str) -> dict[str, Any]:
        """Stand the curator's note to an article's writers, in place of the one it gave before."""
        who = self.find(article)
        if not text.split():
            return {"instructed": who.id, "note": "it gives no words, so nothing changed"}
        return {"instructed": who.id, "note": {
            "written": "it stands on the article for its writers from their next run, in place of any you gave it "
                       "before",
            "standing": "your note on the article already, so nothing is written",
            "superseded": SUPERSEDED}[instruct(self.store, who.id, " ".join(text.split()), self.caller, self.now())]}

    # -- reject: a curator's no to a proposal on its collection

    def reject(self, asked: list[Any]) -> dict[str, Any]:
        """Turn down proposals another curator filed on this collection, each by its id with a reason, as one decide
        with this curator as the caller."""
        rows = [(str(r.get("id", "")).strip(), " ".join(str(r.get("reason", "")).split()))
                for r in asked if isinstance(r, dict)]
        if not rows or len(rows) != len(asked) or not all(id for id, _ in rows):
            raise CurateError("reject: give each proposal as {id, reason}, by the ids your opening lists")
        if not all(reason for _, reason in rows):
            raise CurateError("reject: give each a reason: its proposer reads it")
        assert self.lookup is not None  # a run sets it
        result = self.lookup.decide(self.caller, reject=[Decision(id, reason) for id, reason in rows])
        status = {p.id: p.status for p in proposals(self.store)}
        out = {"rejected": [id for id, answer in result.answers.items() if answer == "decided"],
               "refused": {id: f"{id} is {status[id]} already" if answer == "already decided" else result.notes[id]
                           for id, answer in result.answers.items() if answer != "decided"}}
        return {k: v for k, v in out.items() if v}

    # -- the tools, as the model sees them

    def admit_tool(self) -> Callable[..., dict[str, Any]]:
        """The admit tool, as the model reads and calls it."""

        def admit(name: str = "", reason: str = "", keys: dict[str, list[str]] | None = None,
                  collection: str | None = None, group: str = "",
                  domain: list[str] | None = None, aliases: list[str] | None = None,
                  instructions: str = "", record: dict[str, str] | None = None,
                  records: list[dict[str, str]] | None = None, fields: dict[str, Any] | None = None,
                  proposals: list[str] | None = None) -> dict[str, Any]:
            """Admit an article into the collection you curate. Give its name in name, as its records write it, and
            the keys that bind records to it, if any, such as {"domain": ["alder.example"]} or {"email":
            ["ada@example.com"]}. Copy each value as its records write it. domain and aliases are keys too, given on
            their own. <shared keys> An archived
            article it names comes back. An article in the working set is refused: change its keys or links with link,
            and your note to its writers with instruct. An admit that carries out an accepted ask for a thing that has
            an article already admits that article. Give one line that says why. Set collection to propose the article
            for another collection: that collection's curator or a caller decides it, and a reply with proposed_article
            gives the path to link to. Set group to the line of your mandate that found the article: it is kept with
            the reason, and binds nothing, so never put it in keys. Give instructions, a few sentences with a name, to
            tell the article's writers what to look for or weigh: they stand as your instruction on the article until
            a caller gives it one of their own or you change them with instruct, and they are apart from the reason,
            which a caller reads. Give record, {"source", "id"} as your held list shows it, to admit that record with
            its name and keys, or several in records, each with a reason of its own or this call's, under this group.
            Give fields, {"<field>": value}, to set front matter fields of the article as you admit it, as its writers
            set them; the ones the wiki keeps itself are refused. Give fields {"linked": [<article id>, ...]} to link
            the article to the others it goes with, such as the one it belongs to, made or only proposed."""
            given = [(k, v) for k, v in [*(keys or {}).items(), ("domain", domain), ("alias", aliases)] if v]
            keys = read_keys(given, self.spellings() if given else [])
            # a group given as a key would bind every article of it
            named = [str(v) for v in keys.pop("group", [])]
            group = group.strip() or ", ".join(named)
            words, said = " ".join(instructions.split()), reason
            reason = f"{said.strip()} ({group})" if group and said.strip() else said

            def admit_named(named: str, keys: dict[str, list[str]], ref: tuple[str, str] | None = None
                            ) -> dict[str, Any]:
                return self.admit(named, keys, reason, words, group, ref, fields=fields)

            def call() -> dict[str, Any]:  # by what it gives: records, a record, or a name
                other = self.other(collection)
                if fields and records:
                    raise CurateError("admit: fields go with one name or record, for that article: give them "
                                      "in an admit of its own")
                if records:
                    if record or name.strip() or other is not None or words or keys:
                        raise CurateError("admit: records takes a reason and a group alone: admit a name, keys or "
                                          "instructions in a call of its own")
                    return self.admit_records(list(records), said, group)
                if words and other is not None:
                    raise CurateError(f"admit: instructions are a note to the writers of your own collection, so a "
                                      f"proposal for {other} takes none: put what its curator should know in reason")
                if record and other is not None:
                    found = self.held_there(other, record)  # the parent of what it found, held for that collection
                    return self.admit_elsewhere(other, name.strip() or found.name, _with_record(keys, found),
                                                reason, (found.source, found.id), fields)
                if record:
                    found = self.held(record)
                    return admit_named(name.strip() or found.name, _with_record(keys, found), (found.source, found.id))
                if other is None:
                    return admit_named(name, keys)
                return self.admit_elsewhere(other, name, keys, reason, fields=fields)

            return _refusing(lambda: self.answering(proposals, "article.admit", call))

        shared = f"Your sources also bind by {_either(self.shared, 'and')}: give them in keys. " if self.shared else ""
        admit.__doc__ = " ".join((admit.__doc__ or "").split()).replace(
            "<shared keys> ", shared)  # the keys this workspace's sources issue, so a curator is told of no other
        return admit

    def tools(self) -> dict[str, Callable[..., Any]]:
        admit = self.admit_tool()

        def archive(article: str, reason: str, collection: str | None = None,
                    proposals: list[str] | None = None) -> dict[str, Any]:
            """Take an article out of the working set, by its path or title, with one line that
            says why. Its article is kept, marked archived, and a later admit brings it back. <elsewhere>"""
            return _refusing(lambda: self.answering(proposals, "article.archive",
                                                    lambda: self.archive(article, reason, collection)))

        def merge(article: str, other: str, reason: str, distinct: bool = False,
                  collection: str | None = None, proposals: list[str] | None = None) -> dict[str, Any]:
            """Say two articles are one thing, with one line that says why, naming the records they share: the first
            article named stays, and the other's keys and name join it. With distinct true, record that the two are different things. It is
            recorded, and bars nothing: a later merge of them is judged like any other. <elsewhere>"""
            return _refusing(lambda: self.answering(proposals, "article.merge", lambda: self.merge(
                article, other, reason, distinct, collection), distinct))

        def link(article: str, reason: str, keys: dict[str, list[str] | None] | None = None,
                 wrong: dict[str, list[str]] | None = None, linked: list[str] | None = None,
                 unlinked: list[str] | None = None, collection: str | None = None,
                 proposals: list[str] | None = None) -> dict[str, Any]:
            """Change the keys or the links of an article that exists. Name it by its path or title,
            and give one line that says why. Keys bind records to an article. A key you add brings its records to the
            article on the next pass. A key you take off stops binding them. So when a held record belongs to an article
            you already have, add the key from that record that the article lacks. And when an article holds a key that
            brings another thing's records, that key is wrong: take its value off. keys: the values to add, such as
            {"domain": ["alder.example"]}. Give a key null to take the whole key off. wrong: the values to take off,
            such as {"domain": ["birch.example"]}. To take a value off, give it in wrong alone. To fix one, give the
            wrong value in wrong and the right one in keys. linked: articles to add to its links, made or only
            proposed. unlinked: articles to take off its links, a writer's included. A link can decide what the article's
            writers read, and never changes what the article says. Copy each value as your records write it. A value another article
            of its collection holds is not added, and the reply names that article: the two may be one thing. A key
            your sources issue as shared may sit on many articles. <elsewhere>"""
            return _refusing(lambda: self.answering(proposals, "article.link", lambda: self.link(
                article, reason, dict(keys or {}), dict(wrong or {}), list(linked or []), list(unlinked or []),
                collection)))

        def instruct(article: str, text: str) -> dict[str, Any]:
            """Leave a note for the writers of an article in your collection that exists: what to look for or weigh,
            in a few sentences. It replaces the note you gave that article before. Writers read it as your context,
            never as a source, and a caller's own instruction on the article outranks it. A new article takes its
            note in its admit."""
            return _refusing(lambda: self.instruct(article, text))

        def split(article: str, reason: str, collection: str | None = None,
                  proposals: list[str] | None = None) -> dict[str, Any]:
            """Undo the last merge of an article, named by the path it had, with one line that says why. It comes back
            with its own keys. Keys learned while the two were one stay on the other article: move them with link.
            <elsewhere>"""
            return _refusing(lambda: self.answering(proposals, "article.split",
                                                    lambda: self.split(article, reason, collection)))

        def leave_out(reason: str, name: str = "", keys: dict[str, list[str]] | None = None,
                      record: dict[str, str] | None = None,
                      records: list[dict[str, str]] | None = None) -> dict[str, Any]:
            """List a candidate you weighed and did not admit, with a short reason such as named once in one record.
            Name a record your held list shows, by its source and id, or several in records, or give a name with its
            keys, as admit takes them. Your later runs list it again with its reason."""
            return _refusing(lambda: self.leave_out(reason, name, dict(keys or {}), record, list(records or [])))

        def reject(proposals: list[dict[str, str]]) -> dict[str, Any]:
            """Turn down proposals waiting on your collection, by the ids your opening lists, each with a reason its
            proposer reads: [{"id": ..., "reason": ...}]."""
            return _refusing(lambda: self.reject(list(proposals or [])))

        for tool in (archive, merge, link, split):
            tool.__doc__ = " ".join((tool.__doc__ or "").split()).replace("<elsewhere>", ELSEWHERE)
        for tool in (admit, archive, merge, link, split):
            tool.__doc__ = f"{tool.__doc__} {ANSWERS}"
        for tool in (admit, link, instruct, archive, merge, split, leave_out, reject):  # the decisions:
            tool.decides = True  # type: ignore[attr-defined]  # each runs whatever room or calls the run has left
        out: dict[str, Callable[..., Any]] = {"admit": admit, "link": link, "instruct": instruct, "archive": archive,
                                               "merge": merge, "split": split, "leave_out": leave_out,
                                               "reject": reject}
        if self.measure is None:
            return out

        def measured(tool: Callable[..., Any], measure: _Measure = self.measure) -> Callable[..., Any]:
            @functools.wraps(tool)
            def act(*args: Any, **kwargs: Any) -> Any:
                return measure(lambda: tool(*args, **kwargs))
            return act

        return {name: measured(tool) for name, tool in out.items()}


# -- how a name finds an article, in any spelling ----------------------------------------------------------------------

def _same(name: str, *, accents: bool = True) -> str:
    """The file name a name makes: its words of any script, case folded and joined by dashes, after the width and
    other forms of a letter are folded to one, so `ß` is `ss` and `ı` and `İ` are `i`, and with `accents` the accents
    of its Latin and Greek letters are left out. A word is letters, digits and their marks: a mark of another
    script, such as a dakuten, a tone mark or a vowel sign, makes another letter and is kept."""
    kept: list[str] = []
    for c in unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", name).replace("ı", "i")):
        if "\u0300" <= c <= "\u036f" and kept and unicodedata.name(kept[-1], "").startswith(("LATIN", "GREEK")) and (
                accents or c == "\u0307" and kept[-1] in "Ii"):  # an accent on a Latin or Greek letter; the dot of `İ`
            continue
        kept.append(c)
    folded = unicodedata.normalize("NFKC", "".join(kept)).casefold().replace("ı", "i")
    return "-".join("".join(c if unicodedata.category(c)[0] in "LNM" else " " for c in folded).split())


def _either(keys: list[str], conj: str = "or") -> str:
    return keys[0] if len(keys) == 1 else f"{', '.join(keys[:-1])} {conj} {keys[-1]}"


def _cut_suffix(name: str) -> str:
    """A name with each trailing `.md` left out, in any case."""
    name = name.strip()
    while name[-3:].casefold() == ".md":
        name = name[:-3].rstrip()
    return name


def _spelled_path(text: str) -> str:
    """The path a name spells, in every spelling: separators `/` and `\\` alike, repeated or with space around
    them, each `.` part left out and each `..` part taking back the part before it, each leading `articles/`, and
    each trailing `.md`, `.` and space of the last part, left out, in any case."""
    parts: list[str] = []
    for part in re.split(r"[/\\]+", text):
        part = part.strip()
        if part == "..":
            parts = parts[:-1]
        elif part not in ("", "."):
            parts.append(part)
    while parts and parts[0].casefold() == "articles":
        parts = parts[1:]
    if parts:
        last, was = parts[-1], ""
        while last != was:
            was, last = last, _cut_suffix(last).rstrip(". \t")
        parts = [*parts[:-1], last] if last else parts[:-1]
    return "/".join(parts)


def _link_path(text: str) -> str:
    """The path a name spells: trimmed, and each leading `/`, `./`, `../` and `articles/`,
    and each trailing `.md`, left out, in any case, so an admit by any spelling of an archived article's path waits
    for a person rather than making a second article beside it."""
    path, was = text.strip(), None
    while path != was:
        was, path = path, path.lstrip("/").removeprefix("./").removeprefix("../")
        if path[:9].casefold() == "articles/":
            path = path[9:]
    while path[-3:].casefold() == ".md":
        path = path[:-3]
    return path


def _lower_path(text: str) -> bool:
    """Whether text reads as an article's path: two parts at least, each of lower case
    letters, digits, `.`, `_` and `-`, starting with a letter or a digit."""
    parts = text.split("/")
    return len(parts) >= 2 and all(p and (p[0].islower() or p[0].isdecimal()) and all(
        c.islower() or c.isdecimal() or c in "._-" for c in p) for p in parts)


# -- what a curator's run is told --------------------------------------------------------------------------------------

WOKE = "What woke this run"


def _woke(wake: str, accepted: Collection[str] = (), news: bool = True) -> list[Evidence]:
    """What woke a curator's run, the first lines of its opening: the accepted proposals that woke it, if any, then
    the schedule, the change or the hand that ran it, unless the accepted ones alone woke it."""
    line = {"first": "What woke this run: the first run. Propose what clearly matters, then stop: the runs after "
                     "this one fill in the rest.",
            "hand": "What woke this run: a run you asked for with agents run.",
            "change": "What woke this run: a change wake: your evidence moved since your last run. Weigh by your "
                      "mandate what is new in it, each held record and proposal, and nothing more."}.get(
        wake, f"What woke this run: the {wake} schedule.")
    lines = [f"What woke this run: a proposal was accepted for your collection: {', '.join(accepted)}"] if accepted \
        else []
    if not accepted or wake != "change" or news:
        lines.append(line)
    return [Evidence(WOKE, lines, spelled="woke")]


ON_YOURS = "Proposals waiting on your collection"
ACCEPTED = "Accepted proposals for your collection"
LEFT_BY_NAME = "Candidates you left out"
DEFERRED = "Your deferred list"
DECISIONS = "Decisions on your proposals"
UNLINKED = "Names with no article"
MAX_UNLINKED = 25  # names with no article a run is shown
WAITING_LIST = "Waiting for a caller"
ALSO_WAITING_LIST = "Also waiting for a caller"
MAX_WAITING = 50  # rows of each waiting list a run is shown
NOTE_CAP = 300  # characters of a told proposal's words a list shows


def _unlinked(store: Store, extraction: Extraction | None, own: set[str]) -> list[Evidence]:
    """The subjects of current facts that no article claims, by its title or an alias, each once whatever its case
    and spacing, with how many facts and records name it and the day of the latest: the most facts first, then the
    latest. A name on the workspace's own side, folded in `own`, is marked so."""
    tally: dict[str, dict[str, Any]] = {}
    for fact in extraction.facts() if extraction is not None else []:
        key = " ".join(fact.subject.casefold().split())
        if not key or store.named(fact.subject.strip()):
            continue
        seen = tally.setdefault(key, {"name": "", "facts": 0, "records": set(), "last": "", "own": key in own})
        seen["facts"] += 1
        seen["records"].add((fact.source, fact.record))
        if fact.at >= seen["last"]:
            seen["name"], seen["last"] = " ".join(fact.subject.split()), fact.at
    ranked = sorted(tally.values(), key=lambda t: (-t["facts"], _negated(t["last"]), t["name"].lower()))
    items = []
    for t in ranked[:MAX_UNLINKED]:
        facts, records = t["facts"], len(t["records"])
        line = (f"- {t['name']}{f' ({OWN_SIDE})' if t['own'] else ''}: {facts} fact{'' if facts == 1 else 's'} from "
                f"{records} record"
                f"{'' if records == 1 else 's'}" + (f", latest {t['last'][:10]}" if t["last"] else ""))
        items.append(Item(f"unlinked:{t['name']}", line))
    if not items:
        return []
    intro = ("The subjects of facts that no article claims, each with how many facts and records name it and the day "
             "of the latest. Your instructions say when one is an admit signal.")
    return [Evidence(UNLINKED, [intro, *(item.text for item in items)], items=items, spelled="names with no article")]


def _negated(at: str) -> tuple[int, ...]:
    """A time as a key that sorts the latest first."""
    return tuple(-ord(c) for c in at)


def _deferred(store: Store, agent: Agent, extraction: Extraction | None, sources: Collection[str]) -> list[Evidence]:
    """The admits of this curator's collection a caller rejected that an admit would still be refused, as one
    evidence entry, each with when and why."""
    mine = agent.target + "/"
    items = []
    for p in proposals(store, "rejected"):
        if p.kind != "article.admit" or not p.target.startswith(mine) or p.decided_by is None or \
                p.decided_by.kind != "person" or not turned_down(store, extraction, sources, p.kind, p.target,
                                                                   p.payload):
            continue
        name = str(p.payload.get("name") or p.payload.get("title") or p.target.rpartition("/")[2])
        why = f": {p.decision_reason}" if p.decision_reason.strip() else ""
        items.append(Item(f"deferred:{p.id}", f"- {name} ({p.target}), deferred by {p.decided_by.name} on "
                                              f"{p.decided_at[:10]}{why}"))
    if not items:
        return []
    return [Evidence(DEFERRED, [item.text for item in items], items=items, spelled="deferred")]


def _left_by_name(store: Store, agent: Agent) -> list[Evidence]:
    """The candidates this collection's curator left out by name, with no record, newest first, each once at its
    latest reason."""
    latest: dict[str, dict[str, Any]] = {}
    for row in reversed(store.rows(LEFT_OUT, {"collection": agent.target})):
        if not row.data.get("id") and (name := " ".join(str(row.data.get("name") or "").split())):
            latest.setdefault(name.casefold(), row.data | {"name": name})
    rows = list(latest.values())
    if not rows:
        return []
    lines = ["Candidates you left out by name, newest first, each with its keys, your reason and the day.", ""]
    for d in rows[:MAX_WAITING]:
        keys = d.get("keys") if isinstance(d.get("keys"), dict) else {}
        said = "".join(f", {k} {' '.join(map(str, v))}" for k, v in sorted(keys.items()) if v)
        lines.append(f"- {d['name']}{said}: {d.get('reason', '')}, {str(d.get('at', ''))[:10]}")
    if len(rows) > MAX_WAITING:
        lines.append(f"(and {len(rows) - MAX_WAITING} more left out, not listed)")
    return [Evidence(LEFT_BY_NAME, lines, spelled="left out by name", counted=[])]


def waiting_rows(store: Store, agent: Agent) -> tuple[list[Proposal], list[Proposal]]:
    """What waits for a person that a curator's run is shown, newest first:
    the admits it filed itself, into its collection or proposed for another, and the archives and merges of the
    articles of its collection, whoever filed them, but those it may answer itself."""
    mine, others = [], []
    waiting = proposals(store, "waiting")
    named = cited(waiting)
    for p in waiting:
        if p.kind == "article.admit" and p.by.name == agent.id and p.by.kind == "agent":
            mine.append(p)
        elif p.kind in ("article.archive", "article.merge", "article.split") \
                and p.target.startswith(agent.target + "/") and not may_answer(agent, p, named):
            others.append(p)
    return mine[::-1], others[::-1]


def _claim_line(p: Proposal) -> str:
    """An admit as a line of the opening: its name, then each of its keys with its values, and its record."""
    keys = p.payload.get("keys") if isinstance(p.payload.get("keys"), dict) else {}
    line = f"- {p.payload.get('name') or p.payload.get('title') or p.target}"
    for key in sorted(keys):
        if isinstance(keys[key], list) and keys[key]:
            line += f", {key} {' '.join(map(str, keys[key]))}"
    if isinstance(ref := p.payload.get("record"), dict) and ref.get("id"):
        line += f", record {ref.get('source')}:{ref.get('id')}"
    return line


def _waiting(mine: list[Proposal], others: list[Proposal]) -> list[Evidence]:
    """What waits for the person, as a curator is told after its first run: its own admits, then the archives and
    merges of its collection."""
    out = []
    if mine:
        lines = [("Admits you filed as proposals that no caller has decided yet, each on the day it was filed. "
                  "Admit one again only to add to it: a caller decides the asks of one article as one."), ""]
        for p in mine[:MAX_WAITING]:
            into = p.target.rpartition("/")[0]
            elsewhere = f", into {into}" if into != p.by.name.rpartition("/")[0] else ""
            lines.append(f"{_claim_line(p)}{elsewhere}, filed {p.at[:10]}")
        if len(mine) > MAX_WAITING:
            lines.append(f"(and {len(mine) - MAX_WAITING} more waiting, not listed)")
        out.append(Evidence(WAITING_LIST, lines, spelled="waiting", counted=[]))
    if others:
        lines = [("Archives, merges and splits of articles of your collection, each with its id, who filed it, its "
                  "reason and the day it was filed."), ""]
        lines += [f"- {p.id} {_asks(p)}, by {p.by.name}: {p.reason}, {p.at[:10]}" for p in others[:MAX_WAITING]]
        if len(others) > MAX_WAITING:
            lines.append(f"(and {len(others) - MAX_WAITING} more waiting, not listed)")
        out.append(Evidence(ALSO_WAITING_LIST, lines, spelled="also waiting", counted=[]))
    return out


def _asks(p: Proposal) -> str:
    """What a waiting row asks, in a few words."""
    if p.kind == "article.admit":
        if set(p.payload) == {"title"}:  # a caller's bare name: which thing it names is the curator's to find
            return f"admit what a caller named {p.payload['title']}"
        return f"admit {_claim_line(p)[2:]} at {p.target}"
    if p.payload.get("distinct"):
        return f"keep {p.target} apart from {p.payload.get('with', '')}"
    said = {k: v for k, v in p.payload.items() if k != "from"}  # a split's own article is the one it is of
    return f"{p.kind.partition('.')[2]} {p.target}" + (f" {json.dumps(said, sort_keys=True)}" if said else "")


def _on_yours(store: Store, agent: Agent) -> list[Evidence]:
    """The waiting proposals other curators filed on this curator's collection that it may answer, oldest first, as
    the first run's discovery pinned them when it began."""
    named = cited(proposals(store)) | for_a_person(store, proposals(store, "waiting"))
    rows = [p for p in pinned_waiting(store) if may_answer(agent, p, named)]
    items = [Item(f"proposal:{p.id}", f"- {p.id} {_asks(p)}, by {p.by.name}: {_why(store, p)}, {p.at[:10]}")
             for p in rows[:MAX_WAITING]]
    if not items:
        return []
    lines = [("Proposals other curators filed for your collection, oldest first, each with its id, what it asks, who "
              "filed it, their reason and the day. Answer one by naming its id in your act of the same kind on the "
              "same thing, or turn it down with reject."), *(item.text for item in items)]
    if len(rows) > MAX_WAITING:
        lines.append(f"(and {len(rows) - MAX_WAITING} more waiting, not listed)")
    return [Evidence(ON_YOURS, lines, items=items, spelled="proposals waiting on your collection")]


def _accepted(store: Store, agent: Agent) -> list[Evidence]:
    """The proposals of this curator's collection a caller accepted that it has not carried out yet, oldest first."""
    every = proposals(store)
    named = cited(every)
    rows = [p for p in every if not carried_out(p) and may_answer(agent, p, named)]
    items = [Item(f"proposal:{p.id}", f"- {p.id} {_asks(p)}, filed by {p.by.name}, accepted by "
                  f"{p.decided_by.name if p.decided_by else 'a caller'}: "
                  f"{' '.join(p.decision_reason.split())[:NOTE_CAP] or _why(store, p)}, {p.at[:10]}")
             for p in rows[:MAX_WAITING]]
    if not items:
        return []
    lines = [("Proposals a caller accepted for your collection that are not carried out yet, oldest first, each with "
              "its id, what it asks, who filed it, who accepted it and their reason. Carry each out by naming its id "
              "in the act that does it."), *(item.text for item in items)]
    if len(rows) > MAX_WAITING:
        lines.append(f"(and {len(rows) - MAX_WAITING} more accepted, not listed)")
    return [Evidence(ACCEPTED, lines, items=items, spelled=ACCEPTED.lower())]


def _why(store: Store, p: Proposal) -> str:
    """A proposal's reason, or the words of the tell it was filed from."""
    told = next((r.body for id in p.cites if (r := store.record(id)) is not None), "")
    return " ".join((p.reason or told).split())[:NOTE_CAP]


def _decisions(store: Store, agent: Agent) -> tuple[list[Evidence], int]:
    """What another decided of this curator's proposals since its last run that finished began: each rejection with
    the reason its decider gave, one with no reason not told, and each proposal of its own, on any collection, made
    under another path than it asked. A decision is told once, though its row is written again. Gives too where the
    proposals log stands now, which the run keeps as where its next run starts once it finishes."""
    since = store.state(f"told:{agent.id}")
    if since is None:
        runs = store.rows("agent-runs", {"agent": agent.id, "status": "done"})
        since = runs[-1].seq if runs else 0
    first: dict[tuple[Any, ...], int] = {}
    top = 0
    for row in store.rows("proposals"):
        top = row.seq
        made = (row.data.get("applied") or {}).get("article") if isinstance(row.data.get("applied"), dict) else None
        first.setdefault((row.data.get("id"), row.data.get("status"), row.data.get("decided_at"), made), row.seq)
    items = []
    for p in proposals(store):
        if p.by.name != agent.id or first.get((p.id, p.status, p.decided_at, p.applied.get("article")), 0) <= since:
            continue
        if p.status == "accepted" and p.applied.get("article") not in (None, p.target):
            items.append(Item(f"decision:{p.id}", f"- your proposal for {p.target} was made as "
                                                  f"{p.applied['article']}"))
        elif p.status == "rejected" and p.decided_by is not None and p.decided_by.name != agent.id \
                and p.decision_reason.strip():
            items.append(Item(f"decision:{p.id}", f"- {p.id}, {p.kind.partition('.')[2]} of {p.target}: "
                                                  f"{_decided(p)}"))
    if not items:
        return [], top
    return [Evidence(DECISIONS, [("These were decided since your last run. Weigh a rejection's reason before you "
                                  "propose the same again, and when a proposal of yours was made under another path, "
                                  "update your links that point at the path you proposed, with link."), "",
                                 *(item.text for item in items)], items=items, spelled="decisions")], top


def _decided(p: Proposal) -> str:
    """How a proposal was decided, and by whom, with its decider's reason."""
    who = "a caller" if p.decided_by is None or p.decided_by.kind == "person" else p.decided_by.name
    return f"{p.status} by {who}: {p.decision_reason.strip()}"


def _beside(curation: _Curation) -> list[Evidence]:
    """What a curator reads beside its file's evidence lines, from the store. It is in the digest a run by change
    compares, as far as each entry counts."""
    agent, store, extraction = curation.agent, curation.store, curation.extraction
    evidence = _accepted(store, agent) + _on_yours(store, agent)
    if any(line.kind == "query" and isinstance(line.args, dict) and line.args.get("from") == "facts"
           for line in agent.evidence):  # a curator that reads no fact is not woken by one
        evidence += _unlinked(store, extraction, curation.own)
    return evidence


# -- running a curator -------------------------------------------------------------------------------------------------

def _curator_tools(curation: _Curation, lookup: Lookup, agent_tools: dict[str, Callable[..., Any]],
                   workspace: Workspace | None, sources: Mapping[str, Connector]) -> list[Callable[..., Any]]:
    """The tools a curator's run is given: `query`, `read`, every curator tool, `fetch` where one of `sources` answers
    questions, and every tool an app gives agents."""
    own = curation.tools()
    tools = agent_surface(lookup, curation.caller) + list(own.values())
    tools += [fetch_tool(sources, curation.store)] if askable(sources) else []
    return tools + [bind(tool, curation.caller, workspace) for tool in agent_tools.values()]


# -- what a curator has seen: carrying out a decision is never news to the curator that weighed it ---------------

def _view(curation: _Curation, agent: Agent, lookup: Lookup, functions: dict[str, Callable[..., Any]],
          workspace: Workspace | None, root: Path, starter: Starter | None, sources: Mapping[str, Connector] | None,
          judge: KeepJudge | None) -> list[Evidence]:
    return gather(agent, lookup, functions, workspace, writers=[agent.id], root=root, now=curation.clock(),
                  starter=starter, sources=sources, judge=judge) + _beside(curation)


UNSEEN = " unseen"  # on an item stored as read that the budget cut before the run saw it, so it reads new when shown
NEW = "[new] "  # what marks an item added or changed since the curator last read it


def has_news(store: Store, agent: Agent, view: list[Evidence], but: str = "") -> bool:
    """Whether a curator woken by change has news: its view, but the entry spelled `but`, is not the one stored as
    seen."""
    stored, printed = store.state(f"curated:{agent.id}"), fingerprint(agent, view)
    if not isinstance(stored, dict):
        return True
    def kept(view: dict[str, Any]) -> dict[str, str]:
        return {key: str(value).removesuffix(UNSEEN) for key, value in view.items()
                if not but or key.partition("\0")[0] != but}
    return kept(stored) != kept(printed)


def _counted_items(evidence: list[Evidence]) -> Iterable[tuple[str, Item]]:
    for entry in evidence:
        for item in entry.items if entry.counted is None else entry.counted:
            yield f"{entry.spelled or entry.heading}\0{item.key}", item


def seen(agent: Agent, evidence: list[Evidence]) -> dict[str, str]:
    """The view as a run stores it read: its fingerprint, with each item the budget cut marked unseen."""
    printed, shown = fingerprint(agent, evidence), {line for entry in evidence for line in entry.lines}
    for key, item in _counted_items(evidence):
        if key in printed and item.text not in shown:
            printed[key] += UNSEEN
    return printed


def mark_new(agent: Agent, evidence: list[Evidence], stored: Any) -> None:
    """Mark each item shown that is new since the view `stored` as read, or none on a curator's first run."""
    if not isinstance(stored, dict):
        return
    printed = fingerprint(agent, evidence)
    new = {item.text for key, item in _counted_items(evidence) if stored.get(key) != printed.get(key)}
    for entry in evidence:
        entry.lines = [(f"- {NEW}{line[2:]}" if line.startswith("- ") else NEW + line) if line in new else line
                       for line in entry.lines]


class _Measure:
    """A curator's acts, each landed in one write with the curator's view read just before and just after it. An
    item that changed is taken in as seen only when the act's own store write explains it. A view that cannot be
    read leaves the act unmeasured: the curator's next run reads the same line, and says why it cannot."""

    def __init__(self, store: Store, agent: Agent, read: Callable[[], list[Evidence]],
                 facts: Extraction | None) -> None:
        self.store, self.agent, self.read, self.facts = store, agent, read, facts
        self.stored: dict[str, str] = {}  # the view stored as seen, with each act's own change taken in as it lands
        self.held: list[str] = []  # the held records its acts took off the list

    def _read(self) -> _Read | None:
        """The view, and the store's side of each fact in it by item key, its ties and the expires of its record, with
        the fact's text as the view read it."""
        try:
            view = self.read()
        except (OperationError, EvidenceError):
            return None
        expires: dict[str, list[tuple[str, str]]] = {}
        for p in proposals(self.store):
            if p.kind == EXPIRE:
                expires.setdefault(p.target, []).append((p.id, p.status))
        side = {i.key: (repr((self.store.ties_of(i.key.removeprefix("fact:")), sorted(expires.get(i.left, [])))),
                        i.text)
                for e in view for i in (e.items if e.counted is None else e.counted) if i.key.startswith("fact:")}
        return _Read(fingerprint(self.agent, view), side)

    def __call__(self, act: Callable[[], Any]) -> Any:
        with self.store.exclusive():
            start = self.store.cursor()
            before = self._read()
            out = act()
            after = self._read() if before is not None else None
            if before is not None and after is not None:
                wrote = {id for kind, id in self.store.changed_since(start)[0] if kind == "article"}
                for key in before.view.keys() | after.view.keys():
                    if before.view.get(key) == after.view.get(key) or self.stored.get(key) != before.view.get(key) or \
                            not self._explained(key, before, after, wrote):
                        continue  # unchanged, already news, or not the act's own
                    if key in after.view:
                        self.stored[key] = after.view[key]
                    else:
                        del self.stored[key]
                        self.held += [item] if (item := key.partition("\0")[2]).startswith("held:") else []
        return out

    def _explained(self, key: str, before: _Read, after: _Read, wrote: set[str]) -> bool:
        """Whether the act's own store write explains a change of one item, the one rule of what is taken in. An item
        read from the store alone always is, as nothing else writes the store inside the act. An item that also reads
        what lies outside it is only by its store side: a fact when its ties or the expires of its record moved and
        the facts layer says the same of it, and a name with no article when it left as an article the act wrote came
        to claim it. The agent's own file is never, nor any line a measure does not read."""
        item = key.partition("\0")[2]
        if not key:
            return False
        if item.startswith("fact:"):
            if key not in before.view or key not in after.view or before.side[item][0] == after.side[item][0]:
                return False
            said = [_item(f).text for f in self.facts.facts(ids=[item.removeprefix("fact:")])] if self.facts else []
            return bool(said) and (before.side[item][1] + "\n").startswith((said[0] + "\n", said[0] + " ["))
        if item.startswith("unlinked:"):
            return key not in after.view and bool(set(self.store.named(item.removeprefix("unlinked:"))) & wrote)
        return True

    def keep(self, own: list[str]) -> None:
        """Store the view as seen, and add the held records its acts took off the list to `own`, so their facts are
        taken in as seen when they land."""
        self.store.set_state(f"own:{self.agent.id}", [*own, *self.held] or None)
        self.store.set_state(f"curated:{self.agent.id}", self.stored)


@dataclass
class _Read:
    """One measured read of a curator's view: the view as `fingerprint` gives it, and by each fact's item key the store's
    side of the fact and its text."""

    view: dict[str, str]
    side: dict[str, tuple[str, str]]


STORE_ONLY = ("query", "facts", "articles", "left", "held")  # the lines a measure reads: what reads the store alone


def store_lines(agent: Agent, functions: Mapping[str, Any]) -> Agent:
    """The agent with only the lines a measure reads: those that read the store alone, and no app's function."""
    return replace(agent, evidence=[line for line in agent.evidence
                                    if line.kind in STORE_ONLY and line.kind not in functions])


def _measure(curation: _Curation, lookup: Lookup, functions: dict[str, Callable[..., Any]],
             workspace: Workspace | None, root: Path, starter: Starter | None,
             sources: Mapping[str, Connector] | None) -> _Measure:
    """A measure of a curator's acts. Its reads ask no keep judge and read no line outside the store, so nothing but
    the store is read inside a write."""
    agent, inside = curation.agent, store_lines(curation.agent, functions)
    return _Measure(curation.store, agent,
                    lambda: _view(curation, inside, lookup, {}, workspace, root, starter, sources, None),
                    curation.extraction)


def _measure_of(agent: Agent, workspace: Cerebro) -> _Measure:
    """A measure of a curator's view outside its run."""
    curation = _Curation(agent, workspace.store, workspace.clock, Curated(), workspace.lookup.extraction)
    curation.own = own_names(workspace.store, workspace.connectors, read_ours(workspace.root))
    return _measure(curation, workspace.lookup, workspace.exposed.evidence, workspace.handle, workspace.root,
                    workspace.start_script, workspace.connectors)


def own_facts(workspace: Cerebro, landed: list[tuple[Any, Record]]) -> None:
    """Take the facts that landed from a record a curator's acts took off its held list, at the revision it was held
    at, into the view that curator has seen, once, with each entry they came into, and let the record go at any
    revision: their landing is no news to it. Anything else that moved, and any change of them after, is."""
    records = {f"held:{record.source}:{record.id}" for _, record in landed}
    for agent in workspace.agents:
        own = workspace.store.state(f"own:{agent.id}") or [] if agent.role == "curator" else []
        mine = {f"fact:{fact.id}": key for fact, record in landed
                if (key := f"held:{record.source}:{record.id}:{record.revision}") in own}
        if (left := [h for h in own if not any(h.startswith(r + ":") for r in records)]) == own:
            continue
        with workspace.store.exclusive():
            if mine and (read := _measure_of(agent, workspace)._read()) and isinstance(
                    stored := workspace.store.state(f"curated:{agent.id}"), dict):
                into = {k.partition("\0")[0] for k in read.view if k.partition("\0")[2] in mine}
                workspace.store.set_state(f"curated:{agent.id}", stored | {
                    k: v for k, v in read.view.items() if (k.partition("\0")[2] in mine and k not in stored) or k in into})
            workspace.store.set_state(f"own:{agent.id}", left or None)


def decided(agent: Agent, workspace: Cerebro, caller: Caller, accept: list[str | Decision] | None,
            reject: list[str | Decision] | None) -> DecideResult:
    """Land a decision on proposals that `agent` alone filed, as one act of that curator's, measured as it lands."""
    measure = _measure_of(agent, workspace)
    if not isinstance(stored := workspace.store.state(f"curated:{agent.id}"), dict):  # one that never ran is due anyway
        return workspace.lookup.decide(caller, accept, reject)
    measure.stored = stored
    out = workspace.lookup.decide(caller, accept, reject, measure=measure)
    measure.keep(workspace.store.state(f"own:{agent.id}") or [])
    return out


def run_curator(agent: Agent, *, lookup: Lookup, store: Store, agent_run: AgentRun, root: Path,
                functions: dict[str, Callable[..., Any]], agent_tools: dict[str, Callable[..., Any]],
                workspace: Workspace | None = None, ledger: Ledger | None = None, model: ModelSpec | None = None,
                clock: Clock = system_clock, changed_only: bool = False, starter: Starter | None = None,
                sources: Mapping[str, Connector] | None = None,
                decider: Decider | None = None, decision: ModelSpec | None = None, wake: str = "hand",
                accepted: Collection[str] = ()) -> Curated:
    """Run a curator on its collection: gather its evidence, run it through `run_agent` with the tools
    `_curator_tools` gives, `fetch` over `sources`, the workspace's sources by name, when one of them answers
    questions, and land its acts as its authority says.

    `changed_only` skips the run when the digest of its evidence is the one its last run that finished read, kept
    under `curated:<agent id>` in the store's state, as a pass does for a curator woken by a change. With a `decider`,
    an `articles` line with `keep` is cut by the keep judge on it, as a compile's is, priced on `decision`. `wake`
    is what woke the run: a schedule word, `change`, or `hand`, and a curator that never finished a run has its
    first; the run is told which, after the ids of the accepted proposals that woke it, `accepted`."""
    if agent.role != "curator":
        raise CurateError(f"agents/{agent.id}.md curates no collection: a curator is a collection's curator.md")
    done = Curated()
    curation = _Curation(agent, store, clock, done, getattr(lookup, "extraction", None))
    curation.ours = read_ours(root)
    curation.sources = list(sources or {})
    curation.shared = sorted({key for source in (sources or {}).values() for key, weight in source.IDENTITY_KEYS.items()
                              if not weight})
    first = not store.rows("agent-runs", {"agent": agent.id, "status": "done"})
    curation.wake = "first" if first else wake
    curation.proposing = proposing(store, agent.id)
    curation.own = own_names(store, sources or {}, curation.ours)
    curation.measure = _measure(curation, lookup, functions, workspace, root, starter, sources)
    curation.lookup = lookup
    tools = _curator_tools(curation, lookup, agent_tools, workspace, sources or {})
    judge = keep_judge(decider, store, clock, ledger, decision, root)
    evidence = _view(curation, agent, lookup, functions, workspace, root, starter, sources, judge)
    curation.measure.stored = seen(agent, evidence)  # as it read it: its file is read now, not at the end
    done.read = sum(len(entry.items) for entry in evidence)
    if changed_only and not has_news(store, agent, evidence):
        return Curated(skipped=True)
    mark_new(agent, evidence, store.state(f"curated:{agent.id}"))
    # after the digest: what woke a run, what was decided since the last, the admits a caller turned down, what it
    # left out by name and what it filed that waits, is no change of what it reads
    decided, told = _decisions(store, agent)
    waiting: list[Evidence] = []
    if not curation.proposing:  # a curator in its own first run is shown nothing waiting
        waiting = _waiting(*waiting_rows(store, agent))
    news = has_news(store, agent, evidence, but=ACCEPTED.lower())
    evidence = (_woke(curation.wake, accepted, news) + decided
                + _deferred(store, agent, curation.extraction, curation.sources) + evidence
                + _left_by_name(store, agent) + waiting)
    done.run = run_agent(agent, evidence, tools, agent_run=agent_run, store=store, ledger=ledger, model=model,
                         clock=clock)
    # a run that ended, normally or spent or cut, is fresh until an input moves: what it decided landed as it decided,
    # and an empty verdict is no failure
    if done.run.status in ("done", WROTE_NOTHING, "budget spent"):
        curation.measure.keep(store.state(f"own:{agent.id}") or [])
        store.set_state(f"told:{agent.id}", told)
    return done
