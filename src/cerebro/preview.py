"""What a run would start from, and which runs are stale, read and never written.

`show` gives what a run of one agent by hand sends its model, as `run.PydanticRun` sends it: the instructions, the
names of its tools, and the opening message with its evidence, masked as a run masks them. `stale` says of each
article an agent writes whether compile would run that agent there now, and why.

Neither calls a model, takes a lease, moves a clock, counts a spend or writes. Each works on a copy of the store held
in memory, or on a store that keeps no write when the workspace's store is not SQLite, and does there first what a
run by hand does first, such as linking the facts the wiki holds to an article born since; the copy is let go after.
Evidence is read as a run reads it, but a search by meaning on facts, the keep judge and the re-ranker each ask a
model, so none is asked, and a fact told while reading is not extracted. A `script` evidence line's script is not
run: what a person's script does is its own, so a line says what a run would send there instead, and a note says so.
Whether compile would run an agent is asked of `compile.due`, as compile asks it. A dry run of every article stands
for the next pass, `serve --once`: which agents are due is asked of the pass's own `curators_due` and `writers_due`,
on the store apart, and each due run of `compile.due` as the pass's compile asks it.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from . import first_run, prompts
from .agent import Agent, refusal_of
from .budget import LAYERS, BudgetSpent
from .clock import stamp
from .compile import (
    DRAFTS,
    FAILED_WAIT,
    FAILED_WAIT_MOST,
    CompileError,
    _drafts,
    _failing,
    _inbox,
    _inputs,
    _last_sets,
    _orphans,
    _part_of,
    _read,
    _read_before,
    _returned,
    _stamp_of,
    _subagents,
    _tools,
    _Writes,
    due,
    fixed,
    material_key,
    targets,
    unmoved,
)
from .curate import (
    Curated,
    _beside,
    _Curation,
    _curator_tools,
    _woke,
    has_news,
    proposing,
)
from .errors import CerebroError
from .evidence import DecisionKeepJudge, Evidence, Finished, Item, KeepScores, Launch, digest, gather
from .facts import Extracted
from .held import own_names
from .models import PROVIDERS, TokenCount, cost, price_of
from .project import read_ours
from .run import DECIDE_NOTE, NOTHING, _Room, hand_in, instructions, opening
from .runner import (
    COOLING,
    LeaseHeld,
    PassResult,
    _at_once,
    _beat,
    _release,
    compile_groups,
    curators_due,
    hold,
    ready_by_hand,
    writers_due,
    written,
)
from .runner import _hold as cooling
from .shown import shown
from .store import LogRow, SQLiteIndex, SQLiteStore, Store
from .workspace import Workspace

if TYPE_CHECKING:
    from .app import Cerebro

__all__ = ["DRY_END", "NEXT_PASS", "SHOWN_END", "Archived", "Chosen", "Shown", "Stale", "chosen", "pick", "said", "show",
           "shown_lines", "stale", "stale_lines"]

SHOWN_END = "no model called, nothing written"
DRY_END = "Nothing was run or written."
UNSEARCHED = ("a search by meaning on facts was not made, since it embeds its words with a model: what it would "
              "find is left out here, and a run finds it")
UNJUDGED = ("an articles line with keep was not cut, since its judge asks the decision model: it loads whole here, "
            "and a run cuts it")
UNRANKED = ("a search was left in its own order, since the re-ranker asks the decision model: a run's order may "
            "differ")
UNTOLD = ("a fact an evidence line told or took back was left as it is, since the facts layer keeps its facts "
          "outside this preview and extraction asks a model")
UNREAD = ("the workspace's first run has not read its sources yet, and a run by hand reads them first: what they "
          "would hold for this curator is not shown here")
UNRUN = ("a script evidence line was not run, since what it does is its own: what it prints is left out here, and a "
         "run runs it and sends that")
NOT_RUN = "Not run here: a run runs this script and sends what it prints."
UNREAD_SOURCES = ("Sources were not read: what changed in them since they were last read is not told here, and a pass "
                  "reads them first.")
UNKNOWN_OWN = "it runs again if its subagents return new content, which a dry run cannot know"
NO_TOOL = "sent with tool_choice none: its opening fills the room its budget leaves for reading, so the model can call no tool"
HAND_IN_ONLY = NO_TOOL + " but the two that hand in its text or say it has nothing to write"
BY_HAND = "compile --article runs it again whether or not anything moved"
NEXT_PASS = "What the next pass, cerebro serve --once, would run now:"
ROOM = ("the runs before it may spend or hold what is left under the day's spending cap, so whether it runs or waits "
        "for room is not known until they do")
FIRST_RUN = ("The workspace is in its first run: the next cerebro serve --once runs it in place of a pass, and its "
             "curators propose what the wiki holds, which asks a model: their runs are not listed here. If none of "
             "them proposes anything, it then builds at once, runs the writers below and may ask once more a curator "
             "that found nothing to propose; if one does, it shows the list and runs no writer until a person "
             "decides it")
FIRST_WAITS = ("The workspace's first run waits for its list to be decided: the next cerebro serve --once shows it and "
               "runs nothing until a person decides it, then builds what was approved")
FIRST_BUILDS = ("The workspace's first run builds what was approved: the next cerebro serve --once runs the writers "
                "below and no curator, then may ask once more, which asks a model, a curator that found nothing to "
                "propose at first")
SERVE_HOLDS = "a running cerebro serve holds it: "
NOT_DUE = "not due"
THEN = "=== a second message, in the same request"
DROPPED = ("this workspace's store is not SQLite, so what a run does first, such as linking facts to a new article, "
           "was not kept for this preview: what reads it may differ")
NOT_KNOWN = {UNSEARCHED: "its evidence searches facts by meaning, which asks a model, so a dry run cannot tell",
             UNJUDGED: "its keep line is cut by the decision model, which a dry run does not ask, so it cannot tell",
             UNRANKED: "its search is re-ranked by the decision model, which a dry run does not ask, so it cannot tell",
             UNTOLD: "its evidence tells or takes back a fact, which a dry run does not do, so it cannot tell",
             UNRUN: "its evidence runs a script, which a dry run does not run, so it cannot tell"}
WRITES = ("create", "set_identity", "write_compilation", "delete_compilation", "set_field", "drop_field", "keep", "append",
          "set_state", "link", "unlink", "supersede", "retract_superseded")
READS = ("article", "query", "bound", "named", "keyed", "collections", "record", "records", "rows", "latest",
         "total", "articles_of", "ties_of", "facts_of", "superseded", "cursor", "changed_since",
         "linking_to")
_GONE = object()


class _Overlay(Store):
    """A store that passes every read through to the workspace's and keeps no write: a state it was given reads back
    from memory, and every other write is counted and let go."""

    def __init__(self, inner: Store):
        self.inner, self.values, self.dropped = inner, {}, 0
        self.scrub, self.masks = inner.scrub, inner.masks

    def state(self, key: str) -> Any:
        if key in self.values:
            return None if self.values[key] is _GONE else self.values[key]
        return self.inner.state(key)

    def set_state(self, key: str, value: Any) -> None:
        self.values[key] = _GONE if value is None else copy.deepcopy(value)

    def append(self, log: str, data: dict[str, Any]) -> LogRow:
        self.dropped += 1
        return LogRow(log, 0, data)

    def keep(self, record: Any, *, unique: Any = ()) -> Any:
        self.dropped += 1
        return record

    def retract_superseded(self, ids: list[str]) -> int:
        self.dropped += 1
        return 0

    def supersede(self, *args: Any, **kwargs: Any) -> int:
        self.dropped += 1
        return 0

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        yield

    def close(self) -> None:
        pass


def _dropping(self: _Overlay, *args: Any, **kwargs: Any) -> None:
    self.dropped += 1


def _passing(name: str) -> Callable[..., Any]:
    def read(self: _Overlay, *args: Any, **kwargs: Any) -> Any:
        return getattr(self.inner, name)(*args, **kwargs)

    read.__name__ = name
    return read


for _name in READS:
    setattr(_Overlay, _name, _passing(_name))
for _name in set(WRITES) - set(vars(_Overlay)):
    setattr(_Overlay, _name, _dropping)


def _apart(store: Store) -> Store:
    """A store a preview may write to, which the workspace's never sees: a copy in memory of a SQLite store, or an
    overlay over any other."""
    if not isinstance(store, SQLiteStore):
        return _Overlay(store)
    apart = SQLiteStore()
    with store._lock:
        store.db.backup(apart.db)
    apart.scrub, apart.masks = store.scrub, store.masks
    return apart


class _Unsearched:
    """The facts layer as a preview reads it: every read goes through, a search finds nothing, and an extraction or a
    retraction, which a model or the facts layer's own store would take, is not made. Each is remembered."""

    def __init__(self, inner: Any):
        self.inner, self.asked, self.told = inner, False, False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def search(self, text: str, top_k: int = 10, **options: Any) -> list[Any]:
        self.asked = self.asked or bool(text.strip())
        return []

    def search_spent(self, text: str, top_k: int = 10, **options: Any) -> tuple[list[Any], Extracted]:
        return self.search(text, top_k, **options), Extracted([])

    def extract(self, record: Any) -> Extracted:
        self.told = True
        return Extracted([])

    def retract(self, fact_id: str) -> None:
        self.told = True


