"""Single model calls under a cap: the keep judge and grounding's judge under compilation, the re-ranker under
rerank, and metered extraction and search under extraction. Each call is checked against the cap before it starts,
and once the cap is met nothing new starts."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from cerebro import Record
from cerebro.budget import LOG, BudgetSpent, Caps, Ledger, Metered
from cerebro.compile import _Held
from cerebro.evidence import DecisionKeepJudge, Passage, estimate_tokens
from cerebro.facts import Extracted
from cerebro.fakes import FakeExtraction
from cerebro.models import Answer, Decision, ModelSpec, TokenCount
from cerebro.rerank import DecisionReranker
from cerebro.search import FACT, Hit
from cerebro.store import OWN, SQLiteStore

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
RUNE = ModelSpec("fireworks", "rune", job="decision")
FLASH = ModelSpec("fireworks", "glm-5p3-flash", "low", "extraction")


class StandIn:
    """A decision model that answers yes to each question and spends what it was sent, after `meet`, which stands
    for the time a real call takes."""

    def __init__(self, meet: Callable[[], None] = lambda: None):
        self.meet, self.calls, self.lock = meet, 0, threading.Lock()

    def decide(self, state: Any, questions: Any) -> Decision:
        with self.lock:
            self.calls += 1
        self.meet()
        sent = estimate_tokens(json.dumps([state, {k: str(q) for k, q in questions.items()}], default=str))
        return Decision({k: Answer(yes=0.9) for k in questions}, model="stand-in",
                        usage=TokenCount(input_tokens=sent))


class Extracting(FakeExtraction):
    """Extraction that spends what its record's body holds."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def extract(self, record: Record) -> Extracted:
        self.calls += 1
        got = super().extract(record)
        return Extracted(got.facts, input_tokens=estimate_tokens(record.body or ""))

    def search_spent(self, text: str, top_k: int = 10, *, source: str | None = None) -> tuple[list[Hit], Extracted]:
        self.calls += 1
        return [], Extracted([], input_tokens=estimate_tokens(text))


def note(n: int) -> Record:
    return Record(id=f"n{n}", source="notes", revision="1", body=f"A note {n} on the west team. " * 40)


