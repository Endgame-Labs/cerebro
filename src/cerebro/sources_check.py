"""`cerebro sources check <name>`: list one source as the next pass would, with no model, and say what that pass
does with each record.

A check keeps nothing. It lists from the cursor the last pass kept, or with `whole` from none, by running the pass's
own `runner._list` over the workspace's store seen through `_Unkept`, which reads the store and keeps what the pass
would write in memory alone: the cursor it gets back, a cursor that does not read, the time of the check. The pass
moves a source's cursor only once its records are handed to the facts layer, so a check that moved it would take
records from the next pass. Each record, and each id the last pass left to try again, is put to `runner.decide`, the
function the pass decides it by, called through the module as the pass calls it, in the order the pass puts them;
the check gets only the records the pass gets, and puts each it got to `decide` again, as the pass does before it
reads one. It tests no record itself. Which curators `serve --once` runs before it takes records in is the pass's own
`runner._curate` choice, run the same way with a curator that runs no model. A source the pass refuses is refused
here in the line the workspace gave when it loaded, and a listing or a get that fails is named as a pass names it.
Every line, a refusal's included, comes through the workspace's mask of every value its secrets store holds, and a
record is shown by its id and what it extracts into, never its text.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from . import runner
from .connector import Record
from .curate import proposing
from .errors import CerebroError
from .first_run import in_first_run
from .held import waits_for_own
from .runner import DROP, FIRST_HOLD, HOLD, NOTHING, REFUSE, Decision, PassResult, _Listed
from .source import ALL, SourceError, read_source, span
from .store import LogRow, record_from

if TYPE_CHECKING:
    from .agent import Agent
    from .app import Cerebro
    from .store import Store

__all__ = ["CheckRefused", "Checked", "check"]


class CheckRefused(CerebroError):
    """A source a check cannot list, in the line the workspace gave for it."""


FACTS = "for the next pass"
TRIED = "for the next pass, which tries it again"
UNHELD = "for the next pass, which held it until now"
UNREAD = "for the next pass, which reads its fields alone, none of its text: "
GOT_NOTHING = "for the next pass, which gets it and takes in its fields alone: "
REFUSED = "for the next pass, which gets it and stops at the spending cap: it and every record after it wait unread"
LET_GO = "let go: past the bound of held records, it comes back when its source lists it again"
RUNS = "cerebro serve --once runs it before it takes records in"
WAITS = "it is read, and waits for its own collection's curator, since no article there binds it"


@dataclass
class Checked:
    """What one check found and what the next pass will do with each record. `failure` is the listing's failure, in
    the words a pass gives it; `failed` each get's, by the key a pass names it under. `rows` holds, by id, what the
    next pass does with each record the check names; `take` the ids it gets and reads into facts, `got` those of them
    the check got, `nothing` and `refused` those it gets and reads nothing of; `unread` the ids listed whose source
    reads them for nothing; `held` the ids it holds, in the order it keeps them, each with its decision, `into` the collection
    each held record lands in, and `let_go` those past the bound. `curators` says, by curator, what `serve --once`
    does with it before it takes records in, for each curator that keeps a collection a held record waits on.
    `first_run` says whether `serve --once` runs the workspace's first run instead of a pass. `opened` is the one line
    a check of a source that lists nothing by design gives, once the check opened it."""

    name: str
    since: str
    whole: bool = False
    cursor: bool = False
    listed: list[Record] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    rows: dict[Any, tuple[str, str]] = field(default_factory=dict)
    take: list[Any] = field(default_factory=list)
    got: list[str] = field(default_factory=list)
    nothing: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    unread: list[Any] = field(default_factory=list)
    held: dict[str, Decision] = field(default_factory=dict)
    into: dict[str, str] = field(default_factory=dict)
    let_go: list[str] = field(default_factory=list)
    curators: dict[str, str] = field(default_factory=dict)
    admitting: bool = False
    first_run: bool = False
    failed: dict[str, str] = field(default_factory=dict)
    left: int = 0
    limit: int = 0
    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    failure: str = ""
    opened: str = ""
    mask: Callable[[str], str] = field(default=lambda text: text, repr=False)

    def lines(self) -> list[str]:
        """The check as the command prints it, one labelled line each, as a pass prints its own, every line masked."""
        return [self.mask(line) for line in self._lines()]

    def _waiting(self) -> list[str]:
        """The ids the next pass reads into facts: those the check got and `decide` takes, and those past --limit."""
        prefix = f"sources/{self.name}/"
        out = {*map(str, self.nothing), *map(str, self.refused), *(key.removeprefix(prefix) for key in self.failed)}
        return [id for id in dict.fromkeys(self.take) if str(id) not in out]

    def _lines(self) -> list[str]:
        if self.opened:
            return [f"notice    {self.opened}"]
        mask = self.mask
        out = [f"notice    {line}" for line in self.notices]
        if not self.failure or self.listed:
            out.append(f"listed    {self.name}: {_count(len(self.listed), 'record')} {self.since}")
        shown = [(mask(str(id)), mask(kind), what) for id, (kind, what) in self.rows.items()
                 if id in self.got or id not in self.take]
        width = max((len(id) for id, _, _ in shown), default=0)
        kinds = max((len(kind) for _, kind, _ in shown), default=0)
        for id, kind, what in shown:
            out.append("  ".join(part for part in (f"record    {id:<{width}}", f"{kind:<{kinds}}" if kinds else "",
                                                   what) if part))
        out += [f"warning   {line}" for line in self.warnings]
        out += [f"failed    {key}: {why}" for key, why in self.failed.items()]
        if self.failure:
            out.append(f"failed    sources/{self.name}: {self.failure}")
        if self.removed:
            out.append(f"removed   {self.name}: {_count(len(self.removed), 'record')} the source says "
                       f"{'is' if len(self.removed) == 1 else 'are'} gone, whose facts the next pass takes back")
        if self.left:
            out.append(f"stopped   after {_count(self.limit, 'record')}, as --limit asks: "
                       f"{_count(self.left, 'record')} not fetched")
        if self.unread:
            out.append(f"unread    {_count(len(self.unread), 'record')}: the next pass reads none of "
                       f"{'its' if len(self.unread) == 1 else 'their'} text, only {'its' if len(self.unread) == 1 else 'their'} "
                       "fields, as its source extracts into records or none")
        if self.refused:
            one = len(self.refused) == 1
            out.append(f"stopped   at the spending cap: the next pass reads sources/{self.name} no further from the "
                       f"first record that needs the model, so {_count(len(self.refused), 'record')} "
                       f"{'waits' if one else 'wait'} unread: {next(iter(self.refused.values()))}")
        kept = {id: decision for id, decision in self.held.items() if id not in self.let_go}
        scoped = [id for id, decision in kept.items() if decision.why != FIRST_HOLD]
        if scoped:
            one = len(scoped) == 1
            out.append(f"held      {_count(len(scoped), 'record')} {'stays' if one else 'stay'} held: the next pass "
                       f"tests {'it' if one else 'them'} again, and holds {'it' if one else 'each'} until an article "
                       "in the working set binds it")
        if first := len(kept) - len(scoped):
            out.append(f"held      {_count(first, 'record')} {'stays' if first == 1 else 'stay'} held until the first "
                       "run's list is decided")
        if self.let_go:
            out.append(f"held      {_count(len(self.let_go), 'held record')} past the bound of {runner.HELD_MAX} "
                       f"{'is' if len(self.let_go) == 1 else 'are'} let go, the oldest first: each comes back when its "
                       "source lists it again")
        out += [f"curator   {id}: {what}" for id, what in self.curators.items()]
        waiting = len(self._waiting())
        if self.whole and self.cursor:
            out.append(f"found     {_count(len(self.listed), 'record')} in the whole window, of which the next pass "
                       "takes in only what changed since the cursor it kept: check without --all to see those")
        elif waiting:
            one = waiting == 1
            them = "it" if one else "them"
            names = (f"cerebro distill --source {self.name} takes {them} in" if self.first_run else
                     f"cerebro serve --once or cerebro distill --source {self.name} takes {them} in")
            out.append(f"waiting   {_count(waiting, 'record')} {'waits' if one else 'wait'} for the next pass: {names}")
        elif not self.failure and not self.unread:
            but = " but what a curator admits" if self.admitting else ""
            out.append(f"waiting   none{but}: the next pass has nothing of {self.name} to take in")
        if self.first_run and (waiting or self.unread) and not (self.whole and self.cursor):
            out.append("first run cerebro serve --once runs the workspace's first run instead, so each record is "
                       "held: it is taken in only once the first run's list is decided")
        return out


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _history(behind: str) -> str:
    """How far back a listing from no cursor reaches: the span as written."""
    if behind == ALL:
        return "all of its history"
    if behind.endswith("d"):
        return "the last day" if behind == "1d" else f"the last {behind[:-1]} days"
    return f"the last {behind}"


def _days(span: timedelta) -> str:
    """A span in whole days."""
    days = int(span.total_seconds() // 86400)
    return "day" if days == 1 else f"{days} days"


def _calendar(behind: timedelta | None, ahead: str) -> str:
    """How far a calendar's listing reaches, forward and back."""
    from .connectors.calendar import DEFAULT_AHEAD

    back = "all the past" if behind is None else f"the last {_days(behind)}"
    return f"the next {_days(span(ahead or DEFAULT_AHEAD))} and {back}"


