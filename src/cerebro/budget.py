"""The daily caps, and the ledger of what model calls cost.

Four layers spend: extraction, which turns records into facts, compilation, which writes articles, evaluation,
which `cerebro eval` spends, and rerank, which the decision model's re-ranking of a search spends when settings turn
it on. Each has its own cap in dollars per UTC day, set in `settings.yaml`:

    budget:
      extraction_daily_usd: 5
      compilation_daily_usd: 15
      evaluation_daily_usd: 2  # evals take $2 a day when it is left out
      rerank_daily_usd: 0.5    # a search's re-ranking takes $0.50 a day when it is left out
      daily_usd: 20          # optional: a cap on them together

`daily_usd` caps all spend together. Evaluation and rerank keep their own caps when their keys are left out, and
extraction or compilation left out, or empty, has no cap of its own. A decision call counts toward the layer that asked it.

A workspace's first run has a cap of its own on what discovering spends, `budget.first_run_usd`, $10 when the key is
left out, beside the day's caps: its curators' runs count toward it in the same ledger, by their run ids, across the
runs of one first run. `estimate` prices what compiling a number of articles would cost before any of it runs: one
run of each agent that writes such an article, at what that agent's last run spent, or for an agent with no run that
spent anything, its file's own `budget.tokens_per_run` when it sets one, else `DEFAULT_RUN`, priced on its model at the
catalog's prices. A figure with any such run in it is a guess, and says so; a model the catalog has no price for makes
the cost unknown, never $0.

Spend is kept as rows of the store's `spend` log: when, which layer, dollars, provider, model, and which run. Rows
are only appended, one per call or run, so runs that finish at once never lose one another's spend. A layer's spend
for a day is the sum of that day's rows.

Before an agent's run, or a model call outside one, a caller asks `check` or `may_spend`; after it, `record`. Past its
cap a layer starts nothing new until 00:00 UTC and the other layers carry on. A run or call that began finishes and
counts, so a day can pass a cap by what was under way, each run within its own budget. Reads never touch the ledger,
so they keep working. A model with no known price is refused while its layer has a cap, since it would count $0; with
no cap it runs and is kept at $0, marked unpriced. The time is always an argument: nothing here reads the clock.
"""

from __future__ import annotations

import logging
import math
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from .connector import Record
from .errors import CerebroError
from .facts import Extracted, Extraction, ExtractionFailed, Fact
from .models import ModelSpec, TokenCount, Tokens, cost, price_of
from .store import Store

__all__ = [
    "DEFAULT_RUN",
    "EVALUATION_DAILY_USD",
    "FIRST_RUN_USD",
    "LAYERS",
    "LOG",
    "METER",
    "RERANK_DAILY_USD",
    "SETTINGS_KEYS",
    "BudgetError",
    "BudgetSpent",
    "CallRefused",
    "Caps",
    "Ledger",
    "Meter",
    "MeterTurn",
    "Metered",
    "Priced",
    "RoundCut",
    "Stopped",
    "UnpricedModel",
    "cancelling",
    "estimate",
    "first_run_cap",
    "read_caps",
    "run_estimate",
    "spent_by_runs",
    "stop",
    "stop_line",
    "stopping",
    "under",
]

LAYERS = ("extraction", "compilation", "evaluation", "rerank")
EVALUATION_DAILY_USD = 2.0  # the evaluation layer's cap when settings name none: evals are run by hand
# The rerank layer's cap when settings name none. A search's re-ranking is one decision call on about 20 passages,
# some 4,000 input tokens: at the decision models' $0.042 per million, about $0.0002, so $0.50 is some 3,000 searches.
RERANK_DAILY_USD = 0.5
SEARCH_CHARS = 400  # the longest facts search a handler is handed
LOG = "spend"
LOWEST, HIGHEST = 1e-9, 1e9  # the amounts a cap may name: a nano-dollar to a billion
FIRST_RUN_USD = 10.0  # what a first run may spend discovering when settings name no budget.first_run_usd
# One run of an agent that has spent nothing yet, as an estimate prices it: some $0.01 a run on glm-5p3, where 6,000
# input tokens and 600 output price at $0.011.
DEFAULT_RUN = TokenCount(input_tokens=6_000, output_tokens=600)


