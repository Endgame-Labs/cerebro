"""Single model calls under a cap: the keep judge and grounding's judge under compilation, the re-ranker under
rerank, and metered extraction and search under extraction. Each call is checked against the cap before it starts,
and once the cap is met nothing new starts."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cerebro import Record
from cerebro.budget import LOG, BudgetSpent, Caps, Held, Ledger, Metered
from cerebro.evidence import DecisionKeepJudge, Passage, estimate_tokens
from cerebro.facts import Extracted
from cerebro.fakes import FakeExtraction
from cerebro.models import DEFAULTS, Answer, Decision, ModelSpec, TokenCount
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
    held = Held(decider, ledger, "compilation", RUNE, lambda: NOW, "ground x")  # type: ignore[arg-type]
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


def test_a_try_that_failed_counts_whether_its_connection_dropped_or_the_server_erred() -> None:
    from test_models import Network

    from cerebro.models import HttpDecider, YesNo

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
    Held(decider, Ledger(store, Caps(compilation=10.0)), "compilation", RUNE, lambda: NOW, "ground x").decide(
        {"passage": "a sentence " * 400}, {"q": YesNo("Is it backed?")})
    rows = store.rows(LOG, {"layer": "compilation"})
    assert len(sent) == 3 and len(rows) == 3 and all(r.data["usd"] > 0 for r in rows)


def _grounding_road(tmp_path: Path, decider: Any, store: Any) -> None:
    from cerebro.agent import Agent
    from cerebro.article import Citation
    from cerebro.compile import _Grounding
    from cerebro.evidence import Evidence
    from cerebro.fakes import MemoryStore
    from cerebro.lookup import StoreLookup

    agent = Agent(id="things/a", path=tmp_path / "a.md", role="writer", target="things/a", prompt="Write it.")
    reads = [Evidence("Facts", ["- [^f_1] A thing moved."], [Citation("fact", "f_1", "r", "A thing moved.")])]
    grounding = _Grounding("things/a", lookup=StoreLookup(MemoryStore()), root=tmp_path, ledger=Ledger(store, CAPS),
                           clock=lambda: NOW, decider=decider, decision=RUNE, zone=UTC, floor=0.6)
    grounding.editor(agent, lambda: reads).marked("A thing moved [^f_1].")


def _keep_road(tmp_path: Path, decider: Any, store: Any) -> None:
    DecisionKeepJudge(decider, None, lambda: NOW, Ledger(store, CAPS), RUNE, None, again=False).score(
        "what matters", [Passage("things/a", "Part", "A thing moved.")])


def _sort_road(tmp_path: Path, decider: Any, store: Any) -> None:
    from cerebro.caller import Caller
    from cerebro.records import tell

    tell(store, None, Caller("you", "person"), "A thing moved on Monday.", decider=decider,
         ledger=Ledger(store, CAPS), clock=lambda: NOW, decision_model=RUNE)


def _near_road(tmp_path: Path, decider: Any, store: Any) -> None:
    from cerebro.app import Cerebro
    from cerebro.article import Identity
    from cerebro.fakes import FakeConnector, FakeExtraction

    record = Record(id="n1", source="notes", revision="1", at="2026-11-10T09:00:00Z", body="It renewed.",
                    fields={"subject": "the Alder deal"})
    (tmp_path / "settings.yaml").write_text("budget: { extraction_daily_usd: 1 }\n")
    store.create(Identity("things/alder", "things", "Alder"))
    Cerebro(tmp_path, store=store, extraction=FakeExtraction(), clock=lambda: NOW, decider=decider,
            connectors=[FakeConnector("notes", [record])]).run_once()


def _eval_road(tmp_path: Path, decider: Any, store: Any) -> None:
    from cerebro import evals

    fact = {"id": "f_1", "text": "A thing moved.", "subject": "a thing", "at": "2026-11-03", "source": "local/a.md"}
    (tmp_path / "corpus.json").write_text(json.dumps({"facts": [fact], "cases": [
        {"id": "c1", "sentence": "A thing moved [^f_1].", "label": "supported-in-other-words"}]}))
    evals.run(None, evals.load(tmp_path / "corpus.json"), decider=decider, model=RUNE, ledger=Ledger(store, CAPS),
              cap_usd=1.0, clock=lambda: NOW)


def _proof_road(tmp_path: Path, decider: Any, store: Any) -> None:
    from cerebro import scaffold
    from cerebro.app import Cerebro

    root = scaffold.init(tmp_path / "wiki", daily_usd=None, timezone="UTC", environ={}).root
    workspace = Cerebro(root, store=store, clock=lambda: NOW, trusted=True)  # left open: the test reads its store
    workspace.ledger = Ledger(store, CAPS)
    workspace.prove_key("decision", RUNE, "k-proof")


def _rerank_road(tmp_path: Path, decider: Any, store: Any) -> None:
    DecisionReranker(decider, ledger=Ledger(store, CAPS), decision=RUNE, clock=lambda: NOW).order(
        "a thing", [Hit("f1", 1.0, {"kind": FACT, "text": "A thing moved."})])


CAPS = Caps(extraction=1.0, compilation=1.0, evaluation=1.0)
# Each decision caller that tries a call again, by the layer it spends, as it builds and holds its decider.
TRIED = {"grounding judge": ("compilation", _grounding_road), "keep judge": ("compilation", _keep_road),
         "tell sort": ("extraction", _sort_road), "near-match judge": ("extraction", _near_road),
         "eval judge": ("evaluation", _eval_road), "key proof": ("compilation", _proof_road),
         "rerank": ("rerank", _rerank_road)}


@pytest.mark.parametrize("road", list(TRIED))
def test_each_try_of_a_decision_call_is_checked_against_the_cap_before_it_starts_and_a_failed_try_counts(
        road: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_models import Network

    from cerebro.models import HttpDecider

    layer, make = TRIED[road]
    store, net = SQLiteStore(), Network(503)

    def meet(_: float) -> None:  # while the first try waits to go again, calls side by side meet every cap
        for spent in ("extraction", "compilation", "evaluation", "rerank"):
            Ledger(store, CAPS).record(spent, NOW, RUNE, TokenCount(input_tokens=10**9), "side by side")

    def flaky(spec: ModelSpec, **kw: Any) -> HttpDecider:
        return HttpDecider(spec, **{**kw, "key": lambda: "k"}, send=net.send, sleep=meet)

    monkeypatch.setattr("cerebro.app.HttpDecider", flaky)  # the key proof makes its own client
    try:
        make(tmp_path, flaky(RUNE), store)
    except BudgetSpent:
        pass
    assert len(net.sent) == 1, f"{len(net.sent)} tries started; the second after the cap was met"
    paid = [r for r in store.rows(LOG, {"layer": layer}) if r.data["run"] != "side by side"
            and r.data["model"] in (RUNE.model, DEFAULTS["decision"].model)]
    assert len(paid) == 1, [r.data for r in store.rows(LOG)]  # the try that failed counts


def test_each_try_of_a_writing_models_call_is_checked_against_the_cap_before_it_starts_and_a_failed_try_counts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import pydantic_ai.models
    import pydantic_ai.providers
    from openai import AsyncOpenAI

    from cerebro import scaffold
    from cerebro.app import Cerebro

    store, sent = SQLiteStore(), []
    root = scaffold.init(tmp_path / "wiki", daily_usd=None, timezone="UTC", environ={}).root
    workspace = Cerebro(root, store=store, clock=lambda: NOW, trusted=True)  # left open: the test reads its store
    workspace.ledger = Ledger(store, CAPS)

    def erred(request: httpx.Request) -> httpx.Response:  # a server error, while calls side by side meet the cap
        sent.append(request.url)
        Ledger(store, CAPS).record("extraction", NOW, RUNE, TokenCount(input_tokens=10**9), "side by side")
        return httpx.Response(503, headers={"retry-after": "0"})

    made = pydantic_ai.providers.infer_provider_class
    monkeypatch.setattr(pydantic_ai.providers, "infer_provider_class", lambda name: lambda **kw: made(name)(
        openai_client=AsyncOpenAI(api_key=kw["api_key"], max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(erred)))))
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)
    monkeypatch.setattr("cerebro.run.WAIT_MOST", 0)
    monkeypatch.setenv(workspace.models["extraction"].variable, "k")
    with pytest.raises(BudgetSpent):
        workspace.ask_model("slack-channel-grouping", "{}")
    assert len(sent) == 1, f"{len(sent)} tries started; the second after the cap was met"
    paid = [r for r in store.rows(LOG, {"layer": "extraction"}) if r.data["run"] != "side by side"]
    assert len(paid) == 1 and paid[0].data["usd"] > 0  # the try that failed counts, at what it sent
