"""One pass over a workspace: list, curate, extract, link, compile, render. A pass runs what is due and nothing else.

- **List.** Each source lists what changed since the cursor the last pass kept for it, page by page. With no facts
  layer, no source is listed, so its cursor waits for one.
- **Curate.** Each due curator decides what its collection holds before anything is extracted, so what it admits is
  in scope for this pass. Curators run one at a time, in `curate.curator_order`, so a pass admits the same whatever
  the timing. Only writers run side by side.
- **Hold.** A listed record out of scope is held, its listing kept without a body under `held:<source>`, and tested
  again each pass: once a curator admits its article it is extracted. At most `HELD_MAX` are held per source.
- **Extract.** A record its source removed has its facts retracted, so the articles that read them become due. Each
  record in scope is fetched whole and handed to the facts layer, held to the extraction cap and its cost kept,
  unless its source extracts it into `records` or `none`. Its fields go to the facts layer too, one fact a field. A
  record that fails is reported and tried again next pass. A source's cursor moves once its records were handed over.
- **Kept records.** Each record an app kept or revised since the last pass is extracted the same way, a revision
  replacing what the record gave before.
- **Link.** Each fact is linked to its articles, as `identity` says, a near match judged by the decision model. An
  article born after its facts were kept has every current fact linked again by the keys kept under `keys:<source>`.
- **Compile.** Each article a due writer writes compiles through the app, side by side up to `compile.concurrency`,
  one collection at a time in the order `collection_order` gives. One article's failure stops no other.
- **Render.** Last, each writer's `rendered:` parts are written by code, with no model, and its copy is written.

What is due, in order:
1. An agent on `manual` alone waits for a person.
2. An agent whose schedule word, `hourly`, `daily` or `weekly`, names a period that holds now, in the workspace's
   zone, with no scheduled run of it in that period, is due. Weeks start on Monday. A scheduled run that fails gets
   one more try in its period.
3. An agent on `change` is due, and on each article whose only news is new evidence it waits out its cooldown, as
   `cooldown` and `_at_once` say, and the pass says until when.
4. An agent on `change` is due, and its compile skips when nothing it reads has moved. A curator whose run failed,
   and a writer whose run stopped at its budget with nothing handed in, wait as `waits_after` says.
5. Any other agent waits for its next period.

A subagent is never due on its own: it runs inside its composer's. What ran when is kept under
`heartbeat:<agent id>`, and each run is a row of the `agent-runs` log.

Two rules hold across every stage. A lease lets one process at a time run an agent, or list the sources. And work
a daily cap keeps from starting waits for a later pass and is no failure: `_paused` tells a cap reached, and
`_keep_stop` keeps what waits for `status`.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import posixpath
import threading
import uuid
import weakref
from collections import deque
from collections.abc import Callable, Collection, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from functools import partial
from itertools import islice
from typing import TYPE_CHECKING, Any

from . import rendered
from .agent import Agent, cooldown_seconds, read_yaml, refused_ids
from .article import Article, fields, mirror
from .budget import (
    LAYERS,
    BudgetError,
    BudgetSpent,
    Caps,
    Held,
    HeldForRoom,
    Stopped,
    UnpricedModel,
    read_caps,
    spend_digits,
    stop_line,
    stopping,
)
from .caller import Caller
from .clock import stamp
from .compile import (
    FAILED_WAIT,
    FAILED_WAIT_MOST,
    UNMIRRORED,
    birth,
    drop_material,
    fixed,
    keep_grounding,
    left_alone,
    made_safe,
    material_key,
    targets,
    unmirrored,
)
from .connector import Connector, CursorError, Record
from .connectors.files import FilesConnector
from .curate import (
    FIRST_RUN_KEY,
    close_moot,
    curator_order,
    in_own_first_run,
    in_scope,
    left_first_run,
    new_yes,
    own_facts,
    proposing,
    yes_left,
)
from .errors import CerebroError
from .evidence import IN_FIRST_BUILD, RECENT_DAYS
from .facts import Extracted, ExtractionFailed, Fact, _Hooks, field_name, revision_of, saved_revision
from .ground import counted
from .held import (
    OWN_WEIGHED,
    before_cut,
    first_run_since,
    keep_own,
    pass_first_run,
    reweigh_own,
    waits_for_own,
    weighed,
)
from .identity import (
    DecisionJudge,
    follow,
    kept_off,
    link,
    link_ties,
    linked_keys,
    near_pool,
    own_articles,
    reached,
    redirects,
    ties,
    working,
)
from .models import DecisionError, ModelSpec, cost
from .private import make_file, make_folder
from .project import read_ours, read_schedule, zone_of
from .records import (
    ACCEPTED,
    RETRY,
    TOLD,
    Claimed,
    extract_told,
    finish_retracts,
    instructions_on,
    proposals,
    retracted_records,
    settle_told,
    was_retracted,
)
from .rendered import descriptions
from .run import WROTE_NOTHING, Run
from .source import read_source, span
from .store import MENTIONS, RECORD, SUBJECT, Store, own_key, record_from, update_state

if TYPE_CHECKING:
    from .app import Cerebro

__all__ = ["CHECKED", "FULL", "Ahead", "Decision", "LeaseHeld", "PassResult", "ahead_of", "checks", "decide", "first_list",
           "first_list_line", "fulls", "held_runs", "hold", "hold_sources", "left_by_hand", "let_go", "past_bound", "ran_by_hand",
           "run_curators", "run_forever", "run_pass", "waits_by_hand"]

SCHEDULES = ("hourly", "daily", "weekly")
WRITERS = ("writer", "article", "composer", "query")


@dataclass(frozen=True)
class Progress:
    """One event of a pass, for a screen that draws it as it happens. `stage` is check, distill, agents or compile;
    `kind` is open, with the `total` the stage counts, or start or end of the work `key` names. An end's `mark` is
    done, failed, warning, skipped, or for a compile ran, a run that ran and wrote nothing, `text` what its row says
    after its key, `cost` what an agent's run spent, and `why` what went wrong. A check's end carries in `total` how many records it listed, and a
    distill pass's `count` how many of the `total` records it goes through it is `done` with, `text` its facts."""

    stage: str
    kind: str
    key: str = ""
    total: int = 0
    mark: str = ""
    text: str = ""
    why: str = ""
    done: int = 0
    cost: str = ""


@dataclass
class PassResult:
    """What one pass did: records listed by source, read whole, and the facts extracted, retracted and linked; the
    articles compiled and the runs; and why each agent was due, ran and wrote nothing, failed or was skipped. `held`
    names each run held for room under a cap, which runs on a later pass and is no failure. `notices` name what was
    read but not acted on, and `warnings` what went wrong around an article that stands.

    Work a daily cap kept from starting waits for the next pass and is no failure: `stops` holds the line of each cap
    reached, `stopped` each layer it holds, with the ledger's words, `crossed` the crossing its first refusal met,
    `waiting` the records that wait, by the list each waits on, `tried` each such list as it stood when the pass
    began, and `waiting_runs` the agent runs."""

    listed: dict[str, int] = field(default_factory=dict)
    from_start: set[str] = field(default_factory=set)  # each source listed with no cursor kept: its whole window
    older: set[str] = field(default_factory=set)  # each source/id a first run's build held as older than its cut
    read: int = 0
    extracted: int = 0
    retracted: int = 0
    linked: int = 0
    compiled: list[str] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    due: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    ran: dict[str, str] = field(default_factory=dict)  # each run that ran and wrote nothing, with why: no failure
    skipped: dict[str, str] = field(default_factory=dict)
    held: dict[str, str] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)  # the curators whose run had nothing to read
    next: str = ""  # the command that mends a first run that failed under `serve`
    stops: list[str] = field(default_factory=list)
    stopped: dict[str, str] = field(default_factory=dict)
    waiting: dict[str, list[str]] = field(default_factory=dict)
    waiting_runs: list[str] = field(default_factory=list)
    crossed: dict[str, dict[str, Any]] = field(default_factory=dict)  # by layer, the crossing its first refusal met
    tried: dict[str, list[str]] = field(default_factory=dict)  # each retry list the pass tried, as it began
    paused: set[str] = field(default_factory=set)  # each source a daily cap stopped reading, at its first model record
    grounding: dict[str, dict[str, int]] = field(default_factory=dict)  # what grounding did, by the run's key and kind
    cleaned: dict[str, int] = field(default_factory=dict)  # the things landing made safe, by the run's key
    token: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False)  # whose leases this pass holds
    say: Callable[[str], None] | None = field(default=None, repr=False, compare=False)  # a line as each step happens
    on: Callable[[Progress], None] | None = field(default=None, repr=False, compare=False)  # an event as each happens
    _said: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def tell(self, line: str) -> None:
        """Say a line now, as a stage starts or an agent's run ends, when the pass was handed `say`. Runs end on
        threads side by side, so one line is said at a time."""
        if self.say is not None:
            with self._said:
                self.say(line)

    def step(self, stage: str, kind: str, key: str = "", **more: Any) -> None:
        """Hand `on` one event of the pass, as `tell` hands `say` a line."""
        if self.on is not None:
            with self._said:
                self.on(Progress(stage, kind, key, **more))


# -- a pass --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reader:
    """What holds the lease on listing and extracting the sources, so two passes never extract one record twice. Its
    id is no agent's: an agent's id has no `#`."""

    id: str = "#sources"


def _sources_held(workspace: Cerebro, result: PassResult) -> str:
    """Take the lease on the sources for this pass and give "", or, when a pass in another process holds it, give when
    that lease lapses, and say on `result` that this pass leaves the sources to it."""
    if until := _lease(workspace, _Reader(), result.token, result):  # type: ignore[arg-type]
        result.skipped["sources"] = ("a pass in another process lists and extracts the sources now: this pass "
                                     f"leaves them to it, or to its lease to lapse at {until}")
    return until


def run_pass(workspace: Cerebro, *, scheduled: bool = False, only: str | None = None,
             agents: bool = True, say: Callable[[str], None] | None = None, curators: bool = True,
             on: Callable[[Progress], None] | None = None, articles: Collection[str] | None = None) -> PassResult:
    """One pass over `workspace`: list, curate, extract, link, compile, each agent only when it is due. `scheduled`, as
    under `serve`, checks only the sources whose freshness window has passed; otherwise every source is checked now,
    or the one named `only`. With `agents` false, as `distill` runs it, no agent runs. With `curators` false, as the
    first run builds what a person approved, no curator runs and the writers do. With `articles`, only those are
    compiled, and every other one due stays due."""
    result = PassResult(refused=list(workspace.refused), notices=list(workspace.notices), say=say, on=on)
    workspace.gateway.said = ""  # a host's refusal stops one pass: the next tries again
    if agents and workspace.store.state(FIRST_RUN_KEY) is None and left_first_run(workspace.store):
        pass_first_run(workspace.store, stamp(workspace.clock()))  # a curator's collection left its first run already
    _forget_gone(workspace)
    _take_ours(workspace, result)
    until = _sources_held(workspace, result)
    with _holding(workspace, [] if until else [_Reader()], result.token):  # type: ignore[list-item]
        if not until:
            _own_names_follow(workspace, result, only)
            result.tell("stage     list: checking the sources")
        sources = [] if until else _list(workspace, result, scheduled, only)
        for listed in sources:  # who each listing names as the workspace's own, kept before any curator weighs a name
            keep_own(workspace.store, listed.connector.name, listed.records, workspace.ours, listed.removed)
        if agents and curators:
            _curate(workspace, result)
        if not until:
            result.tell("stage     extract: reading what the sources listed into facts, and linking them")
            if workspace.extraction is not None:
                _leave_no_retract_half_done(workspace, result)
            _link(workspace, _extract_all(workspace, sources, result) + _extract_kept(workspace, result), result)
            if workspace.lookup.extraction is not None:
                _retry_told(workspace, result)
    _link_born(workspace, result)
    if workspace.extraction is not None:
        result.notices += workspace.class_notices()
    if agents:
        _compile(workspace, result, served=scheduled, articles=articles)
        session = workspace.store.state(FIRST_RUN_KEY)
        if (not isinstance(session, dict) or session.get("status") == "done") and not _first_waits(workspace):
            pass_first_run(workspace.store, stamp(workspace.clock()))  # nothing of a first run holds it or waits
    _render(workspace, result)
    _keep_stop(workspace, result, runs=agents)
    if workspace.gateway.said:  # a host refused a call: no model call started after it, as at a cap
        result.stops.insert(0, workspace.gateway.said)
    return result


def _first_waits(workspace: Cerebro) -> bool:
    """Whether an admit of a curator still in its own first run waits for a person, which keeps the first run open."""
    return any(p.kind == "article.admit" and p.by.kind == "agent" and in_own_first_run(workspace.store, p.by.name)
               for p in proposals(workspace.store, "waiting"))


def _going(workspace: Cerebro) -> None:
    """Raise `Stopped` once serve is stopping, between two steps of a pass."""
    if stopping(workspace.store):
        raise Stopped("serve stopped the pass: what it left runs on a later pass")


def run_forever(workspace: Cerebro, *, interval: float, stop: Callable[[], bool], sleep: Callable[[float], None],
                on_pass: Callable[[PassResult], None] | None = None,
                on_error: Callable[[Exception], None] | None = None,
                say: Callable[[str], None] | None = None) -> int:
    """The loop behind `serve`: a pass, then `sleep(interval)`, until `stop()` is true, and give the number of
    passes. Each pass goes to `on_pass`. A pass that raises goes to `on_error`, or the `cerebro.runner` log, and the
    loop goes on. `sleep` is handed in, so a caller can wake it early and a test never waits. Each wake reads the
    daily caps in settings.yaml again first, and says on `say` each cap that no longer stops calls."""
    from .first_run import served  # imported here: the first run builds on this module

    passes, kept = 0, {}
    while not stop():
        try:
            _read_caps(workspace, kept, say)
            workspace.files_read()  # each pass reads workspace.md and profile.md again, and stops on a fault in either
            # a workspace in its first run proposes and waits for a person; each source on its window otherwise
            result = served(workspace, say=say) or workspace.run_once(scheduled=True, say=say)
        except Stopped:
            break  # a stop of serve ended the pass by its own road: what ran is recorded, the rest runs next time
        except Exception as error:
            if on_error is not None:
                on_error(error)
            else:
                logging.getLogger(__name__).error("a pass failed, and the next runs in %ss", interval, exc_info=workspace.safe_error(error))
        else:
            if on_pass is not None:
                on_pass(result)
        passes += 1
        if stop():
            break
        sleep(interval)
    return passes


# -- what is due, and what waits ---------------------------------------------------------------------------------------------------


@dataclass
class _Beat:
    """What the runner keeps of an agent: when its last run started, when its schedule last ran it, and the period
    whose scheduled run failed once already, each as UTC text."""

    ran: str | None = None
    scheduled: str | None = None
    retried: str | None = None
    file: str | None = None  # the agent's file, and its subagents', as its last run read them
    # Each article its runs wrote on, by id: when its last run there started, and the agent's files and the workspace's
    # settings as that run read them. A writer's cooldown counts from it, article by article.
    articles: dict[str, dict[str, str]] | None = None
    # The articles a scheduled run left to do in its period, held, waiting for a cap or failed: the period start, and
    # each article with how its next run runs. The period stays due, and its next run runs on these alone.
    pending: dict[str, Any] | None = None
    # A curator whose run stopped at its budget once it had carried out a yes, with more accepted proposals left: it
    # runs again on the next pass.
    again: bool = False


def _period(word: str, local: datetime) -> tuple[datetime, datetime]:
    """The start of the period a schedule word names that holds `local`, and the start of the next one."""
    if word == "hourly":
        start = local.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    day = local.date() - timedelta(days=local.weekday() if word == "weekly" else 0)
    zone = local.tzinfo
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    after = day + timedelta(days=7 if word == "weekly" else 1)
    return start, datetime(after.year, after.month, after.day, tzinfo=zone)