class _Unjudged(DecisionKeepJudge):
    """The keep judge as a preview has it: the decision model's own, reading each score a run kept in the store, as
    the pass's judge reads it with no call, and asking no model for a passage the store holds no score of. It
    remembers that it would have asked."""

    def __init__(self, store: Store, clock: Any, decision: Any, root: Any):
        super().__init__(None, store, clock, None, decision, root, again=False)  # type: ignore[arg-type]
        self.asked = False

    def _ask(self, contract: str, instruction: str, batch: list[Any], out: KeepScores) -> list[float | None]:
        self.asked = True
        out.wait = "a preview asks no model"
        return [None] * len(batch)


class _Workspace:
    """The workspace as a preview's steps read it: its own store apart, and its facts layer with no search."""

    def __init__(self, workspace: Cerebro, store: Store, facts: Any):
        self.workspace, self.store, self.extraction, self.decider = workspace, store, facts, None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.workspace, name)


class _Reading:
    """The workspace's lookup and handle with no model behind them, over a store apart: no decision model, no ledger,
    a facts layer that makes no search, and a search index of its own."""

    def __init__(self, workspace: Cerebro):
        self.workspace = workspace
        self.store = _apart(workspace.store)
        self.lookup = copy.copy(workspace.lookup)
        self.lookup.store = self.store
        self.facts = _Unsearched(workspace.lookup.extraction) if workspace.lookup.extraction is not None else None
        self.lookup.extraction = self.facts
        self.lookup.decider = self.lookup.ledger = None
        self.ranked = False
        self.lookup.rerank = self._unranked if workspace.lookup.rerank is not None else None
        self.lookup.index = SQLiteIndex(self.store if isinstance(self.store, SQLiteStore) else None)
        decision = workspace.models.get("decision")  # a pass with its key judges a keep line; one with none reads it whole
        judged = workspace.decider is not None or decision is not None and bool(PROVIDERS[decision.provider].decision_url)
        self.judge = _Unjudged(self.store, workspace.clock, decision, workspace.root) if judged else None
        kinds = [kind for app in workspace.apps for kind in app.kinds]
        self.handle = Workspace(self.lookup, self.store, kinds, workspace.clock, mask=workspace.mask_every)
        self.overlay = _Workspace(workspace, self.store, self.facts if self.facts is not None else None)
        self.linked: list[str] = []
        self.unrun = False
        self.capped = ""  # the cap's own line, once a daily cap stops a run
        self.unread = ""  # why a run's evidence may change before it runs, which a dry run cannot read, or empty

    def _unstarted(self, launch: Launch) -> Finished:
        self.unrun = True
        return Finished(0)

    def _unranked(self, text: str, hits: list[Any]) -> list[Any]:
        self.ranked = True
        return hits

    def ready(self, agent: Agent, articles: list[str]) -> None:
        """Do first what a run by hand does first: birth a fixed agent's article and link the facts the wiki holds."""
        self.linked += ready_by_hand(self.overlay, agent, articles)  # type: ignore[arg-type]

    def gather(self, agent: Agent, article: str | None, writers: list[str]) -> list[Evidence]:
        workspace = self.workspace
        out = gather(agent, self.lookup, workspace.exposed.evidence, self.handle, this=article, writers=writers,
                     root=workspace.root, now=workspace.clock(), last=_last_sets(self.store, agent, article) if article else None,
                     starter=self._unstarted, zone=workspace.zone, sources=workspace.connectors, judge=self.judge)
        for entry in out:
            if entry.spelled.startswith("script "):
                entry.items, entry.notes, entry.lines = [Item(f"{entry.spelled}: not run", NOT_RUN)], [], [NOT_RUN]
        return out

    def tools(self, agent: Agent) -> list[Callable[..., Any]]:
        return _tools(agent, self.lookup, self.workspace.exposed.agent_tools, self.handle, [])

    def unasked(self) -> list[str]:
        """What a model would have been asked while reading, and was not, each once, since the last `begin`."""
        out = [UNSEARCHED] if self.facts is not None and self.facts.asked else []
        out += [UNJUDGED] if self.judge is not None and self.judge.asked else []
        out += [UNRANKED] if self.ranked else []
        out += [UNTOLD] if self.facts is not None and self.facts.told else []
        return out + ([UNRUN] if self.unrun else [])

    def begin(self) -> None:
        self.ranked = self.unrun = False
        if self.facts is not None:
            self.facts.asked = self.facts.told = False
        if self.judge is not None:
            self.judge.asked = False