def _since(workspace: Cerebro, name: str, cursor: str | None) -> str:
    """How far the listing reached: from the cursor, or, from none, the window the source file says it reads, its
    span as written, else what the source itself says it reads when the file says nothing. A calendar reads its
    whole window, ahead and back, at every listing, so its line names that window from a cursor too."""
    path = workspace.root / "sources" / f"{name}.yaml"
    try:
        source = read_source(path) if path.is_file() else None
        if source is not None and source.kind == "calendar":
            window = _calendar(source.behind(), source.window.ahead)
            return f"new or changed since the cursor the last pass kept, from {window}" if cursor is not None else \
                f"from {window}"
        if cursor is not None:
            return "new or changed since the cursor the last pass kept"
        if source is None:
            return "from the start"
        return f"from {_history(source.window.behind or source.behind_default())}"
    except (SourceError, ValueError):
        return "new or changed since the cursor the last pass kept" if cursor is not None else "from the start"


def refusal(workspace: Cerebro, name: str) -> str:
    """Why `name` cannot be checked, in the line the workspace gave for it when it loaded, or "" when it can."""
    if name in workspace.connectors:
        return ""
    for line in [*workspace.refused, *workspace.notices]:
        if line.startswith((f"sources/{name}.yaml:", f"sources/{name}.yaml ", f"sources/{name}:")):
            return line
    return f"no source named {name}: sources/{name}.yaml does not exist"