def _when(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _beat(workspace: Cerebro, agent: Agent) -> _Beat:
    return _Beat(**(workspace.store.state(f"heartbeat:{agent.id}") or {}))


def _words(workspace: Cerebro, agent: Agent) -> list[str]:
    """The words that wake an agent: its own, or for a composer, which has none, every word of its subagents."""
    if agent.role != "composer":
        return agent.heartbeat
    subs = [a for a in workspace.agents if a.id in agent.subagent_ids]
    return list(dict.fromkeys(word for sub in subs for word in sub.heartbeat))


def _wake(agent: Agent, words: list[str], beat: _Beat, now: datetime, zone: tzinfo) -> tuple[str | None, str, str]:
    """Whether `agent`, woken by `words`, is due at `now`: the schedule word whose period is due, or None, and why it
    runs, or empty with why it waits. A writer due for a change may still wait out its cooldown on an article, which
    compile asks `_hold` once it knows that new evidence alone moved there."""
    if not words:
        return None, "", "nothing wakes it: give it a heartbeat, or its subagents one"
    if all(word == "manual" for word in words):
        return None, "", "heartbeat is manual: run it by hand"
    local, scheduled = now.astimezone(zone), _when(beat.scheduled)
    nexts = []
    for word in (w for w in words if w in SCHEDULES):
        start, after = _period(word, local)
        if scheduled is None or scheduled < start or scheduled > now:
            last = f"it last ran {scheduled.astimezone(zone):%Y-%m-%d %H:%M}" if scheduled else "it never ran"
            return word, f"{word}, and {last}", ""
        nexts.append(after)
    if "change" in words:
        return None, "change", ""
    plan = ", ".join(words)
    return None, "", f"heartbeat is {plan}: it ran this period, and runs next at {min(nexts):%Y-%m-%d %H:%M}"


def _due(workspace: Cerebro, agents: list[Agent], result: PassResult,
         woken: Collection[str] = ()) -> list[tuple[Agent, str | None]]:
    """The agents of `agents` due now, each with the schedule word it runs for, or None for a change. A subagent is
    never due on its own: it drafts inside its composer's run, whose stamp covers what it reads. A curator of `woken`
    has accepted proposals to carry out, and is due as for a change, whatever its heartbeat."""
    now, zone, due = workspace.clock(), zone_of(workspace.settings), []
    for agent in agents:
        if agent.role == "subagent":
            continue
        word, why, waits = _wake(agent, _words(workspace, agent), _beat(workspace, agent), now, zone)
        if agent.id in woken and word is None:
            why, waits = "a caller accepted a proposal for its collection", ""
        if not waits and agent.role in (*WRITERS, "curator") and (until := _lease(workspace, agent, result.token, result)):
            waits = f"a pass in another process runs it now: it waits for that, or for its lease to lapse at {until}"
        if waits:
            result.skipped[agent.id] = waits
        elif agent.role not in (*WRITERS, "curator"):
            result.skipped[agent.id] = f"Cerebro does not run an agent of role {agent.role}"
        else:
            result.due[agent.id] = why
            due.append((agent, word))
    return due


def _left(workspace: Cerebro, agent: Agent, word: str | None) -> dict[str, str] | None:
    """The articles a scheduled run of `agent` left to do in the period `word` names now, or None: in another period,
    or a run of no schedule word, every article runs."""
    pending = _beat(workspace, agent).pending
    if word is None or not pending:
        return None
    start = stamp(_period(word, workspace.clock().astimezone(zone_of(workspace.settings)))[0])
    return dict(pending.get("articles") or {}) if pending.get("period") == start else None


FAILED = ("failed",)  # how a period's run left an article that failed


def _ran(workspace: Cerebro, agent: Agent, word: str | None, run: Run | None, failed: bool,
         began: datetime | None = None, articles: list[str] | None = None,
         left: dict[str, str] | None = None) -> None:
    """Keep what one agent's turn did: when it ran, the files it ran on, and whether its schedule's period is spent.
    When it ran is `began`, the workspace's clock as the turn began, never the run's own stamps, and the period is the
    one it began in. Each of `articles`, those a run of this turn wrote on, takes that time as its mark, so its
    cooldown counts from there. A scheduled run that fails leaves its period due for one more try, then spends it.
    `left` names the articles a scheduled run did not finish, as `_Beat.pending` keeps them, each `failed`, or `whole`
    or `parts` for one held or waiting for a cap: those stay pending until they run, then the period is spent."""
    beat, now = _beat(workspace, agent), workspace.clock()
    if run is not None:
        beat.ran = stamp(began or now)
        beat.file = _file_stamp(workspace, agent)
    if articles:
        beat.articles = (beat.articles or {}) | dict.fromkeys(articles, _mark(workspace, agent, began or now))
    if word is not None:
        when = began or now
        start = stamp(_period(word, when.astimezone(zone_of(workspace.settings)))[0])
        waits = {article: how for article, how in (left or {}).items() if how not in FAILED}
        if failed and beat.retried != start:
            beat.retried, beat.pending = start, {"period": start, "articles": left} if left else None
        elif waits:  # a failure spent its one more try; what was held or waits for a cap stays until it runs
            beat.pending = {"period": start, "articles": waits}
        else:
            beat.scheduled, beat.retried, beat.pending = stamp(when), None, None
    workspace.store.set_state(f"heartbeat:{agent.id}", asdict(beat))


def ran_on(workspace: Cerebro, agent: Agent, article: str, began: datetime) -> None:
    """Move `article`'s mark to `began` for a run of `agent` that landed there, by hand or in a pass, so its cooldown
    counts from there and an instruction it took in is no cause again. One write under the store's lock, since a
    pass's compiles run side by side."""
    mark = _mark(workspace, agent, began)
    with workspace.store.exclusive():
        beat = _beat(workspace, agent)
        beat.articles = (beat.articles or {}) | {article: mark}
        workspace.store.set_state(f"heartbeat:{agent.id}", asdict(beat))


def _mark(workspace: Cerebro, agent: Agent, began: datetime) -> dict[str, str]:
    """What a run of `agent` keeps on an article it ran on: when it began, and the agent's files and the workspace's
    settings as they stand, which `_at_once` reads as causes once they change."""
    return {"ran": stamp(began), "file": _file_stamp(workspace, agent), "setting": _setting_stamp(workspace)}


def cooldown(workspace: Cerebro, agent: Agent, part: str = "", cadence: bool = True) -> tuple[int, str]:
    """How long new evidence waits after `agent` runs on an article, in seconds and as written: its own heartbeat's
    `cooldown:`, else for a writer the workspace's `schedule.articles`, 4h when left out. A curator has none: its
    file may not set one. `part`, a subagent `agent` lists, gives that subagent's own `cooldown:` when it sets one.
    Without `cadence`, as a pass by hand counts it, only an agent's own cooldown holds."""
    if part and part != agent.id and (sub := workspace.agent(part)) is not None and sub.cooldown is not None:
        return cooldown_seconds(sub.cooldown) or 0, sub.cooldown
    if agent.cooldown is not None:
        return cooldown_seconds(agent.cooldown) or 0, agent.cooldown
    if agent.role == "curator" or not cadence:
        return 0, ""
    block = workspace.settings.get("schedule")
    said = block.get("articles") if isinstance(block, dict) else None
    return int(read_schedule(workspace.settings).articles.total_seconds()), str(said if said is not None else "4h")


def _file_stamp(workspace: Cerebro, agent: Agent) -> str:
    """One hash of the agent's file and of each of its subagents' files, as they stand now."""
    hashed = hashlib.sha256()
    for one in [agent, *(a for a in workspace.agents if a.id in agent.subagent_ids)]:
        hashed.update(one.id.encode() + b"\0" + (one.path.read_bytes() if one.path.is_file() else one.prompt.encode()))
    return hashed.hexdigest()[:16]


def _setting_stamp(workspace: Cerebro) -> str:
    """One hash of `workspace.md` and of each prompt ejected under `prompts/`, as they stand now: what a run and its
    grounding read besides the agent's files and evidence."""
    hashed = hashlib.sha256()
    for path in [workspace.root / "workspace.md", *sorted((workspace.root / "prompts").glob("*.md"))]:
        if path.is_file():
            hashed.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return hashed.hexdigest()[:16]


def _at_once(workspace: Cerebro, agent: Agent, article_id: str, mark: dict[str, str]) -> str:
    """Why `agent` runs on `article_id` now though new evidence would wait, or empty, counted since its last run there,
    `mark`: the article's first build, a change to the agent's file or a subagent's, a change to `workspace.md` or an
    ejected prompt, an instruction on the article that another than the agent decided, or a fact its text cites that
    was retracted. An instruction counts once, against the run that took it in, since that run moves the mark."""
    if not written(workspace, agent, article_id):
        return "its first build"
    if mark.get("file") != _file_stamp(workspace, agent):
        return "its agent file changed"
    if mark.get("setting") != _setting_stamp(workspace):
        return "workspace.md or a prompt changed"
    since, moved = _when(mark.get("ran")), redirects(workspace.store)
    if since is not None and any(
            p.kind == "article.instruct" and p.status == "accepted" and moved.get(p.target, p.target) == article_id
            and (p.decided_by or p.by).name != agent.id and _when(p.decided_at or p.at) > since  # type: ignore[operator]
            for p in proposals(workspace.store)):
        return "an instruction on it"
    article, writers = workspace.store.article(article_id), (agent.id, *agent.subagent_ids)
    cites = [c.id for part in (article.compilations if article else []) if part.by in writers for c in part.citations
             if c.kind == "fact"]
    if cites and workspace.extraction is not None and any(
            fact.status == "retracted" for fact in workspace.extraction.facts(ids=list(dict.fromkeys(cites)))):
        return "a fact its text cites was retracted"
    return ""


COOLING = "cooling down: "  # opens the line that says a writer waits out its cooldown


def _hold(workspace: Cerebro, agent: Agent, article: str, cadence: bool = True) -> Callable[[list[str]], str]:
    """What compile asks, on a pass, once it found that the evidence of the agents `moved` names moved on `article`
    and nothing else did: why that rebuild waits, or empty when it runs now. It waits while the cooldown of the first
    of them due, counted from the agent's last run on the article, has not passed, unless a cause `_at_once` names runs
    it all the same. Without `cadence`, on a pass by hand, only an agent's own `cooldown:` holds, as `cooldown` says."""

    def hold(moved: list[str]) -> str:
        mark = (_beat(workspace, agent).articles or {}).get(article) or {}
        ran = _when(mark.get("ran"))
        if ran is None or _at_once(workspace, agent, article, mark):
            return ""
        seconds, said = min((cooldown(workspace, agent, id, cadence) for id in moved or [agent.id]),
                            key=lambda gap: gap[0])
        until = ran + timedelta(seconds=seconds)
        if not seconds or workspace.clock() >= until:
            return ""
        return f"{COOLING}new evidence waits until {until.astimezone(zone_of(workspace.settings)):%Y-%m-%d %H:%M}, " \
               f"cooldown {said}"
    return hold


WAITS = "waits:"  # + an agent's id: its runs that wait after a failure or a stop at its budget, by article


def _wait_mark(workspace: Cerebro, agent: Agent, article: str) -> dict[str, Any]:
    """What ends a wait: the agent's files, the workspace's settings and `settings.yaml` as they stand, the newest
    row of its runs on `article`, so a run there by hand or in another process ends it, and for a curator the newest
    proposal of its collection accepted and not carried out, so a new yes ends it."""
    newest = workspace.store.latest("agent-runs", "target", {"agent": agent.id, "target": article})
    settings = workspace.root / "settings.yaml"
    setting = _setting_stamp(workspace) + (hashlib.sha256(settings.read_bytes()).hexdigest()[:16] if settings.is_file()
                                       else "")
    yes = yes_left(workspace.store, agent.target) if agent.role == "curator" else []
    return {"file": _file_stamp(workspace, agent), "setting": setting, "seq": newest[-1].seq if newest else 0,
            "accepted": yes[-1] if yes else ""}


def waits_after(workspace: Cerebro, agent: Agent, article: str) -> str:
    """Why `agent`'s run on `article`, its collection for a curator, waits, or empty. A curator whose run failed, and a
    writer whose run there stopped at its budget with nothing handed in, wait as a writer's failed run does: 30
    seconds, then twice as long after each more, up to an hour. A curator whose run stopped at its budget with
    nothing decided waits for its file to change, where its budget is. Every wait ends when the agent's files,
    `workspace.md`, `settings.yaml` or a prompt change, or once it runs there elsewhere, as by hand."""
    kept = (workspace.store.state(WAITS + agent.id) or {}).get(article)
    if not kept or {key: kept.get(key, "") for key in ("file", "setting", "seq", "accepted")} != _wait_mark(
            workspace, agent, article):
        return ""
    if kept.get("stopped"):
        return (f"its run stopped at its budget with nothing decided, so it waits until agents/{agent.id}.md changes: "
                f"raise {kept['stopped']} there")
    times = int(kept.get("times", 1))
    until = _when(kept["at"]) + timedelta(seconds=min(FAILED_WAIT * 2 ** (times - 1), FAILED_WAIT_MOST))  # type: ignore[operator]
    if workspace.clock() >= until:
        return ""
    what = f"stopped at its budget with nothing handed in ({kept['spent']})" if kept.get("spent") else "failed"
    return (f"its run {what} {times} {'time' if times == 1 else 'times'}, so it waits until {stamp(until)} before it "
            "runs again, or until its file, workspace.md or settings.yaml changes")


def _waited(workspace: Cerebro, agent: Agent, article: str, *, failed: bool = False, spent: str = "",
            stopped: str = "") -> None:
    """Keep the wait of `agent` on `article` after a run there that `failed`, or stopped at the budget key `spent` with
    nothing handed in, or at `stopped` with nothing decided, counting one more when the files are the ones the last
    wait was kept on; or end it after any other run."""
    if not (failed or spent or stopped):
        if (workspace.store.state(WAITS + agent.id) or {}).get(article) is not None:
            update_state(workspace.store, WAITS + agent.id,
                         lambda kept: {k: v for k, v in (kept or {}).items() if k != article} or None)
        return
    mark, now = _wait_mark(workspace, agent, article), stamp(workspace.clock())

    def change(kept: Any) -> dict[str, Any]:
        last = (kept or {}).get(article) or {}
        same = last.get("file") == mark["file"] and last.get("setting") == mark["setting"]
        wait = {**mark, "times": int(last.get("times", 0)) + 1 if same else 1, "at": now,
                **({"spent": spent} if spent else {}), **({"stopped": stopped} if stopped else {})}
        return {**(kept or {}), article: wait}
    update_state(workspace.store, WAITS + agent.id, change)


def waiting_runs(workspace: Cerebro) -> list[str]:
    """One line for each agent's run that waits after a failure or a stop at its budget, and why, as `waits_after` says."""
    out = []
    for agent in workspace.agents:
        for article in workspace.store.state(WAITS + agent.id) or {}:
            if why := waits_after(workspace, agent, article):
                out.append(f"{_key(agent, article)}: {why}")
    return out


def failing(workspace: Cerebro, id: str) -> bool:
    """Whether the last run of the curator `id` failed, so its wait stands, whatever its time."""
    agent = workspace.agent(id)
    kept = (workspace.store.state(WAITS + id) or {}).get(agent.target) if agent is not None else None
    return bool(kept) and not kept.get("stopped") and not kept.get("spent")


def _budget_key(run: Run) -> str:
    """The key of an agent file's `budget:` that a run which stopped at its budget ran into."""
    return "budget.tool_calls" if "tool_calls" in run.note else "budget.tokens_per_run"


# -- leases: one process runs an agent at a time -------------------------------------------------------------------


LEASE = 1800  # seconds an agent's lease holds, after which a pass that died lets go of it
RENEW = 300.0  # seconds between two renewals of the leases a run holds, while it runs
_TAKEN: dict[tuple[str, str], None] = {}  # (lease key, token) of each lease taken in this process and not given back
# the stores of workspaces this process is stopping: no lease is taken
_STOPPING: weakref.WeakSet[Any] = weakref.WeakSet()
_SAID: set[tuple[str, int]] = set()  # (pass token, process id) of each takeover said once


def _lease(workspace: Cerebro, agent: Agent, token: str, result: PassResult | None = None) -> str:
    """Take the agent's lease for this pass, under `lease:<agent id>` in state, and give "", or, when a pass in
    another process holds it, when that lease lapses. A lease lapses `LEASE` seconds after it was taken, so a pass
    that died does not hold its agents for good, and one whose process is gone is taken over at once, said on `result`
    once a pass."""
    key, now = f"lease:{agent.id}", workspace.clock()
    with workspace.store.exclusive():
        if stopping(workspace.store):
            raise Stopped(f"serve is stopping, so {agent.id} does not start: it runs on a later pass")
        if workspace.store in _STOPPING:
            return stamp(now)
        held = workspace.store.state(key)
        gone = bool(held) and held["by"] != token and _gone(workspace, held)
        if held and held["by"] != token and datetime.fromisoformat(held["until"]) > now and not gone:
            return held["until"]
        workspace.store.set_state(key, _held_by(workspace, token, now + timedelta(seconds=LEASE)))
        _TAKEN[(key, token)] = None
    if gone and result is not None and (token, held.get("pid")) not in _SAID:  # said once a pass
        _SAID.add((token, held.get("pid")))
        result.tell(f"note      took over the leases of pid {held['pid']}, which ended without letting them go")
    return ""


def _held_by(workspace: Cerebro, token: str, until: datetime) -> dict[str, Any]:
    return {"by": token, "until": stamp(until), "pid": os.getpid(), "holder": _holder(workspace)}


_HOLDERS: dict[str, tuple[str, Any]] = {}  # this process's holder file of each workspace: its id, and the file it locks
_HOLDERS_LOCK = threading.Lock()


def _holder(workspace: Cerebro) -> str:
    """The id this process's leases on `workspace` name: a file under `.cerebro/holders/` it holds locked for as long as
    it runs. The kernel lets the lock go when the process ends, however it ends, so the next start can tell a holder
    that ended from one that runs, in any process or container on this machine. Empty where a file cannot be locked, and
    a lease of no holder waits until it lapses. The file is touched each time it is named, so one that is free and was
    not touched for `LEASE` seconds names no lease that holds, and the next process to make its own file sweeps it."""
    if getattr(workspace, "root", None) is None:
        return ""
    root = str(workspace.root)
    with _HOLDERS_LOCK:
        if root not in _HOLDERS:
            try:
                import fcntl

                folder = workspace.root / ".cerebro" / "holders"
                make_folder(folder)
                id = uuid.uuid4().hex
                make_file(folder / f"{id}.lock")
                held = open(folder / f"{id}.lock", "w")  # noqa: SIM115 - held open, and locked, while the process runs
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                made = os.fstat(held.fileno()).st_mtime
                for file in folder.glob("*.lock"):
                    with contextlib.suppress(OSError):  # another process that starts may sweep it first
                        if file.stat().st_mtime < made - LEASE and _free(file):
                            file.unlink()
                _HOLDERS[root] = (id, held)
            except (ImportError, OSError):
                _HOLDERS[root] = ("", None)
        id, held = _HOLDERS[root]
        if held is not None:
            with contextlib.suppress(OSError):  # a file that cannot be touched may be swept once its process ends:
                os.utime(held.fileno())  # its leases then wait until they lapse
        return id


def _gone(workspace: Cerebro, held: dict[str, Any]) -> bool:
    """Whether the process that holds a lease ended without giving it back, as after a second Ctrl-C, a kill or a
    power cut: its holder file is there and its lock is free. A lease of a process that runs, of another machine
    whose file this one cannot lock, or of no holder, waits until it lapses. Only a sweep removes the file, so every
    lease of a process that ended is taken over this way."""
    holder = held.get("holder")
    if not isinstance(holder, str) or len(holder) != 32 or not holder.isalnum() or holder == _holder(workspace):
        return False
    return _free(workspace.root / ".cerebro" / "holders" / f"{holder}.lock")


def _free(file: os.PathLike[str]) -> bool:
    """Whether the holder file `file` is there and no process holds its lock."""
    try:
        import fcntl

        fd = os.open(file, os.O_RDWR)
    except (ImportError, OSError):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False  # its process holds it: alive
    finally:
        os.close(fd)
    return True


def _release(workspace: Cerebro, agents: list[Agent], token: str) -> None:
    for agent in agents:
        with workspace.store.exclusive():
            if (workspace.store.state(f"lease:{agent.id}") or {}).get("by") == token:
                workspace.store.set_state(f"lease:{agent.id}", None)
        _TAKEN.pop((f"lease:{agent.id}", token), None)


def let_go(workspace: Cerebro) -> int:
    """Give back every lease this process holds on `workspace`, and take no more, for a process that stops while a pass
    is under way. Gives how many it gave back."""
    given = 0
    with workspace.store.exclusive():
        _STOPPING.add(workspace.store)
        for key, token in list(_TAKEN):
            if (workspace.store.state(key) or {}).get("by") == token:
                workspace.store.set_state(key, None)
                given += 1
                _TAKEN.pop((key, token), None)
    return given


def _renew(workspace: Cerebro, agents: list[Agent], token: str) -> None:
    """Extend each lease `token` still holds by `LEASE` seconds from now, by the workspace's clock."""
    until = workspace.clock() + timedelta(seconds=LEASE)
    for agent in agents:
        with workspace.store.exclusive():
            if (workspace.store.state(f"lease:{agent.id}") or {}).get("by") == token:
                workspace.store.set_state(f"lease:{agent.id}", _held_by(workspace, token, until))


@contextmanager
def _holding(workspace: Cerebro, agents: list[Agent], token: str) -> Iterator[None]:
    """Keep the leases `token` took on `agents` while the block runs, renewed every `RENEW` seconds, and give each
    back when the block ends, however it ends."""
    stop = threading.Event()

    def keep() -> None:
        while not stop.wait(RENEW):
            try:
                _renew(workspace, agents, token)
            except Exception:
                logging.getLogger(__name__).error("renewing the leases of a run failed", exc_info=workspace.safe_error())

    keeper = threading.Thread(target=keep, name="cerebro-leases", daemon=True)
    if agents:
        keeper.start()
    try:
        yield
    finally:
        stop.set()
        if agents:
            keeper.join(timeout=5)
        _release(workspace, agents, token)


class LeaseHeld(CerebroError):
    """An agent is running in another process, which holds its lease."""


@contextmanager
def hold(workspace: Cerebro, agent: Agent) -> Iterator[None]:
    """Hold an agent's lease for one run outside a pass, as `agents run` and `compile` do, renewed while it runs and
    given back after. Raises `LeaseHeld` when a pass or a run in another process holds it."""
    token = uuid.uuid4().hex
    if until := _lease(workspace, agent, token):
        raise LeaseHeld(f"{agent.id} is running in another process: wait for it to finish, or for its lease to lapse "
                        f"at {until}")
    with _holding(workspace, [agent], token):
        yield


# -- list ----------------------------------------------------------------------------------------------------------


@dataclass
class _Listed:
    """What one source listed this pass: records, ids removed, and the cursor to keep once they are handed over."""

    connector: Connector
    records: list[Record] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    cursor: str | None = None
    moved: bool = False
    full: str = ""  # when a listing in full began, for a source that lists changes, kept once its records are taken in
    again: bool = False  # listed from a cursor kept back for a record left out, as `AGAIN` says
    back: list[str | None] = field(default_factory=list)  # the cursor a page that left a record out was listed from


CHECKED = own_key("checked", "")  # + a source's name: when it was last checked, whether that check finished, when next
FULL = own_key("full", "")  # + a source's name: when it was last listed in full, kept once its records were taken in
FULL_EVERY = timedelta(hours=24)  # how often a source that lists changes is listed in full, when its file says not
ALWAYS = "always"  # full_every: always lists a source that lists changes in full at every check, by choice
AGAIN = own_key("again", "")  # + a source's name: its cursor is kept back to list again a record left out for its id
HELD_MAX = 2000  # held records kept per source: past it the oldest are let go, until their source lists them again


def checks(workspace: Cerebro) -> dict[str, dict[str, Any]]:
    """Each listing source's last check, as the store keeps it: when, whether it finished, and when it is next due.
    A source never checked has none."""
    return {name: workspace.store.state(CHECKED + name) or {} for name, c in sorted(workspace.connectors.items())
            if not c.lists_nothing}


def _freshness(workspace: Cerebro, name: str) -> timedelta:
    """How often a source is checked under `serve`: its file's `freshness:`, else `schedule.sources`."""
    path = workspace.root / "sources" / f"{name}.yaml"
    try:
        given = read_source(path).freshness if path.is_file() else ""
        return span(given) if given else read_schedule(workspace.settings).sources
    except (CerebroError, ValueError):
        return read_schedule(workspace.settings).sources


def _checked(workspace: Cerebro, name: str, connector: Connector, finished: bool) -> None:
    """Keep when a source was checked and when it is next due: after its freshness window, or, for a files source
    that left a file still being written, once that file has settled."""
    now = workspace.clock()
    due = now + _freshness(workspace, name)
    inner = getattr(connector, "inner", connector)
    if isinstance(inner, FilesConnector) and inner.unsettled_until is not None:
        due, finished = min(due, inner.unsettled_until), False
    update_state(workspace.store, CHECKED + name, lambda _: {"at": stamp(now), "finished": finished, "next": stamp(due)})


def fulls(workspace: Cerebro) -> dict[str, dict[str, Any]]:
    """Each listing source's cursor, as the store keeps it: whether it holds one, whether it lists only what changed
    since one, whether its file asks for a listing in full at every check, and when it was last listed in full, ""
    for never or for a source that does not list changes."""
    out = {}
    for name, connector in sorted(workspace.connectors.items()):
        if connector.lists_nothing:
            continue
        changes = getattr(connector, "inner", connector).lists_changes
        out[name] = {"cursor": workspace.store.state(f"cursor:{name}") is not None, "changes": changes,
                     "always": changes and _full_every(workspace, name) is None,
                     "full": (workspace.store.state(FULL + name) or {}).get("at", "") if changes else ""}
    return out


def _full_every(workspace: Cerebro, name: str) -> timedelta | None:
    """How often a source that lists changes is listed in full: its file's `full_every:`, else 24 hours, or None for
    `full_every: always`, at every check."""
    path = workspace.root / "sources" / f"{name}.yaml"
    try:
        given = read_source(path).full_every if path.is_file() else ""
        if given == ALWAYS:
            return None
        return span(given) if given else FULL_EVERY
    except (CerebroError, ValueError):
        return FULL_EVERY


def _full_due(workspace: Cerebro, name: str, cursor: str | None) -> bool:
    """Whether a source that lists changes is listed in full this pass: with no cursor, never listed in full, last
    listed in full `full_every:` ago or more, by the app's clock, or at a time that clock has not reached yet, and
    at every check with `full_every: always`."""
    last = (workspace.store.state(FULL + name) or {}).get("at")
    every = _full_every(workspace, name)
    if cursor is None or not last or every is None:
        return True
    now = workspace.clock()
    try:
        at = datetime.fromisoformat(last)
    except ValueError:
        return True
    if at > now:  # stamped by a clock that ran ahead and was set back: waiting for it would wait that long
        return True
    try:
        return now >= at + every
    except OverflowError:  # a span past the calendar's end: never due by time
        return False


def _list(workspace: Cerebro, result: PassResult, scheduled: bool = False, only: str | None = None) -> list[_Listed]:
    """List each source that is due, as `_list_one` lists one, and keep its check for `status`. A source that fails
    keeps what it listed before the failure.

    `scheduled`, as under `serve`, checks a source only once its freshness window has passed since its last check,
    and reads a file only once it has been left alone for `schedule.settle`. Otherwise every source is checked now,
    as `serve --once` does, or the one named `only`. A check that stopped on an error, a rate limit among them, is
    tried again after its window, never at the next pass."""
    out = []
    now, settle = workspace.clock(), read_schedule(workspace.settings).settle
    for name, connector in workspace.connectors.items():
        if connector.lists_nothing:  # said once, when the workspace loaded
            continue
        if only is not None and name != only:
            continue
        last = workspace.store.state(CHECKED + name) or {}
        if scheduled and last.get("next") and datetime.fromisoformat(last["next"]) > now:
            continue  # not due: no request is made
        inner = getattr(connector, "inner", connector)
        if isinstance(inner, FilesConnector):
            inner.settle = settle if scheduled else None
        if workspace.extraction is None:
            result.skipped[f"sources/{name}"] = ("no facts layer, so its records wait and its cursor stays where it "
                                                 "is: plug in an extraction handler")
            continue
        listed = _Listed(connector, cursor=workspace.store.state(f"cursor:{name}"),
                         again=bool(workspace.store.state(AGAIN + name)))
        out.append(listed)
        result.step("check", "start", name)
        finished = _list_one(workspace, name, listed, result)
        if not finished:
            listed.full = ""  # a listing in full that stopped is not one: the next pass lists in full again
        if listed.again:
            _taken_again(workspace, listed, name, result)
        _checked(workspace, name, connector, finished)
        result.step("check", "end", name, mark="done" if finished else "failed",
                    why=result.failed.get(f"sources/{name}", ""), total=result.listed.get(name, 0))
    return out


def _list_one(workspace: Cerebro, name: str, listed: _Listed, result: PassResult) -> bool:
    """List one source from `listed.cursor`, every page. False, with the reason on the pass, if it failed. A source
    that lists changes is listed for changes alone, and in full when `_full_due` says so. A cursor that does not read
    is dropped, with a notice: a source that lists changes is listed in full at once, any other from the start on the
    next pass."""
    connector = listed.connector
    inner = getattr(connector, "inner", connector)
    changes = inner.lists_changes
    full = changes and _full_due(workspace, name, listed.cursor)
    relisted = False  # listed again in full, in this pass, after its cursor was dropped
    while True:
        if listed.cursor is None:
            result.from_start.add(name)
        try:
            if relisted:
                listed.cursor = inner.cursor_for(None, full=True)
            else:
                result.listed[name] = 0
                listed.full = stamp(workspace.clock()) if full else ""
                if changes:
                    listed.cursor = inner.cursor_for(listed.cursor, full=full)
            _articles(connector, listed, name, result)
            return True
        except Exception as error:  # noqa: BLE001 - a pass reports each failure and goes on
            if relisted or not isinstance(error, CursorError):
                result.failed[f"sources/{name}"] = f"{type(error).__name__}: {error}"
                if not listed.records:
                    result.listed.pop(name, None)  # it listed nothing, and failed: say so once, as a failure
                return False
            update_state(workspace.store, f"cursor:{name}", lambda _: None)
            if not changes:
                listed.cursor, listed.moved = None, False
                result.notices.append(f"sources/{name}: its cursor did not read ({error}), so it was cleared: the "
                                      "next pass lists the source from the start")
                return False
            result.notices.append(f"sources/{name}: its cursor did not read ({error}), so it was dropped and the "
                                  "source was listed in full")
            listed.records, listed.removed, listed.cursor, listed.moved, listed.back = [], [], None, False, []
            listed.full, result.listed[name] = stamp(workspace.clock()), 0
            relisted = True


def _articles(connector: Connector, listed: _Listed, name: str, result: PassResult) -> None:
    """List one source every page from `listed.cursor`, adding what it gives to `listed`."""
    more = True
    while more:
        since = listed.cursor
        listing = connector.list(since=since)
        listed.back = listed.back or ([since] if listing.again else [])
        result.listed[name] += len(listing.records)
        said = (f"sources/{name}: {line}" for line in listing.notices)  # a source may give each again on every page
        result.warnings += [line for line in dict.fromkeys(said) if line not in result.warnings]
        listed.records += listing.records
        listed.removed += listing.removed
        listed.cursor, listed.moved = listing.cursor, True
        more = listing.more and listing.cursor is not None


def _taken_again(workspace: Cerebro, listed: _Listed, name: str, result: PassResult) -> None:
    """Of what a source listed again from a cursor kept back, drop each record already taken in as it stands, as
    `_taken_as_it_is` tells, and each that waits for another try, which a pass fetches anyway. A record the listing
    gave twice is kept once, as it was given last. So none is extracted twice."""
    waiting = set(workspace.store.state(f"failed:{name}") or [])
    last = {r.id: r for r in listed.records}
    fresh = [r for r in last.values() if r.id not in waiting and not _taken_as_it_is(workspace, listed.connector, r)]
    if name in result.listed:
        result.listed[name] -= len(listed.records) - len(fresh)
    listed.records = fresh


def _taken_as_it_is(workspace: Cerebro, connector: Connector, record: Record) -> bool:
    """Whether a listed record was last taken in as it stands: at the revision it gives, or, when it gives none,
    with the body it holds now, fetched whole and masked, as `revision_of` tells it. One the source cannot give now
    is not, so a pass fetches it again."""
    saved = saved_revision(workspace.store, connector.name, record.id)
    if not saved or record.revision:
        return bool(saved) and saved == record.revision
    try:
        return revision_of(connector.get(record.id)) == saved
    except Exception:  # noqa: BLE001 - extracting it fetches it again, and reports a failure there
        return False


def _keep_cursor(workspace: Cerebro, listed: _Listed, name: str) -> None:
    """Keep where a source's next listing starts: where the first page that left a record out for its id was listed
    from, so that record is listed again, or else where this listing ended."""
    if not listed.moved:
        return
    workspace.store.set_state(f"cursor:{name}", listed.back[0] if listed.back else listed.cursor)
    if bool(listed.back) != listed.again:
        workspace.store.set_state(AGAIN + name, True if listed.back else None)


def hold_sources(workspace: Cerebro, result: PassResult, scheduled: bool = False) -> int:
    """List every source now and hold every record it lists, extracting none, as a first run reads its sources before
    anything is decided: each listing is kept without a body under `held:<source>`, as a pass holds a record out of
    scope, and the source's cursor moves. A later pass tests each held record again and extracts those in scope then.
    Gives how many records were listed. Another process that lists the sources now leaves this one to it.
    `scheduled`, as under `serve`, checks a source only once its freshness window has passed."""
    if _sources_held(workspace, result):
        return 0
    _take_ours(workspace, result)
    with _holding(workspace, [_Reader()], result.token):  # type: ignore[list-item]
        _own_names_follow(workspace, result)
        sources = _list(workspace, result, scheduled)
        for listed in sources:
            name, held_key = listed.connector.name, f"held:{listed.connector.name}"
            gone, fresh = set(listed.removed), {record.id for record in listed.records}
            now = stamp(workspace.clock())
            keep_own(workspace.store, name, listed.records, workspace.ours, gone)
            with workspace.store.exclusive():
                was = workspace.store.state(held_key) or []
                first = {row["id"]: row.get("listed_at") or now for row in was}
                kept = [row for row in was if row["id"] not in gone and row["id"] not in fresh]
                kept += [asdict(replace(record, body=None)) | {"listed_at": first.get(record.id, now)}
                         for record in listed.records]
                workspace.store.set_state(held_key, kept[-HELD_MAX:])
                _keep_cursor(workspace, listed, name)
                if listed.full:
                    workspace.store.set_state(FULL + name, {"at": listed.full})
    return sum(len(listed.records) for listed in sources)


def _take_ours(workspace: Cerebro, result: PassResult) -> None:
    """Tell the workspace's own side as its files give it now, to Cerebro and to every connector, so a domain or
    an address made its own, or no longer its own, holds from this pass on, under `serve` too, with no restart. A
    file that does not read keeps the side read before, with a notice, and the pass goes on."""
    try:
        ours = read_ours(workspace.root)
    except CerebroError as error:
        result.notices.append(f"{error}: who is on the workspace's own side stays as it was read before")
        return
    if ours != workspace.ours:
        workspace.ours = ours
        for connector in workspace.connectors.values():
            getattr(connector, "inner", connector).take_ours(ours)


def _own_names_follow(workspace: Cerebro, result: PassResult, only: str | None = None) -> None:
    """When the workspace's own side changed since a source's names were read, list that source in full and read
    them again, before any source is listed for the pass and before any curator runs, so a person named on a record
    listed before the change is weighed by it. Nothing else of the listing is kept. A source never listed has no
    names to read again. One that fails keeps the names it gave, and the next pass tries it alone again."""
    if workspace.extraction is None:
        return
    now = weighed(workspace.ours)
    for name, connector in workspace.connectors.items():
        if connector.lists_nothing or only is not None and name != only:
            continue
        was = workspace.store.state(f"{OWN_WEIGHED}:{name}")
        if was != now and (was is None or _reweigh(workspace, name, connector, result)):
            workspace.store.set_state(f"{OWN_WEIGHED}:{name}", now)


def _reweigh(workspace: Cerebro, name: str, connector: Connector, result: PassResult) -> bool:
    """List one source in full and read again who its records name as the workspace's own. False, with a notice,
    if it failed."""
    inner = getattr(connector, "inner", connector)
    records: list[Record] = []
    try:
        cursor, more = inner.cursor_for(None, full=True) if inner.lists_changes else None, True
        while more:
            listing = connector.list(since=cursor)
            records += listing.records
            cursor, more = listing.cursor, listing.more and listing.cursor is not None
    except Exception as error:  # noqa: BLE001 - a pass reports each failure and goes on
        result.notices.append(f"sources/{name}: the workspace's own side changed, and listing it in full to read who "
                              f"it names as its own failed ({type(error).__name__}: {error}): the next pass tries again")
        return False
    reweigh_own(workspace.store, name, records, workspace.ours)
    return True


# -- curate --------------------------------------------------------------------------------------------------------


def first_list(workspace: Cerebro, agent: str) -> list[str]:
    """The ids of the admits of a curator's first list that are awaiting review: what the curator `agent`, by its id,
    filed in proposal mode for a person into its own collection, as `curate.proposing` has it. Empty for a curator
    past its first run."""
    if not proposing(workspace.store, agent):
        return []
    own = workspace.agent(agent).target + "/"
    return [p.id for p in proposals(workspace.store, "waiting")
            if p.kind == "article.admit" and p.by.name == agent and p.target.startswith(own)]


def first_list_line(agent: str, waiting: int) -> str:
    """The line that says a curator's first list is awaiting review, and how to decide it."""
    rows = f"{waiting} admit proposal{'' if waiting == 1 else 's'}"
    return (f"the first list of {agent}: {rows} awaiting review, and only its articles wait: decide with cerebro "
            f"review --accept-from agents/{agent}.md, or run cerebro serve --once at a terminal")


def curators_due(workspace: Cerebro, result: PassResult) -> list[tuple[Agent, str | None]]:
    """The curators a pass runs now, in order, each with the schedule word it runs for, or None for a change; why
    each other one waits goes on `result.skipped`. It takes each due one's lease, so `compile --dry-run` calls it on a
    store apart."""
    curators = curator_order([agent for agent in workspace.agents if agent.role == "curator"])
    listed = {agent.id: ids for agent in curators if (ids := first_list(workspace, agent.id))}
    for id, ids in listed.items():  # its list is decided first: running it again would cost a call and add nothing
        result.skipped[id] = "its " + first_list_line(id, len(ids)).removeprefix("the ")
    session = workspace.store.state(FIRST_RUN_KEY)
    cut = set(session.get("cut") or []) if isinstance(session, dict) and session.get("status") == "done" else set()
    for agent in curators:  # the first run's cap cut it: it runs when a person asks for it, never on its own
        if agent.id in cut and agent.id not in listed:
            listed[agent.id] = []
            result.skipped[agent.id] = (f"the first run's budget cut it before it ran: cerebro agents run {agent.id} "
                                        "asks it now")
    result.notices += close_moot(workspace.store, {agent.target for agent in curators})
    woken = {agent.id for agent in curators if _beat(workspace, agent).again or new_yes(workspace.store, agent)}
    now, zone = workspace.clock(), zone_of(workspace.settings)
    for agent in curators:  # a scheduled run does not wait, as a writer's does not
        word, _, held = _wake(agent, _words(workspace, agent), _beat(workspace, agent), now, zone)
        if agent.id not in listed and word is None and not held and (why := waits_after(workspace, agent, agent.target)):
            listed[agent.id] = []
            result.skipped[agent.id] = why
    return _due(workspace, [agent for agent in curators if agent.id not in listed], result, woken)


def _curate(workspace: Cerebro, result: PassResult) -> None:
    _going(workspace)
    due = curators_due(workspace, result)
    if due:
        result.tell(f"stage     curate: {len(due)} curator{'' if len(due) == 1 else 's'} due, one at a time")
        result.step("agents", "open", total=len(due))
    with _holding(workspace, [agent for agent, _ in due], result.token):
        _curate_due(workspace, due, result)
    warned = [first_list_line(agent.id, len(ids)) for agent, _ in due if (ids := first_list(workspace, agent.id))]
    if warned:  # the line names review --accept-from, which takes what was shown by now
        from .first_run import stamp_shown  # imported here: the first run builds on this module

        stamp_shown(workspace)
    result.warnings += warned


def run_curators(workspace: Cerebro, agents: list[Agent], result: PassResult) -> None:
    """Run each curator of `agents` in turn, outside a pass's own choice of what is due, holding their leases, as the
    first run runs them, whether or not its evidence moved. Each turn
    is kept as a pass keeps it, its run on `result` and its heartbeat in the store, so a later pass does not run it
    again before it is due. A run that did not fail is its schedule's run of the period it began in, so a pass the
    same day does not run it again for its schedule."""
    with _holding(workspace, agents, result.token):
        _curate_due(workspace, [(agent, None) for agent in agents], result, changed_only=False, spends=True)


def _curate_due(workspace: Cerebro, due: list[tuple[Agent, str | None]], result: PassResult,
                changed_only: bool | None = None, spends: bool = False) -> None:
    """Run each due curator in turn, so each weighs what the ones before it admitted. `changed_only`, when given,
    says whether a curator whose evidence has not moved is skipped, in place of its schedule word saying so. With
    `spends`, a run that did not fail spends the period of its schedule as a scheduled run does."""
    for agent, word in due:
        curated, error, began = None, None, workspace.clock()
        result.step("agents", "start", agent.id)
        try:
            yes = _beat(workspace, agent).again or new_yes(workspace.store, agent)
            woken = word is None and yes
            # a yes to carry out runs it, though a decide measured as the curator's own left its view as seen
            curated = workspace.curate(agent, changed_only=word is None and not woken if changed_only is None
                                       else changed_only,
                                       wake=word or "change",
                                       accepted=yes_left(workspace.store, agent.target) if yes else ())
        except Stopped:
            result.step("agents", "end", agent.id, mark="skipped", text="serve stopped before it ended")
            raise  # its heartbeat stands, so it is due again, and nothing after it runs
        except Exception as failed:  # noqa: BLE001 - a pass reports each failure and goes on
            error = failed
        run = curated.run if curated is not None else None
        broke = run is not None and run.status not in ("done", WROTE_NOTHING, "budget spent")
        # a run the runtime gave back as failed, a server error, a timeout or a dropped connection, fails as a raise does
        why = f"{type(error).__name__}: {error}" if error is not None else (run.note or "its run failed") if (
            broke and run is not None) else ""
        if isinstance(error, HeldForRoom) or error is not None and _paused(workspace, "compilation", result, error):
            why = "held for room under the cap" if isinstance(error, HeldForRoom) else "waits for the daily cap"
            if not isinstance(error, HeldForRoom):
                result.waiting_runs.append(agent.id)
            result.held[agent.id] = str(error)  # no failure: a first run reads it here as a curator that did not finish
            result.tell(f"ran       {agent.id}: {why}, for the next pass")
            result.step("agents", "end", agent.id, mark="skipped", text=why)
            continue  # its heartbeat stands, so it is due again on the next pass
        stopped = _settle_curated(workspace, agent, curated, why, result)
        if why or (curated is not None and curated.run is not None):
            _waited(workspace, agent, agent.target, failed=bool(why), stopped=stopped)
        if curated is not None and curated.run is not None:
            _again(workspace, agent, curated)
        if word is None and spends and not why and curated is not None and curated.run is not None:
            word = next((w for w in _words(workspace, agent) if w in SCHEDULES), None)
        _ran(workspace, agent, word, curated.run if curated else None, bool(why), began)


def _again(workspace: Cerebro, agent: Agent, curated: Any) -> None:
    """Keep whether a curator runs again on the next pass: its run stopped at its budget once it carried out a yes,
    and accepted proposals of its collection are left."""
    again = curated.run.status == "budget spent" and bool(curated.carried) and bool(yes_left(workspace.store,
                                                                                              agent.target))
    beat = _beat(workspace, agent)
    if beat.again != again:
        workspace.store.set_state(f"heartbeat:{agent.id}", asdict(replace(beat, again=again)))


def _settle_curated(workspace: Cerebro, agent: Agent, curated: Any, why: str, result: PassResult) -> str:
    """Say how a curator's run ended and keep it on `result`: failed for `why`, as it raised and left no `curated` or
    its run ended in an error, skipped, or what it decided and cost. Gives the budget key of a run that stopped at its
    budget with nothing decided, or empty."""
    if why:
        cost = "" if curated is None else f", {_usd(workspace, [curated.run])}"
        result.tell(f"ran       {agent.id}: failed, {why}{cost}")
        result.step("agents", "end", agent.id, mark="failed", why=why)
        result.failed[agent.id] = why
        if curated is not None:  # its run ended in an error
            result.runs.append(curated.run)
            result.warnings += [f"{agent.id}: {note}" for note in curated.notes]
        return ""
    if curated.skipped:
        result.tell(f"ran       {agent.id}: skipped, nothing it reads has moved")
        result.step("agents", "end", agent.id, mark="skipped", text="nothing it reads has moved")
        result.skipped[agent.id] = "nothing it reads has moved since its last run"
        result.due.pop(agent.id, None)
        return ""
    kinds = [kind for kind in ("admitted", "archived", "merged", "split", "linked", "carried", "proposed")
             if getattr(curated, kind)]
    did = ", ".join(f"{kind.replace('carried', 'carried out')} {len(getattr(curated, kind))}" for kind in kinds)
    runs, said, stopped = ([curated.run] if curated.run is not None else []), did or "changed nothing", ""
    if not did and curated.run is not None and curated.run.status == "budget spent":  # a run that ran
        stopped = _budget_key(curated.run)
        said = (f"stopped at its budget for {agent.target} collection with nothing decided, after reading "
                f"{curated.read} {'item' if curated.read == 1 else 'items'} of its evidence: raise {stopped} "
                f"in agents/{agent.id}.md")
        result.warnings.append(f"{agent.id}: {said}")
    result.tell(f"ran       {agent.id}: {said}, {_usd(workspace, runs)}")
    row = ", ".join(f"{kind} {len(getattr(curated, kind))}" for kind in kinds if kind != "carried") if did else said
    result.step("agents", "end", agent.id, mark="warning" if stopped else "done", text=row, cost=_usd(workspace, runs))
    if curated.run is not None:
        result.runs.append(curated.run)
        result.warnings += [f"{agent.id}: {note}" for note in curated.notes]
        if not curated.read:
            result.empty.append(agent.id)
    return stopped


# -- what a pass does with a record --------------------------------------------------------------------------------


TAKE, HOLD, DROP, NOTHING, REFUSE = "take", "hold", "drop", "nothing", "refuse"
FIRST_HOLD = "the first run holds it until its list is decided"  # why a record is held while the first run is undecided
OLDER_HOLD = f"the first run's build reads the last {RECENT_DAYS} days: a later pass reads it"


@dataclass(frozen=True)
class Decision:
    """What a pass does with one record, and why, in words a person reads. `act` is TAKE, get it and read it into
    facts; HOLD; DROP, an id left to try again that its source says is gone; NOTHING, a record its source reads for
    nothing, `records` or `none`; or REFUSE, the cap, which `error` is."""

    act: str
    why: str = ""
    error: BudgetSpent | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Ahead:
    """What a pass reads of one source before it decides its records: the ids its listing says are gone, whether the
    first run holds what was held before, the collections a curator keeps, the ids held before, and, while a first
    run's build runs, the first day it reads, the records an admit a person accepted names, by source and id, and the
    articles those admits made."""

    gone: Collection[str]
    discovering: bool
    curated: Collection[str]
    first: Collection[str] = ()
    since: str = ""
    asked: Collection[tuple[str, str]] = ()
    approved: Collection[str] = ()


def ahead_of(workspace: Cerebro) -> Ahead:
    """What a pass reads once before it decides any record: whether the first run holds what was held before, as it
    does while a first-run session is stored and not done, the collections a curator keeps, and, while a first run's
    build runs, the first day it reads, the records a person asked for and the articles a person approved."""
    session = workspace.store.state(FIRST_RUN_KEY)
    since = first_run_since(workspace.store) if IN_FIRST_BUILD.get() else ""
    yes = [p for p in (proposals(workspace.store, ACCEPTED) if since else [])
           if p.kind == "article.admit" and p.decided_by is not None and p.decided_by.kind == "person"]
    asked = {(str(ref.get("source")), str(ref.get("id"))) for p in yes
             if isinstance(ref := p.payload.get("record"), dict)}
    approved = {str(p.applied["article"]) for p in yes if p.applied.get("article")}
    return Ahead((), isinstance(session, dict) and session.get("status") != "done",
                 {agent.target for agent in workspace.agents if agent.role == "curator"} | {
                     id.split("/")[0] for id in refused_ids(workspace.refused) if id.count("/") == 1 and id.endswith("/curator")},
                 since=since, asked=asked, approved=approved)


def decide(workspace: Cerebro, record: Record | Any, ahead: Ahead | None = None) -> Decision:
    """What a pass does with one record, changing nothing. Anything but a record is the id of one the last pass left to try again: dropped
    when its source says it is gone, else got and read. A record listed, or held before, is held while the first run
    holds it or while it is out of scope, lands only its fields when its source reads it for that or for nothing,
    is held while a first run's build runs when it is dated before the first day the build reads, no admit a
    person accepted names it and it binds no article of its own collection such an admit made, and is got and read
    otherwise. With no `ahead` the record is one the pass got whole: its source reads it for nothing,
    the extraction cap refuses it, or it is read into facts."""
    if not isinstance(record, Record):
        return Decision(DROP) if ahead is not None and record in ahead.gone else Decision(TAKE)
    if ahead is None:
        if record.extract in ("records", "none"):  # its source maps fields or reads it for nothing; no facts come of it
            return Decision(NOTHING, f"its source extracts it into {record.extract}")
        try:
            workspace.ledger.check("extraction", workspace.clock(), workspace.models["extraction"])
        except BudgetSpent as error:
            return Decision(REFUSE, f"{type(error).__name__}: {error}", error)
        return Decision(TAKE)
    if ahead.discovering and record.id in ahead.first:  # held for the first run's discovery: it waits for the decision
        return Decision(HOLD, FIRST_HOLD)
    if not in_scope(workspace.store, record, curated=ahead.curated):
        return Decision(HOLD, "it binds only articles out of the working set" if reached(workspace.store, record) else
                        f"no article binds it, so it waits for the curator of {record.into}")
    if record.extract in ("records", "none"):
        return Decision(NOTHING, f"its source extracts it into {record.extract}")
    if before_cut(record.at, ahead.since) and (record.source, record.id) not in ahead.asked and not (
            record.into and any(id in ahead.approved for id in own_articles(workspace.store, record))):
        return Decision(HOLD, OLDER_HOLD)
    return Decision(TAKE)


def past_bound(held: list[Any]) -> list[Any]:
    """The held records a pass lets go, the oldest first: those past `HELD_MAX`. Each comes back when its source lists
    it again."""
    return held[:-HELD_MAX] if len(held) > HELD_MAX else []


# -- extract and link ----------------------------------------------------------------------------------------------


class _AheadStopped(CerebroError):
    """The calls asked ahead of records' turns stop: the pass stops, or a cap is reached."""


class _Early:
    """The model calls of each record's extraction that read the record alone, asked ahead of its turn, for up to
    `compile.concurrency` records after the one at its turn, while the records are extracted one at a time, in order:
    what lands, and in what order, is decided at each record's turn. The answers wait in the facts layer's
    call log, which the turn replays. Each record is got whole here, on the pass's own thread, and its turn reads the
    record got here. Each call ahead is checked against the ledger before it starts, and once a cap is met none
    starts; what it billed is a row of the ledger, under its record, the moment it ends, so every spender sees it. A
    pass can so end over a cap by the calls under way when the cap was met, as a cap is a soft limit."""

    def __init__(self, workspace: Cerebro, connector: Connector) -> None:
        self.workspace, self.connector = workspace, connector
        self.handler = getattr(workspace.extraction, "prepare", None) and workspace.extraction
        self.model = workspace.models["extraction"]
        width = max(1, int((workspace.settings.get("compile") or {}).get("concurrency") or 8))
        self.lead = width if self.handler and width > 1 else 0
        self.pool = ThreadPoolExecutor(max_workers=self.lead) if self.lead else None
        self.started: dict[str, tuple[Record, Any]] = {}  # by record id: the record got, and its calls ahead
        self.order: dict[str, int] = {}  # each record's place in the pass
        self.tried: set[str] = set()
        self.stopped = False  # the pass stops, or a cap is met: no call goes ahead

    def ahead(self, going: list[Record], at: int, ahead: Ahead) -> None:
        """At the turn of `going[at]`, ask the calls of the next records the pass will extract, up to the width, a
        record whose calls are done counting until its turn takes it."""
        if self.pool is None:
            return
        if not self.order:
            self.order = {record.id: n for n, record in enumerate(going)}
        for id in [id for id in self.started if self.order.get(id, -1) < at]:
            self.started.pop(id)[1].exception()  # its turn passed without it
        for record in going[at + 1:]:
            if self.stopped or len(self.started) >= self.lead:
                return
            if record.id in self.tried:
                continue
            self.tried.add(record.id)
            if record.extract in ("records", "none") or decide(self.workspace, record, ahead).act != TAKE:
                continue
            try:
                whole = self.connector.get(record.id)
            except Exception:  # noqa: BLE001, S112 - its turn gets it again, and says what failed
                continue
            if whole.extract in ("records", "none"):
                continue
            hooks = _Hooks(self._check, lambda name, given, taken, id=record.id: self._billed(id, name, given, taken))
            self.started[record.id] = (whole, self.pool.submit(self.handler.prepare, whole, hooks))

    def _check(self, _: dict[str, Any]) -> None:
        """Before a call ahead: none starts once the pass stops or a cap is met."""
        workspace = self.workspace
        if self.stopped or stopping(workspace.store):
            raise _AheadStopped("the calls asked ahead stop: each record is asked at its turn")
        try:
            workspace.ledger.check("extraction", workspace.clock(), self.model)
        except BudgetError as error:
            self.stopped = True
            raise _AheadStopped(str(error)) from error

    def _billed(self, id: str, name: str, given: int, taken: int) -> None:
        """After a call ahead: what it billed is a row of the ledger now, under its record, priced on the model
        `name`, its `provider/model`."""
        if given or taken:
            self.workspace.ledger.record_extracted(self.workspace.clock(), self.model,
                                                   Extracted([], given, taken, models={name: (given, taken)}), id)

    def take(self, id: str) -> Record | None:
        """The record got ahead of its turn, once its calls asked ahead are done, or None when none was."""
        found = self.started.get(id)
        if found is None:
            return None
        found[1].exception()  # waits for it: what it raised is asked, and said, at the turn
        return found[0]

    def done(self, id: str) -> None:
        self.started.pop(id, None)

    def close(self) -> None:
        """Stop asking ahead, and wait for the calls under way, each of which keeps what it billed."""
        self.stopped = True
        if self.pool is not None:
            self.pool.shutdown(wait=True)
            self.started.clear()
            self.pool = None


def _extract_all(workspace: Cerebro, sources: list[_Listed], result: PassResult) -> list[tuple[Fact, Record]]:
    """Retract what each source removed, extract what it listed that is in scope, what failed before, and what it
    held before that is in scope now, then keep its cursor, the records that wait for another try, and those held.
    While a workspace's first run is undecided, every record held before stays held, listed again or not: the first
    run holds them until its list is decided, and only a record listed new is tested for scope."""
    once = ahead_of(workspace)
    landed: list[tuple[Fact, Record]] = []
    result.step("distill", "open", total=len(sources))
    for listed in sources:
        _extract_source(workspace, listed, replace(once, gone=set(listed.removed)), result, landed)
    return landed


def _extract_source(workspace: Cerebro, listed: _Listed, ahead: Ahead, result: PassResult,
                    landed: list[tuple[Fact, Record]]) -> None:
    """Retract what one source removed, then extract what failed before, what it held before and what it listed,
    each as `decide` says, and keep what waits for another try and what is held."""
    name, connector = listed.connector.name, listed.connector
    read, facts = result.read, result.extracted
    result.step("distill", "start", name)
    for id in dict.fromkeys(listed.removed):
        _retract(workspace, name, id, result)
    tried = list(workspace.store.state(f"failed:{name}") or [])
    result.tried[f"failed:{name}"] = tried
    waiting = [id for id in tried if decide(workspace, id, ahead).act != DROP
               and not _extract(workspace, connector, id, result, landed)]
    fresh = {record.id for record in listed.records}
    was_held = workspace.store.state(f"held:{name}") or []
    now = stamp(workspace.clock())
    first = {row["id"]: row.get("listed_at") or now for row in was_held}  # when a pass first held each
    held = {row["id"]: record_from(row) for row in was_held if row["id"] not in ahead.gone and row["id"] not in fresh}
    handled = {*tried, *ahead.gone, *fresh, *(row["id"] for row in was_held)}
    ahead = replace(ahead, first=first)
    taken = {row["id"] for row in was_held if row.get("read") and row["id"] in held}
    for id in list(taken):  # read before, unchanged: it stays held only while it waits for its own article
        if not waits_for_own(workspace.store, held[id], ahead.curated):
            held.pop(id)
            taken.discard(id)
    going = [*(record for record in held.values() if record.id not in taken), *listed.records]
    early = _Early(workspace, connector)  # what is asked ahead is let go however the turns end
    try:
        for done, record in enumerate(going, 1):
            if result.on is not None:  # how far the pass is through its records, for the row that turns while it runs
                result.step("distill", "count", name, total=len(going), done=done - 1,
                            text=_n(result.extracted - facts, "fact"))
            decision = decide(workspace, record, ahead)
            if decision.act == HOLD:
                held[record.id] = replace(record, body=None)
                if decision.why == OLDER_HOLD:
                    result.older.add(f"{name}/{record.id}")
                continue
            held.pop(record.id, None)
            if waits_for_own(workspace.store, record, ahead.curated):  # read, and held for its own collection's curator
                held[record.id] = replace(record, body=None)
                taken.add(record.id)
            _keep_keys(workspace, name, record)
            if decision.act == NOTHING:  # its text is read for nothing: its fields, as listed, still go
                if not _extract_fields(workspace, name, record, result, landed):
                    waiting.append(record.id)
                continue
            early.ahead(going, done - 1, ahead)  # the next records' calls that read them alone, beside this one
            if not _extract(workspace, connector, record.id, result, landed, early):
                waiting.append(record.id)
    finally:
        early.close()
    rows = {id: asdict(record) | {"listed_at": first.get(record.id, now)} | ({"read": True} if record.id in taken else {})
            for id, record in held.items()}
    _keep_extracted(workspace, listed, handled, waiting, rows, result)
    result.step("distill", "end", name, mark="warning" if waiting else "done",
                text=f"{_n(result.read - read, 'record')} • {_n(result.extracted - facts, 'fact')}",
                why=f"{_n(len(waiting), 'record')} wait for another try" if waiting else "")


def _keep_extracted(workspace: Cerebro, listed: _Listed, handled: set[str], waiting: list[str],
                    rows: dict[str, dict[str, Any]], result: PassResult) -> None:
    """Keep a source's cursor once its records were handed over, the records that wait for another try, and the rows
    held, by id, at most `HELD_MAX`. What another pass added meanwhile stays: only what this one `handled` changes."""
    name = listed.connector.name
    failed_key, held_key = f"failed:{name}", f"held:{name}"
    with workspace.store.exclusive():
        _keep_cursor(workspace, listed, name)
        if listed.full:  # only once its records were taken in, as the cursor is
            workspace.store.set_state(FULL + name, {"at": listed.full})
        failed_now = workspace.store.state(failed_key) or []
        workspace.store.set_state(failed_key, list(dict.fromkeys(
            [*(id for id in failed_now if id not in handled), *waiting])))
        kept = [*(row for row in workspace.store.state(held_key) or []
                  if row["id"] not in handled and row["id"] not in rows), *rows.values()]
        workspace.store.set_state(held_key, kept)
        if past := past_bound(kept):
            result.notices.append(f"sources/{name}: {len(past)} held records past the bound of "
                                  f"{HELD_MAX} were let go, the oldest first: each comes back when its source "
                                  "lists it again")
            workspace.store.set_state(held_key, kept[len(past):])


def _extract(workspace: Cerebro, connector: Connector, id: str, result: PassResult,
             landed: list[tuple[Fact, Record]], early: _Early | None = None) -> bool:
    """Fetch one record whole and extract it, adding its facts to `landed`. False, with the reason on the pass, if it
    failed, or if it waits for a daily cap. The cap is checked once a record is known to need the model, and at the
    first such record it holds, the source is read no further this pass: that record and every one after it wait unread,
    one that needs no model included. Once serve is stopping, none is fetched: each waits for the next pass, as a
    failure does."""
    if stopping(workspace.store):
        return False
    if connector.name in result.paused:
        _waits(result, f"failed:{connector.name}", id)
        return False
    try:
        record = (early.take(id) if early is not None else None) or connector.get(id)
        if record.extract not in ("records", "none") and _paused(workspace, "extraction", result,
                                                                 model=workspace.models["extraction"]):
            result.paused.add(connector.name)
            _waits(result, f"failed:{connector.name}", id)
            return False
        try:
            return _extract_whole(workspace, record, result, landed, fields=True)
        finally:
            if early is not None:
                early.done(id)
    except Stopped:
        return False
    except Exception as error:  # noqa: BLE001 - one record's failure is reported, and the rest go on
        if _paused(workspace, "extraction", result, error):
            result.paused.add(connector.name)
            _waits(result, f"failed:{connector.name}", id)
        else:
            result.failed[f"sources/{connector.name}/{id}"] = f"{type(error).__name__}: {error}"
        return False


def _extract_whole(workspace: Cerebro, record: Record, result: PassResult, landed: list[tuple[Fact, Record]],
                   fields: bool = False) -> bool:
    """Extract one whole record, a source's or one an app kept, held to the extraction cap and its cost kept, and add
    its facts to `landed`, and with `fields`, as for a source's record, its fields too, as `_extract_fields` does.
    Raises what the handler or the cap raises on its text. False when its fields failed, which it reports, so the
    record waits for the next pass with its text."""
    decision = decide(workspace, record)
    if decision.act == NOTHING:
        return _extract_fields(workspace, record.source, record, result, landed) if fields else True
    if decision.error is not None:
        raise decision.error
    model = workspace.models["extraction"]
    extracted = Extracted([])  # a handler that raises spent what its error reports, or nothing
    try:
        extracted = workspace.extraction.extract(record)  # type: ignore[union-attr]
    except ExtractionFailed as error:
        extracted = error.spent
        raise
    finally:
        workspace.ledger.record_extracted(workspace.clock(), model, extracted, record.id)
    result.extracted += len(extracted.facts)
    result.read += 1
    landed += [(fact, record) for fact in extracted.facts]
    _keep_keys(workspace, record.source, record)
    return _extract_fields(workspace, record.source, record, result, landed) if fields else True


def _extract_fields(workspace: Cerebro, name: str, record: Record, result: PassResult,
                    landed: list[tuple[Fact, Record]]) -> bool:
    """Hand the fields a source gives a record in scope to the facts layer, one fact a field, each value as the
    source gave it, held to the extraction cap and its cost kept, and add their facts to `landed`. A record that gives
    no name of its own is named as the one article of its own it binds.
    A failure is reported and gives False, so the record waits for the next pass, and the pass goes on."""
    if workspace.extraction is None:
        return True
    if not record.fields:  # nothing to say, and a field fact standing from before no longer holds: no model call
        try:
            workspace.extraction.extract_fields(record)
        except Exception as error:  # noqa: BLE001 - one record's failure is reported, and the rest go on
            result.failed[f"sources/{name}/{record.id}#fields"] = f"{type(error).__name__}: {error}"
            return False
        return True
    worded = record
    own = own_articles(workspace.store, record)
    if not _gives_a_name(record) and len(own) == 1 and (article := workspace.store.article(own[0])) and article.identity.title:
        worded = replace(record, keys={**record.keys, "name": [article.identity.title]})  # never named by its id
    model, extracted = workspace.models["extraction"], Extracted([])
    try:
        workspace.ledger.check("extraction", workspace.clock(), model)
        extracted = workspace.extraction.extract_fields(worded)
    except ExtractionFailed as error:
        extracted = error.spent
        result.failed[f"sources/{name}/{record.id}#fields"] = f"{type(error).__name__}: {error}"
        return False
    except Exception as error:  # noqa: BLE001 - one record's failure is reported, and the rest go on
        result.failed[f"sources/{name}/{record.id}#fields"] = f"{type(error).__name__}: {error}"
        return False
    finally:
        workspace.ledger.record_extracted(workspace.clock(), model, extracted, record.id)
    result.extracted += len(extracted.facts)
    landed += [(fact, record) for fact in extracted.facts]
    return True


def _gives_a_name(record: Record) -> bool:
    """Whether a record gives a name of its own: a `name` key, or a `name`, `title` or `subject` field."""
    return any(str(v).strip() for v in record.keys.get("name", [])) or any(
        isinstance(record.fields.get(k), str) and record.fields[k].strip() for k in ("name", "title", "subject"))


KEPT_CURSOR, KEPT_FAILED = own_key("cursor", "kept"), own_key("failed", "kept")  # how far into the store's changes kept records were read


def _extract_kept(workspace: Cerebro, result: PassResult) -> list[tuple[Fact, Record]]:
    """Extract each record an app kept or revised since the last pass, and each that failed before, as a source's
    records are: held to the extraction cap, and tried again next pass when it fails. A revised record is handed over
    whole, so the facts layer replaces what its earlier revision gave, and keeps that as superseded. A revision a
    retract took back is never extracted, and what it gave while a retract landed, or what a pass that stopped
    before it checked left, is withdrawn."""
    landed: list[tuple[Fact, Record]] = []
    kinds = {kind for app in workspace.apps for kind in app.kinds}
    if not kinds or workspace.extraction is None:
        return landed
    changed, later = workspace.store.changed_since(workspace.store.state(KEPT_CURSOR) or 0)
    waiting, handled = [], set()
    fresh = [id for kind, id in changed if kind == "record"]
    result.tried[KEPT_FAILED] = list(workspace.store.state(KEPT_FAILED) or [])
    taken = retracted_records(workspace.store)
    for id in dict.fromkeys([*result.tried[KEPT_FAILED], *fresh]):
        handled.add(id)
        record = workspace.store.record(id)
        if record is None or record.source not in kinds:
            continue
        if was_retracted(record, taken):
            if not _withdraw_retracted(workspace, record, result):
                waiting.append(id)
            continue
        if record.extract not in ("records", "none") and "extraction" in result.stopped:  # it checks the ledger
            _waits(result, KEPT_FAILED, id)
            waiting.append(id)
            continue
        try:
            _extract_whole(workspace, record, result, landed)
        except Exception as error:  # noqa: BLE001 - one record's failure is reported, and the rest go on
            if _paused(workspace, "extraction", result, error):
                _waits(result, KEPT_FAILED, id)
            else:
                result.failed[f"{record.source}/{id}"] = f"{type(error).__name__}: {error}"
            waiting.append(id)
    with workspace.store.exclusive():  # waits for a tell's or a decide's write, so a retract filed in it is read here
        late = retracted_records(workspace.store) if landed else {}
        overtaken = {r.id: r for _, r in landed if was_retracted(r, late)}  # a retract landed while it was read
        for record in overtaken.values():
            if not _withdraw_retracted(workspace, record, result):
                waiting.append(record.id)
        # only what this pass handled leaves the lists, and the cursor only moves on
        update_state(workspace.store, KEPT_CURSOR, lambda now: max(now or 0, later))
        update_state(workspace.store, KEPT_FAILED, lambda now: list(dict.fromkeys(
            [*(id for id in now or [] if id not in handled), *waiting])))
    result.read -= len(overtaken)
    result.extracted -= sum(r.id in overtaken for _, r in landed)
    return [(f, r) for f, r in landed if r.id not in overtaken]


def _withdraw_retracted(workspace: Cerebro, record: Record, result: PassResult) -> bool:
    """Withdraw every fact a record a retract took back still has standing, as the retract's own sweep does, and say
    so: those a pass read as the retract landed, and those a pass that stopped before it checked left behind. A
    failure is reported and gives False, so the record waits for the next pass, and the pass goes on."""
    try:
        standing = workspace.extraction.facts(record=record.id, latest=False)  # type: ignore[union-attr]
        for fact in standing:
            workspace.extraction.retract(fact.id)  # type: ignore[union-attr]
    except Exception as error:  # noqa: BLE001 - one record's failure is reported, and the rest go on
        result.failed[f"{record.source}/{record.id}"] = f"{type(error).__name__}: {error}"
        return False
    if standing:
        result.retracted += len(standing)
        result.notices.append(_retracted_notice(record))
    return True


def _leave_no_retract_half_done(workspace: Cerebro, result: PassResult) -> None:
    """Finish each retract whose take-back stopped partway, and say what a record's withdrew."""
    withdrawn, failed = finish_retracts(workspace.store, workspace.extraction,  # type: ignore[arg-type]
                                        stamp(workspace.clock()))
    result.failed.update(failed)
    for id, facts in withdrawn.items():
        result.retracted += len(facts)
        if (record := workspace.store.record(id)) is not None:  # a fact's retract names no record
            result.notices.append(_retracted_notice(record))


def _retracted_notice(record: Record) -> str:
    return f"{record.source}/{record.id}: record {record.id} was retracted, so the facts read from it are withdrawn"


def _retry_told(workspace: Cerebro, result: PassResult) -> None:
    """Extract each told record whose facts did not come out when it was told, such as one the cap stopped. One
    that fails again waits for the next pass."""
    done = set()
    result.failed.update(settle_told(workspace.store, workspace.lookup.extraction, workspace.clock()))  # type: ignore[arg-type]
    result.tried[RETRY] = list(workspace.store.state(RETRY) or [])
    for id in result.tried[RETRY]:
        record = workspace.store.record(id)
        if record is None:
            done.add(id)
            continue
        if "extraction" in result.stopped:  # its extraction checks the ledger before any call
            _waits(result, RETRY, id)
            continue
        try:
            result.extracted += len(extract_told(workspace.store, workspace.lookup.extraction, record,  # type: ignore[arg-type]
                                                 stamp(workspace.clock()), clock=workspace.clock))
            result.read += 1
            done.add(id)
        except Claimed as held:  # its tell, or another process, extracts it now, and takes it off the list
            if held.until:
                result.skipped[f"tell/{id}"] = ("its tell or another pass extracts it now: this pass leaves it to "
                                                f"that, or to its claim to lapse at {held.until}")
            continue
        except Exception as error:  # noqa: BLE001 - one record's failure is reported, and the rest go on
            if _paused(workspace, "extraction", result, error):
                _waits(result, RETRY, id)
            else:
                result.failed[f"tell/{id}"] = f"{type(error).__name__}: {error}"
    if done:  # only what this pass handled leaves the list: a tell that failed meanwhile stays on it
        update_state(workspace.store, RETRY, lambda now: [id for id in now or [] if id not in done])


def _retract(workspace: Cerebro, name: str, id: str, result: PassResult) -> None:
    """Retract the facts of a record its source removed: its current ones, and those its earlier saves gave. A record
    whose id holds a stored value is named without it."""
    mask = workspace.mask_every()
    hidden = mask(str(id)) != str(id)
    _gone_at_source(workspace, name, id, result, hidden)
    _keep_keys(workspace, name, id)
    try:
        for fact in workspace.extraction.facts(source=name, record=id, latest=False):  # type: ignore[union-attr]
            workspace.extraction.retract(fact.id)  # type: ignore[union-attr]
            result.retracted += 1
    except Exception as error:  # noqa: BLE001 - one record's failure is reported, and the rest go on
        if hidden:
            result.failed[f"sources/{name}: a removed record whose id holds a value the secrets store keeps"] = \
                f"{type(error).__name__}: {mask(str(error))}"
        else:
            result.failed[f"sources/{name}/{id}"] = f"{type(error).__name__}: {error}"


def _gone_at_source(workspace: Cerebro, name: str, id: str, result: PassResult, hidden: bool = False) -> None:
    """A record its source removed leaves the working set with its own article, archived and kept, unless a caller
    said something of that article, an instruction or its own admit: then it stays, and its identity's `in_source`
    says it is gone there. One whose id is `hidden`, as it holds a stored value, is named without it."""
    # one never read, as one admitted and gone before a pass reads it, is found by its id under the keys its source issues
    keys = (workspace.store.state(f"keys:{name}") or {}).get(id) or workspace.connectors[name].IDENTITY_KEYS
    owned ={follow(article_id, redirects(workspace.store)) for key in keys for article_id in workspace.store.bound(key, id)}
    what = "the record it is bound to, whose id holds a value the secrets store keeps," if hidden else f"{id},"
    for article_id in sorted(p for p in owned if working(workspace.store, p)):
        article = workspace.store.article(article_id)
        if article is None:
            continue
        by = article.identity.admitted_by
        if (by is not None and by.kind == "person") or any(p.by.kind == "person"
                                                           for p in instructions_on(workspace.store, article_id)):
            article.identity.in_source = False
            workspace.store.set_identity(article.identity)
            result.notices.append(f"{article_id}: sources/{name} removed {what} and a caller's word stands on the article, "
                                  "so it stays, with in_source false")
        else:
            article.identity.status = "archived"
            workspace.store.set_identity(article.identity)
            drop_material(workspace.store, article_id)
            result.notices.append(f"{article_id}: sources/{name} removed {what} so the article is archived: admit it to "
                                  "bring it back")


def _keep_keys(workspace: Cerebro, name: str, record: Record | str) -> None:
    """Keep the keys of a record just extracted, or forget those of a record removed, under `keys:<source>`, the
    names of those it only carries under `carried:<source>`, and the name a field of it gives under `name:<source>`,
    so its facts can be linked again when an article is born, as landing linked them. A record with no keys keeps
    nothing. A removed id that is not text, such as a number, is taken by its text, as the store keeps it."""
    key = f"keys:{name}"
    kept = workspace.store.state(key) or {}
    if not isinstance(record, Record):
        record = str(record)
        if record in kept:
            update_state(workspace.store, key, lambda now: {k: v for k, v in (now or {}).items() if k != record})
        for each in (f"ours:{name}", f"carried:{name}", f"name:{name}"):
            if record in (workspace.store.state(each) or {}):
                update_state(workspace.store, each, lambda now: {k: v for k, v in (now or {}).items() if k != record})
        return
    if kept.get(record.id) != (linked_keys(record) or None):
        keys = linked_keys(record) if record.keys else None
        update_state(workspace.store, key, lambda now: {**{k: v for k, v in (now or {}).items() if k != record.id},
                                                    **({record.id: keys} if keys else {})})
    carried = [k for k in record.carried or () if k in record.keys]
    if (workspace.store.state(f"carried:{name}") or {}).get(record.id) != (carried or None):
        update_state(workspace.store, f"carried:{name}", lambda now: {
            **{k: v for k, v in (now or {}).items() if k != record.id}, **({record.id: carried} if carried else {})})
    named = field_name(record)
    if (workspace.store.state(f"name:{name}") or {}).get(record.id) != (named or None):
        update_state(workspace.store, f"name:{name}", lambda now: {
            **{k: v for k, v in (now or {}).items() if k != record.id}, **({record.id: named} if named else {})})
    # the names of the parties on it who are yours, as its source says, kept for the label its facts carry
    names = [str(n) for n in record.fields.get("ours") or [] if str(n).strip()]
    if (workspace.store.state(f"ours:{name}") or {}).get(record.id) != (names or None):
        update_state(workspace.store, f"ours:{name}", lambda now: {**{k: v for k, v in (now or {}).items()
                                                                 if k != record.id}, **({record.id: names} if names else {})})


LINKED = own_key("linked", "articles")  # each article every current fact was linked to, by a digest of its keys then


def _link(workspace: Cerebro, landed: list[tuple[Fact, Record]], result: PassResult) -> None:
    """Link each fact to its articles, in order, on this thread. The ties of up to `compile.concurrency` facts are
    worked out at once, so their judge calls wait side by side. With a decision model, a near match goes to its judge,
    each call held to the extraction cap as `budget.Held` holds it, under the fact's id, priced on the decision
    model. The working set is read once for every near match. A judge that fails is named in `failed`, and its fact
    is linked by its other ties. Results match one fact at a time only while `ties` reads nothing `link_ties` writes,
    and linking changes no article."""
    learned, model = stamp(workspace.clock()), workspace.models["decision"]
    width = max(1, int((workspace.settings.get("compile") or {}).get("concurrency") or 8))
    waiting, among = iter(landed), near_pool(workspace.store) if workspace.decider is not None else []
    asked: deque[tuple[Fact, Record, Future[list[tuple[str, str]]]]] = deque()

    def ask(fact: Fact, record: Record) -> tuple[Fact, Record, Future[list[tuple[str, str]]]]:
        judge = None if workspace.decider is None else DecisionJudge(
            Held(workspace.decider, workspace.ledger, "extraction", model, workspace.clock, fact.id))
        return fact, record, pool.submit(ties, workspace.store, fact, record, judge, among=among)

    with ThreadPoolExecutor(max_workers=width) as pool:
        asked.extend(ask(*pair) for pair in islice(waiting, width))
        while asked:
            fact, record, made = asked.popleft()
            try:
                if isinstance(failed := made.exception(), DecisionError):
                    result.failed[f"facts/{fact.id}"] = ("the near-match judge failed, so the fact is linked without "
                                                         f"it: {failed}")
                    made = pool.submit(ties, workspace.store, fact, record)
                result.linked += bool(link_ties(workspace.store, fact.id, made.result(), learned))
            except Exception as error:  # noqa: BLE001 - one fact's failure is reported, and the rest go on
                result.failed[f"facts/{fact.id}"] = f"{type(error).__name__}: {error}"
            asked.extend(ask(*pair) for pair in islice(waiting, 1))
    own_facts(workspace, landed)


def _link_born(workspace: Cerebro, result: PassResult) -> None:
    """Link the facts kept before an article was born, or before its keys last changed, to it. Each article not seen
    with its keys as they are, as `cerebro:linked:articles` keeps them, has every current fact linked again, by its
    record's kept keys and by name, as `identity.link` does with no judge, so it costs no model call, once the facts
    a record tied to it lose that tie, so a key taken off unties what it brought. A link already there is left."""
    if workspace.extraction is None:
        return
    store = workspace.store
    seen = store.state(LINKED) or {}
    every = {a.identity.id: (a.identity.status, hashlib.sha256(json.dumps(a.identity.keys, sort_keys=True).encode())
                             .hexdigest()[:16]) for c in store.collections() for a in store.query(c)}
    born = [id for id, (_, digest) in every.items() if seen.get(id) != digest]
    if not born:
        return
    changed = {id for id in born if id in seen and every[id][0] != "merged"}  # a merged one keeps its ties for a split
    taken_off: dict[str, set[tuple[str, str]]] = {id: set() for id in changed}
    for p in proposals(store, "accepted") if changed else []:
        if p.kind == "article.link" and p.target in changed:
            removed = ((p.applied or {}).get("keys") or {}).get("removed", {})
            taken_off[p.target] |= {(k, v.lower()) for k, vs in removed.items() for v in vs}
    moved, among = redirects(workspace.store), near_pool(store) if changed else []
    keys: dict[str, dict[str, dict[str, list[str]]]] = {}
    ours: dict[str, dict[str, list[str]]] = {}  # who each record marks as the workspace's own, which binds no namesake
    only: dict[str, dict[str, list[str]]] = {}  # the keys each record only carries
    named: dict[str, dict[str, str]] = {}  # the name a field of each record gives
    learned = stamp(workspace.clock())
    for fact in workspace.extraction.facts():
        if fact.source not in keys:  # a source's records, or an app's kind's
            keys[fact.source] = workspace.store.state(f"keys:{fact.source}") or {}
            ours[fact.source] = workspace.store.state(f"ours:{fact.source}") or {}
            only[fact.source] = workspace.store.state(f"carried:{fact.source}") or {}
            named[fact.source] = workspace.store.state(f"name:{fact.source}") or {}
        marked, own = ours[fact.source].get(fact.record), named[fact.source].get(fact.record)
        record = Record(id=fact.record, source=fact.source, revision="", keys=keys[fact.source].get(fact.record, {}),
                        fields={**({"ours": marked} if marked else {}), **({"name": own} if own else {})},
                        carried=only[fact.source].get(fact.record, []))
        if changed and fact.source != TOLD:  # a told fact is tied by its target, which no key of its record gives
            for id in changed - set(reached(store, record)):
                if RECORD in store.ties_of(fact.id).get(id, []):
                    store.unlink(fact.id, [id], RECORD)
            for id, how in kept_off(store, fact, record, among):
                if id in changed:  # `link` below ties it again by what still holds, such as a key
                    store.unlink(fact.id, [id], how)
            carried = {(k, v.lower()) for m in fact.mentions for k, vs in m.keys.items() for v in vs}
            for id in [id for id in changed if taken_off[id] & carried]:  # a judge's tie to it goes too: one row
                if id not in {tied for tied, _ in ties(store, fact, record)}:
                    store.unlink(fact.id, [id], SUBJECT)
                    store.unlink(fact.id, [id], MENTIONS)
        before = set(workspace.store.articles_of(fact.id))
        try:
            found = link(workspace.store, fact, record, at=learned)
        except Exception as error:  # noqa: BLE001 - one fact's failure is reported, and the rest go on
            result.failed[f"facts/{fact.id}"] = f"{type(error).__name__}: {error}"
            continue
        result.linked += bool({follow(id, moved) for id in found} - before)
    update_state(store, LINKED, lambda now: {**(now or {}), **{id: digest for id, (_, digest) in every.items()}})


# -- the order collections compile in ------------------------------------------------------------------------------


def _collection(article: str) -> str:
    """The collection an article is in: the folder it is directly in, "" for one in no folder."""
    return article.rpartition("/")[0]


def _link_path(value: Any) -> str:
    """The article a link value names, as `collection/name`, or "" when it is no path of that shape: a `../` and an
    `articles/` before it and a `.md` after it left out, and no space, tab or `@` in it."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    v = str(value).strip().removeprefix("../").removeprefix("articles/").removesuffix(".md")
    if not v or "/" not in v or any(c in v for c in " \t@") or posixpath.normpath(v) != v or v.startswith(("../", "/")):
        return ""
    return v


def _link_paths(article: Article, name: str) -> list[str]:
    """The articles a field of `article` links to, once each and in its order: `id` none, and any other field its
    value, or each item of a list."""
    if name.lower() == "id":
        return []
    held = fields(article)  # the field by its name, else in any case
    prop = held.get(name) or next((c for k, c in held.items() if k.lower() == name.lower()), None)
    value = prop.value if prop is not None else None
    raw = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(p for v in raw if (p := _link_path(v))))


def _anchored(value: Any) -> str:
    """The field a value of an evidence line reaches through: its own for `this.<field>`, or "" for any other
    value, `this` in any case."""
    if not isinstance(value, str):
        return ""
    head, dot, name = value.strip().partition(".")
    return name.strip() if head.lower() == "this" and dot and name.strip() else ""


def _folder(value: str) -> str:
    """A collection an evidence line names by its path, such as `articles/lab/notes/`, without its marks."""
    return value.strip().removeprefix("articles/").strip("/")


def _named(agent: Agent) -> set[str]:
    """The collections `agent`'s evidence names by path: a `query` or `left` from one, and an `articles` line's
    folders and the collection of each article it names, nested ones whole."""
    named: set[str] = set()
    for line in agent.evidence:
        args = line.args
        if line.kind in ("query", "left") and isinstance(args, dict) and isinstance(args.get("from"), str):
            named.add(_folder(args["from"]))
        elif line.kind == "articles":
            for value in args if isinstance(args, list) else [args]:
                if isinstance(value, str) and not _anchored(value) and value.strip().lower() != "this":
                    folder = _folder(value)
                    named |= {folder, folder.rpartition("/")[0]}  # a folder, or an article of its parent's
    return named - {""}


def _link_fields(agent: Agent) -> tuple[set[str], set[str]]:
    """The fields `agent`'s evidence reaches other articles through: an `articles` line's `this.<field>`, such as
    `this.linked`, and a query filtered on one; with the collections such a query reads from, which it reaches too."""
    found, froms = set(), set()
    for line in agent.evidence:
        args = line.args
        if line.kind == "articles":
            found |= {f for v in (args if isinstance(args, list) else [args]) if (f := _anchored(v))}
        elif line.kind in ("query", "left") and isinstance(args, dict):
            reached = {f for key, v in args.items() if key != "from" for f in [_anchored(v)] if f}
            found |= reached
            if reached and isinstance(args.get("from"), str):
                froms.add(_folder(args["from"]))
    return found, froms


def _writes_in(agent: Agent) -> str:
    """The collection the articles `agent` writes are in."""
    return _collection(agent.target) if fixed(agent) else agent.target


@dataclass
class CollectionOrder:
    """The order a pass compiles the collections in, each group a collection alone or the collections of a cycle in
    path order, and whether any evidence declares one."""

    groups: list[list[str]]
    declared: bool = False

    def cycles(self) -> list[list[str]]:
        return [g for g in self.groups if len(g) > 1]

    def line(self) -> str:
        """What a pass and `status` say of the order, or "" when no evidence declares one:
        `compile order: ones <-> twos (compiled once per pass, in path order), alls`."""
        if not self.declared:
            return ""
        return "compile order: " + ", ".join(" <-> ".join(g) + (" (compiled once per pass, in path order)"
                                                                 if len(g) > 1 else "") for g in self.groups)


def collection_order(store: Store, agents: list[Agent], colls: list[str]) -> CollectionOrder:
    """`colls` in the order a pass compiles them: a collection whose writers reach into another compiles after it.
    A writer reaches into a collection through a link of `this`, read from the links of every article of
    its collection to an article that exists or a merge leads to, or a query filtered on one; and by naming it by its
    path, in a `query`, a `left` or an `articles` line, so a digest waits for what it reads. A single-article agent
    orders only by what it names, never by its links. A subagent's evidence counts for the agent that lists it.

    Collections that reach into each other run one after the other in path order, before any collection that reaches
    into one of them, and otherwise it is path order, never a name. Path order is the order of the collections'
    article paths: a collection sorts by its name and a slash, so `a-b` comes before `a`."""
    colls = sorted(set(colls), key=_path_key)
    writers = [a for a in agents if a.role in WRITERS]
    targeted = {_writes_in(a) for a in writers}
    deps: dict[str, set[str]] = {c: set() for c in colls}

    def add(source: str, to: str) -> None:
        if to != source and to in deps and to in targeted:
            deps[source].add(to)

    link_fields: dict[str, set[str]] = {c: set() for c in colls}
    for writer in writers:
        source = _writes_in(writer)
        if source not in deps:
            continue
        for one in [writer, *(a for a in agents if a.id in writer.subagent_ids)]:
            for to in _named(one):
                add(source, to)
            if fixed(writer):
                continue  # one file's agent: no link of it orders anything
            reached, froms = _link_fields(one)
            link_fields[source] |= reached
            for to in froms:
                add(source, to)
    moved = redirects(store)
    for source in colls:
        if not link_fields[source]:
            continue
        reach = len([c for c in colls if c != source and c in targeted])
        for found in store.query(source):
            if len(deps[source]) >= reach:
                break  # no further article can add a collection
            article = store.article(found.identity.id)
            if article is None:
                continue
            for name in sorted(link_fields[source]):
                for path in _link_paths(article, name):
                    if store.article(to := follow(path, moved)) is not None:
                        add(source, _collection(to))
    groups = _in_order(colls, deps)
    return CollectionOrder([[c for c in g if c in targeted] for g in groups if any(c in targeted for c in g)],
                           declared=any(deps.values()))


def compile_groups(store: Store, agents: list[Agent], plan: list[tuple[Agent, str | None, str]],
                   ordered: CollectionOrder | None = None) -> list[list[int]]:
    """The indexes of `plan`, each an agent and the article it compiles, in the groups a pass compiles them in, one
    group after another: the articles of one collection together, and the next collection once the one before is done,
    in the order `collection_order` gives, or `ordered` when the pass worked it out already, so it reads the articles
    once. Collections that read each other come one after the other in path order."""
    colls = sorted({_collection(article) for _, _, article in plan}, key=_path_key)
    by_coll = {c: [n for n, (_, _, article) in enumerate(plan) if _collection(article) == c] for c in colls}
    ordered = ordered or collection_order(store, agents, colls)
    placed = [c for g in ordered.groups for c in g]
    return [by_coll[c] for c in placed] + [by_coll[c] for c in colls if c not in placed]


def _path_key(collection: str) -> str:
    """A collection's place in path order: its articles' paths all start with its name and a slash."""
    return collection + "/"


def _in_order(colls: list[str], deps: dict[str, set[str]]) -> list[list[str]]:
    """`colls`, in path order, grouped so each group comes after every collection it reaches into: a collection
    alone, or the collections of a cycle, which reach into each other."""
    reach_of = {c: _reachable(c, deps) for c in colls}
    group = {c: min((d for d in colls if d == c or (d in reach_of[c] and c in reach_of[d])), key=_path_key)
             for c in colls}  # its cycle, by its first collection in path order
    done: set[str] = set()
    groups: list[list[str]] = []
    while sum(len(g) for g in groups) < len(colls):
        ready = next(c for c in colls if c not in done and all(
            group[d] == group[c] or d in done for member in colls if group[member] == group[c]
            for d in deps[member]))
        members = [c for c in colls if group[c] == group[ready]]
        groups.append(members)
        done |= set(members)
    return groups


def _reachable(start: str, deps: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(deps[start])
    while stack:
        c = stack.pop()
        if c not in seen:
            seen.add(c)
            stack += deps[c]
    return seen


# -- compile -------------------------------------------------------------------------------------------------------


def writers_due(workspace: Cerebro, result: PassResult) -> list[tuple[Agent, str | None]]:
    """The writers a pass compiles now, each with the schedule word it runs for, or None for a change; why each other
    one waits goes on `result.skipped`. It takes each due one's lease, so `compile --dry-run` calls it on a store
    apart."""
    return _due(workspace, [agent for agent in workspace.agents if agent.role != "curator"], result)


def _compile(workspace: Cerebro, result: PassResult, served: bool = False,
             articles: Collection[str] | None = None) -> None:
    """Compile each article each due writer writes, or each of `articles` alone, one job per article, side by side.
    `served`, a pass under `serve`, holds each rebuild that new evidence alone asks for to its cooldown; a pass at a
    person's hand runs everything due."""
    _going(workspace)
    due = writers_due(workspace, result)
    with _holding(workspace, [agent for agent, _ in due], result.token):
        _compile_due(workspace, due, result, served, articles)


def _compile_due(workspace: Cerebro, due: list[tuple[Agent, str | None]], result: PassResult,
                 served: bool = False, articles: Collection[str] | None = None) -> None:
    """Compile each article of each due writer, or each of `articles` alone, side by side, one collection at a time,
    then keep each writer's turn as its heartbeat. `served` hands each change's compile the cooldown's hold, as
    `_hold` gives it."""
    plan, parts, changes = _plan(workspace, due, result, articles)
    for agent, _, article in plan:
        if fixed(agent) and workspace.store.article(article) is None:
            birth(workspace.store, agent, article, stamp(workspace.clock()))
    _link_born(workspace, result)
    began = workspace.clock()  # when this pass's compile turns began, by the workspace's clock
    if plan:
        # due, not to write: whether each is written is known only once its compile has read what it reads
        result.tell(f"stage     compile: {len(plan)} article{'' if len(plan) == 1 else 's'} due, side by side")
        result.step("compile", "open", total=len(plan))
    outcomes: list[tuple[Any, BaseException | None]] = [(None, None)] * len(plan)
    ordered = collection_order(workspace.store, workspace.agents, [_collection(article) for _, _, article in plan])
    if ordered.cycles():  # said once a pass, naming every cycle
        result.warnings.append(ordered.line())
    for group in compile_groups(workspace.store, workspace.agents, plan, ordered):  # one collection at a time, as ordered
        jobs = []
        for agent, word, article in (plan[n] for n in group):
            change, owed = word is None or (agent.id, article) in changes, (agent.id, article) in parts
            jobs.append(partial(_compile_one, workspace, result, agent, word, article, change=change, parts=owed,
                                served=served))
        for n, outcome in zip(group, _side_by_side(workspace, jobs)):
            outcomes[n] = outcome
    turns: dict[str, _Turn] = {}
    for (agent, word, article), (compiled, error) in zip(plan, outcomes):
        _settle(workspace, turns.setdefault(agent.id, _Turn(agent, word)), article, compiled, error, result)
    for turn in turns.values():
        agent = turn.agent
        if turn.skipped:
            result.skipped[agent.id] = turn.waits or unmoved(workspace, agent, [p for a, _, p in plan if a.id == agent.id])
            del result.due[agent.id]
        if (turn.held or turn.waits or turn.stopped) and turn.run is None and not turn.failed:
            continue  # held, stopped, cooling down or backing off: no run, so its heartbeat stands and it is due again
        # A run of it held, waiting for a cap or failed leaves its period due for those articles alone: the next pass
        # runs what did not finish, and an article that was written is not run again in the period.
        _ran(workspace, agent, None if turn.stopped else turn.word, turn.run, turn.failed, began, turn.wrote,
             turn.left if turn.held or turn.failed else None)


def _key(agent: Agent, article: str) -> str:
    """How a pass names an agent's run on an article: by the agent alone when the article is its own target."""
    return agent.id if article == agent.target else f"{agent.id} on {article}"


def _plan(workspace: Cerebro, due: list[tuple[Agent, str | None]], result: PassResult,
          only: Collection[str] | None = None) -> tuple[list[tuple[Agent, str | None, str]], set[tuple[str, str]],
                                                        set[tuple[str, str]]]:
    """The articles a pass compiles, each with its agent and the schedule word it runs for, in path order, of `only`
    alone when given; the articles whose next run runs only the parts that did not run; and those an agent on
    `change` runs for a change while its period is owed. Why a due writer runs on none goes on `result.skipped`, and
    one with none of `only` stays due, unsaid."""
    plan: list[tuple[Agent, str | None, str]] = []
    parts: set[tuple[str, str]] = set()
    changes: set[tuple[str, str]] = set()
    for agent, word in due:
        try:
            articles = sorted(targets(agent, workspace.lookup))
        except Exception as error:  # noqa: BLE001 - a pass reports each failure and goes on
            result.failed[agent.id] = f"{type(error).__name__}: {error}"
            result.due.pop(agent.id, None)
            _ran(workspace, agent, word, None, True)
            continue
        if only is not None and not (articles := [article for article in articles if article in only]):
            del result.due[agent.id]
            continue
        if not articles:  # an archived article of its own is left alone, and nothing is wrong
            result.skipped[agent.id] = left_alone(agent, workspace.lookup) or \
                f"{agent.target} holds no article yet: its curator admits them"
            del result.due[agent.id]
            continue
        if (left := _left(workspace, agent, word)) is not None:  # what its period's run did not finish, alone
            parts |= {(agent.id, article) for article, how in left.items() if how == "parts"}
            owed = [article for article in articles if article in left]
            rest = [article for article in articles if article not in left] if "change" in _words(workspace, agent) else []
            changes |= {(agent.id, article) for article in rest}  # pending narrows the schedule alone: a change still runs
            if not owed:
                _ran(workspace, agent, word, None, False)
                if not rest:
                    result.skipped[agent.id] = "the articles its scheduled run left to do are gone: its period is spent"
                    del result.due[agent.id]
                    continue
                word = None
            articles = [article for article in articles if article in left or article in rest]  # still in path order
        held = {article: why for article in articles if word is None and (why := waits_after(workspace, agent, article))}
        for article, why in held.items():
            result.skipped[_key(agent, article)] = why
        if len(held) == len(articles):
            del result.due[agent.id]
            continue
        plan += [(agent, word, article) for article in articles if article not in held]
    return plan, parts, changes


def _compile_one(workspace: Cerebro, result: PassResult, agent: Agent, word: str | None, article: str, *, change: bool,
                 parts: bool, served: bool) -> Any:
    """Compile one article, counting what grounding does on it, and say how it ended. A run for a `change` is held to
    the cooldown, and one owed only its `parts` runs only the part marked."""
    key = _key(agent, article)
    result.step("compile", "start", article)
    try:
        compiled = workspace.compile(agent, this=article, scheduled=not change and not parts,
                                 hold=_hold(workspace, agent, article, served) if change else None,
                                 only=_marked(workspace, agent, article) if parts else "")
    except HeldForRoom:
        result.step("compile", "end", article, mark="skipped", text="held for room under the cap")
        raise
    except Stopped:
        result.step("compile", "end", article, mark="skipped", text="serve stopped before it ended")
        raise
    except Exception as error:
        if _capped(error):
            result.step("compile", "end", article, mark="skipped", text="waits for the daily cap")
            raise
        result.tell(f"ran       {key}: failed, {type(error).__name__}: {error}")
        result.step("compile", "end", article, mark="failed", why=f"{agent.id}: {type(error).__name__}: {error}")
        raise
    result.tell(_compiled_line(workspace, key, compiled))
    result.step("compile", "end", article, **_compiled_step(agent, compiled))
    # what grounding did and what landing made safe, as counts, under the article's row
    safe = made_safe(len(compiled.cleaned)) if compiled.cleaned else ""
    if said := ", ".join(n for n in (counted(compiled.grounding), safe) if n):
        result.step("compile", "note", article, text=said)
    return compiled


@dataclass
class _Turn:
    """What one due writer's compiles in a pass came to, which its heartbeat keeps once they all end."""

    agent: Agent
    word: str | None
    run: Run | None = None  # the first run any of its compiles made
    skipped: bool = True  # every compile of it skipped
    failed: bool = False
    held: bool = False  # a compile of it held for room, waiting for a cap, or leaving parts to run next
    stopped: bool = False  # a stop of serve cut it off: due again, with what ran recorded
    waits: str = ""  # why it skipped: its cooldown, or a back-off after failed runs
    wrote: list[str] = field(default_factory=list)  # the articles its runs wrote on, or ran on and wrote nothing
    left: dict[str, str] = field(default_factory=dict)  # the articles its run did not finish, and how each runs next


def _settle(workspace: Cerebro, turn: _Turn, article: str, compiled: Any, error: BaseException | None,
            result: PassResult) -> None:
    """Keep on `result` and on `turn` how one compile of `turn.agent` on `article` ended: stopped, held for room,
    waiting for a daily cap, failed, skipped, or ran, with each of its parts that did not run and waits or runs next."""
    agent, key = turn.agent, _key(turn.agent, article)
    if isinstance(error, Stopped):
        turn.skipped, turn.stopped = False, True
        return
    if isinstance(error, HeldForRoom):
        result.held[key] = str(error)
        turn.skipped, turn.held, turn.left[article] = False, True, "whole"
        return
    if error is not None and _paused(workspace, "compilation", result, error):
        result.waiting_runs.append(key)  # no run started, so nothing was paid for: it waits, as a hold does
        turn.skipped, turn.held, turn.left[article] = False, True, "whole"
        return
    if error is not None:
        result.failed[key] = f"{type(error).__name__}: {error}"
        turn.skipped, turn.failed, turn.left[article] = False, True, "failed"
        return
    if not compiled.skipped and _parts_not_run(workspace, agent, article, compiled, result):
        turn.held, turn.left[article] = True, "parts"  # what ran stands: only what was refused runs next
    cut: list[str] = []  # the parts the budget cut while a daily cap holds: they wait, and fail nothing
    if not compiled.skipped and agent.subagent_ids and (cut := _parts_cut(workspace, article, compiled, result)):
        turn.held = True
        if compiled.article is not None:  # what the other parts returned landed: only the cut ones run next
            _run_again(workspace, agent, article, cut)
        # Nothing of the article landed, so nothing the other parts returned was kept: it runs whole next.
        turn.left[article] = "parts" if compiled.article is not None else "whole"
    if compiled.skipped:
        if compiled.rendered:
            result.notices.append(f"{article}: wrote {', '.join(compiled.rendered)} with no model, as its rendered: "
                                  "key says")
        if compiled.waiting:
            turn.waits = compiled.waiting
        return
    _settle_ran(workspace, turn, article, compiled, cut, result)


def _settle_ran(workspace: Cerebro, turn: _Turn, article: str, compiled: Any, cut: list[str], result: PassResult) -> None:
    """Keep how a compile that ran ended: what it wrote, warned and cost, and whether it failed, ran and wrote nothing,
    or waits for a daily cap, the parts `cut` while one holds among them."""
    agent, key = turn.agent, _key(turn.agent, article)
    turn.skipped = False
    run, shown = compiled.run, _shown(compiled)
    if run is not None:  # a stop at its budget with nothing landed waits, as a failed run does in compile
        _waited(workspace, agent, article, spent=_budget_key(run) if run.status == "budget spent" and run.id
                and compiled.article is None else "")  # a run the day's cap refused has no id: it never ran
    result.runs += compiled.runs or ([run] if run is not None else [])
    if compiled.article is not None or shown is not None and shown.status == WROTE_NOTHING:
        turn.wrote.append(article)  # its clock moves; a failed run keeps it
    turn.run = turn.run or run
    result.warnings += [f"{key}: {line}" for line in compiled.dropped]  # each line says what went and why
    # each line kept on its run's line, and counted for its end
    keep_grounding(workspace.store, run, None, compiled.cleaned)
    if compiled.grounding:
        result.grounding[key] = compiled.grounding
    if compiled.cleaned:
        result.cleaned[key] = len(compiled.cleaned)
    result.warnings += [f"{key}: {note}" for note in compiled.notes]
    if compiled.warning:
        result.warnings.append(compiled.warning)
    if compiled.article is not None:
        result.compiled.append(compiled.article.identity.id)
    elif article in turn.left and all(run.agent in cut for run in compiled.runs if run.id and (
            run.status == "failed" or run.status == "budget spent" and not run.result)):
        pass  # what the cap refused waits, and what ran was kept: no failure
    elif run is not None and run.status == "budget spent" and not run.result and _paused(
            workspace, "compilation", result):  # cut before any text while a daily cap holds: it waits, as refused
        result.waiting_runs.append(key)
        turn.held = True
        if agent.subagent_ids:  # its parts that finished stand: only the one cut runs next
            _run_again(workspace, agent, article, [run.agent])
        turn.left[article] = "parts" if agent.subagent_ids else "whole"
    elif shown is not None and shown.status == WROTE_NOTHING:  # its inputs are recorded, so it stays fresh
        result.ran[key] = shown.note or "it handed in nothing"
    elif shown is not None:
        result.failed[key] = failure(shown, agent.id)
        turn.failed, turn.left[article] = True, "failed"


def _side_by_side(workspace: Cerebro, jobs: list[Callable[[], Any]]) -> list[tuple[Any, BaseException | None]]:
    """Run each job from a thread pool, as wide as `compile.concurrency`, and give each outcome in order."""
    if not jobs:
        return []
    width = (workspace.settings.get("compile") or {}).get("concurrency", 8)
    with ThreadPoolExecutor(max_workers=width) as pool:
        futures = [pool.submit(job) for job in jobs]
    out: list[tuple[Any, BaseException | None]] = []
    for future in futures:
        try:
            out.append((future.result(), None))
        except (Exception, Stopped) as error:  # noqa: BLE001 - a pass reports each failure and goes on
            out.append((None, error))
    return out


HELD_PART = "{}: held for room under the cap ("  # how compile's note opens for a part it did not start


def _why_not_run(workspace: Cerebro, who: str) -> tuple[str, BudgetSpent | None]:
    """Why a run of the agent `who` was refused before it started, asked of the ledger now with that agent's own
    model: `unpriced`, `cap` with the ledger's refusal, or `held`, for room under a cap that is not reached."""
    agent = next(agent for agent in workspace.agents if agent.id == who)  # it ran, or was refused, in this compile
    try:
        workspace.ledger.check("compilation", workspace.clock(), workspace.model_of(agent))
    except UnpricedModel as error:
        return "unpriced", error
    except BudgetSpent as error:
        return "cap", error
    return "held", None


def _parts_not_run(workspace: Cerebro, agent: Agent, article: str, compiled: Any, result: PassResult,
                   when: str = "runs next pass") -> list[str]:
    """The parts of `agent`'s run on `article`, a writer with subagents, refused before they started, each said and
    counted by its kind: a daily cap reached waits and is counted in the stop line, a hold for room is a hold, and a
    model with no price fails with its reason. The composer's own refused run is a `Run` with no id, `held` or
    `budget spent`. A subagent's is not handed back as a run, so it is found by the note compile writes for it, which
    names the subagent first: that is the one line of compile's text read here, for which part, never for why. Gives
    the parts to run next, held or waiting: each is marked so the next compile, which runs only what is stale, runs
    it, and no part already paid for runs again. `when` says when a held part runs: a pass's own run owes it to the
    next pass."""
    found: dict[str, int | None] = {run.agent: None for run in compiled.runs
                                    if not run.id and run.status in ("held", "budget spent")}
    for n, note in enumerate(compiled.notes):
        found |= {sub: n for sub in agent.subagent_ids if note.startswith(HELD_PART.format(sub))}
    again = []
    for who, n in found.items():
        kind, error = _why_not_run(workspace, who)
        key = f"{who} on {article}"
        if kind == "unpriced":
            result.failed[key] = f"{type(error).__name__}: {error}"
            if n is not None:
                compiled.notes[n] = f"{who}: did not run, since {error}, so what it returned before stands"
            continue
        if kind == "cap" and _paused(workspace, "compilation", result, error):
            result.waiting_runs.append(key)
            if n is not None:
                compiled.notes[n] = (f"{who}: waits for the daily cap ({error}), so what it returned before stands, "
                                     "and it runs on the next pass")
        else:
            result.held[key] = f"held for room under the cap, {when}"
        again.append(who)
    if again:
        _run_again(workspace, agent, article, again)
    return again


def _parts_cut(workspace: Cerebro, article: str, compiled: Any, result: PassResult) -> list[str]:
    """The parts of a run on `article`, a writer with subagents, that the budget cut before they handed in text while a
    daily cap holds: each waits for the cap, as a part the cap refused does, and is marked so the next run runs it.
    None when a part failed: that fails the whole article, and the article's next try runs every part."""
    if any(run.id and run.status == "failed" for run in compiled.runs):
        return []
    cut = [run.agent for run in compiled.runs if run.id and run.status == "budget spent" and not run.result]
    waits = [who for who in cut if _paused(workspace, "compilation", result)]
    result.waiting_runs += [f"{who} on {article}" for who in waits]
    return waits


def _marked(workspace: Cerebro, agent: Agent, article: str) -> str:
    """The one subagent `_run_again` marked on `article`, when it is the only part marked, so the run that finishes the
    period runs it alone, and the composer after it only when what it returned moved: a part that finished and whose
    evidence moved since is not paid for again. Empty otherwise, and the run takes every part whose digest moved."""
    kept = workspace.store.state(material_key(article)) or {}
    marked = [sub for sub in agent.subagent_ids if (kept.get(sub) or {}).get("digest") == ""]
    composer = workspace.store.state(f"compiled:{agent.id}:{article}") is None and bool(agent.own)
    return marked[0] if len(marked) == 1 and not composer else ""


def _run_again(workspace: Cerebro, agent: Agent, article: str, who: list[str]) -> None:
    """Mark the parts `who` of `agent` on `article` as not run, as compile marks a part whose run the budget cut:
    a subagent's kept digest emptied, and the composer's record of what it last read let go, so the next compile runs
    each of them, and only them, and the composer once."""
    with workspace.store.exclusive():
        kept = workspace.store.state(material_key(article)) or {}
        subs = [sub for sub in who if sub in kept]
        if subs:
            workspace.store.set_state(material_key(article), {name: dict(held) | ({"digest": ""} if name in subs else {})
                                                       for name, held in kept.items()})
        if agent.id in who:
            workspace.store.set_state(f"compiled:{agent.id}:{article}", None)


def _usd(workspace: Cerebro, runs: list[Run], checks: float = 0.0) -> str:
    """What the runs cost on their agents' models, as a line says it, and once grounding's `checks` of their text
    cost anything, what the writing and the checks each cost, at places where the two add up to the whole."""
    total = 0.0
    for run in runs:
        agent = workspace.agent(run.agent) if hasattr(workspace, "agent") else None
        if agent is not None:
            spec = workspace.model_of(agent)
            total += cost(spec.provider, spec.model, run.usage)
    if not checks:
        return f"${total:.4f}"
    digits = spend_digits(total + checks, [total, checks], least=4)
    return f"${total + checks:.{digits}f} (writing ${total:.{digits}f}, checks ${checks:.{digits}f})"


def _shown(compiled: Any) -> Run | None:
    """The run a compile that wrote no article is told by: the first of its runs that failed, as one that ended in an
    error or with nothing after its budget was spent, so a composer's run that added nothing never hides a subagent's
    failure, else the run it ended on. A composed article's run that failed as a whole, as a subagent's failure fails
    it, is none of the runs the compile made, and is told by that, which names the subagent's run."""
    whole = compiled.run
    if whole is not None and whole.status == "failed" and all(run is not whole for run in compiled.runs or []):
        return whole
    failed = (run for run in compiled.runs or [] if run.status == "failed"
              or run.status == "budget spent" and run.result is None)
    return next(failed, compiled.run)


def failure(run: Run, agent: str) -> str:
    """Why a compile failed, as its `failed` line says it: the run, its agent when that is not `agent`, and
    what it ended with; or for a composed article's run that failed as a whole, which has no run id of its own, why
    and the subagent's run that failed it."""
    if not run.id:
        return run.note
    return (f"run {run.id}" + (f" of {run.agent}" if run.agent != agent else "") + f": {run.status}"
            + (f": {run.note}" if run.note else ""))


def _compiled_line(workspace: Cerebro, key: str, compiled: Any) -> str:
    """The line said as one compile ends: what it wrote, or why not, and what its runs cost."""
    runs = compiled.runs or ([compiled.run] if compiled.run is not None else [])
    if compiled.skipped:
        return f"ran       {key}: skipped, {compiled.waiting or 'nothing it reads has moved'}"
    if compiled.article is not None:
        article = compiled.article.identity.id
        return f"ran       {key}: wrote articles/{article}.md, {_usd(workspace, runs, compiled.checks)}"
    run = _shown(compiled)
    if run is not None and run.status == WROTE_NOTHING:  # its status is what the line says already: why, once
        return (f"ran       {key}: wrote nothing: {run.note or 'it handed in nothing'}, "
                f"{_usd(workspace, runs, compiled.checks)}")
    said = "no run" if run is None else run.status + (f": {run.note}" if run.note else "")
    return f"ran       {key}: {said}, {_usd(workspace, runs, compiled.checks)}"


def _compiled_step(agent: Agent, compiled: Any) -> dict[str, str]:
    """What a compile's event says as it ends: the article it wrote, or why it wrote none."""
    if compiled.skipped:
        if not compiled.waiting:  # nothing it reads moved: fresh, which the screen gives no line
            return {"mark": "fresh", "text": "nothing it reads has moved"}
        return {"mark": "skipped", "text": compiled.waiting}
    if compiled.article is not None:
        return {"mark": "done", "text": f"articles/{compiled.article.identity.id}.md"}
    run = _shown(compiled)
    if run is not None and run.status == WROTE_NOTHING:  # its inputs are recorded: no failure
        return {"mark": "ran", "text": f"ran and wrote nothing: {run.note or 'it handed in nothing'}"}
    said = "no run" if run is None else run.status + (f": {run.note}" if run.note else "")
    who = agent.id if run is None or run.agent == agent.id else run.agent
    return {"mark": "failed", "why": f"{who}: {said}"}


def _n(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def written(workspace: Cerebro, agent: Agent, article_id: str) -> bool:
    """Whether a run of `agent`, or of a subagent it lists, ever landed a part on `article_id`."""
    article = workspace.store.article(article_id)
    return article is not None and any(compilation.by in (agent.id, *agent.subagent_ids) for compilation in article.compilations)


def unmoved(workspace: Cerebro, agent: Agent, articles: list[str]) -> str:
    """Why a due agent was skipped when nothing it reads has moved, with what rebuilds it now."""
    again = f"cerebro agents run {agent.id} runs it again now"
    if not any(written(workspace, agent, article) for article in articles):
        return f"nothing it reads has moved since its last run, which wrote nothing: {again}"
    return f"nothing it reads has moved since its last run: {again}, whether or not anything moved"


def _render(workspace: Cerebro, result: PassResult) -> None:
    """Write each writer agent's `rendered:` parts on its article with no model, once curators and compiles have moved
    what they list, and its copy in `articles/`, with a line for each article that changed."""
    at = stamp(workspace.clock())
    for article, headings in rendered.refresh(workspace.store, workspace.agents, at, descriptions(workspace.settings)).items():
        result.notices.append(f"{article}: wrote {', '.join(headings)} with no model, as its rendered: key says")
        _copy(workspace, article, result)
    for article in workspace.store.state(UNMIRRORED) or []:  # a copy that did not write before, due or not, with no model
        _copy(workspace, article, result)


def _copy(workspace: Cerebro, article_id: str, result: PassResult) -> None:
    """Write `article_id`'s copy in `articles/`, and remember it until one writes when it does not."""
    article = workspace.store.article(article_id)
    if article is None:
        unmirrored(workspace.store, article_id, False)
        return
    try:
        mirror(article, workspace.root, facts=workspace.lookup.read(Caller("compile", "agent"), [article_id]).facts,
               zone=workspace.zone)
    except OSError as error:
        unmirrored(workspace.store, article_id, True)
        line = (f"{article_id} is stored, and its copy articles/{article_id}.md was not written ({error.strerror or error}): "
                "fix the path, and the next pass writes it")
        if line not in result.warnings:
            result.warnings.append(line)
    else:
        unmirrored(workspace.store, article_id, False)


# -- a run by hand -------------------------------------------------------------------------------------------------


def ready_by_hand(workspace: Cerebro, agent: Agent, articles: list[str]) -> list[str]:
    """Ready a run by hand as a pass readies a due one: birth a fixed agent's article, then link to each article not
    linked before the facts the workspace already holds. Gives a line for each fact that failed to link."""
    for article in articles:
        if fixed(agent) and workspace.store.article(article) is None:
            birth(workspace.store, agent, article, stamp(workspace.clock()))
    result = PassResult()
    _link_born(workspace, result)
    return [f"{id}: {why}" for id, why in result.failed.items()]


def left_by_hand(workspace: Cerebro, agent: Agent, article: str, compiled: Any, result: PassResult) -> None:
    """What a person's run of `agent` on `article` left unfinished, kept on `result` as a pass keeps it: each part
    refused before it started, said by its kind and marked to run next, with when that is, and each run that went and
    did not finish. Of those, one the budget cut before it handed in text waits while a daily cap holds, and with none
    reached fails on its own budget, naming the keys that set it; any other fails. One cut after it handed in text
    landed it. The article's own run that wrote nothing is its caller's line. A held part's compile note goes: its
    `held` line says it once, with when it runs. Each line of what landing made safe is kept on its run's line."""
    keep_grounding(workspace.store, compiled.run, None, compiled.cleaned)
    held = _parts_not_run(workspace, agent, article, compiled, result, held_runs(workspace, agent, article))
    compiled.notes[:] = [note for note in compiled.notes if not any(
        note.startswith(HELD_PART.format(who)) for who in held if f"{who} on {article}" in result.held)]
    broke = any(run.id and run.status == "failed" for run in compiled.runs)  # the article fails whole
    for run in compiled.runs:
        if not run.id or run.status in ("done", WROTE_NOTHING) or run.status == "budget spent" and run.result:
            continue
        if compiled.article is None and (run.status != "budget spent" or broke):
            continue  # nothing of the article landed: the article's own line says why
        key, note = f"{run.agent} on {article}", f": {run.note}" if run.note else ""
        if run.status == "budget spent" and _paused(workspace, "compilation", result):
            result.waiting_runs.append(key)
        elif run.status == "budget spent" and compiled.article is not None:
            continue  # what the rest returned landed, as a pass counts it: its note says the run did not end
        elif run.status == "budget spent":  # no daily cap holds, so it spent its own budget
            result.failed[key] = (f"run {run.id} spent its own budget, not a daily cap, before it handed in text: "
                                  f"budget: {{ tokens_per_run, tool_calls }} in agents/{run.agent}.md sets it{note}")
        else:
            result.failed[key] = f"run {run.id} {run.status}{note}"


def held_runs(workspace: Cerebro, agent: Agent, article: str) -> str:
    """When a part a run by hand of `agent` left held on `article` runs: the next pass, while its period's run is owed
    there, or else the writer's next run, which runs every part that did not run. A period whose earlier run left
    other articles to do owes only those."""
    zone, words = zone_of(workspace.settings), _words(workspace, agent)
    word, _, _ = _wake(agent, words, _beat(workspace, agent), workspace.clock(), zone)
    if word is not None and ((owed := _left(workspace, agent, word)) is None or article in owed):
        return "runs next pass"
    local = workspace.clock().astimezone(zone)
    nexts = [_period(word, local)[1] for word in words if word in SCHEDULES]
    when = (["on a pass for a change"] if "change" in words else []) + (
        [f"at its writer's next scheduled run, from {min(nexts):%Y-%m-%d %H:%M}"] if nexts else [])
    return f"runs {', or '.join(when)}" if when else "runs when its writer is run by hand again"


def ran_by_hand(workspace: Cerebro, agent: Agent, began: datetime) -> None:
    """Keep a person's whole run of a writer, every article of it written, as its period's run: its period is spent and
    nothing is left pending. A run by hand that failed or left work waiting records nothing, so what its schedule owes
    stays owed."""
    words = [word for word in _words(workspace, agent) if word in SCHEDULES]
    _ran(workspace, agent, words[0] if words else None, None, False, began)


def waits_by_hand(workspace: Cerebro, error: BaseException, runs: list[str]) -> tuple[str, list[str]]:
    """For a run by hand that a daily cap stopped: why `runs`, the agent runs it did not start, wait, and the stop
    lines, kept for `status` as a pass keeps them, its records left as they stand."""
    result = PassResult()
    _paused(workspace, "compilation", result, error)
    result.waiting_runs = runs
    _keep_stop(workspace, result, runs=True, writer={run.split(" on ")[0] for run in runs})
    layer = str((result.crossed.get("compilation") or {}).get("layer") or "all")
    return ("the daily budget is spent" if layer == "all" else f"the daily {layer} budget is spent"), result.stops


# -- daily caps: what waits for one is no failure ------------------------------------------------------------------


STOPPED = own_key("stopped")  # what the last passes left waiting for a daily cap, for `status`


def _capped(error: BaseException) -> bool:
    """Whether `error` is a daily cap reached: a model with no price, or a run held for room, is not."""
    return isinstance(error, BudgetSpent) and not isinstance(error, (HeldForRoom, UnpricedModel))


def _paused(workspace: Cerebro, layer: str, result: PassResult, error: BaseException | None = None,
            model: ModelSpec | None = None) -> bool:
    """Whether a daily cap the day's spend reached holds `layer`'s calls on `model`: with `error`, raised by a call,
    whether it is that cap's refusal; without, whether one stopped the layer earlier in the pass, or the ledger says
    so now. A model with no price is refused first, and is no cap reached. From the first time on, the pass starts no
    more of that layer's work, which waits for the next pass and is no failure."""
    if error is None and layer not in result.stopped:
        try:
            workspace.ledger.check(layer, workspace.clock(), model)
        except BudgetSpent as refused:
            error = refused
    if error is not None:
        if not _capped(error):
            return False
        if layer not in result.stopped:
            result.stopped[layer] = str(error)
            _crossed(workspace, layer, result)
    return layer in result.stopped


def _crossed(workspace: Cerebro, layer: str, result: PassResult) -> None:
    """Keep the crossing of the cap that holds `layer`, as the first refusal met it, so a pass that ends after
    00:00 UTC still names it."""
    now = workspace.clock()
    cap = workspace.ledger.holding(layer, now)
    if (crossing := next((c for c in workspace.ledger.reached(now) if c["cap"] == cap), None)) is not None:
        result.crossed[layer] = crossing


def _waits(result: PassResult, kept: str, id: str) -> None:
    """Keep a record that waits for a daily cap on the pass, by the state key of the list it waits on."""
    ids = result.waiting.setdefault(kept, [])
    if id not in ids:  # a record on its retry list that its source lists again is one record
        ids.append(id)


def _keep_stop(workspace: Cerebro, result: PassResult, *, runs: bool, writer: set[str] | None = None) -> None:
    """Keep, for `status`, what waits for a daily cap, and give the pass its stop lines: for each cap whose refusal held
    work of this pass, at the crossing the first refusal met, the counts of what waits under it: every record on a retry
    list that waits for the cap, and with `runs`, the agent runs this pass did not start, a kind it looked at with
    nothing waiting counted as none beside one that waits. What waits is merged by list: a list this pass tried takes
    what waits on it now, and one it did not try keeps what an earlier pass left on it. A stop at a later crossing
    replaces the one kept, and `status` shows a count while its cap stands reached on that UTC day. A record that waits
    is kept by id, and one whose last try truly failed before the cap stopped it is kept as such, so `status` names it
    as failed still. The agent runs that wait are kept by key too: a pass that ran agents takes its own, and a run by
    hand of `writer`, the agents of one writer, takes its own for them and keeps every other writer's."""
    now, zone = workspace.clock(), zone_of(workspace.settings)
    layer_of = {"records": "extraction", "agent_runs": "compilation"}
    with workspace.store.exclusive():
        kept = workspace.store.state(STOPPED) or {}
        old_waiting, old_failed = kept.get("waiting") or {}, kept.get("failed") or {}
        waiting = {key: ids for key, ids in old_waiting.items() if key not in result.tried}
        failed = {key: ids for key, ids in old_failed.items() if key not in result.tried}
        for key, before in result.tried.items():
            hidden = set(old_waiting.get(key) or []) - set(old_failed.get(key) or [])
            ids = result.waiting.get(key) or []
            waiting[key] = ids
            failed[key] = [id for id in ids if id in before and id not in hidden]  # failed before the cap held it
        old_runs = [str(key) for key in kept.get("runs") or []]
        runs_waiting = ([key for key in old_runs if key.split(" on ")[0] not in writer] + result.waiting_runs
                        if writer is not None else result.waiting_runs if runs else old_runs)
        counts = {"records": sum(len(ids) for ids in waiting.values()), "agent_runs": len(runs_waiting)}
        looked = {"records": bool(result.tried) or counts["records"] > 0, "agent_runs": runs}
        refused = {"records": any(result.waiting.values()), "agent_runs": bool(result.waiting_runs)}
        stops: dict[str, dict[str, Any]] = {}
        for kind, layer in layer_of.items():
            if refused[kind] and (crossing := result.crossed.get(layer)) is not None:
                stops.setdefault(crossing["cap"], dict(crossing))
        for kind, layer in layer_of.items():
            crossing = result.crossed.get(layer) if refused[kind] else None
            cap = crossing["cap"] if crossing is not None else workspace.ledger.holding(layer, now)
            if looked[kind] and cap is not None and cap in stops:
                stops[cap][kind] = counts[kind]
        order = ["daily_usd", *(f"{layer}_daily_usd" for layer in LAYERS)]
        result.stops = [stop_line(stops[cap], zone) for cap in order if cap in stops]
        entries = {str(entry["cap"]): dict(entry) for entry in kept.get("stops") or []}
        home = next((cap for cap, entry in entries.items() if entry.get("records") is not None), None)
        for kind in counts:
            if looked[kind]:
                for entry in entries.values():
                    entry.pop(kind, None)
        for cap, stop in stops.items():  # a stop at another crossing replaces the one kept
            if entries.get(cap, {}).get("at") != stop["at"]:
                entries[cap] = {"cap": cap, "at": stop["at"]}
            entries[cap].update({kind: stop[kind] for kind in counts if kind in stop})
        home = next((cap for cap, stop in stops.items() if "records" in stop), home)
        if home is not None and home in entries and (counts["records"] or "records" in stops.get(home, {})):
            entries[home]["records"] = counts["records"]
        keep = {"stops": [entry for entry in entries.values() if any(entry.get(kind) is not None for kind in counts)],
                "waiting": {key: ids for key, ids in waiting.items() if ids},
                "failed": {key: ids for key, ids in failed.items() if ids}, "runs": runs_waiting}
        if keep["stops"] or keep["waiting"] or keep["runs"]:
            workspace.store.set_state(STOPPED, keep)
        elif kept:
            workspace.store.set_state(STOPPED, None)


CAP_FIELDS = (("total", "daily_usd", "all"), *((layer, f"{layer}_daily_usd", layer) for layer in LAYERS))


def _read_caps(workspace: Cerebro, kept: dict[str, Any], say: Callable[[str], None] | None) -> None:
    """Read the daily caps in settings.yaml again at a wake of `serve`, so a cap a person raised or lowered holds from
    this pass on, and say each cap that was reached and is no longer. A cap taken out is taken once two wakes in a row
    read it so, so a file caught half written lifts none. A file that does not read, or is empty, keeps the caps serve
    holds, said once until it changes. `kept` is the loop's."""
    try:
        text = (workspace.root / "settings.yaml").read_text(encoding="utf-8").removeprefix("\ufeff")
        settings, problem = read_yaml(text)
        if problem is not None:
            raise BudgetError(f"settings.yaml is not YAML at {problem}")
        if not isinstance(settings, dict):  # an empty file, as a save leaves for a moment, takes no cap out
            raise BudgetError("settings.yaml is not a map of settings")
        caps = read_caps(settings)
    except Exception as error:  # noqa: BLE001 - a file that does not read keeps the caps, and the loop goes on
        line = f"settings: {' '.join(str(error).split())}, so the daily budget keeps the caps serve read before"
        if kept.get("error") != line and say is not None:
            say(line)
        kept["error"], kept["gone"] = line, None  # a cap taken out needs two reads in a row: the count restarts
        return
    kept.pop("error", None)
    held: Caps = workspace.ledger.caps
    gone = any(getattr(held, name) is not None and getattr(caps, name) is None for name, _, _ in CAP_FIELDS)
    if caps == held or gone and kept.get("gone") != caps:
        kept["gone"] = caps if caps != held else None
        return
    kept.pop("gone", None)
    now = workspace.clock()
    was = {reached["cap"] for reached in workspace.ledger.reached(now)}
    workspace.ledger.caps = caps
    still = {reached["cap"] for reached in workspace.ledger.reached(now)}
    lifted = was - still if "daily_usd" not in still else set()  # the cap on all spend holds every call
    for name, key, layer in CAP_FIELDS:
        if key in lifted and say is not None:
            usd = getattr(caps, name)
            what = "Daily budget" if layer == "all" else f"Daily {layer} budget"
            calls = "model calls" if layer == "all" else f"{layer} calls"
            held = [f"{layer} calls" for _, key, layer in CAP_FIELDS[1:] if key in still] if layer == "all" else []
            say(f"{what}: {'the cap is removed' if usd is None else f'cap is now ${usd:,.2f}'}, so {calls} resume"
                + (f", but for {' and '.join(held)}, whose own cap is still reached" if held else ""))


# -- what went -----------------------------------------------------------------------------------------------------


KNOWN = own_key("known")  # the agents and sources the last pass knew, so the state of one that went is let go
AGENT_STATE = ("heartbeat:", "curated:", "own:", WAITS, "told:")
SOURCE_STATE = ("held:", "failed:")  # its cursor and its records' keys stay: its facts do, and it may come back


def _forget_gone(workspace: Cerebro) -> None:
    """Let go of the state of each agent and each source the last pass knew that is gone now: an agent's heartbeat
    and what its curator read, and a source's held records and those that wait for another try."""
    files = {path.stem for path in (workspace.root / "sources").glob("*.yaml") if not path.name.startswith(".")}
    now = {"agents": sorted({a.id for a in workspace.agents} | refused_ids(workspace.refused)),
           "sources": sorted({*workspace.connectors, *files})}
    with workspace.store.exclusive():
        known = workspace.store.state(KNOWN) or {}
        if known == now:
            return
        for agent in sorted(set(known.get("agents", [])) - set(now["agents"])):  # what its subagents returned goes
            collection, _, name = agent.partition("/")
            articles = [a.identity.id for a in workspace.store.query(collection)] if name == "article" else [agent]
            drop_material(workspace.store, *articles)
        for prefixes, gone in ((AGENT_STATE, set(known.get("agents", [])) - set(now["agents"])),
                               (SOURCE_STATE, set(known.get("sources", [])) - set(now["sources"]))):
            for key in (prefix + name for name in sorted(gone) for prefix in prefixes):
                if workspace.store.state(key) is not None:
                    workspace.store.set_state(key, None)
        workspace.store.set_state(KNOWN, now)