class BudgetError(CerebroError, ValueError):
    """A `budget:` setting Cerebro cannot run on, or a call that names no layer or no time zone."""


class BudgetSpent(CerebroError):
    """A layer's spend for the UTC day has reached its cap. Its calls wait for the next day."""


class CallRefused(BudgetSpent):
    """A host model calls go through refused one in a way no try again mends: the pass starts no more of them."""


class RoundCut(BudgetSpent):
    """A run a meter refused: at its first model call once the spend it meters, of runs under way and runs ended, has
    reached its cap, so it does not start, or at any call once the meter is stopped, so it stops where it is, keeping
    what its turns spent."""


class Meter:
    """The spend of a set of runs side by side, held to one cap, as a first-run round is metered: every
    run under it is checked against the cap before its first model call, and what each reply cost is counted the
    moment it comes, so once the cap is reached no run starts, and each run under way finishes. `base` gives what the
    set spent before, as the ledger has it. A run opens a `turn` of it, which the `METER` context variable holds while
    the run is under way, and `stop` cuts every run at its next call, as a signal or a host's refusal does."""

    def __init__(self, cap: float, base: Callable[[], float]):
        self.cap, self.base = cap, base
        self._lock = threading.Lock()
        self._turns: list[MeterTurn] = []
        self.stopped = ""

    def spent(self) -> float:
        """What the set spent: what the ledger has of the runs that ended, and what the replies of each run still
        under way cost so far. A run's spend is in the ledger once it ends, before its turn closes, so for that moment
        it counts twice: the cap errs toward stopping."""
        with self._lock:
            running = sum(turn.usd for turn in self._turns if not turn.closed)
        return self.base() + running

    def reached(self) -> bool:
        return bool(self.stopped) or self.spent() >= self.cap

    def turn(self, name: str) -> MeterTurn:
        made = MeterTurn(self, name)
        with self._lock:
            self._turns.append(made)
        return made

    def stop(self, why: str) -> None:
        """Cut every run under the meter at its next model call."""
        self.stopped = self.stopped or why


class MeterTurn:
    """One run's share of a `Meter`: `before` refuses the run's first call once the meter's cap is reached, and any
    call once the meter is stopped, `add` counts what a reply cost, and `close` says the run ended and the ledger has
    its spend."""

    def __init__(self, meter: Meter, name: str):
        self.meter, self.name, self.usd, self.closed, self.cut, self.begun = meter, name, 0.0, False, False, False

    def before(self) -> None:
        if self.meter.stopped or not self.begun and self.meter.spent() >= self.meter.cap:
            self.cut = True
            raise RoundCut(self.meter.stopped or f"the cap of ${self.meter.cap:g} was reached before {self.name} "
                                                 "started, so it did not run")
        self.begun = True

    def add(self, usd: float) -> None:
        with self.meter._lock:
            self.usd += max(usd, 0.0)

    def close(self) -> None:
        self.closed = True


METER: ContextVar[MeterTurn | None] = ContextVar("cerebro_meter", default=None)  # the run's turn of a meter, if any


class Stopped(BaseException):
    """A model call refused, or a run cancelled, because the process serving the workspace is stopping. It is no
    failure: what it left runs on a later pass. Like asyncio's
    CancelledError it is no `Exception`, so a pass's catch-all for one record's failure lets it through."""


class _Stop:
    """The stop of one workspace in this process: once set, no model call starts, and each under way is cancelled."""

    def __init__(self) -> None:
        self.lock, self.set, self.calls = threading.Lock(), False, dict[int, Callable[[], None]]()


_STOPS: weakref.WeakKeyDictionary[Any, _Stop] = weakref.WeakKeyDictionary()
_STOPS_LOCK = threading.Lock()
_UNDER: ContextVar[_Stop | None] = ContextVar("cerebro_stop", default=None)


def _stop_of(store: Any) -> _Stop:
    with _STOPS_LOCK:
        return _STOPS.setdefault(store, _Stop())


def stop(store: Any) -> None:
    """Stop every agent's run on the workspace of `store` in this process: none starts after this, each under way is
    cancelled now, and no model call starts inside one."""
    found = _stop_of(store)
    with found.lock:
        found.set, calls = True, list(found.calls.values())
    for cancel in calls:
        try:
            cancel()
        except Exception:
            logging.getLogger(__name__).debug("a cancel of a run under way failed", exc_info=True)


