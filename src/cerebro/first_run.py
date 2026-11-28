"""A workspace's first run: read the sources, ask each curator what the wiki should hold, and write nothing until a
person decides.

A workspace is in its first run when it has a curator, no curator has finished a run, and no collection a curator
keeps holds an article, until its list is decided. Its session is kept in the store under `KEY` (`cerebro:first-run`):
its status, the curators' runs whose spend counts toward its cap, each curator's state, and where a stop left it. A
library call (`Cerebro.run_once`) never begins one: `serve --once` and the loop behind `serve` run it.

- **Discover.** Every source is listed and every record held, none extracted. The curators then run once each, side
  by side, in proposal mode (`curate.proposing`): each admit waits for a person whatever its authority,
  and each candidate a curator leaves out is a row of the `left-out` log. Each is told to propose what clearly
  matters and stop, and its held list holds the records of the last `evidence.RECENT_DAYS` days. Each reads the
  proposals as they stood when discovery began, the session's `pinned` ids, where a pending admit stands in for its
  article (`held.stand_ins`), so none weighs what another proposes beside it. What each did lands in id order,
  whichever finished first. A curator that fails is shown on the list, and runs again on a later pass of `serve`.
- **The list.** The waiting admits are the list, with the candidates each curator left out. Shown to a person, it
  is a tree one model call arranges (`tree.arrange`): what is one thing is one line, the groups are ordered and
  ticked, and each sits under what its `linked` names. A group the checks refuse stays separate items. With no
  model, or a reply that is not the JSON asked for, it is the plain tree: the order filed, nothing ticked. `--yes`
  takes what is ticked. The list only approves: what is not ticked waits, undecided, for `cerebro review`, for a late
  list too, and is marked as offered.
- **The decision.** A screen works through `Approval`, a seam with two jobs: give the list, and apply a person's
  choices. `LocalApproval` reads with the `query` operation and decides with `decide`, as the caller deciding. A ticked left-out candidate is filed as an admit and accepted in the same
  write; one left alone stays evidence. A decision is a new row of the `proposals` log.
- **Build.** Once anything is approved, or nothing waits, each curator makes the accepted rows of its collection, then
  a pass with no curator reads the records of the same last `RECENT_DAYS` days for the articles and compiles each
  once. The older records stay held: a later pass, under `serve`, reads them and rebuilds what they change. A curator
  still in its own first run after that, as one that reads facts and found none before they existed, runs once more
  in proposal mode: its list is late, and waits alone.
- **The cap.** Discovery spends at most `budget.first_run_usd`, $10 when the key is left out, counted from the ledger
  by the curators' run ids, and every model call of discovery is metered against it: once it is reached the runs under
  way stop at their next call. A run's spend lands whole when it ends, so the cap is checked before each curator
  starts. At a terminal the run asks whether to go on for about what the curators left would spend; otherwise it
  stops with the line that says what to raise. The late round counts toward it too, and a curator
  the cap cut runs only when a person asks, at that question or by `cerebro agents run`, which goes through `by_hand`.

Every string a list shows from a workspace's files or a model passes through `shown`.
"""

from __future__ import annotations

import re
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .agent import Budget
from .budget import METER, BudgetSpent, Meter, Priced, first_run_cap, run_estimate, spent_by_runs
from .caller import Caller
from .clock import stamp
from .compile import FAILED_WAIT, FAILED_WAIT_MOST
from .curate import (
    FIRST_RUN_KEY,
    LEFT_OUT,
    curator_order,
    proposing,
    weights_of,
    working_set,
    yes_left,
)
from .errors import CerebroError
from .evidence import IN_FIRST_BUILD, RECENT_DAYS
from .held import (
    Held,
    _name,
    before_cut,
    first_run_since,
    held_for,
    own_held,
    own_names,
    own_side,
    passed_first_run,
    same_name,
)
from .identity import naming_keys, redirects, working, would_bind
from .records import ACCEPTED, MAX_DECIDE, WAITING, NotAllowed, Proposal, _refuse_secret, proposals, propose
from .records import _slug as record_slug
from .review import held_lines
from .run import Run
from .runner import PassResult, Progress, first_list, first_list_line, hold_sources, run_curators, run_pass
from .shown import shown
from .store import record_from
from .tree import Group, Row, arrange, plain, tree

if TYPE_CHECKING:
    from .agent import Agent
    from .app import Cerebro
    from .connector import Record
    from .store import Store

__all__ = [
    "KEY",
    "Applied",
    "Approval",
    "Choices",
    "Listed",
    "LocalApproval",
    "Outcome",
    "Pile",
    "Row",
    "build",
    "by_hand",
    "discover",
    "distill_line",
    "in_first_run",
    "late",
    "lines",
    "run",
    "served",
    "shown_at",
    "stamp_shown",
    "waiting_line",
]

KEY = FIRST_RUN_KEY
NOT_WEIGHED = "not weighed"  # the reason of a held record its curator neither proposed nor left out
OLDER = f"not weighed yet: older than {RECENT_DAYS} days"  # the reason of one the first run's held list did not show
DECLINED = "declined"  # a session whose list a person declined whole: the next run begins a new one
CLOSED_OVER = 10  # a group of more rows than this starts closed, and the plain list shows its first rows
SHOWN_ROWS = 5  # rows the plain list shows of a closed group
OWN_LISTED = 50  # own-side held records no curator weighed that one collection lists; the rest are counted
NOTE_CAP = 300  # characters of a curator's note, or of a line the list writes under a row; a reason shows whole
NAME_CAP = 200  # characters of a name a row shows
WRITERS = ("article", "composer", "subagent", "query")  # the roles whose runs compile an article of a collection
OWNER = Caller("you", "person")
SERVED = ("A client with a person token can decide them over MCP or the JSON API with decide. Until then the sources "
          "are checked and what they list is held for the first run, not extracted.")
FAILED = f"{KEY}:failed"  # what a failed first run left under `serve`: when, how often, and what it lacked
QUIT_LINE = "Nothing recorded. Run again when you are ready, or admit one with {form}."  # said on Escape;
# running again shows the list with no model call
QUIT_NEXT = "next      cerebro serve --once"  # the last line of a list left with nothing recorded


class FirstRunError(CerebroError):
    """A first run's list cannot be decided as asked."""


# The screen a first run hands its events to, as `run` was given it: the sources read, discovery's curators, the
# approval and the build, each shown on a live step or in its progress region. None hands
# nothing: every other caller of the steps below shows only the lines `say` is given.
_ON: ContextVar[Callable[[Progress], None] | None] = ContextVar("first_run_on", default=None)


def _heard(workspace: Cerebro) -> Callable[[Progress], None] | None:
    """The screen `run` was given, which hears each event with its words masked, or None."""
    on = _ON.get()
    if on is None:
        return None
    every = workspace.mask_every()
    return lambda event: on(replace(event, key=every(event.key), text=every(event.text), why=every(event.why)))


def _emit(workspace: Cerebro, stage: str, kind: str, key: str = "", **more: Any) -> None:
    """Hand the screen one event of the first run, when `run` was given one."""
    if (heard := _heard(workspace)) is not None:
        heard(Progress(stage, kind, key, **more))


# -- the list ---------------------------------------------------------------------------------------------------------


@dataclass
class Listed:
    """One curator's part of the list: the rows it filed and the candidates it left out, and its state, `proposed`,
    `nothing`, `failed` or `budget`, with a note that says what stopped it. `cut` marks a curator the cap cut."""

    agent: str
    collection: str
    rows: list[Row] = field(default_factory=list)
    state: str = "proposed"
    note: str = ""
    cut: bool = False
    said: str = ""  # the curator's own last line, which counts what it left out by reason, in its words
    unlisted: str = ""  # the line that counts the held records of the workspace's own side the list leaves off

    def proposed(self) -> list[Row]:
        return [row for row in self.rows if not row.left_out]

    def left(self) -> list[Row]:
        return [row for row in self.rows if row.left_out]


@dataclass
class Pile:
    """What waits for a person, as one list they decide: each curator's part, the tree its proposals make, why the
    tree is plain when it is, the line that says why discovery stopped short, if it did, and whether this is a late
    list or the review's."""

    lists: list[Listed] = field(default_factory=list)
    stopped: str = ""
    late: bool = False
    asked: list[str] | None = None  # the curators a late list was asked of, so it is decided as it was given
    review: bool = False  # `cerebro review`'s list, where a row can be rejected
    groups: list[Group] = field(default_factory=list)  # the top-level groups in order, each with what sits under it
    note: str = ""  # why the tree is the plain one, when a model call was asked to arrange it and was not taken

    def rows(self) -> list[Row]:
        return [row for listed in self.lists for row in listed.rows]

    def waiting(self) -> int:
        """How many proposals wait for a decision."""
        return sum(len(listed.proposed()) for listed in self.lists)

    def left(self) -> list[Group]:
        """The candidates the curators left out that a tick may admit, each a group of its own, unticked."""
        return [Group([row]) for row in self.rows() if row.left_out]


@dataclass
class Choices:
    """A person's choices on a list: each article to make, by the ids of the rows that make it, the lead's first, in
    the order they land, and every other row left as it is, by id. A row left alone is no decision to reject it."""

    accept: list[list[str]] = field(default_factory=list)
    leave: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, pile: Pile, ticked: list[Group]) -> Choices:
        """The choices of the groups `ticked`: a top-level group lands before what sits under it."""
        keys = {tuple(t.ids()) for t in ticked}
        order = [g for top in [*pile.groups, *pile.left()] for g in (top, *top.under)]
        taken = [g for g in order if g.rows and tuple(g.ids()) in keys]
        ids = {id for g in taken for id in g.ids()}
        return cls([g.ids() for g in taken], [row.id for row in pile.rows() if row.id not in ids])

    @classmethod
    def starter(cls, pile: Pile) -> Choices:
        """What Enter on Approve takes as the list opens, the plain accept line and `--yes`: what the tree ticks."""
        return cls.of(pile, [g for top in pile.groups for g in (top, *top.under) if g.tick])


def arranged(workspace: Cerebro, pile: Pile) -> Pile:
    """The pile with its tree as the model arranges it, or the plain tree and why. A row left on an earlier list
    starts unticked."""
    n = pile.waiting()
    groups, pile.note = arrange(workspace, [row for row in pile.rows() if not row.left_out],
                                asking=lambda: _emit(workspace, "arrange", "start", total=n))
    _emit(workspace, "arrange", "end", total=n)
    for g in groups:
        g.tick = g.tick and not any(row.offered for row in g.rows)
    pile.groups = tree(groups, _articles(workspace), redirects(workspace.store))
    return pile