@dataclass
class Shown:
    """What one run would send: the instructions, its tools by name, the opening message, the names of the files sent
    beside it, the digest of its evidence, and lines of what to know of how it was read."""

    system: str
    tools: list[str]
    opening: str
    digest: str
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    then: list[str] = field(default_factory=list)
    no_tool: str = ""


class Archived(CerebroError):
    """An archived article named for a run: a run by hand leaves it alone, and a preview refuses it."""

    def __init__(self, article: str, agent: str):
        super().__init__(f"{article} is out of the working set, so {agent} does not run on it")
        self.article = article


def pick(agent: Agent, articles: list[str], named: str, held: Callable[[str], str | None]) -> str:
    """The one article of `articles` a run of `agent` would start from: the one `named` gives, by id, by path under
    articles/ or by its name in the agent's collection, or the only one. `held` gives the status of an article the
    store holds, or None, so one out of the working set is told apart from one that is not there."""
    if named:
        name = named.strip().removeprefix("articles/").removesuffix(".md")
        found = next((p for p in articles if name in (p, p.rpartition("/")[2])), None)
        if found is not None:
            return found
        whole = name if "/" in name else f"{agent.target}/{name}"
        status = held(whole) if whole.rpartition("/")[0] == agent.target else None
        if status == "archived":
            raise Archived(whole, agent.id)
        if status is not None:
            raise CerebroError(f"{whole} is out of the working set, so {agent.id} does not run on it")
        raise CerebroError(f"{named} is not in articles/{agent.target}, which {agent.id} writes: name one of "
                           f"{', '.join(articles) or 'its articles'}")
    if not articles:
        raise CerebroError(f"{agent.target} holds no article yet for {agent.id} to write: its curator admits them, "
                           "or run cerebro agents run on that curator")
    if len(articles) > 1:
        raise CerebroError(f"{agent.id} runs on {len(articles)} articles in the working set: name one, as in cerebro "
                           f"agents run {agent.id} {articles[0]} --show-prompt")
    return articles[0]


def status_of(store: Store, id: str) -> str | None:
    """The status of the article `id`, as `pick` asks it, or None when the store holds none."""
    found = store.article(id)
    return found.identity.status if found is not None else None


def cap_words(line: str) -> str:
    """What a run a daily cap stopped is told, from the cap's own `line`."""
    layer = line.split(" ", 1)[0]
    return f"the daily {layer} budget is spent" if layer in LAYERS else "the daily budget is spent"


def said(workspace: Cerebro, error: BaseException) -> str:
    """What `error` says, on one line and masked."""
    text = str(error) if isinstance(error, CerebroError) else f"{type(error).__name__}: {error}"
    return workspace.mask_every()(" ".join(text.split()))


def show(workspace: Cerebro, agent: Agent, named: str = "") -> Shown:
    """What a run of `agent` by hand would send its model, on the article `named` gives, or the one it writes. A
    refusal, or any error while reading, comes as one masked line."""
    try:
        return _show(workspace, agent, named)
    except Exception as error:  # noqa: BLE001 - whatever an evidence line raised, said in one masked line
        raise CerebroError(said(workspace, error)) from None


def _show(workspace: Cerebro, agent: Agent, named: str) -> Shown:
    reading = _Reading(workspace)
    if agent.role == "curator":
        if named:
            raise CerebroError(f"{agent.id} runs on its collection, not on one article: give no article")
        return _curator(workspace, reading, agent)
    lists = workspace.agent(agent.composers[0]) if agent.role == "subagent" and agent.composers else None
    writer = lists or agent
    articles = targets(writer, reading.lookup)
    article = pick(agent, articles, named, lambda id: status_of(reading.store, id))
    reading.ready(writer, articles)
    if reading.store.article(article) is None and not fixed(writer):
        raise CompileError(f"agents/{writer.id}.md writes articles/{article}.md, which does not exist: its collection's "
                           "curator admits it")
    if lists is not None:
        out = _subagent(workspace, reading, agent, lists, article)
    elif agent.role == "composer":
        out = _composer(workspace, reading, agent, article)
    else:
        out = _single(workspace, reading, agent, article)
    mask = workspace.mask_every()
    out.notes += [f"a fact did not link: {mask(line)}" for line in reading.linked]
    if isinstance(reading.store, _Overlay) and reading.store.dropped:
        out.notes.append(DROPPED)
    return out