def stopping(store: Any) -> bool:
    """Whether `stop` was called on the workspace of `store`."""
    with _STOPS_LOCK:
        found = _STOPS.get(store)
    return found is not None and found.set


@contextmanager
def under(store: Any) -> Iterator[None]:
    """Run the block as a model call on the workspace of `store`, so `cancelling` in it reaches its stop."""
    token = _UNDER.set(_stop_of(store))
    try:
        yield
    finally:
        _UNDER.reset(token)


@contextmanager
def cancelling(cancel: Callable[[], None]) -> Iterator[None]:
    """Hand `cancel` to the stop of the model call the block runs under, called the moment the stop comes, at once
    when it came already. Outside `under`, nothing calls it."""
    found = _UNDER.get()
    if found is None:
        yield
        return
    with found.lock:
        key, now = id(cancel), found.set
        if not now:
            found.calls[key] = cancel
    if now:
        cancel()
    try:
        yield
    finally:
        with found.lock:
            found.calls.pop(key, None)


class UnpricedModel(BudgetSpent):
    """A call on a model with no known price, while its layer has a cap."""


@dataclass(frozen=True)
class Caps:
    """Dollars per UTC day: one cap for each layer, and an optional cap on all of them together. None means no cap."""

    extraction: float | None = None
    compilation: float | None = None
    total: float | None = None
    evaluation: float | None = EVALUATION_DAILY_USD
    rerank: float | None = RERANK_DAILY_USD

    def of(self, layer: str) -> float | None:
        return {"extraction": self.extraction, "compilation": self.compilation, "evaluation": self.evaluation,
                "rerank": self.rerank}[layer]


def _cap(block: Mapping[str, Any], key: str) -> float | None:
    value = block.get(key)
    if value is None:
        return None
    where = f"settings.yaml budget.{key} is {value!r}"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            isinstance(value, float) and not math.isfinite(value)):  # a whole number of any size is finite
        raise BudgetError(f"{where}: write a number of dollars, or leave the key out for no cap")
    if value <= 0:
        raise BudgetError(f"{where}, which would stop every model call: write an amount above zero, or leave the "
                          "key out for no cap")
    if not LOWEST <= value <= HIGHEST:
        raise BudgetError(f"{where}: write an amount from 0.000000001 to 1000000000, or leave the key out for no cap")
    return float(value)


SETTINGS_KEYS = {"budget": dict.fromkeys(("daily_usd", "extraction_daily_usd", "compilation_daily_usd",
                                          "evaluation_daily_usd", "rerank_daily_usd", "first_run_usd"))}


def read_caps(settings: Mapping[str, Any]) -> Caps:
    """The caps in the `budget:` block of settings. An empty or absent block means no caps."""
    block = settings.get("budget")
    if block is None:
        return Caps()
    if not isinstance(block, dict):
        raise BudgetError("settings.yaml budget is not a map: write extraction_daily_usd and compilation_daily_usd "
                          "under it")
    evaluation = _cap(block, "evaluation_daily_usd")
    rerank = _cap(block, "rerank_daily_usd")
    return Caps(_cap(block, "extraction_daily_usd"), _cap(block, "compilation_daily_usd"), _cap(block, "daily_usd"),
                EVALUATION_DAILY_USD if evaluation is None else evaluation,
                RERANK_DAILY_USD if rerank is None else rerank)


def first_run_cap(settings: Mapping[str, Any]) -> float:
    """What a first run may spend discovering: `budget.first_run_usd`, or `FIRST_RUN_USD` when it is left out."""
    block = settings.get("budget")
    given = _cap(block, "first_run_usd") if isinstance(block, dict) else None
    return FIRST_RUN_USD if given is None else given


def spent_by_runs(store: Store, runs: list[str]) -> float:
    """Dollars the ledger holds for these runs, by their ids, whatever the day."""
    return sum(store.total(LOG, "usd", {"run": run}) for run in dict.fromkeys(runs) if run)


def _clock(at: str, zone: tzinfo) -> str:
    return datetime.fromisoformat(at).astimezone(zone).strftime("%H:%M %Z")