def check(workspace: Cerebro, name: str, *, whole: bool = False, limit: int = 0) -> Checked:
    """List the source `name` from the cursor the last pass kept, or with `whole` from none, to its end, and say what
    the next pass does with each record, as `runner.decide` decides it. Get each record the pass would get, at most
    `limit` when it is above 0. Reads the store and keeps nothing. A source that lists nothing by design, a database
    source, is instead opened once, read only, by a query that reads none of its tables. Raises `CheckRefused`,
    masked, for a source the workspace refused or does not list, and the masked error of one that does not open."""
    workspace.refresh_mask()
    mask = workspace.mask_every()
    if why := refusal(workspace, name):
        raise CheckRefused(mask(why))
    connector = workspace.connectors[name]
    if connector.lists_nothing:
        connector.select("select 1")
        return Checked(name, "", mask=mask, opened=f"sources/{name}: the database opens, read only, and "
                       f"{connector.lists_nothing}")
    store = workspace.store
    result = Checked(name, "", whole=whole, cursor=store.state(f"cursor:{name}") is not None, limit=limit, mask=mask,
                     first_run=in_first_run(workspace))
    listed, kept = _list(workspace, name, whole, result)
    result.listed, result.removed = listed.records, listed.removed
    result.since = _since(workspace, name, kept)
    _plan(workspace, name, result, list(store.state(f"failed:{name}") or []))
    _curators(workspace, result)
    todo = list(dict.fromkeys(result.take))
    if limit > 0 and len(todo) > limit:
        result.left, todo = len(todo) - limit, todo[:limit]
    for id in todo:
        if result.refused:  # the pass reads the source no further once the cap stops it, as `runner._extract` does
            result.refused[id] = next(iter(result.refused.values()))
            continue
        try:
            got = connector.get(id)
        except Exception as error:  # noqa: BLE001 - one record's failure is said, and the rest go on
            result.failed[f"sources/{name}/{id}"] = f"{type(error).__name__}: {error}"
            continue
        result.got.append(id)
        _got(result, id, got, runner.decide(workspace, got))
    return result