def _sent(workspace: Cerebro, reading: _Reading, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]],
          read: str, writes: bool = True) -> Shown:
    mask = workspace.mask_every()
    names = [getattr(t, "__name__", "") for t in tools] + ([hand_in(agent), NOTHING] if writes else [])
    text = opening(evidence, workspace.clock().astimezone(workspace.zone), workspace.root)
    told = instructions(agent, workspace.root)
    room = _Room(agent.budget.tokens_per_run, told + text, agent.budget.tool_calls, deciding=not writes)
    closed = "" if not room.closed() else HAND_IN_ONLY if writes else NO_TOOL
    return Shown(mask(told), names, mask(text), read, [mask(f.name) for entry in evidence for f in entry.files],
                 reading.unasked(), [DECIDE_NOTE] if not room.closed() and room.short() else [], closed)


def _single(workspace: Cerebro, reading: _Reading, agent: Agent, article: str) -> Shown:
    evidence = reading.gather(agent, article, [agent.id]) + _inbox(reading.store, agent, article)
    read = digest(agent, evidence + _read_before(reading.lookup, reading.store, agent, article))
    tools = reading.tools(agent) + _Writes(article, reading.store, agent.id).tools()
    out = _sent(workspace, reading, replace(agent, target=article), evidence, tools, read)
    return out


def _subagent(workspace: Cerebro, reading: _Reading, sub: Agent, composer: Agent, article: str) -> Shown:
    writers = [composer.id, *composer.subagent_ids]
    evidence = reading.gather(sub, article, writers) + _inbox(reading.store, sub, article)
    read = _stamp_of(digest(sub, evidence + _read_before(reading.lookup, reading.store, sub, article)), composer.context)
    tools = reading.tools(sub) + _Writes(article, reading.store, sub.id).tools()
    out = _sent(workspace, reading, _part_of(sub, composer, article, workspace.root), evidence, tools, read)
    return out


def _composer(workspace: Cerebro, reading: _Reading, composer: Agent, article: str) -> Shown:
    if not composer.own:
        raise CerebroError(f"{composer.id} has no instructions of its own, so its run asks no model: it joins what its "
                           f"subagents return. Run cerebro agents run <subagent> --show-prompt for one of "
                           f"{', '.join(composer.subagent_ids) or 'them'}")
    store = reading.store
    subs = _subagents(composer, workspace.agents)
    writers = [composer.id, *(sub.id for sub in subs)]
    lead = reading.gather(composer, article, writers) + _inbox(store, composer, article)
    kept = store.state(material_key(article)) or {}
    held = [dict(kept.get(sub.id) or {}) for sub in subs]
    read = _stamp_of(digest(composer, lead + _read_before(reading.lookup, store, composer, article)),
                     *_returned(subs, held))
    words = prompts.fill("composer", workspace.root, drafts=DRAFTS) + "\n\n" + composer.prompt
    tools = reading.tools(composer) + _Writes(article, store, composer.id).tools()
    out = _sent(workspace, reading, replace(composer, target=article, prompt=words), [_drafts(subs, held), *lead], tools,
                read)
    out.notes.append("what the subagents returned is what each returned last: a run first runs each subagent whose "
                     "evidence moved, and sends what it returns now")
    return out


def _curator(workspace: Cerebro, reading: _Reading, agent: Agent) -> Shown:
    store, notes = reading.store, []
    session = first_run._begin(reading.overlay) or first_run._state(reading.overlay)  # type: ignore[arg-type]
    if session is not None and session.get("status") != "done" and not session.get("read"):
        notes.append(UNREAD)
    curation, evidence = _curator_view(workspace, reading, agent)
    first = not store.rows("agent-runs", {"agent": agent.id, "status": "done"})
    curation.wake = "first" if first else "hand"
    curation.proposing = proposing(store, agent.id)
    tools = _curator_tools(curation, reading.lookup, workspace.exposed.agent_tools, workspace.handle,
                           workspace.connectors)
    out = _sent(workspace, reading, agent, _woke(curation.wake) + evidence, tools, digest(agent, evidence), writes=False)
    out.notes += notes
    return out


def _curator_view(workspace: Cerebro, reading: _Reading, agent: Agent) -> tuple[_Curation, list[Evidence]]:
    """A curator's view as its run would read it, and the curation it was read through."""
    curation = _Curation(agent, reading.store, workspace.clock, Curated(), reading.facts)  # type: ignore[arg-type]
    curation.ours = read_ours(workspace.root)
    curation.sources = list(workspace.connectors)
    curation.own = own_names(reading.store, workspace.connectors, curation.ours)
    return curation, reading.gather(agent, None, [agent.id]) + _beside(curation)


def _clean(text: str) -> list[str]:
    return [shown(line, None) for line in text.split("\n")]


def shown_lines(out: Shown) -> list[str]:
    """What `agents run --show-prompt` prints, each line with any control character in it
    written as a visible escape."""
    lines = ["=== system", "", *_clean(out.system.rstrip("\n")), "", "=== tools", "", shown(", ".join(out.tools), None),
             *(["", out.no_tool] if out.no_tool else []), "", "=== opening message", "", *_clean(out.opening.rstrip("\n"))]
    if out.files:
        lines += ["", "=== files sent beside it", "", *(shown(name, None) for name in out.files)]
    for text in out.then:
        lines += ["", THEN, "", *_clean(text)]
    lines += ["", *(f"note     {shown(line, None)}" for line in out.notes)] if out.notes else []
    return [*lines, "", f"=== evidence digest {out.digest} · {SHOWN_END}"]