def stop_line(reached: Mapping[str, Any], zone: tzinfo) -> str:
    """The stop line of a cap reached, as `Ledger.reached` gives it, in the workspace's zone: the
    sum at the call that reached it, the cap and the minute that was, then what waits, by the counts a pass kept under
    `records` and `agent_runs`, each kind it looked at counted, none included, or when calls resume, with none kept:
    `Daily budget: $52.40 of $50.00 spent at 07:02 PDT. 412 records and 18 agent runs wait.`"""
    layer = reached["layer"]
    what = "Daily budget" if layer == "all" else f"Daily {layer} budget"
    head = f"{what}: ${reached['spent']:,.2f} of ${reached['usd']:,.2f} spent at {_clock(reached['at'], zone)}."
    counts = [(int(reached[kind]), noun) for kind, noun in (("records", "record"), ("agent_runs", "agent run"))
              if reached.get(kind) is not None]
    if not counts:
        calls = "Model calls" if layer == "all" else f"{layer.capitalize()} calls"
        return f"{head} {calls} resume at {_clock(reached['resumes'], zone)}."
    said = " and ".join(f"{n:,} {noun}" + ("" if n == 1 else "s") for n, noun in counts)
    return f"{head} {said} {'waits' if len(counts) == 1 and counts[0][0] == 1 else 'wait'}."


@dataclass(frozen=True)
class Priced:
    """An estimate: dollars at catalog prices, whether any run in it was guessed, having nothing run yet to go by, and
    each model the catalog has no price for, which makes the cost unknown."""

    usd: float = 0.0
    guessed: bool = False
    unpriced: tuple[str, ...] = ()

    def __add__(self, other: Priced) -> Priced:
        return Priced(self.usd + other.usd, self.guessed or other.guessed,
                      tuple(dict.fromkeys([*self.unpriced, *other.unpriced])))

    def times(self, n: int) -> Priced:
        return Priced(self.usd * n, self.guessed, self.unpriced)


def run_estimate(store: Store, agent: str, model: ModelSpec | None, tokens: int | None = None) -> Priced:
    """What one run of the agent `agent`, by its id, is estimated to cost: what its last run that spent anything
    spent; else, a guess, `guessed_run`: `DEFAULT_RUN`, held to `tokens`, its file's own budget per run when it sets
    one, on `model`; else unknown, when the catalog has no price for `model`."""
    for row in reversed(store.rows("agent-runs", {"agent": agent})):
        if run := row.data.get("run"):
            spent = store.total(LOG, "usd", {"run": run})
            if spent > 0:
                return Priced(spent)
    if model is None or price_of(model.provider, model.model) is None:
        return Priced(unpriced=(f"{model.provider} {model.model}" if model else "no model",))
    return Priced(cost(model.provider, model.model, guessed_run(tokens)), guessed=True)