class _Unkept:
    """The workspace's store as the pass's own code sees it in a check: a read comes from the store, unless the pass
    wrote that key in this check, and a write stays here, in memory, and goes with the check. It answers only what
    the pass's listing and its choice of curators ask of a store, so a call that would change anything else fails."""

    def __init__(self, store: Store) -> None:
        self._store, self.kept, self.appended = store, {}, []

    def state(self, key: str) -> Any:
        return self.kept[key] if key in self.kept else self._store.state(key)

    def set_state(self, key: str, value: Any) -> None:
        self.kept[key] = value

    def exclusive(self) -> AbstractContextManager[None]:
        return nullcontext()

    def article(self, id: str) -> Any:
        return self._store.article(id)

    def rows(self, log: str, where: dict[str, Any] | None = None) -> list[LogRow]:
        """The log's rows, the store's and then those the pass added in this check, as the pass would read them."""
        return [r for r in [*self._store.rows(log), *(r for r in self.appended if r.log == log)]
                if all(r.data.get(k) == v for k, v in (where or {}).items())]

    def latest(self, log: str, key: str = "id", where: dict[str, Any] | None = None) -> list[LogRow]:
        newest: dict[str, LogRow] = {}
        for r in self.rows(log):
            if r.data.get(key) is not None:
                newest.pop(repr(r.data[key]), None)
                newest[repr(r.data[key])] = r
        return [r for r in newest.values() if all(r.data.get(k) == v for k, v in (where or {}).items())]

    def append(self, log: str, data: dict[str, Any]) -> None:
        """A row the pass would add, such as a yes it closes with nothing left to do: kept with the check alone."""
        seq = max((r.seq for r in self.rows(log)), default=0) + 1
        self.appended.append(LogRow(log, seq, dict(data)))


class _Ran(Exception):
    """A curator `serve --once` would run now, stopped before anything of its run starts."""


class _Unseen:
    """The workspace as the pass's own code sees it in a check: every attribute is the workspace's, but its store
    is `_Unkept`, and a curator the pass runs is refused before it starts, after `refused` keeps why its run would
    be refused, or "" when it would run."""

    def __init__(self, workspace: Cerebro) -> None:
        self._workspace, self.store, self.refused = workspace, _Unkept(workspace.store), {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._workspace, name)

    def curate(self, agent: Agent, **_: Any) -> Any:
        """Keep whether the curator's run would be refused before its model is asked, as `Cerebro.curate` refuses a
        run with no key and its run is refused at a spent cap, and stop it."""
        workspace, why = self._workspace, ""
        try:
            workspace.need_key(agent)
            workspace.ledger.check("compilation", workspace.clock(), workspace.model_of(agent))
        except CerebroError as error:
            why = f"{type(error).__name__}: {error}"
        self.refused[agent.id] = why
        raise _Ran


def _list(workspace: Cerebro, name: str, whole: bool, result: Checked) -> tuple[_Listed, str | None]:
    """List as the next pass lists, with the pass's own `runner._list`, over a store that keeps nothing, from no
    cursor with `whole`. Gives what was listed and the cursor it was listed from."""
    unseen = _Unseen(workspace)
    if whole:
        unseen.store.set_state(f"cursor:{name}", None)
    kept, seen = unseen.store.state(f"cursor:{name}"), PassResult()
    sources = runner._list(unseen, seen, only=name)  # type: ignore[arg-type]
    listed = sources[0] if sources else _Listed(workspace.connectors[name])
    lead = f"sources/{name}: its cursor did not read ("
    for line in seen.notices:  # the pass's own line says which way it went; a test ties each way to the pass
        if line.startswith(lead):
            why = line.removeprefix(lead).rpartition("), so ")[0]
            if line.endswith("listed in full"):
                kept = None
                result.notices.append(f"{lead}{why}), so the next pass drops it and lists the source in full, as "
                                      "the check did")
            else:
                result.notices.append(f"{lead}{why}), so the next pass clears it: the pass after lists the source "
                                      "from the start")
    result.failure = seen.failed.get(f"sources/{name}", "") or seen.skipped.get(f"sources/{name}", "")
    result.warnings = seen.warnings
    return listed, kept