def test_keep_scores_cached_side_by_side_are_all_kept() -> None:
    met = threading.Barrier(2, timeout=0.5)  # both read the cache before either writes it back
    decider = StandIn(met.wait)
    store = SQLiteStore()
    judge = DecisionKeepJudge(decider, store, lambda: NOW, None, RUNE, None, again=False)  # type: ignore[arg-type]
    threads = [threading.Thread(target=judge.score, args=("keep what matters", [Passage(f"a/{n}", "S", f"passage {n}")]))
               for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert decider.calls == 2
    keys = [k for (k,) in store._all("select key from state") if k.startswith(OWN + "keep")]
    assert len(keys) == 1
    assert len(store.state(keys[0])) == 2, "a score paid for was dropped from the cache"
    again = DecisionKeepJudge(decider, store, lambda: NOW, None, RUNE, None, again=False)  # type: ignore[arg-type]
    for n in range(2):
        assert again.score("keep what matters", [Passage(f"a/{n}", "S", f"passage {n}")]).cached == 1
    assert decider.calls == 2  # nothing is paid for twice


def _keep(ledger: Ledger) -> tuple[Callable[[int], Any], Callable[[], int]]:
    decider = StandIn()
    judge = DecisionKeepJudge(decider, None, lambda: NOW, ledger, RUNE, None, again=False)  # type: ignore[arg-type]
    return lambda n: judge.score("keep what matters", [Passage(f"a/{n}", "S", "passage " * 40)]), lambda: decider.calls


def _ground(ledger: Ledger) -> tuple[Callable[[int], Any], Callable[[], int]]:
    decider = StandIn()
    held = _Held(decider, ledger, RUNE, lambda: NOW, "ground x")  # type: ignore[arg-type]
    return lambda n: held.decide({"sentence": "a sentence " * 40}, {"q": "is it grounded?"}), lambda: decider.calls


def _rerank(ledger: Ledger) -> tuple[Callable[[int], Any], Callable[[], int]]:
    decider = StandIn()
    rr = DecisionReranker(decider, ledger=ledger, decision=RUNE, clock=lambda: NOW)  # type: ignore[arg-type]
    return (lambda n: rr.order(f"search {n}", [Hit(f"f{n}", 1.0, {"kind": FACT, "text": "a fact " * 40})]),
            lambda: decider.calls)


def _extract(ledger: Ledger) -> tuple[Callable[[int], Any], Callable[[], int]]:
    handler = Extracting()
    metered = Metered(handler, ledger, FLASH, lambda: NOW)
    return lambda n: metered.extract(note(n)), lambda: handler.calls


def _search(ledger: Ledger) -> tuple[Callable[[int], Any], Callable[[], int]]:
    handler = Extracting()
    metered = Metered(handler, ledger, FLASH, lambda: NOW)
    return lambda n: metered.search(f"the west team {n} " * 10), lambda: handler.calls


ROADS = {"keep judge": ("compilation", _keep), "grounding judge": ("compilation", _ground),
         "rerank": ("rerank", _rerank), "extraction": ("extraction", _extract), "search": ("extraction", _search)}


@pytest.mark.parametrize("road", list(ROADS))
def test_each_call_is_checked_before_it_starts_and_once_the_cap_is_met_none_starts(road: str) -> None:
    layer, make = ROADS[road]
    probe = Ledger(SQLiteStore(), Caps())
    call, _ = make(probe)
    call(0)  # one call alone, to price a call of this road
    one = probe.spent(NOW, layer)
    store = SQLiteStore()
    ledger = Ledger(store, Caps(**{layer: one * 5.5}))
    call, calls = make(ledger)
    for n in range(20):
        try:
            call(n)
        except BudgetSpent:
            pass
    assert calls() == 6, f"{calls()} calls started under a cap that five and a half fill"
    assert len(store.rows(LOG, {"layer": layer})) == 6



def test_a_metered_call_waiting_behind_one_under_way_is_checked_when_it_starts() -> None:
    class Counting(Ledger):
        checks, second = 0, threading.Event()

        def check(self, *args: Any, **kwargs: Any) -> None:
            self.checks += 1
            if self.checks == 2:
                self.second.set()
            super().check(*args, **kwargs)

    one, inside = threading.Lock(), threading.Event()  # as the facts layer holds one lock for a whole extraction

    class Alone(Extracting):
        def extract(self, record: Record) -> Extracted:
            with one:
                if not inside.is_set():
                    inside.set()
                    ledger.second.wait(0.5)  # the next caller's check, were it made before this call ends
                return super().extract(record)

    ledger = Counting(SQLiteStore(), Caps(extraction=1e-9))
    handler = Alone()
    metered = Metered(handler, ledger, FLASH, lambda: NOW)
    first = threading.Thread(target=metered.extract, args=(note(0),))
    first.start()
    inside.wait(5)
    with pytest.raises(BudgetSpent):
        metered.extract(note(1))
    first.join()
    assert handler.calls == 1, "a call checked before the one under way met the cap still started after it"


def test_each_try_of_a_grounding_judge_call_is_checked_against_the_cap_before_it_starts_and_a_failed_try_counts(
        ) -> None:
    from test_models import Network

    from cerebro.models import HttpDecider, YesNo

    state, questions = {"passage": "a sentence " * 400}, {"q": YesNo("Is it backed?")}
    store, net, sent = SQLiteStore(), Network(), []
    drops: list[Any] = [503, ConnectionResetError()]  # a dropped connection, then a server error, then the answer

    def flaky(url: str, headers: dict[str, str], data: bytes) -> tuple[int, dict[str, str], bytes]:
        sent.append(url)
        drop = drops.pop() if drops else None
        if isinstance(drop, int):
            return drop, {}, b""
        if drop is not None:
            raise drop
        return net.send(url, headers, data)

    decider = HttpDecider(RUNE, key=lambda: "k", send=flaky, sleep=lambda _: None)
    _Held(decider, Ledger(store, Caps(compilation=10.0)), RUNE, lambda: NOW, "ground x").decide(state, questions)
    rows = store.rows(LOG, {"layer": "compilation"})
    assert len(sent) == 3 and len(rows) == 3 and all(r.data["usd"] > 0 for r in rows)  # each failed try counts
    one = rows[0].data["usd"]  # what a failed try is held to have spent
    store, net = SQLiteStore(), Network(503, 503)
    with pytest.raises(BudgetSpent):  # the first failed try meets the cap, so no try again starts
        _Held(net.decider(), Ledger(store, Caps(compilation=one / 2)), RUNE, lambda: NOW, "ground x").decide(
            state, questions)
    assert len(net.sent) == 1 and len(store.rows(LOG, {"layer": "compilation"})) == 1