@dataclass
class Row:
    """One agent on one article: what compile would do there now, and why."""

    article: str
    status: str
    agent: str
    why: str = ""


@dataclass
class Stale:
    """What compile would do on each article, or the next pass on every one: a row each for what is stale, failed,
    cooling down, waiting, not known or not due, a count of what is fresh, and each agent file that did not load, as a
    pass names it. `first` says what the next `serve --once` does with a workspace in its first run, in place of a
    pass or before one."""

    rows: list[Row] = field(default_factory=list)
    fresh: int = 0
    refused: list[str] = field(default_factory=list)
    whole: bool = False
    archived: list[str] = field(default_factory=list)
    first: str = ""
    capped: str = ""  # the cap's own line, said once, when a daily cap stops a run

    def count(self, status: str) -> int:
        return sum(row.status == status for row in self.rows)


def _hold(workspace: Any, agent: Agent, article: str, by_hand: bool) -> Callable[[list[str]], str] | None:
    return None if by_hand else cooling(workspace, agent, article, cadence=False)


def _cause(workspace: Any, agent: Agent, article: str) -> str:
    mark = (_beat(workspace, agent).articles or {}).get(article) or {}
    if not mark:
        return "its evidence moved" if written(workspace, agent, article) else "its first build"
    return _at_once(workspace, agent, article, mark) or "its evidence moved"


def _unknown(reading: _Reading, article: str, agents: list[str]) -> list[Row] | None:
    said = reading.unasked()
    if not said:
        return None
    why = "; ".join(NOT_KNOWN[line] for line in said)
    return [Row(article, "not known", id, why) for id in agents]


def _row(article: str, agent: str, verdict: tuple[str, str]) -> Row:
    status, why = verdict
    return Row(article, status or "stale", agent, why.removeprefix("cooling down: "))


def _capped(reading: _Reading, agent: Agent, row: Row) -> Row:
    """A stale run as the spending cap leaves it: `waiting` with why when the day's cap, checked as a run checks it
    before it starts, would refuse its model call."""
    if row.status != "stale":
        return row
    workspace = reading.workspace
    try:
        workspace.ledger.check("compilation", workspace.clock(), workspace.model_of(agent))
    except BudgetSpent as error:
        line = said(workspace, error)
        reading.capped = reading.capped or line
        return Row(row.article, "waiting", row.agent, cap_words(line))
    return row


def _served(reading: _Reading, agent: Agent, article: str, moved: list[str], why: str) -> str:
    """`why` a stale run runs on the next pass, with what a running `serve` does instead when the workspace's
    `schedule.articles` would hold it there, as `serve --once` does not."""
    held = cooling(reading.overlay, agent, article)(moved)
    return f"{why}; {SERVE_HOLDS}{held.removeprefix(COOLING)}" if held else why


def _single_stale(reading: _Reading, agent: Agent, article: str, by_hand: bool, woke: str = "") -> list[Row]:
    """`woke` is why a pass runs the agent on its schedule, or empty for a change."""
    store, overlay = reading.store, reading.overlay
    evidence = reading.gather(agent, article, [agent.id]) + _inbox(store, agent, article)
    if (unknown := _unknown(reading, article, [agent.id])) is not None:
        return unknown
    read = digest(agent, evidence + _read_before(reading.lookup, store, agent, article))
    row = _row(article, agent.id, due(agent, article, store, read, reading.workspace.clock(),
                                   fresh=unmoved(store, agent, article, read), by_hand=by_hand, scheduled=bool(woke),
                                   hold=None if woke else _hold(overlay, agent, article, by_hand)))
    if row.status != "stale":
        return [row]
    cause = _cause(overlay, agent, article) if by_hand and not unmoved(store, agent, article, read) else ""
    if woke:
        return [_capped(reading, agent, replace(row, why=cause or woke))]
    cause = _cause(overlay, agent, article)
    return [_capped(reading, agent, replace(row, why=cause if by_hand else _served(reading, agent, article, [agent.id],
                                                                                    cause)))]


def _composer_stale(reading: _Reading, composer: Agent, article: str, by_hand: bool, woke: str = "") -> list[Row]:
    """`woke` is why a pass runs the composer on its schedule, or empty for a change."""
    workspace, store, overlay = reading.workspace, reading.store, reading.overlay
    subs = _subagents(composer, workspace.agents)
    writers = [composer.id, *(sub.id for sub in subs)]
    instructed = bool(composer.own)
    lead = reading.gather(composer, article, writers) + _inbox(store, composer, article) if instructed else []
    parts = [reading.gather(sub, article, writers) + _inbox(store, sub, article) for sub in subs]
    if (unknown := _unknown(reading, article, [*(sub.id for sub in subs), *([composer.id] if instructed else [])])):
        return unknown
    stamps = [_stamp_of(digest(sub, read + _read_before(reading.lookup, store, sub, article)), composer.context)
              for sub, read in zip(subs, parts)]
    kept = store.state(material_key(article)) or {}
    held = [dict(kept.get(sub.id) or {}) for sub in subs]
    own = _stamp_of(digest(composer, lead + _read_before(reading.lookup, store, composer, article)),
                    *_returned(subs, held))
    now = workspace.clock()
    rows = [_row(article, sub.id, due(sub, article, store, stamps[n], now, scheduled=bool(woke),
                                   fresh=held[n].get("digest") == stamps[n], by_hand=by_hand))
            for n, sub in enumerate(subs)]
    if instructed:
        rows.append(_row(article, composer.id, due(composer, article, store, own, now, by_hand=by_hand,
                                                scheduled=bool(woke), fresh=store.state(_read(composer, article)) == own)))
        if rows[-1].status == "fresh" and any(row.status == "stale" for row in rows[:-1]):
            rows[-1] = Row(article, "not known", composer.id, UNKNOWN_OWN)
    current = store.article(article)
    order = [s.by for s in (current.compilations if current is not None else []) if s.by in writers]
    regrouped = _regrouped(store, composer, article, subs, held, order, instructed, workspace.agents)
    moved = [row.agent for row in rows if row.status == "stale"]
    hold = None if woke else _hold(overlay, composer, article, by_hand)
    if moved and not regrouped and hold is not None and (why := hold(moved)):
        rows = [Row(article, "cooling down", row.agent, why.removeprefix(COOLING))
                if row.status == "stale" else row for row in rows]
    cause = woke or _cause(overlay, composer, article)
    if moved and not woke and not by_hand and not regrouped:
        cause = _served(reading, composer, article, moved, cause)
    found = {sub.id: sub for sub in [*subs, composer]}
    rows = [_capped(reading, found[row.agent], replace(row, why=cause)) if row.status == "stale" else row
            for row in rows]
    if regrouped and not any(row.status == "stale" for row in rows):
        rows = [row for row in rows if row.agent != composer.id] + [Row(article, "stale", composer.id, regrouped)]
    return rows