def _articles(workspace: Cerebro) -> dict[str, str]:
    """The articles of the working set, id to title, that a group may sit under."""
    return {i.id: i.title or i.id for i in working_set(workspace.store)}


@dataclass(frozen=True)
class Admit:
    """An admit a person rejected, as the list reads it: its name, the collection it would admit into, its keys and
    the held record it names, so the record is not listed again as one its curator never weighed."""

    id: str
    collection: str
    name: str
    keys: dict[str, list[str]] = field(default_factory=dict)
    record: tuple[str, str] | None = None



@dataclass
class Applied:
    """What applying a person's choices did: the ids accepted, the ids refused with the line that says why, and a
    line for each decided."""

    accepted: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    already: dict[str, str] = field(default_factory=dict)  # a selected row decided the other way meanwhile: its line


class Approval:
    """The seam between a screen that shows the list and the workspace that holds it: give the list, and apply a
    person's choices. A screen works through it alone, so a workspace served elsewhere can stand behind another."""

    def pile(self) -> Pile:
        raise NotImplementedError

    def apply(self, choices: Choices) -> Applied:
        raise NotImplementedError


class LocalApproval(Approval):
    """The list of a workspace on this machine, decided as `caller`, the owner by default. `agents` names the curators
    whose lists it gives, every curator when None, and `late` marks a late list."""

    def __init__(self, workspace: Cerebro, caller: Caller = OWNER, agents: list[str] | None = None, late: bool = False):
        self.workspace, self.caller, self.late = workspace, caller, late
        self.every = _curators(workspace)
        self.agents = [a for a in self.every if agents is None or a.id in agents]
        self.agents_given = agents is not None

    def _query(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return list((self.workspace.call(self.caller, "query", args) or {}).get("items") or [])

    def pile(self) -> Pile:
        """The list as it stands, with its plain tree: every proposal that waits a row of the curator of its
        collection, in the order filed, and each curator asked with the candidates it left out and the held records
        it never weighed, those of the workspace's own side last and at most `OWN_LISTED`, with a line that counts the
        rest. Each row of the workspace's own side says so."""
        session = _state(self.workspace) or {}
        since = str(session.get("at") or "")
        states = session.get("lists") or {}
        cut = set(session.get("cut") or [])
        offered = set(session.get("offered") or [])
        pile = Pile(stopped=str(session.get("stopped") or "") if not self.late else "", late=self.late,
                    asked=[a.id for a in self.agents] if self.agents_given else None)
        asked = {a.id for a in self.agents}
        keeper = {a.target: a for a in self.every}
        rows_of: dict[str, list[Row]] = {}
        for item in self._waiting(session):
            agent = keeper[_collection(item)]
            row = _proposed(agent, item)
            row.offered = row.id in offered
            rows_of.setdefault(agent.id, []).append(row)
        declined = self._declined_admits()
        store, sources, ours = self.workspace.store, self.workspace.connectors, self.workspace.ours
        held, own = own_held(store, sources, ours), own_names(store, sources, ours)
        for agent in self.every:
            rows = rows_of.get(agent.id, [])
            if self.agents_given and agent.id not in asked and not rows:
                continue
            state = states.get(agent.id) or {}
            if not rows and state.get("state") == "empty" and not state.get("passes"):
                continue  # never asked, as it had nothing to weigh: off the list
            names = {row.name.lower() for row in rows}
            completed: list[Row] = []
            if agent.id in asked:
                rows += [row for row in self._left_out(agent, since) if row.name.lower() not in names]
                rows += (completed := self._unweighed(agent, rows, declined))
            for row in rows:  # a row is of the workspace's own side by its record, else by its keys or its name
                row.own = (row.record["source"], row.record["id"]) in held if row.record else \
                    own_side(row.keys, ours) or same_name(row.name, own)
            unlisted = {row.id for row in [row for row in completed if row.own][OWN_LISTED:]}
            rows = [row for row in rows if row.id not in unlisted]
            said = str(state.get("late_said" if self.late else "said") or "")  # a late list's own
            if completed:  # code completed its rows, whatever the model listed, so the line counts
                # the rows the list shows; elsewhere the line is the curator's own
                said = counted_left_out([row for row in rows if row.left_out and row.record])
            if self.late and not rows:
                continue
            pile.lists.append(Listed(agent.id, agent.target, rows,
                                     str(state.get("state") or ("proposed" if rows else "nothing")),
                                     str(state.get("note") or ""), agent.id in cut, said,
                                     f"{len(unlisted)} more held for {agent.target} are of the workspace's own side "
                                     "and not listed" if unlisted else ""))
        pile = self.workspace.masked(pile)  # before its tree, whose groups hold its rows
        pile.groups = tree(plain([row for row in pile.rows() if not row.left_out], weights_of(self.workspace.store)),
                           _articles(self.workspace), redirects(self.workspace.store))
        return pile

    def _waiting(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        """The admits that are rows of this list, in the order discovery kept them, the same whoever finished first,
        then as they were filed: every admit into a collection a curator keeps, whoever filed it, and on a late list
        one by a curator it was asked of."""
        order = {id: n for n, id in enumerate(session.get("order") or [])}
        # the query gives the newest first; a stable sort keeps the filing order among the rest
        filed = self._query({"from": "proposals", "status": "waiting", "kind": "article.admit"})[::-1]
        keeper = {a.target: a for a in self.every}
        asked = {a.id for a in self.agents}
        out = []
        for p in sorted(filed, key=lambda p: order.get(str(p.get("id")), len(order))):
            by = str((p.get("by") or {}).get("name") or "")
            agent = keeper.get(_collection(p))
            if agent is None or self.late and by not in asked:
                continue
            out.append(p)
        return out

    def _declined_admits(self) -> list[Admit]:
        """The admits a caller rejected, as the `query` operation gives
        them, but one whose article is in the working set now, decided otherwise since."""
        out = []
        for item in self._query({"from": "proposals", "status": "rejected", "kind": "article.admit"}):
            who = item.get("decided_by") or {}
            if who.get("kind") == "person" and not working(self.workspace.store, str(item.get("target") or "")):
                out.append(_admit_of(item))
        return out

    def _left_out(self, agent: Agent, since: str) -> list[Row]:
        """The candidates the curator left out in this first run, each once, at its latest reason."""
        rows, seen = [], set()
        held: dict[str, dict[str, Any]] = {}  # each source's held rows by id, read once for the whole list
        for item in self._query({"from": LEFT_OUT, "agent": agent.id}):  # newest first
            if since and str(item.get("at") or "") < since:
                continue
            key = (str(item.get("name", "")).lower(), item.get("source", ""), item.get("id", ""))
            if key in seen:
                continue
            seen.add(key)
            keys = _held_keys(self.workspace, str(item.get("source") or ""), str(item.get("id") or ""), held)
            if not keys and isinstance(item.get("keys"), dict):
                keys = {str(k): [str(v) for v in vs] for k, vs in item["keys"].items()}
            id = str(item.get("id") or "")
            name = " ".join(str(item.get("name") or "").split()) or _held_name(
                self.workspace, str(item.get("source") or ""), id, held) or id.rpartition("/")[2].rpartition(".")[0] or id
            if not name:
                continue  # nothing names it: no row a person could tell apart
            ref = {"source": str(item["source"]), "id": id} if item.get("source") and id else None
            rows.append(Row(f"left-out:{item.get('seq', len(seen))}", agent.id, agent.target, name,
                            str(item.get("reason") or ""), True, keys, record=ref))
        return list(reversed(rows))

    def _unweighed(self, agent: Agent, rows: list[Row], declined: list[Admit]) -> list[Row]:
        """The records held for the curator's collection that it neither proposed nor left out, each a left-out row,
        so no record it holds is on no list: under the reason an earlier run of the curator gave
        it, else `not weighed`, or `OLDER` when the first run's held list did not show it. A proposed row speaks for
        each held record its admit binds alone, as `_bound_alone` gives it, and counts them under it. A row or a
        declined admit that names a record speaks for that record; one by name alone speaks for one held record of
        that name: the one a value of its keys names, or an address at a domain it gives, and none when its keys name
        none, or with no keys the first nothing spoke for yet. Every held record no admit takes is a row of its own,
        whatever name or address it shares, since past what an admit binds the engine cannot tell one thing's records
        from another's. A row is named by its record's name, else by its file's name with no folder and no extension,
        never by its path."""
        prior: dict[tuple[str, str], str] = {}
        for item in self._query({"from": LEFT_OUT, "agent": agent.id})[::-1]:  # oldest first: the latest stands
            if item.get("source") and item.get("id") and str(item.get("reason") or "").strip():
                prior[(str(item["source"]), str(item["id"]))] = str(item["reason"])
        own = f"agents/{agent.id}.md"  # a proposal of this curator's is a row here, and its rows say what it is for
        every = held_for(self.workspace.store, agent.target, sorted(self.workspace.connectors), limit=10**6,
                         ours=self.workspace.ours).items
        held = [h for h in every if not h.proposed or h.proposed.get("by") == own]
        mine = [row for row in rows if row.collection == agent.target]
        theirs = [d for d in declined if d.collection == agent.target]
        taken = {(str(row.record["source"]), str(row.record["id"])) for row in mine if row.record}
        taken |= {d.record for d in theirs if d.record}
        seen: dict[str, dict[str, Any]] = {}
        taken |= _bound_alone(self.workspace.store, [r for r in mine if not r.left_out],
                              [record_from(_held_row(self.workspace, h.source, h.id, seen)) for h in every])

        def fold(name: str) -> str:
            return " ".join(name.split()).casefold()

        def at(h: Held, want: set[str]) -> bool:  # a value it gives, or an address at a domain it gives
            return any(v.strip().lower() in want or v.strip().lower().rpartition("@")[2] in want
                       for k, vs in h.keys.items() if k != "name" for v in vs if v.strip())

        def pick(name: str, keys: dict[str, list[str]]) -> Held | None:
            # those that write the name, in any case and spacing, else those of its file name
            same = [h for h in held if name.strip() and fold(h.name) == fold(name)] or [
                h for h in held if _slug(name) and _slug(h.name) == _slug(name)]
            named = [h for h in same if (h.source, h.id) not in taken]
            want = {str(v).strip().lower() for k, vs in keys.items() if k != "name" for v in vs if str(v).strip()}
            chosen = next((h for h in named if at(h, want)), None) if want else named[0] if named else None
            if chosen is not None:
                taken.add((chosen.source, chosen.id))
            return chosen

        for row in [*(r for r in mine if not r.left_out), *(r for r in mine if r.left_out)]:  # proposed first
            if not row.record and (chosen := pick(row.name, row.keys)) and row.left_out:
                row.record = {"source": chosen.source, "id": chosen.id}  # the row takes the record of whom it is for
        for d in theirs:
            if not d.record:
                pick(d.name, d.keys)
        since = first_run_since(self.workspace.store)
        return [Row(f"not-weighed:{h.source}:{h.id}", agent.id, agent.target,
                    h.name or (base := h.id.rpartition("/")[2]).rpartition(".")[0] or base,
                    prior.get((h.source, h.id), OLDER if before_cut(h.at, since) else NOT_WEIGHED), True, dict(h.keys),
                    record={"source": h.source, "id": h.id}, unweighed=(h.source, h.id) not in prior)
                for h in held if (h.source, h.id) not in taken]

    def apply(self, choices: Choices) -> Applied:
        """Decide the list as `choices` say, in one `decide` call as the person, in one write that lands whole or
        not at all. A candidate left out is filed first as an admit of its curator's collection, then accepted. The
        path of each other row of an article leads to the lead's, so its curator makes one article of them. What is
        left alone is marked as offered, never rejected. Refused for an agent, an app or a source, none of which is a caller."""
        if self.caller.kind != "person":
            raise NotAllowed(f"the first run's list: {self.caller.name} is not a caller, and only a caller decides it")
        rows = {row.id: row for row in self.pile().rows()}
        named = [id for ids in choices.accept for id in ids]
        stood = decided_meanwhile(self.workspace, [id for id in named if id not in rows], ACCEPTED)
        if unknown := [id for id in named if id not in rows and id not in stood]:
            raise FirstRunError(f"the first run's list holds no row {', '.join(unknown)}: it changed since it was shown, "
                                "so nothing was decided: show it again")
        applied = Applied(already={id: line for id, line in stood.items() if line})
        articles = [[id for id in ids if id in rows] for ids in choices.accept]
        answers: dict[str, str] = {}
        try:
            with self.workspace.store.exclusive():
                self._not_compiling([rows[ids[0]] for ids in articles if ids])
                filed = {id: self._file(rows[id]) for ids in articles for id in ids if rows[id].left_out}
                for ids in articles:
                    if ids:
                        _lead_to(self.workspace, rows[ids[0]].path, [rows[id].path for id in ids[1:]])
                calls: list[list[str]] = []
                for ids in articles:  # the rows of one article go in one call, as decide takes them
                    if not calls or len(calls[-1]) + len(ids) > MAX_DECIDE:
                        calls.append([])
                    calls[-1] += [filed.get(id, id) for id in ids]
                for accept in (call for call in calls if call):
                    said = self.workspace.decide(self.caller, list(accept))
                    answers |= said.answers
                    applied.notes |= said.notes
                    if any(answer != "decided" for answer in answers.values()):
                        raise _Refused
                offer(self.workspace, [rows[id] for id in choices.leave if id in rows])
                if not self.late and not any(articles) and self._list_declined():
                    _declined_whole(self.workspace)  # clearing its session is part of the same write
        except _Refused:
            applied.refused = {id: applied.notes.get(id, answer) for id, answer in answers.items()
                               if answer not in ("decided", "not decided")}
            return applied
        applied.accepted = [id for ids in articles for id in ids]
        return applied

    def _list_declined(self) -> bool:
        """Whether the first run's list stands declined whole: its session is under way, a person accepted nothing of
        it since it began, and no row of it waits."""
        session = _state(self.workspace) or {}
        return session.get("status") not in (None, "done", DECLINED) \
            and not _decided_since(self.workspace, str(session.get("at") or ""))[0] and not self._waiting(session)

    def _not_compiling(self, accepted: list[Row]) -> None:
        """Refuse, before anything is decided, a batch that accepts an article of a collection a compile is writing
        now, by the lease of one of its writers that another run holds, so an approval and a
        compile never write one collection at once. A batch of
        rejects alone makes no article, and decides whatever runs."""
        now = self.workspace.clock()
        for collection in dict.fromkeys(row.collection for row in accepted):
            for agent in self.workspace.agents:
                held = self.workspace.store.state(f"lease:{agent.id}") or {}
                if agent.target == collection and agent.role in WRITERS and held.get("until") and \
                        datetime.fromisoformat(str(held["until"])) > now:
                    raise FirstRunError(f"a compile of {collection} collection runs now, so nothing was decided: "
                                        "decide again once it ends")

    def _file(self, row: Row) -> str:
        """File a ticked candidate left out as an admit of its curator's collection, for the accept that follows,
        its reason `left out: <reason>`, with its held record when it names one: no operation files an admit with its
        keys, so it is written here, through `records`, as a curator files one."""
        _refuse_secret(self.workspace.store, f"{row.collection}/{_slug(row.name)}")
        proposal = Proposal(id=f"p_{uuid.uuid4().hex[:12]}", kind="article.admit", target=f"{row.collection}/{_slug(row.name)}",
                            by=Caller(row.agent, "agent"), at=stamp(self.workspace.clock()),
                            payload={"name": row.name, "keys": row.keys} | ({"record": dict(row.record)} if row.record else {}),
                            reason=f"left out: {row.reason}")
        propose(self.workspace.store, proposal)
        return proposal.id


def decided_meanwhile(workspace: Cerebro, ids: list[str], asked: str) -> dict[str, str]:
    """Of `ids`, a person's choice of `asked` on a list, those decided while the list was open, each with its line: none
    for one that stands as asked, and `review --accept`'s for one decided the other way, which stands as it was."""
    state = {p.id: p.status for p in proposals(workspace.store) if p.id in ids and p.status != WAITING}
    return {id: "" if status == asked else f"{id} is already {status}" for id, status in state.items()}


def offer(workspace: Cerebro, rows: list[Row]) -> None:
    """The proposed rows a person was shown and left start unticked on a later list or review, though a tick still
    takes them. Kept on the first run's session, begun as done past a first run, and not kept in one that has not
    begun."""
    session = _state(workspace)
    left = {row.id for row in rows if not row.left_out}
    if not left or session is None and in_first_run(workspace):
        return
    session = session or {"status": "done"}
    session["offered"] = sorted({*(session.get("offered") or []), *left})
    _save(workspace, session)


class _Refused(CerebroError):
    """A list whose decide did not land: raised to undo what was filed for it."""


def _lead_to(workspace: Cerebro, lead: str, paths: list[str]) -> None:
    """Lead each path of `paths` to `lead`, as a merge leads a merged-away id, so the decide makes one article of
    them and a link to a path a curator was given reaches it."""
    for gone in dict.fromkeys(paths):
        if gone not in ("", lead) and workspace.store.article(gone) is None:
            workspace.store.append("merges", {"at": stamp(workspace.clock()), "redirect": gone, "to": lead, "path": gone})


def _slug(name: str) -> str:
    """The file name a proposal's name gives, as a curator's admit makes it."""
    return record_slug(name) or "article"


def _collection(item: dict[str, Any]) -> str:
    """The collection a waiting admit would admit into."""
    return str(item.get("target") or "").rpartition("/")[0]


def _proposed(agent: Agent, item: dict[str, Any]) -> Row:
    """A row from a waiting admit, as the `query` operation gives it, in the part of `agent`, the curator of the
    collection it is for."""
    payload = item.get("payload") or {}
    target = str(item.get("target") or "")
    keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    ref = payload.get("record") if isinstance(payload.get("record"), dict) else None
    record = {"source": str(ref["source"]), "id": str(ref["id"])} if ref and ref.get("source") and ref.get("id") \
        else None
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    linked = fields.get("linked") if isinstance(fields.get("linked"), list) else []
    by = str((item.get("by") or {}).get("name") or agent.id)
    return Row(str(item["id"]), by, _collection(item) or agent.target,
               str(payload.get("name") or payload.get("title") or target.rpartition("/")[2]),
               str(item.get("reason") or ""), keys=dict(keys), record=record, path=target,
               linked=[str(p) for p in linked])


def _admit_of(item: dict[str, Any]) -> Admit:
    """A rejected admit, as the `query` operation gives it, as the list reads it."""
    payload = item.get("payload") or {}
    ref = payload.get("record")
    record = (str(ref["source"]), str(ref["id"])) if isinstance(ref, dict) and ref.get("source") and ref.get("id") \
        else None
    target = str(item.get("target") or "")
    keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    return Admit(str(item["id"]), _collection(item), str(payload.get("name") or payload.get("title") or
                                                         target.rpartition("/")[2]),
                 {str(k): [str(v) for v in vs] for k, vs in keys.items() if isinstance(vs, list)}, record)


def _held_row(workspace: Cerebro, source: str, id: str, seen: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The row a source holds for a record, by its source and id, the first that names it, or none. `seen` keeps
    each source's held rows by id once read, so a list that looks up many records reads each source's once."""
    if not source or not id:
        return {}
    if source not in seen:
        seen[source] = {}
        for row in workspace.store.state(f"held:{source}") or []:
            seen[source].setdefault(row.get("id"), row)
    return seen[source].get(id) or {}


def _held_name(workspace: Cerebro, source: str, id: str, seen: dict[str, dict[str, Any]]) -> str:
    """The name a held record goes by, by its source and id, as its held line names it, or ""."""
    if row := _held_row(workspace, source, id, seen):
        record = record_from(row)
        return " ".join(_name(record.keys, record.fields).split())
    return ""


def _held_keys(workspace: Cerebro, source: str, id: str, seen: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """The keys of a record a source holds, by its source and id, or none."""
    row = _held_row(workspace, source, id, seen)
    return {str(k): [str(v) for v in vs] for k, vs in (row.get("keys") or {}).items()}


def _bound_alone(store: Store, rows: list[Row], held: list[Record]) -> set[tuple[str, str]]:
    """Give each proposed row, as `binds`, the held records its admit binds if it lands alone, and return all they
    bind. Each is counted alone, since what lands first is the person's to choose, so two admits may count one record."""
    near: dict[str, set[int]] = {}  # each value a record is named by, and each part of it between commas
    for n, record in enumerate(held):
        for value in (str(v) for vs in naming_keys(record).values() for v in vs):
            for word in {value, *value.split(",")}:
                near.setdefault(word.strip().lower(), set()).add(n)
    for row in rows:
        found = set().union(near.get(row.name.strip().lower(), set()),
                            *(near.get(str(v).strip().lower(), set()) for vs in row.keys.values() for v in vs))
        row.binds = [(held[n].source, held[n].id) for n in sorted(found)
                     if would_bind(store, row.keys, row.name, held[n])]
    return {bound for row in rows for bound in row.binds}


# -- the plain list ---------------------------------------------------------------------------------------------------


def _show(text: str, cap: int) -> str:
    return shown(" ".join(str(text).split()), cap)


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def lines(pile: Pile) -> list[str]:
    """The list as plain lines, for --yes, --plain and a terminal that cannot take raw keys: the tree written plain,
    which `first_run_list.plain` writes; it loads no widget."""
    from .first_run_list import plain

    return plain(pile)


# -- each curator's last line: what it left out, counted by reason, with no address in it ------------------------


SAID_CAP = 2 * NOTE_CAP  # characters of a curator's own line the list shows


def said_by(pile: Pile) -> dict[str, str]:
    """Each curator's own last line, which counts what it left out by reason, by the curator's id, shown
    under its collection: a line another curator said already is not shown again, and each is cut to
    `SAID_CAP`."""
    out: dict[str, str] = {}
    for listed in pile.lists:
        if listed.said and listed.said not in out.values():
            out[listed.agent] = listed.said
    return {agent: _show(said, SAID_CAP) for agent, said in out.items()}


def said_lines(pile: Pile) -> list[str]:
    """Each curator's own line, once, named by its collection, as a first run that proposes nothing prints them."""
    said = said_by(pile)
    return [f"{listed.collection}: {said[listed.agent]}" for listed in pile.lists if listed.agent in said]


def counted_left_out(rows: list[Row]) -> str:
    """The line that counts left-out rows by their reason, the commonest first and ties in the order they came, or ""
    with none."""
    counts = Counter((row.unweighed, row.reason) for row in rows)  # Cerebro's own rows apart from a curator's words
    reasons = sorted(counts, key=lambda reason: -counts[reason])  # stable: ties keep the order they came in
    return without_domains(f"Left out {len(rows)}: " + ", ".join(f"{counts[r]} {r[1]}" for r in reasons)) \
        if rows else ""


_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
_DOMAIN = re.compile(r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b"
                     r"|\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}\b")
_EMPTY_BRACKETS = re.compile(r"\s*[(\[](?:[\s,;/&]|\band\b|\bor\b)*[)\]]")
_SPACE_BEFORE = re.compile(r"\s+([,;:.)])")
_REPEATED = re.compile(r"([,;])(?:\s*[,;])+")
_WRITTEN = (".md", ".yaml", ".yml", ".jsonl", ".json")  # the files a workspace writes


def left_out_line(run: Run | None) -> str:
    """A curator run's own last line, the one its prompt asks to count what it left out by reason: the last line
    holding a word of what its last turn wrote, its spaces folded, with every domain and address taken out:
    none when that turn wrote no text."""
    if run is None:
        return ""
    turn: list[str] = []  # the last turn: what the model said after the last tool reply, never an earlier remark
    for entry in reversed(run.transcript):
        if entry.role not in ("model", "call"):
            break
        if entry.role == "model":
            turn.insert(0, entry.text)
    text = run.result.text if run.result is not None and run.result.text.strip() else "\n".join(turn)
    last = next((" ".join(line.split()) for line in reversed(text.splitlines()) if line.strip()), "")
    return without_domains(last)


def _file_name(line: str, start: int, end: int) -> bool:
    """Whether the domain-like match at `start:end` is a file name at the end of a path, which stays: it follows a
    path's slash, not a scheme's, holds no @, ends its segment, and ends in an extension a workspace writes after a
    stem that is no domain itself."""
    match = line[start:end].lower()
    if start == 0 or line[start - 1] != "/" or (start > 1 and line[start - 2] == "/") or "@" in match:
        return False
    if end < len(line) and (line[end] in "/-_" or line[end].isascii() and line[end].isalnum()):
        return False
    for ext in _WRITTEN:
        if match.endswith(ext):
            stem = match[: -len(ext)]
            found = _DOMAIN.search(stem)
            return stem != "" and (found.group(0) if found else "") != stem
    return False


def without_domains(line: str) -> str:
    """`line` with every web address, domain and email address taken out, with the brackets, separators and slashes
    that held only them: a slash between two of them goes, and one taken out
    of a path leaves one slash in its place. A file name at the end of a path stays."""
    removed = False  # only the address goes: a bracket goes once it holds separators alone
    if _URL.search(line):
        line, removed = _URL.sub("", line), True
    out, last, cut = "", 0, 0
    for m in _DOMAIN.finditer(line):
        if _file_name(line, m.start(), m.end()):
            continue
        between = line[last:m.start()]
        if cut > 0 and "/" in between and between.strip(" /") == "":
            between = " "
        out += between
        last, removed = m.end(), True
        cut += 1
        if out.endswith("/") and last < len(line) and line[last] == "/":
            last += 1
    if not removed:
        return line
    line = out + line[last:]
    line = _EMPTY_BRACKETS.sub("", line)
    line = _REPEATED.sub(r"\1", line)
    line = _SPACE_BEFORE.sub(r"\1", line)
    return " ".join(line.split()).strip(" ,;")


# -- the session ------------------------------------------------------------------------------------------------------


def _state(workspace: Cerebro) -> dict[str, Any] | None:
    session = workspace.store.state(KEY)
    return session if isinstance(session, dict) else None


def _save(workspace: Cerebro, session: dict[str, Any]) -> None:
    workspace.store.set_state(KEY, session)


def _curators(workspace: Cerebro) -> list[Agent]:
    return curator_order([a for a in workspace.agents if a.role == "curator"])


def in_first_run(workspace: Cerebro) -> bool:
    """Whether the workspace is in its first run: its session says so, or it has none, a curator, and no article in
    a collection a curator keeps that a curator or a source admitted. A curator's run ends nothing, so a first run
    that failed and kept no session is a first run still. A workspace a run went past (`passed_first_run`) is never
    put back into a whole first run: a curator still in its own first run is late, and only its own admits wait."""
    session = _state(workspace)
    if session is not None:
        return session.get("status") != "done"
    if passed_first_run(workspace.store):
        return False
    curators = _curators(workspace)
    if not curators:
        return False
    held = set(workspace.store.collections())
    ids = {a.id for a in curators}
    return not any(a.target in held and any(_curated(article, ids) for article in workspace.store.query(a.target))
                   for a in curators)


def _curated(article: Any, curators: set[str]) -> bool:
    """Whether an article came by a curator's admit, or a source's: one a person admitted by hand does not end a
    first run, since a person decided it, and neither does one a writer's run made for the article it writes, since
    nobody decided which articles exist."""
    by = article.identity.admitted_by
    if by is None:
        return True
    return by.kind != "person" and not (by.kind == "agent" and by.name not in curators)


def _begin(workspace: Cerebro) -> dict[str, Any] | None:
    """The first run's session, begun when the workspace is in its first run and has none yet, or None."""
    session = _state(workspace)
    if session is not None and session.get("status") != DECLINED:
        return session if session.get("status") != "done" else None
    if session is None and not in_first_run(workspace):
        return None
    session = {"status": "discovering", "at": stamp(workspace.clock()), "read": False, "todo": None,
               "runs": [], "lists": {}, "unfinished": [], "raised": 0.0, "stopped": "", "cut": []}
    _save(workspace, session)
    return session


def _cap(workspace: Cerebro, session: dict[str, Any]) -> float:
    return first_run_cap(workspace.settings) + float(session.get("raised") or 0.0)


def _spent(workspace: Cerebro, session: dict[str, Any]) -> float:
    return spent_by_runs(workspace.store, list(session.get("runs") or []))


def _stop_line(collections: list[str]) -> str:
    return (f"Discovery stopped at its budget; {_and(collections)} did not finish: raise budget.first_run_usd in "
            "settings.yaml, or narrow what your source files under sources/ read, and run again.")


def stamp_shown(workspace: Cerebro) -> None:
    """Keep when the list was last given out, for `review --accept-from`."""
    session = _state(workspace)
    if session is not None:
        session["shown"] = stamp(workspace.clock())
        _save(workspace, session)


def shown_at(workspace: Cerebro, agent: str) -> str:
    """When the list a curator's waiting admits are on was last shown, while that curator still proposes, so
    `review --accept-from` accepts what the list showed and not what came after; "" when no such list is at hand."""
    session = _state(workspace)
    if session is None or not proposing(workspace.store, agent):
        return ""
    return str(session.get("shown") or "")


def _decided_since(workspace: Cerebro, since: str) -> tuple[bool, list[str]]:
    """What a person decided of this first run's list since its session began at `since`: whether they accepted any
    admit, and the curators whose admits they declined. An
    admit accepted before is no approval of this list, and only an admit a curator proposed is of the list,
    so one a person admitted by hand while the list waits approves nothing of it."""
    accepted, declined = False, []
    curators = {agent.id for agent in _curators(workspace)}
    for p in proposals(workspace.store):
        if p.kind != "article.admit" or p.decided_by is None or p.decided_by.kind != "person" \
                or (p.decided_at or "") < since or p.by.kind != "agent" or p.by.name not in curators:
            continue
        if p.status == "accepted":
            accepted = True
        elif p.status == "rejected":
            declined.append(p.by.name)
    return accepted, declined


def _declined_whole(workspace: Cerebro) -> None:
    """End a first run whose list a person declined whole: nothing is built,
    and the next run begins a new one and asks the curators again. What was declined stays rejected, so a curator's
    admit of it again is marked as asked again."""
    _save(workspace, {"status": DECLINED, "at": stamp(workspace.clock())})
    workspace.store.set_state(FAILED, None)  # a first run that ran to a list ends the failures in a row


def _done(workspace: Cerebro) -> None:
    session = _state(workspace)
    if session is not None and session.get("status") != "done":
        session["cut"] = sorted(set(session.get("cut") or []) | set(session.get("unfinished") or []))
        session.update(status="done", decided=stamp(workspace.clock()))
        _save(workspace, session)
        workspace.store.set_state(FAILED, None)


# -- discover ---------------------------------------------------------------------------------------------------------


def discover(workspace: Cerebro, *, say: Callable[[str], None] | None = None,
             ask_more: Callable[[float, float], bool] | None = None) -> Pile | None:
    """Discover, or carry on where an earlier run of this first run stopped, and give the pile; None when the
    workspace is not in its first run. A pile already discovered comes back with no model call. A cap reached stops
    discovery: `ask_more(spent, more)` is asked, at a terminal, whether to continue for about `more`; without it,
    or on its no, the pile is kept as found, with the line that says what to raise."""
    session = _begin(workspace)
    if session is None:
        return None
    tell = say or (lambda line: None)
    if session["status"] == "stopped":
        if _spent(workspace, session) < _cap(workspace, session):  # the cap was raised: ask the curators it stopped, first
            session.update(status="discovering", todo=list(session["unfinished"]), stopped="")
        elif ask_more is not None and ask_more(_spent(workspace, session), more := _more(workspace, session["unfinished"])):
            _raise(workspace, session, more)
            session.update(status="discovering", todo=list(session["unfinished"]), stopped="")
        _save(workspace, session)
    if session["status"] == "discovering":
        _discover(workspace, session, tell, ask_more)
    return LocalApproval(workspace).pile()


def _raise(workspace: Cerebro, session: dict[str, Any], more: float) -> None:
    """Raise this first run's cap to what discovery has spent and `more` on top. A run's spend lands whole when it
    ends, so discovery can stand past its cap by what its last run spent."""
    session["raised"] = _spent(workspace, session) + more - first_run_cap(workspace.settings)


def _more(workspace: Cerebro, agents: list[str]) -> float:
    """About what the curators `agents` would spend on one more run each, or the cap itself when that is unpriced."""
    found = {a.id: a for a in workspace.agents}
    priced = Priced()
    for id in agents:
        if id in found:
            priced = priced + run_estimate(workspace.store, id, workspace.model_of(found[id]), _own_budget(found[id]))
    return priced.usd if priced.usd and not priced.unpriced else first_run_cap(workspace.settings)


def _own_budget(agent: Agent) -> int | None:
    """The tokens per run an agent's file sets, or None when it sets none."""
    return agent.budget.tokens_per_run if agent.budget.tokens_per_run != Budget().tokens_per_run else None


def _discover(workspace: Cerebro, session: dict[str, Any], tell: Callable[[str], None],
              ask_more: Callable[[float, float], bool] | None) -> None:
    if not session.get("read"):
        tell("stage     discover: reading the sources, then each curator in proposal mode; nothing is written or "
             "compiled until you decide")
        _emit(workspace, "read", "start")
        result = PassResult(on=_heard(workspace))
        count = hold_sources(workspace, result)
        _emit(workspace, "read", "end", total=count)
        for name, listed in result.listed.items():
            tell(f"listed    {name}: {_n(listed, 'record')}")
        for name, why in result.failed.items():
            tell(workspace.mask_every()(f"failed    {name}: {why}"))
        tell(f"read      {_n(count, 'record')} from {_n(len(result.listed), 'source')}, held until you decide")
        session["read"] = True
        _save(workspace, session)
    # Sorted by id, never by what one curator reads of another: every curator reads the queue as it stood when
    # discovery began, so none weighs what another proposed beside it, and they are asked side by side, as many at
    # once as compile's concurrency allows; what each did is said and kept in this order, each once those before it
    # are done, whichever finished first.
    curators = {a.id: a for a in sorted(_curators(workspace), key=lambda a: a.id)}
    width = max(1, int((workspace.settings.get("compile") or {}).get("concurrency", 8)))
    todo = session.get("todo")
    if todo is None:
        todo = session["todo"] = list(curators)
        session["pinned"] = sorted(p.id for p in proposals(workspace.store, "waiting"))
        _save(workspace, session)  # the pin is read from the store by each curator's run
    # a curator with nothing to weigh is not asked: no model call and no line, and its collection stays in its
    # first run, so a later run asks it once it has something
    unasked = [id for id in todo if id in curators and workspace.weighs_nothing(curators[id])]
    for id in unasked:
        session.setdefault("lists", {}).setdefault(id, {"state": "empty", "note": ""})
    todo[:] = [id for id in todo if id in curators and id not in unasked]
    _emit(workspace, "discover", "open", total=len(todo))  # discovery's line and a queued step for each curator
    for id in todo:
        _emit(workspace, "discover", "queue", id, text=curators[id].target)
    with ThreadPoolExecutor(max_workers=width) as pool:
        if _round(workspace, session, todo, curators, width, pool, tell, ask_more):
            return
    session.update(status="waiting", todo=None, pinned=None)
    _save(workspace, session)


class RoundStopped(FirstRunError):
    """A first-run round stopped by what every curator of it would meet alike: the
    day's budget paused, or a provider that refuses the key. The round does not count: its curators that did not
    finish are asked again on the next run."""


def _round(workspace: Cerebro, session: dict[str, Any], todo: list[str], curators: dict[str, Agent],
           width: int, pool: ThreadPoolExecutor, tell: Callable[[str], None],
           ask_more: Callable[[float, float], bool] | None) -> bool:
    """Ask the curators of `todo` side by side: each starts in the round's order once a slot is free, and lands, said
    and kept, once those before it have, whichever finished first. Every model call is metered against the first
    run's cap as each reply comes, so once it is reached the runs under way are cut at their next call and no curator
    starts; a curator it cut stays in `todo`, asked again first. A paused budget or a provider's refusal stops the
    round as `RoundStopped`, and an interrupt goes on up: the curators that did not finish stay in `todo`, and the
    round does not count. A curator held for room the runs under way hold is no failure: it is asked again once they
    land, and if nothing else runs the round stops as `RoundStopped`. True when the cap stopped discovery."""
    ended: list[str] = []  # the runs of the round that ended, whose spend the ledger has
    meter = Meter(_cap(workspace, session), lambda: spent_by_runs(workspace.store, list(dict.fromkeys(
        [*session.get("runs", []), *ended]))))
    said: set[str] = set()  # the curators whose start was said, so one asked again for room is not said twice
    while True:
        order = list(todo)
        started: list[Future[_Turn]] = []
        landed, stop = 0, ""
        waits: dict[str, str] = {}  # the curators held for room, each with why
        try:
            while landed < len(order):
                while landed < len(started) and started[landed].done():
                    turn = started[landed].result()
                    landed += 1
                    if turn.capped or turn.stops:  # what it spent counts toward the first run, cut or stopped
                        session["runs"] = [*session.get("runs", []), *(r.id for r in turn.result.runs if r.id)]
                    if turn.capped:
                        _emit(workspace, "discover", "end", turn.agent.id, mark="budget", text=turn.agent.target)
                        continue  # cut at the cap: no failure of its own, and asked again first
                    if turn.stops:
                        _emit(workspace, "discover", "end", turn.agent.id, mark="failed", why=turn.stops,
                              text=f"Discovery for {turn.agent.target} collection did not finish")
                        stop = stop or turn.stops
                        meter.stop(f"the round stopped: {turn.stops}")
                        continue
                    if turn.held:
                        waits[turn.agent.id] = turn.held
                        continue
                    _landed(workspace, session, turn, tell)
                    todo.remove(turn.agent.id)
                    _save(workspace, session)
                if landed == len(order):
                    break
                running = [f for f in started if not f.done()]
                if len(started) < len(order) and len(running) < width and not stop and not meter.reached():
                    agent = curators[order[len(started)]]
                    _emit(workspace, "discover", "start", agent.id, text=agent.target)
                    if agent.id not in said:
                        tell(f"stage     discover: reviewing sources for {agent.target} collection")
                        said.add(agent.id)
                    started.append(pool.submit(_turn, workspace, agent, meter, ended))
                    continue
                if not running:
                    break  # nothing runs, and none may start: the cap, or a stop
                wait(running, return_when=FIRST_COMPLETED)
        except BaseException:
            meter.stop("the run was interrupted")  # what runs is cut at its next call, and keeps what it spent
            wait(started)
            # every asked curator's spend counts toward the first run, an interrupted one's too
            session["runs"] = list(dict.fromkeys([*session.get("runs", []), *ended]))
            _save(workspace, session)
            raise
        if stop:
            _save(workspace, session)
            raise RoundStopped(workspace.mask(f"discovery stopped: {stop}: the curators that did not finish are asked "
                                          "again on the next run"))
        if not todo:
            return False
        if waits and not meter.reached():
            if len(waits) < len(order):
                continue  # those that held the room have landed: ask the held ones again
            _save(workspace, session)
            raise RoundStopped(workspace.mask(f"discovery waits: {next(iter(waits.values()))}: nothing failed, and "
                                              "the curators that did not run are asked again on the next run"))
        more = _more(workspace, todo)  # the cap cut the round
        if ask_more is not None and ask_more(_spent(workspace, session), more):
            _raise(workspace, session, more)
            meter.cap = _cap(workspace, session)
            _save(workspace, session)
            continue
        _stopped(workspace, session, todo, curators, tell)
        return True


def _pending(workspace: Cerebro) -> int:
    """How many proposals wait, for a person or a curator."""
    return len(proposals(workspace.store, "waiting"))


@dataclass
class _Turn:
    """One curator's turn in a round: what it had filed and weighed before, and what its run did."""

    agent: Agent
    before: set[str]
    weighed: int
    result: PassResult
    capped: bool = False  # the round's cap cut its run
    stops: str = ""  # what stopped its run that would meet every curator of the round
    held: str = ""  # why it did not start: runs under way hold the room the day's cap has left


def _turn(workspace: Cerebro, agent: Agent, meter: Meter, ended: list[str]) -> _Turn:
    """Ask one curator, in proposal mode, beside the others of its round, under the round's meter: it changes nothing
    of the session, which `_landed` keeps in the round's order."""
    before = set(first_list(workspace, agent.id))
    weighed = len(workspace.store.rows(LEFT_OUT, {"agent": agent.id}))
    result = PassResult()
    turn = meter.turn(agent.id)
    held = METER.set(turn)
    try:
        run_curators(workspace, [agent], result)
    finally:
        METER.reset(held)
        ended += [run.id for run in result.runs if run.id]
        turn.close()
    failed = result.failed.get(agent.id, "")
    paused = result.held.get(agent.id, "") if agent.id in result.waiting_runs else ""  # a daily cap: a pass waits
    # what stops the round meets every curator of it: the day's spent budget or a
    # provider's refusal (an interrupt goes on up). Room the runs under way hold is no stop and no failure: it waits
    stops = (failed if failed.startswith(f"{BudgetSpent.__name__}:") else "") or (
        f"{BudgetSpent.__name__}: {paused}" if paused else "") or next(
        (f"{run.note or 'the provider refused the key'}" for run in result.runs if run.status == "refused"), "")
    return _Turn(agent, before, weighed, result, capped=turn.cut, stops=stops,
                 held="" if paused else result.held.get(agent.id, ""))


def _failure(result: PassResult, agent: str) -> str:
    """Why a curator's run in `result` failed, or "": what it raised, or the note of a run the runtime gave back as
    failed, as it gives back a server error, a timeout or a dropped connection. A curator's run
    error is its failure however it is reported."""
    return result.failed.get(agent) or next(
        (run.note or "its run failed" for run in result.runs if run.agent == agent and run.status == "failed"), "")


def _landed(workspace: Cerebro, session: dict[str, Any], turn: _Turn, tell: Callable[[str], None]) -> None:
    """Say what one curator's turn did, and keep what became of it."""
    agent, result, weighed, before = turn.agent, turn.result, turn.weighed, turn.before
    session["runs"] = [*session.get("runs", []), *(run.id for run in result.runs if run.id)]
    new = [id for id in first_list(workspace, agent.id) if id not in before]
    session["order"] = [*(session.get("order") or []), *new]  # the curators' order, whichever finished first
    filed = len(new)
    left = len(workspace.store.rows(LEFT_OUT, {"agent": agent.id})) - weighed  # it weighed someone: it read something
    state = session.setdefault("lists", {}).setdefault(agent.id, {"state": "nothing", "note": ""})
    state["passes"] = int(state.get("passes") or 0) + 1
    _keep_said(workspace, state, agent.id, result)
    what = _into(workspace, new, agent.target) if filed > 0 else f"nothing to {agent.target} collection"
    said = f"proposed  {what}" + (f" and left out {left}" if left > 0 else "") + f", by agents/{agent.id}.md"
    if why := _failure(result, agent.id):
        note = workspace.mask_every()(f"discovery for {agent.target} collection did not finish: {why}")
        state.update(state="failed", note=note)
        tell(f"failed    {agent.id}: {note}")
        _emit(workspace, "discover", "end", agent.id, mark="failed",
              text=f"Discovery for {agent.target} collection did not finish", why=why)
    elif filed > 0 or state["state"] == "proposed":
        state.update(state="proposed", note="")
        tell(said)
        _emit(workspace, "discover", "end", agent.id, mark="done" if filed > 0 else "nothing", text=what)
    else:
        _emit(workspace, "discover", "end", agent.id, mark="nothing")  # it ran and proposed none: it says why in its own words, its last line, when it gave one
        empty = agent.id in result.empty and left <= 0  # it read nothing yet: it runs once more after the build
        state.update(state="empty" if empty else "nothing", note="")
        own = str(state.get("said") or "")
        tell(f"{said}: {_show(own, NOTE_CAP)}" if own else said)


def _keep_said(workspace: Cerebro, state: dict[str, Any], agent: str, result: PassResult, key: str = "said") -> None:
    """Keep the curator's own last line from its run in `result`, masked, under `key`, so a list shown on a later
    run shows it too; a run that said none leaves the line it said before. A run that failed, or that a cap cut or
    ran out of budget, gives none."""
    if said := next((line for run in reversed(result.runs) if run.agent == agent and run.status == "done"
                     if (line := left_out_line(run))), ""):
        state[key] = workspace.mask(said)


def _into(workspace: Cerebro, ids: list[str], home: str) -> str:
    """How many admits went to each collection, the curator's own first: 2 articles to parts collection and 1 to
    things collection."""
    found = {p.id: p for p in proposals(workspace.store, "waiting")}
    count: dict[str, int] = {}
    for id in ids:
        p = found.get(id)
        where = str((p.payload or {}).get("collection") or p.target.rpartition("/")[0]) if p is not None else ""
        count[where or home] = count.get(where or home, 0) + 1
    order = sorted(count, key=lambda c: (c != home, c))
    if not order:
        return f"0 articles to {home} collection"
    first, *rest = order
    return " and ".join([f"{_n(count[first], 'article')} to {first} collection",
                         *(f"{count[c]} to {c} collection" for c in rest)])


def _stopped(workspace: Cerebro, session: dict[str, Any], todo: list[str], curators: dict[str, Agent],
             tell: Callable[[str], None]) -> None:
    """Stop discovery at the cap: the curators not asked yet did not finish, and the pile is kept as found."""
    for id in todo:
        session.setdefault("lists", {})[id] = {"state": "budget", "note": "raise budget.first_run_usd in settings.yaml "
                                                                          "and run again"}
        tell(f"stopped   {id}: budget reached for {curators[id].target} collection")
        _emit(workspace, "discover", "end", id, mark="budget", text=curators[id].target)
    session.update(status="stopped", unfinished=list(todo),
                   stopped=_stop_line([curators[id].target for id in todo]))
    _save(workspace, session)


# -- the run: the decision, the build and the late list ---------------------------------------------------------------


@dataclass
class Outcome:
    """What a first run did: the pass that built what was approved, when one ran; the pile, and whether it waits, the
    person quit or it was applied; the late list; and the lines to end with."""

    pile: Pile | None = None
    result: PassResult | None = None
    waits: bool = False
    quit: bool = False
    applied: list[Applied] = field(default_factory=list)
    late: Pile | None = None
    lines: list[str] = field(default_factory=list)
    next: str = ""  # what to run next, when the run ends with nothing built: the hand admit
    failed: list[str] = field(default_factory=list)  # each curator's failure, when the run failed for them
    run: str = ""  # the command a failure names, or "" when none names one
    after: list[str] = field(default_factory=list)  # said after the run's summary: how many rows of the list wait


def run(workspace: Cerebro, *, choose: Callable[[Pile], Choices | None] | None,
        say: Callable[[str], None] | None = None, ask_more: Callable[[float, float], bool] | None = None,
        approval: Approval | None = None, on: Callable[[Progress], None] | None = None) -> Outcome | None:
    """A first run, from wherever it stands, or past it a curator's first list that waits, shown before the pass as
    `_late_waits` says, or None when there is neither. `choose` is the screen: it
    is shown the pile and gives the person's choices, or None to quit and record nothing. With no `choose`, as off a
    terminal, nothing is decided and the list waits, with the line that says how to decide it. Once nothing of the
    list waits, what was approved is built, then any late list is shown to `choose` the same way, and what it
    approves is built in one more pass. `on` is handed each event of it as it happens, its words masked: the sources
    read, each curator's start and end, the approval, and the build's pass with the late curators' runs."""
    hooked = _ON.set(on)
    try:
        return _run(workspace, choose, say, ask_more, approval)
    finally:
        _ON.reset(hooked)


def _run(workspace: Cerebro, choose: Callable[[Pile], Choices | None] | None, say: Callable[[str], None] | None,
         ask_more: Callable[[float, float], bool] | None, approval: Approval | None) -> Outcome | None:
    pile = discover(workspace, say=say, ask_more=ask_more)
    if pile is None:
        return _late_waits(workspace, choose, say)
    since = str((_state(workspace) or {}).get("at") or "")
    seam = approval or LocalApproval(workspace)
    out = Outcome(pile)
    rest: list[str] = []
    if pile.waiting() or pile.stopped:
        stamp_shown(workspace)
        if choose is None:  # off a terminal: nothing is decided, and a stop at the cap is said first
            said = [pile.stopped] if pile.stopped else []
            return replace(out, waits=True, lines=[*said, *held_lines(workspace), waiting_line(pile)]
                           if pile.waiting() else said)
        choices = choose(arranged(workspace, pile))
        if choices is None:
            return replace(out, quit=True, lines=[*quit_lines(workspace, pile), QUIT_NEXT])
        out.applied.append(_apply(workspace, seam, pile, choices))
        pile = seam.pile()
        if pile.waiting() and not out.applied[-1].accepted:  # what was selected is built; the rest waits for a person
            rest = [row.id for row in pile.rows() if not row.left_out]
            return replace(out, waits=True, lines=[*held_lines(workspace), waiting_line(pile, chosen=True)],
                           after=rest_lines(workspace, rest, more=False))
        rest = [row.id for row in pile.rows() if not row.left_out]
    accepted, declined = _decided_since(workspace, since)
    failures = _failures(workspace)
    if not accepted and not any(applied.accepted for applied in out.applied):
        shown = [row.agent for row in (out.pile.rows() if out.pile is not None else []) if not row.left_out]
        if failures and not shown and not declined:  # nothing to decide, and a curator
            # that could propose failed, so the failure is the run's: nothing is kept, built or written, and the next
            # run asks the curators again
            workspace.store.set_state(KEY, None)  # no session: none is kept as decided
            from .output import command_in  # status and mcp load no output module

            return replace(out, failed=failures, run=command_in(" ".join(failures)))
        form = admit_form(workspace, [*declined, *shown])
        capped = any(listed.state == "budget" for listed in pile.lists)
        if declined or shown or capped:  # nothing waits and nothing of this list was
            # approved, however it was declined, at the list, through review or decide, or cut at the cap, so
            # nothing is built and the next run starts afresh
            _declined_whole(workspace)
            said = "Nothing approved" if declined or shown else "Nothing to propose"
            return replace(out, lines=[*out.lines, f"{said}: the wiki starts empty; add with {form}.",
                                       *held_lines(workspace)], next=form)
        out.lines.append(f"Nothing to propose: the wiki starts empty; add with {form}.")
        out.lines += [f"  {line}" for line in said_lines(pile)]
    out.lines += failures  # a curator that failed beside a list that went on: each a line to check
    lates: list[tuple[Pile | None, list[str]]] = []
    out.result = _build(workspace, say, lambda: lates.append(late(workspace, say=say, ask_more=ask_more)))
    out.late, cut = lates[0]
    out.lines += cut
    if out.late is not None:
        _decide_late(workspace, out, out.late, out.result, choose, say)
        rest += [row.id for row in out.late.rows() if not row.left_out and row.id not in rest]  # it waits too
    out.result.older &= {f"{name}/{row['id']}" for name in workspace.connectors  # a late build reads some held before
                         for row in workspace.store.state(f"held:{name}") or []}
    if (older := len(out.result.older)) and out.result.compiled:
        out.result.notices.append(f"The wiki starts from the last {RECENT_DAYS} days. cerebro serve reads "
                                  f"{_n(older, 'older record')} on its next {'pass' if older == 1 else 'passes'} "
                                  f"and rewrites the articles {'it changes' if older == 1 else 'they change'}.")
    out.lines += held_lines(workspace)
    out.after = rest_lines(workspace, rest)
    return out


def _apply(workspace: Cerebro, seam: Approval, pile: Pile, choices: Choices) -> Applied:
    """Apply a person's choices on the step that records them."""
    _emit(workspace, "approve", "start", total=len(choices.accept))
    applied = seam.apply(choices)
    _emit(workspace, "approve", "end", **_admitted(pile, choices, applied))
    return applied


def _admitted(pile: Pile, choices: Choices, applied: Applied) -> dict[str, Any]:
    """The end of an approval's step: over one collection the articles it accepted there, one for each line ticked,
    which its curator makes as the build runs, over several the sum with a line for each collection in name order; no
    mark when it accepted nothing."""
    of = {row.id: row.collection for row in pile.rows()}
    count = Counter(next((of[id] for id in ids if id in of), "") for ids in choices.accept
                    if any(id in applied.accepted for id in ids))
    if not count:
        return {}
    if len(count) == 1:
        (coll, n), = count.items()
        return {"mark": "done", "text": f"Accepted {_n(n, 'article')} for {coll} collection: its curator makes "
                                        f"{'it' if n == 1 else 'them'} as the build runs"}
    return {"mark": "done", "text": f"Accepted {_n(sum(count.values()), 'article')} for {len(count)} collections: "
                                    "their curators make them as the build runs",
            "why": "\n".join(f"{coll}: {_n(count[coll], 'article')}" for coll in sorted(count))}


def _build(workspace: Cerebro, say: Callable[[str], None] | None, then: Callable[[], Any] | None = None,
           only: list[str] | None = None) -> PassResult:
    """Build, and run `then` after it, inside the build's progress."""
    _emit(workspace, "build", "start")
    result: PassResult | None = None
    try:
        result = build(workspace, say=say, only=only)
        if then is not None:
            then()
        return result
    finally:  # what this build warned of, for its own rows, before a later build's region takes the screen
        said = [] if result is None else [*result.warnings, *(f"{k}: {v}" for k, v in result.ran.items()),
                                          *(f"{k}: {v}" for k, v in result.failed.items())]
        _emit(workspace, "build", "end", why="\n".join(said))


def build(workspace: Cerebro, *, say: Callable[[str], None] | None = None, only: list[str] | None = None) -> PassResult:
    """Build what the person approved, once nothing of the list waits: each curator makes the rows of its collection
    that were accepted, in as many runs as it takes while each carries out more, then a pass reads the sources for
    the articles and compiles them, with no curator. The curators and the pass read the records of the last
    `RECENT_DAYS` days, as discovery did, and a later pass the older ones. With `only`, the ids of the proposals a
    late list accepted, the pass compiles only the articles those made, so no article of the first build is built
    twice."""
    _done(workspace)
    told = None if say is None else (lambda line: say(workspace.mask_every()(line)))
    made = PassResult(say=told, on=_heard(workspace))
    left = {agent.id: len(yes_left(workspace.store, agent.target)) for agent in _curators(workspace)}
    building = IN_FIRST_BUILD.set(True)  # its curators read held lists as discovery did, so no record a person never
    try:  # saw is admitted under a curator's own authority before a later pass
        while due := [agent for agent in _curators(workspace) if left.get(agent.id)]:
            run_curators(workspace, due, made)
            now = {agent.id: len(yes_left(workspace.store, agent.target)) for agent in due}
            left = {id: n for id, n in now.items() if n < left[id]}  # a curator that carried out nothing more stops
        made_by = None if only is None else {str(p.applied["article"]) for p in proposals(workspace.store, "accepted")
                                             if p.id in only and p.applied.get("article")}
        result = run_pass(workspace, say=told, curators=False, on=_heard(workspace), articles=made_by)
    finally:
        IN_FIRST_BUILD.reset(building)
    _merge(made, result)
    return _masked(workspace, made)


def _masked(workspace: Cerebro, result: PassResult) -> PassResult:
    result.failed = {k: workspace.mask_every()(v) for k, v in result.failed.items()}
    result.ran = {k: workspace.mask_every()(v) for k, v in result.ran.items()}
    result.skipped = {k: workspace.mask(v) for k, v in result.skipped.items()}
    result.refused = [workspace.mask_every()(line) for line in result.refused]
    result.notices = [workspace.mask_every()(line) for line in result.notices]
    result.warnings = [workspace.mask_every()(line) for line in result.warnings]
    return result


def _merge(into: PassResult, more: PassResult) -> None:
    """Add a second build's pass to the first's, every field of it: counts add up, rows and lines join, and an agent
    due in either pass is not said to be skipped. A run that ran again in the second pass is told as that pass tells
    it: its first pass's wrote nothing, failure or hold goes. `token`, `say` and
    `on` are the first pass's own."""
    for name, count in more.listed.items():
        into.listed[name] = into.listed.get(name, 0) + count
    into.from_start |= more.from_start
    into.older |= more.older
    into.read += more.read
    into.extracted += more.extracted
    into.retracted += more.retracted
    into.linked += more.linked
    into.compiled += [id for id in more.compiled if id not in into.compiled]
    into.runs += more.runs
    into.due |= more.due
    into.skipped = {k: v for k, v in (into.skipped | more.skipped).items() if k not in into.due}
    again = {*more.ran, *more.failed, *more.held, *(r.agent for r in more.runs),
             *(f"{r.agent} on {r.target}" for r in more.runs)}
    for rows in (into.ran, into.failed, into.held):
        for key in [k for k in rows if k in again]:
            del rows[key]
    into.failed |= more.failed
    into.ran |= more.ran
    into.held |= more.held
    for lines, extra in ((into.refused, more.refused), (into.notices, more.notices),
                         (into.warnings, more.warnings), (into.empty, more.empty), (into.stops, more.stops),
                         (into.waiting_runs, more.waiting_runs)):
        lines += [line for line in extra if line not in lines]
    into.stopped |= more.stopped
    for name, ids in more.waiting.items():
        into.waiting[name] = list(dict.fromkeys([*into.waiting.get(name, []), *ids]))
    into.crossed = more.crossed | into.crossed  # the first refusal's crossing stands
    into.tried = more.tried | into.tried  # each retry list as it stood when the first pass began
    into.paused |= more.paused
    into.grounding |= more.grounding
    into.cleaned |= more.cleaned


def late(workspace: Cerebro, *, say: Callable[[str], None] | None = None,
         ask_more: Callable[[float, float], bool] | None = None) -> tuple[Pile | None, list[str]]:
    """Ask once more, in proposal mode, each curator that had nothing to read at discovery, as one that reads facts
    and found none before they existed, and give the late list of those that proposed something, or None, with the
    lines to end with. A curator that took part in discovery stays out: its list was decided. One that failed there
    runs again on a later pass of `serve`, and a curator the cap cut runs when a person asks for it. The late runs count toward the first run's cap, checked before each: one the cap reaches runs only when
    `ask_more` says to continue, and is otherwise cut, with the line that says how to ask it later."""
    tell = say or (lambda line: None)
    session = _state(workspace)
    if session is None:
        return None, []
    left = set(session.get("cut") or [])
    states = session.get("lists") or {}
    asked = [a for a in _curators(workspace)
             if str((states.get(a.id) or {}).get("state") or "") == "empty"
             and proposing(workspace.store, a.id) and not first_list(workspace, a.id) and a.id not in left
             and not workspace.weighs_nothing(a)]  # one with still nothing to weigh is not asked, nor named
    if not asked:
        return None, []
    names = " and ".join(f"agents/{a.id}.md" for a in asked)
    tell(f"stage     late: asking {names} again in proposal mode, now that the sources are read; what "
         f"{'it proposes is' if len(asked) == 1 else 'they propose is'} a late list a person decides")
    ran: list[Agent] = []
    cut: list[str] = []
    _emit(workspace, "agents", "open", total=len(asked))
    for agent in asked:
        if _spent(workspace, session) >= _cap(workspace, session):
            more = _more(workspace, [a.id for a in asked if a not in ran and a.id not in cut])
            if ask_more is None or not ask_more(_spent(workspace, session), more):
                cut.append(agent.id)
                tell(f"stopped   {agent.id}: budget reached for {agent.target} collection")
                continue
            _raise(workspace, session, more)
        result = PassResult(on=_heard(workspace))
        try:
            run_curators(workspace, [agent], result)
        finally:  # every run of a first run counts toward its cap
            session["runs"] = [*session.get("runs", []), *(run.id for run in result.runs if run.id)]
            _keep_said(workspace, session.setdefault("lists", {}).setdefault(agent.id, {}), agent.id, result, "late_said")
            _save(workspace, session)
        if why := _failure(result, agent.id):
            tell(workspace.mask_every()(f"failed    {agent.id}: {why}"))
        ran.append(agent)
    out = []
    if cut:
        session["cut"] = sorted(left | set(cut))
        _save(workspace, session)
        names = " and ".join(f"agents/{id}.md" for id in cut)
        out.append(f"The late round stopped at the first run's budget before {names}: cerebro agents run "
                   f"{cut[0]} asks {'it' if len(cut) == 1 else 'each'} by hand, and an ordinary pass leaves "
                   f"{'it' if len(cut) == 1 else 'them'} alone")
    pile = LocalApproval(workspace, agents=[a.id for a in ran if first_list(workspace, a.id)], late=True).pile()
    return (pile if pile.lists else None), out


def late_line(curators: int) -> str:
    """The line above a late list that opens after the build, for one curator or more."""
    if curators == 1:
        return "One more list: a curator that could not propose before the build proposes now."
    return "One more list: curators that could not propose before the build propose now."


def _decide_late(workspace: Cerebro, out: Outcome, late: Pile, result: PassResult,
                 choose: Callable[[Pile], Choices | None] | None, say: Callable[[str], None] | None) -> None:
    """Show the late list that opened after the build to `choose`, apply what it gives, and build what it admitted
    in one more pass, joined to `result`; off a terminal, say that it waits."""
    stamp_shown(workspace)
    if choose is None:
        out.lines += [first_list_line(listed.agent, len(listed.proposed())) for listed in late.lists]
        return
    tell = say or (lambda line: None)
    # said above the late list, which nothing else announces after the build
    _emit(workspace, "late", "open", total=len(late.lists))
    tell(late_line(len(late.lists)))
    choices = choose(arranged(workspace, late))
    if choices is None:  # Escape: nothing recorded, and run again next
        for line in quit_lines(workspace, late):
            tell(line)
        result.next = "cerebro serve --once"
    else:
        # decided by the curators it was asked of: a row of the first list that waits is not of it
        asked = late.asked if late.asked is not None else [listed.agent for listed in late.lists]
        applied = _apply(workspace, LocalApproval(workspace, agents=asked, late=True), late, choices)
        out.applied.append(applied)
        if applied.accepted:  # one more pass compiles what the late list admitted
            _merge(result, _build(workspace, say, only=applied.accepted))
    for listed in late.lists:
        if waiting := first_list(workspace, listed.agent):
            out.lines.append(first_list_line(listed.agent, len(waiting)))


def _late_waits(workspace: Cerebro, choose: Callable[[Pile], Choices | None] | None,
                say: Callable[[str], None] | None) -> Outcome | None:
    """Past the first run, the curators' first lists that wait, shown to `choose` as one late list before the pass: what
    it approves is decided and the pass goes on; Escape records nothing and ends the run with no pass. None when no list
    waits, with no `choose`, or off a terminal, where the pass goes on and says the list waits, so answers are never
    read from a pipe here."""
    agents = [agent.id for agent in _curators(workspace) if first_list(workspace, agent.id)]
    if choose is None or not agents or not _at_a_terminal():
        return None
    pile = LocalApproval(workspace, agents=agents, late=True).pile()
    if not pile.waiting():
        return None
    stamp_shown(workspace)
    out = Outcome(pile)
    choices = choose(arranged(workspace, pile))
    if choices is None:
        return replace(out, quit=True, lines=[*quit_lines(workspace, pile), QUIT_NEXT])
    out.applied.append(_apply(workspace, LocalApproval(workspace, agents=agents, late=True), pile, choices))
    _emit(workspace, "build", "start")
    try:
        out.result = workspace.run_once(say=say, on=_ON.get())
    finally:
        _emit(workspace, "build", "end")
    return out


def _at_a_terminal() -> bool:
    """Whether standard input is a terminal."""
    return sys.stdin.isatty()


def _failures(workspace: Cerebro) -> list[str]:
    """Each curator of this first run whose discovery did not finish, in the words `Discovery
    for <collection> collection did not finish: <why>`, masked of every value the secrets store holds now."""
    states = (_state(workspace) or {}).get("lists") or {}
    return [workspace.mask_every()(note[:1].upper() + note[1:]) for a in _curators(workspace)
            if str((state := states.get(a.id) or {}).get("state") or "") == "failed"
            and (note := str(state.get("note") or ""))]


def waiting_line(pile: Pile, chosen: bool = False) -> str:
    """The line a run ends with while its list waits: how many admits are awaiting review, and how to decide them,
    with no other way to show the list again once it was `chosen` on and nothing approved."""
    agents = list(dict.fromkeys(row.agent for listed in pile.lists for row in listed.proposed()))
    flags = " ".join(f"--accept-from agents/{agent}.md" for agent in agents)
    return (f"the first run's list: {_n(pile.waiting(), 'admit proposal')} awaiting review, and nothing is written or "
            f"compiled until they are decided: decide with cerebro review {flags}"
            + ("" if chosen else ", or run cerebro serve --once at a terminal, or with --yes"))


def rest_lines(workspace: Cerebro, rest: list[str], more: bool = True) -> list[str]:
    """What the first run says after its summary while rows of its list wait, by id in `rest`: how many, and where a
    person decides them; `more` once something was approved beside them."""
    held = {p.id for p in workspace.with_curators()}  # counted apart, by the line that says they wait there
    waiting = {p.id for p in proposals(workspace.store, "waiting") if p.id not in held}
    if not (count := sum(id in waiting for id in rest)):
        return []
    line = (f"{count}{' more' if more else ''} {'is' if count == 1 else 'are'} waiting. Run cerebro review, or ask your "
            "agent what is waiting.")
    return [line]


def quit_lines(workspace: Cerebro, pile: Pile) -> list[str]:
    """What a list left with Escape or `q` says: a blank line and that nothing was recorded, with
    the hand admit of a collection the list proposed to, then
    a blank line and how many proposals wait, with the command that reviews them."""
    waiting = _pending(workspace)
    return ["", QUIT_LINE.format(form=admit_form(workspace, proposers(pile))),
            *(["", f"{_n(waiting, 'proposal')} waiting · cerebro review"] if waiting else [])]


def proposers(pile: Pile) -> list[str]:
    """The curators that proposed the list's rows, by id, for `admit_form`. A curator that proposed nothing, or only
    left rows out, is none of them."""
    return sorted({row.agent for row in pile.rows() if not row.left_out})


def admit_form(workspace: Cerebro, by: list[str] | None = None) -> str:
    """The hand admit a first run's lines name: `cerebro admit <collection>/<name>`
    with the collection the first by id of the curators `by` keeps, those that proposed the list's rows,
    or of every curator when none proposed."""
    curators = sorted(set(by or []) or {a.id for a in workspace.agents if a.role == "curator"})
    collection = next((a.target for id in curators for a in workspace.agents if a.id == id and a.target), "<collection>")
    return f"cerebro admit {collection}/<name>"


# -- past the run: a curator run by hand, distill and serve -----------------------------------------------------------


class by_hand:
    """A curator run by hand, as `cerebro agents run` runs one, inside a first run: the first run begins first, if
    the workspace is in one, so the curator proposes and its admits are rows of the list; its run counts toward the
    cap while the first run is undecided; and a curator the cap cut is asked, since a person asked for it. Use:
    `with by_hand(workspace, agent) as count: ...; count(run)`."""

    def __init__(self, workspace: Cerebro, agent: Agent, say: Callable[[str], None] | None = None):
        self.workspace, self.agent = workspace, agent
        self.say = say or (lambda line: None)
        self.runs: list[str] = []

    def __enter__(self) -> Callable[[Any], None]:
        session = _begin(self.workspace) or _state(self.workspace)
        if session is not None and session.get("status") != "done" and not session.get("read"):
            # a first run begun by hand reads the sources first, as discovery does, so a curator that weighs held
            # records has them; discovery after it does not read them again
            result = PassResult()
            count = hold_sources(self.workspace, result)
            for name, why in result.failed.items():
                self.say(self.workspace.mask_every()(f"failed    {name}: {why}"))
            self.say(f"read      {_n(count, 'record')} from {_n(len(result.listed), 'source')}, held until you "
                     "decide the first run's list")
            session["read"] = True
            _save(self.workspace, session)
        if session is not None and self.agent.id in (session.get("cut") or []):
            session["cut"] = [id for id in session["cut"] if id != self.agent.id]
            _save(self.workspace, session)
        return lambda run: self.runs.append(run.id) if run is not None and run.id else None

    def __exit__(self, *_: object) -> None:
        session = _state(self.workspace)
        if session is not None and session.get("status") != "done" and self.runs:
            session["runs"] = [*session.get("runs", []), *self.runs]
            _save(self.workspace, session)


def distill_line(workspace: Cerebro) -> str:
    """The line `distill` ends with while the workspace's first run waits, or "": that a first run is waiting, and
    how many records the sources hold for it, which no distill extracts until its list is decided."""
    if not in_first_run(workspace):
        return ""
    held = sum(len(workspace.store.state(f"held:{name}") or []) for name in sorted(workspace.connectors))
    return (f"a first run is waiting, and {_n(held, 'record')} {'is' if held == 1 else 'are'} held for it, extracted "
            "only once its list is decided: run cerebro serve --once at a terminal, or decide it with cerebro review")


def served(workspace: Cerebro, *, say: Callable[[str], None] | None = None) -> PassResult | None:
    """A pass of `serve` while the workspace is in its first run, or None when it is not: it discovers, approves
    nothing, and says how many admits are awaiting review; once nothing waits, it builds what was decided, and a late
    list waits alone. A list that waits is said once, on the pass that finds it so, and again only when what it says
    changes, as when some of it was decided: the lines said last are kept in the session under `said`. A first run
    that failed gives a pass that says so: each curator's failure, and under `next` the command that mends it. Its
    curators are not asked again on every wake: the passes after it skip them, saying until when, and ask them once
    that wait is over, or at once when a model key a curator lacked is there."""
    if not in_first_run(workspace):
        return None
    if (waiting := _waits_after_failing(workspace)) is not None:
        return waiting
    held = PassResult()
    if (_state(workspace) or {}).get("status") in ("waiting", "stopped"):  # sources go on being checked, and held
        hold_sources(workspace, held, scheduled=True)
    out = run(workspace, choose=None, say=say)
    if out is None:
        return None
    if out.failed:
        return _failed_pass(workspace, out)
    workspace.store.set_state(FAILED, None)
    result = out.result or PassResult()
    if out.result is None:
        result.listed, result.from_start = held.listed, held.from_start
        result.failed = {k: workspace.mask_every()(v) for k, v in held.failed.items()}
    said = [f"{line} {SERVED}" if line.startswith("the first run's list") else line for line in out.lines]
    lines = [workspace.mask(line) for line in said]
    session = _state(workspace)
    if out.waits and session is not None:
        if session.get("said") == lines:
            return result  # said already, on an earlier pass of this first run
        session["said"] = lines
        _save(workspace, session)
    result.warnings += lines
    return result


def _lacking(workspace: Cerebro) -> list[str]:
    """The curators whose model has no key now, by id: what a failed first run under `serve` most often waits for."""
    from .app import MissingModelKey

    out = []
    for agent in _curators(workspace):
        try:
            workspace.need_key(agent)
        except MissingModelKey:
            out.append(agent.id)
    return out


def _failed_pass(workspace: Cerebro, out: Outcome) -> PassResult:
    """The pass of `serve` in which the first run failed: each curator's failure under its id and the command that
    mends it, and, kept under `FAILED`, when it failed, how often in a row, and which curators lacked a key."""
    result = PassResult()
    for agent in _curators(workspace):
        start = f"Discovery for {agent.target} collection did not finish: "
        if line := next((line for line in out.failed if line.startswith(start)), ""):
            result.failed[agent.id] = workspace.mask(line[:1].lower() + line[1:])
    result.next = out.run or "cerebro serve --once"
    kept = workspace.store.state(FAILED) or {}
    workspace.store.set_state(FAILED, {"at": stamp(workspace.clock()), "times": int(kept.get("times") or 0) + 1,
                                   "lacking": _lacking(workspace), "failed": sorted(result.failed), "next": result.next})
    return result


def _waits_after_failing(workspace: Cerebro) -> PassResult | None:
    """The pass of `serve` that asks nobody, since the first run failed a moment ago and nothing that would mend it
    has changed, or None to ask the curators again: the wait is compile's own after a failed run, doubled by each
    failure in a row up to an hour, and it ends at once when a curator that lacked a model key has one, or when a
    first run by hand began a session. No source is read and no model is called while it lasts."""
    kept = workspace.store.state(FAILED)
    if not isinstance(kept, dict) or _state(workspace) is not None:
        return None
    try:
        times = max(1, int(kept.get("times") or 1))
        until = datetime.fromisoformat(str(kept.get("at"))) + timedelta(
            seconds=min(FAILED_WAIT * 2 ** (times - 1), FAILED_WAIT_MOST))
        over = workspace.clock() >= until
    except (TypeError, ValueError):  # a kept wait that does not read waits for nothing
        workspace.store.set_state(FAILED, None)
        return None
    if over or kept.get("lacking") != _lacking(workspace):
        return None
    result = PassResult()
    why = (f"the first run failed {times} {'time' if times == 1 else 'times'}, so its curators wait until "
           f"{stamp(until)} before they are asked again, or until a model key one lacked is set")
    result.skipped = {id: why for id in kept.get("failed") or []}
    result.next = str(kept.get("next") or "")
    return result