def guessed_run(tokens: int | None = None) -> TokenCount:
    """What a run nothing has priced yet is guessed to spend: `DEFAULT_RUN`, and no more than `tokens`, an agent's own
    budget per run, in the same share of input and output. A budget is what a run may spend, not what it does, so it
    lowers the guess and never raises it."""
    whole = DEFAULT_RUN.input_tokens + DEFAULT_RUN.output_tokens
    if not tokens or tokens >= whole:
        return DEFAULT_RUN
    return TokenCount(input_tokens=DEFAULT_RUN.input_tokens * tokens // whole,
                      output_tokens=DEFAULT_RUN.output_tokens * tokens // whole)


def estimate(store: Store, writers: list[tuple[str, str, ModelSpec | None, int | None]],
             articles: Mapping[str, int]) -> Priced:
    """What compiling articles would cost at catalog prices: for each collection, its count in `articles` times one
    run of each of `writers`, an agent's id, the collection it writes, its model and its file's own budget per run or
    None, that writes an article of it. It counts no distill, no curator and no article that compiles again."""
    total = Priced()
    for agent, collection, model, tokens in writers:
        if articles.get(collection):
            total = total + run_estimate(store, agent, model, tokens).times(articles[collection])
    return total


def _utc(at: datetime) -> datetime:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise BudgetError(f"the time {at!r} has no time zone: pass an aware datetime, such as datetime.now(UTC)")
    return at.astimezone(UTC)


def _layer(layer: str) -> str:
    if layer not in LAYERS:
        raise BudgetError(f"{layer!r} is not a layer that spends: use extraction, compilation, evaluation or rerank")
    return layer


class Ledger:
    """The day's spend of a workspace, kept in its store, held to its caps."""

    def __init__(self, store: Store, caps: Caps, refused: Callable[[], None] = lambda: None):
        self.store = store
        self.caps = caps
        self.refused = refused  # raises `CallRefused` once a host model calls go through refused one this pass
        self._lock = threading.Lock()
        self.recorded = 0  # the calls and runs this ledger kept the cost of: what its command paid for

    def spent(self, at: datetime, layer: str | None = None) -> float:
        """Dollars spent on the UTC day of `at`, by one layer, or by every layer with no layer."""
        where: dict[str, Any] = {"day": _utc(at).date().isoformat()}
        if layer is not None:
            where["layer"] = _layer(layer)
        return self.store.total(LOG, "usd", where)

    def check(self, layer: str, at: datetime, model: ModelSpec | None = None) -> None:
        """Refuse a call `layer` would start at `at`: `BudgetSpent` past a cap, `UnpricedModel` for a model with no
        known price while a cap is set. With no cap, nothing is refused but a call inside an agent's run on a
        workspace whose serve is stopping, `Stopped`. A call outside a run, such as a request's own, is not: a
        request has the stop's wait to answer."""
        if _UNDER.get() is not None and stopping(self.store):
            raise Stopped(f"serve is stopping, so no {layer} model call starts: it runs on a later pass")
        self.refused()
        cap, total = self.caps.of(_layer(layer)), self.caps.total
        day = _utc(at).date().isoformat()
        if cap is None and total is None:
            return
        if model is not None and price_of(model.provider, model.model) is None:
            raise UnpricedModel(f"{model.provider} model {model.model} has no known price, so {layer} does not run "
                                "it while a daily cap is set: pick a priced model, or remove the cap")
        if cap is not None and (spent := self.spent(at, layer)) >= cap:
            raise BudgetSpent(f"{layer} spent ${spent:.2f} of its ${cap:g} cap on {day} (UTC): its model calls wait "
                              f"for 00:00 UTC, or raise budget.{layer}_daily_usd")
        if total is not None and (spent := self.spent(at)) >= total:
            raise BudgetSpent(f"model calls spent ${spent:.2f} of the ${total:g} daily cap on {day} (UTC): they wait "
                              "for 00:00 UTC, or raise budget.daily_usd")

    def holding(self, layer: str, at: datetime) -> str | None:
        """The setting of the cap that holds `layer`'s calls at `at`, as `check` finds it, its layer's own before the
        one on all spend, or None while neither is reached."""
        cap, total = self.caps.of(_layer(layer)), self.caps.total
        if cap is not None and self.spent(at, layer) >= cap:
            return f"{layer}_daily_usd"
        if total is not None and self.spent(at) >= total:
            return "daily_usd"
        return None

    def reached(self, at: datetime) -> list[dict[str, Any]]:
        """Each cap the spend of `at`'s UTC day reached, the cap on all spend first: its setting, its layer, the cap,
        the sum at the call that reached it, when that was, and when calls resume, at 00:00 UTC."""
        day = _utc(at).date()
        rows = [row.data for row in self.store.rows(LOG, {"day": day.isoformat()})]
        resumes = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
        out = []
        for layer, cap, key in [("all", self.caps.total, "daily_usd"),
                                *((layer, self.caps.of(layer), f"{layer}_daily_usd") for layer in LAYERS)]:
            if cap is None:
                continue
            spent = 0.0
            for row in rows:
                if layer != "all" and row.get("layer") != layer:
                    continue
                spent += float(row.get("usd") or 0)
                if spent >= cap:
                    out.append({"cap": key, "layer": layer, "usd": cap, "spent": round(spent, 6),
                                "at": str(row["at"]), "resumes": resumes.isoformat().replace("+00:00", "Z")})
                    break
        return out

    def may_spend(self, layer: str, at: datetime, model: ModelSpec | None = None) -> bool:
        """Whether `layer` may start a model call at `at`. `check` says why not."""
        try:
            self.check(layer, at, model)
        except BudgetSpent:
            return False
        return True

    def record(self, layer: str, at: datetime, model: ModelSpec, usage: Tokens, run: str = "") -> dict[str, Any]:
        """Keep what one call or run cost, priced on the model it asked for. Gives the row kept."""
        when = _utc(at)
        row: dict[str, Any] = {
            "at": when.isoformat(timespec="seconds").replace("+00:00", "Z"), "day": when.date().isoformat(),
            "layer": _layer(layer), "usd": cost(model.provider, model.model, usage), "provider": model.provider,
            "model": model.model, "run": run,
        }
        if price_of(model.provider, model.model) is None:
            row["unpriced"] = True
        with self._lock:
            self.recorded += 1
        return self.store.append(LOG, row).data

    def record_extracted(self, at: datetime, model: ModelSpec, spent: Extracted, run: str = "") -> None:
        """Keep what an extraction spent under extraction, a row for each model that spent it, priced on that model.
        What no model is named for, or a model with no known price spent, is `model`'s, so no row of Factoid's work
        counts $0 under a cap. One row is kept when nothing was spent."""
        rows = []
        for name, (given, taken) in spent.models.items():
            provider, _, named = name.partition("/")
            if (given or taken) and price_of(provider, named) is not None:
                rows.append((ModelSpec(provider, named), TokenCount(given, taken)))
        rest = TokenCount(spent.input_tokens - sum(u.input_tokens for _, u in rows),
                          spent.output_tokens - sum(u.output_tokens for _, u in rows),
                          spent.cache_read_tokens, spent.cache_write_tokens)
        if rest != TokenCount() or not rows:
            rows.insert(0, (model, rest))
        for spec, usage in rows:
            self.record("extraction", at, spec, usage, run)


class Metered(Extraction):
    """An extraction handler held to the extraction cap: each `extract` and `search` is checked against the ledger
    before it starts, and what it spent is recorded after, priced on each model that spent it, even when it raises. Every other call
    goes straight through. A tell's extraction goes through one, as the runner's does through its own checks."""

    def __init__(self, handler: Extraction, ledger: Ledger, model: ModelSpec, clock: Callable[[], datetime]):
        self.handler, self.ledger, self.model, self.clock = handler, ledger, model, clock
        # one call at a time, as the facts layer runs them anyway, so a call is checked when it starts, not before
        # it waits behind another
        self._one = threading.Lock()

    def extract(self, record: Record) -> Extracted:
        with self._one:
            self.ledger.check("extraction", self.clock(), self.model)
            extracted = Extracted([])  # a handler that raises spent what its error reports, or nothing
            try:
                extracted = self.handler.extract(record)
                return extracted
            except ExtractionFailed as error:
                extracted = error.spent
                raise
            finally:
                self.ledger.record_extracted(self.clock(), self.model, extracted, record.id)

    def sent(self, record: Record) -> str:
        return self.handler.sent(record)

    def facts(self, **filters: Any) -> list[Fact]:
        return self.handler.facts(**filters)

    def retract(self, fact_id: str) -> None:
        self.handler.retract(fact_id)

    def search(self, text: str, top_k: int = 10, **options: Any) -> list[Any]:
        """A search, held to the extraction cap, since the handler may embed its words, and to `SEARCH_CHARS`.
        What it spent, as the handler's `search_spent` reports it, is recorded under extraction, priced on the model that
        spent it as an extraction's own embedding is, when it spent any, and when it raised after spending too."""
        if len(text) > SEARCH_CHARS:
            raise BudgetError(f"a facts search takes at most {SEARCH_CHARS} characters, and this one has "
                              f"{len(text)}: search by a few words")
        with self._one:
            self.ledger.check("extraction", self.clock(), self.model)
            spent = Extracted([])
            try:
                hits, spent = self.handler.search_spent(text, top_k, **options)
                return hits
            except ExtractionFailed as error:  # a search that failed after its calls still spent them
                spent = error.spent
                raise
            finally:
                if spent.input_tokens or spent.output_tokens:
                    self.ledger.record_extracted(self.clock(), self.model, spent, "search")