def _regrouped(store: Store, composer: Agent, article: str, subs: list[Agent], held: list[dict[str, Any]],
               order: list[str], instructed: bool, others: list[Agent]) -> str:
    """Why compile would land the article again though no part's evidence moved, as `_compose` would: a part left by
    a subagent no agent lists now, what a subagent returned that was never placed, or parts no longer in the order
    `subagents:` gives. Empty when none holds."""
    gone = _orphans(composer, article, store, others)
    if gone:
        return f"{', '.join(gone)} left a part on it, and no agent lists it now"
    if any(h.get("landed") is False for h in held):
        return "what a subagent returned was never placed on it"
    if not instructed and order != [sub.id for sub in subs if sub.id in order]:
        return "its parts are not in the order subagents: gives"
    return ""


WRITERS = ("writer", "article", "composer", "query")


@dataclass
class Chosen:
    """What `compile --article` names: each agent with the articles it writes there, the archived articles it leaves
    alone, and whether a collection was named."""

    jobs: list[tuple[Agent, list[str]]] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    collection: bool = False


def chosen(workspace: Cerebro, lookup: Any, named: str) -> Chosen:
    """The articles `compile --article` names: a collection by its folder,
    trimmed of slashes, else an article by its id or its path under articles/. An archived article is left alone, and
    one merged away is not there. Refused in one masked line when it names none, naming an agent file of that folder
    that did not load."""
    mask, store = workspace.mask_every(), lookup.store
    folder = named.removeprefix("articles/").strip("/")
    out = Chosen(collection=True)
    for agent in workspace.agents:
        if agent.role not in WRITERS:
            continue
        try:
            articles = [article_id for article_id in targets(agent, lookup) if article_id.startswith(folder + "/")]
        except Exception:  # noqa: BLE001, S112 - an agent whose targets fail is told by its own name, not here
            continue
        if articles:
            out.jobs.append((agent, articles))
    if "/" not in folder:
        out.archived = [a.identity.id for a in store.query(folder) if a.identity.status == "archived"
                        and workspace.writer_of(a.identity.id) is not None]
    if out.jobs or out.archived:
        return out
    article = named.removeprefix("articles/").removesuffix(".md")
    found, writer = store.article(article), workspace.writer_of(article)
    status = found.identity.status if found is not None else None
    if writer is not None and status == "archived":
        return Chosen(archived=[article])
    if writer is not None and status != "merged" and (found is not None or fixed(writer)):
        return Chosen(jobs=[(writer, [article])])
    collection = folder if "/" not in folder else article.rpartition("/")[0]
    refused = [line for line in workspace.refused if collection and line.startswith(f"agents/{collection}/")
               or line.startswith(f"agents/{article}.md:")]
    for id in (article, f"{article.split('/')[0]}/article") if writer is None and "/" in folder else ():
        if why := refusal_of(workspace.refused, id):
            raise CerebroError(mask(f"agents/{id}.md is refused, so nothing writes {article}: {why}"))
    if refused and writer is None:
        raise CerebroError(mask(f"no agent writes {collection if '/' not in folder else article}: {refused[0]}"))
    if writer is not None or article.count("/") != 1 or article.startswith("/"):
        raise CerebroError(mask(f"no article or collection {named}: name an article by its id or its path under "
                                "articles/, or a collection by its folder"))
    raise CerebroError(mask(f"no agent writes {article}: add one with cerebro agents new {article}"))


def stale(workspace: Cerebro, article: str = "") -> Stale:
    """What compile would do now on `article`, as `compile.due` decides it for compile, or with no `article` what the
    next pass, `serve --once`, would run. A compile that names its article is a person's ask: it runs though a cooldown
    would hold it or a failed run would wait, and leaves a fresh run alone. The next pass runs the curators and writers
    the pass's own `curators_due` and `writers_due` give, each on its schedule or on what moved, heeding the cooldown an
    agent's own heartbeat sets and a failed run's wait; a row says why each other agent is not due."""
    reading, mask = _Reading(workspace), workspace.mask_every()
    out = Stale(whole=not article)
    out.refused = [mask(line) for line in workspace.refused if line.startswith("agents/")]
    if article:
        picked = chosen(workspace, reading.lookup, article)
        out.archived = picked.archived
        _rows(reading, out, [(agent, articles, BY_HAND) for agent, articles in picked.jobs], by_hand=True)
    elif first_run.in_first_run(reading.overlay):  # type: ignore[arg-type]
        out.first = _first_run(reading)
        if out.first != FIRST_WAITS:  # the build runs these, at once when the curators propose nothing
            reading.unread = _held_for_first_run(reading, out.first)
            _next_pass(reading, out, curators=False)
    else:
        _next_pass(reading, out)
    _room(reading, out, side_by_side=not article)
    out.capped = reading.capped
    return out