def _curators(workspace: Cerebro, result: Checked) -> None:
    """Say what `serve --once` does, before it takes records in, with each curator that keeps a collection a held
    record waits on, as the pass's own `runner._curate` chooses: it runs it, refuses it, or leaves it, and why."""
    waits = {result.into[id] for id, decision in result.held.items()
             if id not in result.let_go and decision.why != FIRST_HOLD}
    curators = [agent for agent in workspace.agents if agent.role == "curator" and agent.target in waits]
    if result.first_run or not curators:
        return
    unseen, seen = _Unseen(workspace), PassResult()
    runner._curate(unseen, seen)  # type: ignore[arg-type]
    for agent in curators:
        held = f"each held record of {agent.target} stays held"
        if agent.id not in unseen.refused:
            what = f"cerebro serve --once does not run it: {seen.skipped.get(agent.id, 'it is not due')}; {held}"
        elif why := unseen.refused[agent.id]:
            what = f"cerebro serve --once refuses it before it runs: {why}; {held}"
        elif proposing(workspace.store, agent.id):
            what = (f"{RUNS}, in its first run, so what it admits waits for a person: {held} until a person accepts "
                    f"an article that binds it, with cerebro review --accept-from agents/{agent.id}.md")
        else:
            result.admitting = True
            what = (f"{RUNS}, so a held record of {agent.target} is taken in that same pass when the curator admits "
                    f"an article that binds it: cerebro distill --source {result.name} runs no curator")
        result.curators[agent.id] = what


def _plan(workspace: Cerebro, name: str, result: Checked, tried: list[str]) -> None:
    """Put each id the last pass left to try again, then each record held before and each listed, to `decide`, in the
    order `runner._extract_all` puts them, and keep what it decides. A record the pass gets and reads is said so even
    when the pass then holds it again, since reading it spends model money."""
    store = workspace.store
    ahead = replace(runner.ahead_of(workspace), gone=set(result.removed))
    for id in tried:
        if runner.decide(workspace, id, ahead).act != DROP:
            result.rows.setdefault(id, ("", TRIED))
            result.take.append(id)
    was_held = store.state(f"held:{name}") or []
    ahead = replace(ahead, first={row["id"] for row in was_held})
    fresh = {record.id for record in result.listed}
    before = [record_from(row) for row in was_held if row["id"] not in ahead.gone and row["id"] not in fresh]
    taken = {row["id"] for row in was_held if row.get("read") and row["id"] not in fresh}
    curated = ahead.curated
    for record in [*before, *result.listed]:
        listed = record.id in fresh
        if record.id in taken:  # read already, as the pass skips it: held while it waits for its own article
            if waits_for_own(store, record, curated):
                result.held[record.id], result.into[record.id] = Decision(HOLD, WAITS), record.into
            continue
        decision = runner.decide(workspace, record, ahead)
        if decision.act == HOLD:
            result.held[record.id] = decision
            result.into[record.id] = record.into
            if listed and record.id not in result.take:
                result.rows[record.id] = (record.extract, f"held: {decision.why}")
            continue
        result.held.pop(record.id, None)
        if waits_for_own(store, record, curated):  # read, and held for its own collection's curator all the same
            result.held[record.id], result.into[record.id] = Decision(HOLD, WAITS), record.into
        if decision.act == NOTHING:
            result.unread.append(record.id)
            result.rows.setdefault(record.id, (record.extract, UNREAD + decision.why))
            continue
        result.take.append(record.id)
        result.rows.setdefault(record.id, (record.extract, FACTS if listed else UNHELD))
    result.let_go = runner.past_bound(list(result.held))
    for id in result.let_go:
        if id in result.rows and id not in result.take:
            result.rows[id] = (result.rows[id][0], LET_GO)


def _got(result: Checked, id: Any, got: Record, decision: Decision) -> None:
    """Keep what the pass does with a record it got whole, as `decide` says of it."""
    kind, what = result.rows.get(id, ("", FACTS))
    if decision.act == NOTHING:
        result.nothing.append(id)
        what = GOT_NOTHING + decision.why
    elif decision.act == REFUSE:
        result.refused[id] = decision.why
        what = REFUSED
    result.rows[id] = (kind, what) if kind else (got.extract, what)