def _room(reading: _Reading, out: Stale, side_by_side: bool) -> None:
    """Make `not known` each stale run that the runs before it could leave no room for under the day's caps, in the
    order its command runs them: a pass runs its curators one at a time and then each collection's articles side by
    side, one collection after another, as `compile_groups` gives them, and `compile --article` runs one at a time.
    A run waits when the caps are spent before it starts, or, as `Ledger.reserve` holds it, when runs under way beside
    it hold the room it needs. What a run spends is not known before it runs, so each is taken at the most its budget
    lets it spend; a run that nothing could come before always starts, as the ledger lets the first run start."""
    workspace = reading.workspace
    caps, at = workspace.ledger.caps, workspace.clock()
    limits = [(cap, workspace.ledger.spent(at, layer)) for cap, layer in ((caps.of("compilation"), "compilation"),
                                                                     (caps.total, None)) if cap is not None]
    if not limits:
        return
    left = min(cap - spent for cap, spent in limits)
    found = {agent.id: agent for agent in workspace.agents}

    def most(id: str) -> float:  # the most one run of `id`, and its subagents' runs, can spend
        agent = found.get(id)
        ids = [id, *agent.subagent_ids] if agent is not None and agent.role == "composer" else [id]
        total = 0.0
        for one in (found[i] for i in ids if i in found):
            model = workspace.model_of(one)
            if model is not None and price_of(model.provider, model.model) is not None:
                total += cost(model.provider, model.model, TokenCount(output_tokens=one.budget.tokens_per_run))
        return total

    runs = [n for n, row in enumerate(out.rows) if row.status in ("stale", "not known") and row.agent in found]
    curators = [n for n in runs if found[out.rows[n].agent].role == "curator"]
    writers = [n for n in runs if n not in curators]
    groups = [[n] for n in curators]
    if side_by_side:
        plan = [(found[out.rows[n].agent], None, out.rows[n].article) for n in writers]
        groups += [[writers[i] for i in group] for group in compile_groups(reading.store, list(workspace.agents), plan)]
    else:
        groups += [[n] for n in writers]
    before = 0.0
    for group in groups:
        spend = {n: most(out.rows[n].agent) for n in group}
        whole = sum(spend.values())
        for n in group:
            worst = before + whole - spend[n]  # what may be spent or held before it starts
            if out.rows[n].status == "stale" and worst > 0 and (worst >= left or worst + spend[n] > left):
                out.rows[n] = replace(out.rows[n], status="not known", why=ROOM)
        before += whole


def _first_run(reading: _Reading) -> str:
    """What the next `serve --once` does with a workspace in its first run, as `first_run.run` decides it: discover,
    show the list that waits, or build what was approved with a pass that runs no curator."""
    overlay = reading.overlay
    session = first_run._state(overlay)  # type: ignore[arg-type]
    status = (session or {}).get("status")
    if session is None or status == "discovering" or status == "stopped" and \
            first_run._spent(overlay, session) < first_run._cap(overlay, session):  # type: ignore[arg-type]
        return FIRST_RUN
    pile = first_run.LocalApproval(overlay).pile()  # type: ignore[arg-type]
    return FIRST_WAITS if status == "stopped" or pile.waiting() or pile.stopped else FIRST_BUILDS


def _held_for_first_run(reading: _Reading, first: str) -> str:
    """Why a writer's run in the first run's build cannot be told now, or empty: the build reads the records held for
    the first run into facts before its writers run, which asks a model, and discovery holds what it reads first."""
    overlay = reading.overlay
    session = first_run._state(overlay) or {}  # type: ignore[arg-type]
    held = sum(len(reading.store.state(f"held:{name}") or []) for name in sorted(overlay.connectors))
    if first == FIRST_RUN and overlay.connectors and not session.get("read"):
        return ("the first run reads the sources and holds what they list, and its build reads those records into "
                "facts before this runs, which asks a model, so a dry run cannot tell")
    if held:
        return (f"the first run's build reads the {held} record{'' if held == 1 else 's'} held for it into facts "
                "before this runs, which asks a model, so a dry run cannot tell")
    return ""


def _next_pass(reading: _Reading, out: Stale, curators: bool = True) -> None:
    """The rows of what the next pass would run: its curators, unless `curators` is false, as the first run builds,
    then its writers, then each agent it leaves alone."""
    workspace, overlay, result = reading.workspace, reading.overlay, PassResult()
    try:
        due = curators_due(overlay, result) if curators else []  # type: ignore[arg-type]
        writers = writers_due(overlay, result)  # type: ignore[arg-type]
    finally:
        _release(overlay, list(workspace.agents), result.token)  # type: ignore[arg-type]
    for agent, word in due:
        reading.begin()
        try:
            row = _capped(reading, agent, _curator_stale(reading, agent, result.due[agent.id] if word is not None else ""))
        except Exception as error:  # noqa: BLE001 - one agent's failure is a row, and the others go on
            row = Row(agent.target, "failed", agent.id, said(workspace, error))
        if row.status == "fresh":
            out.fresh += 1
        else:
            out.rows.append(replace(row, why=workspace.mask_every()(row.why)))
    jobs: list[tuple[Agent, list[str], str]] = []
    for agent, word in writers:
        try:
            articles = targets(agent, reading.lookup)
        except Exception as error:  # noqa: BLE001 - one agent's failure is a row, and the others go on
            out.rows.append(Row(agent.target, "failed", agent.id, said(workspace, error)))
            continue
        if not articles:
            result.skipped[agent.id] = f"{agent.target} holds no article yet: its curator admits them"
            continue
        jobs.append((agent, articles, result.due[agent.id] if word is not None else ""))
    _rows(reading, out, jobs, by_hand=False)
    found = {agent.id: agent for agent in workspace.agents}
    mask = workspace.mask_every()
    out.rows += [Row(found[id].target if id in found else id, NOT_DUE, id, mask(" ".join(why.split())))
                 for id, why in result.skipped.items()]


def _curator_stale(reading: _Reading, agent: Agent, woke: str) -> Row:
    """Whether a pass runs the curator `agent`: on its schedule when `woke` says why, else when what it reads moved
    since its last run that finished, as `curate.run_curator` tells with `changed_only`."""
    if woke:
        return Row(agent.target, "stale", agent.id, woke)
    _, view = _curator_view(reading.workspace, reading, agent)
    if (unknown := _unknown(reading, agent.target, [agent.id])) is not None:
        return unknown[0]
    if not has_news(reading.store, agent, view):
        return Row(agent.target, "fresh", agent.id)
    never = reading.store.state(f"curated:{agent.id}") is None
    return Row(agent.target, "stale", agent.id, "it never finished a run" if never else "its evidence moved")


def _rows(reading: _Reading, out: Stale, jobs: list[tuple[Agent, list[str], str]], by_hand: bool) -> None:
    """A row for each run of `jobs`, each agent with its articles and why its schedule runs it, or empty."""
    workspace = reading.workspace
    for agent, articles, woke in jobs:
        with contextlib.ExitStack() as held:
            try:
                if by_hand:  # a run by hand takes the agent's lease, as compile --article does, here on the copy
                    held.enter_context(hold(reading.overlay, agent))  # type: ignore[arg-type]
                reading.ready(agent, articles)
            except LeaseHeld as error:
                out.rows += [Row(article, "failed", agent.id, said(workspace, error)) for article in articles]
                continue
            except Exception as error:  # noqa: BLE001
                out.rows.append(Row(agent.target, "failed", agent.id, said(workspace, error)))
                continue
            _rows_of(reading, out, agent, articles, woke, by_hand)


def _gather_waits(reading: _Reading, agent: Agent, article: str) -> str:
    """Why compile would not gather `agent`'s evidence on `article` now, in `compile._gather`'s words, or "": its
    evidence failed to gather last, on the same agent file and workspace.md, and its wait after that has not ended. A
    run on its schedule or by hand gathers at once, as `_gather` lets a scheduled run, so only a change is asked here. A
    composer waits when it or any of its subagents does, as `_compose` gathers them all first."""
    agents = [agent, *_subagents(agent, reading.workspace.agents)] if agent.role == "composer" else [agent]
    for one in agents:
        kept = reading.store.state(_failing(one, article)) or {}
        if not kept.get("gather") or kept.get("inputs") != _inputs(one, reading.workspace.root):
            continue
        times = int(kept.get("times", 1))
        until = datetime.fromisoformat(kept["at"]) + timedelta(seconds=min(FAILED_WAIT * 2 ** (times - 1),
                                                                           FAILED_WAIT_MOST))
        if reading.workspace.clock() < until:
            return (f"its evidence failed to gather, the last of {times} {'failure' if times == 1 else 'failures'} "
                    f"in a row, so it waits until {stamp(until)} before it gathers again, or until its agent file or "
                    "workspace.md changes")
    return ""


def _rows_of(reading: _Reading, out: Stale, agent: Agent, articles: list[str], woke: str, by_hand: bool) -> None:
    """A row for each run of `agent` on `articles`, one article's failure its own row."""
    workspace, mask = reading.workspace, reading.workspace.mask_every()
    for article in articles:
        reading.begin()
        try:
            if not by_hand and not woke and (why := _gather_waits(reading, agent, article)):
                out.rows.append(Row(article, "waiting", agent.id, mask(why)))
                continue
            if reading.store.article(article) is None and not fixed(agent):
                raise CompileError(f"agents/{agent.id}.md writes articles/{article}.md, which does not exist: its "
                                   "collection's curator admits it")
            rows = (_composer_stale if agent.role == "composer" else _single_stale)(reading, agent, article, by_hand,
                                                                                   woke)
        except Exception as error:  # noqa: BLE001 - one article's failure is a row, and the others go on
            rows = [Row(article, "failed", agent.id, said(workspace, error))]
        if reading.unread:  # what it reads may move before it runs: a run it would skip may run
            rows = [Row(row.article, "not known", row.agent, reading.unread)
                    if row.status == "fresh" else row for row in rows]
        for row in rows:
            if row.status == "fresh":
                out.fresh += 1
            else:
                out.rows.append(replace(row, why=mask(row.why)))


def stale_lines(report: Stale) -> list[str]:
    """What `compile --dry-run` prints after its first lines, which say what it stands for and each agent file that
    did not load: a line for each run that is not fresh, its columns aligned, then what it
    counts. Fresh runs are counted, not listed. A dry run of the next pass ends saying the sources were not read,
    which a pass reads first."""
    if report.first == FIRST_WAITS:
        return [report.first, DRY_END, UNREAD_SOURCES]
    table = [[shown(c) for c in (row.article, row.status, row.agent, row.why) if c] for row in report.rows]
    widths = [max((len(cells[i]) for cells in table if len(cells) > i), default=0) for i in range(3)]
    lines = [report.first] if report.first else []
    lines += ["  ".join(c.ljust(widths[i]) if i < len(cells) - 1 else c for i, c in enumerate(cells))
              for cells in table]
    stale_, failed = report.count("stale"), report.count("failed")
    held = "".join(f", {n} {status}" for status in ("cooling down", "waiting", "not known", NOT_DUE)
                   if (n := report.count(status)))
    held += f", {len(report.refused)} refused" if report.refused else ""
    lines += [f"{shown(id)} is archived, left alone" for id in report.archived]
    if not stale_ and not failed and not report.refused:
        runs = f"{report.fresh} agent run" + ("" if report.fresh == 1 else "s")
        lines.append(f"Nothing stale: {runs} fresh{held}. {DRY_END}")
    else:
        lines.append(f"{stale_} stale, {report.fresh} fresh, {failed} failed{held}. {DRY_END}")
    lines += [report.capped] if report.capped else []
    return [*lines, UNREAD_SOURCES] if report.whole else lines
