"""Re-ranking a `matching` search on the decision model: off by default, held to its own cap and a time bound, never
a failure, cached, and never showing its scores. Every model here is a stand-in that counts its calls."""

from __future__ import annotations

import dataclasses
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from test_tokens import no_network  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro import Caller, Cerebro, Record
from cerebro.article import Compilation, Identity
from cerebro.budget import RERANK_DAILY_USD, BudgetError, Caps, Ledger, read_caps
from cerebro.fakes import FakeDecider, FakeExtraction, MemoryStore
from cerebro.lookup import StoreLookup
from cerebro.mcp import server
from cerebro.models import DEFAULTS, Decision, DecisionError, DecisionTooLarge, Question, TokenCount
from cerebro.rerank import (
    RERANK_CACHE_ENTRIES,
    RERANK_TOP_DEFAULT,
    DecisionReranker,
    RerankSettings,
    RerankSettingsError,
    read_rerank,
)
from cerebro.search import Hit
from cerebro.store import Store

YOU = Caller("you", "person")
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
JEV = DEFAULTS["decision"]
ARTICLES = (("projects/alder", "Alder", "The launch moved to March. The budget is short."),
         ("projects/birch", "Birch", "The launch is on track."),
         ("projects/cedar", "Cedar", "The budget review is done, and the budget is closed."))
MEMO = Record(id="memo-1", source="notes", revision="1", at="2026-11-03T09:00:00Z",
              body="The Alder budget is short by a third.\n\nThe Birch budget review waits on finance.\n\n"
                   "The lunch moved to noon.")
# A search for "budget" across everything, in search order: Alder earns from its own place and its linked fact's, then
# Cedar and the West team, and last the Birch fact, on its own since the Birch article does not hold the word.
FUSED = ["projects/alder", "projects/cedar", "teams/west", "Birch"]


def fill(store: Store) -> Store:
    for id, title, text in ARTICLES:
        store.create(Identity(id, "projects", title))
        store.write_compilation(Compilation(id, "Overview", text, by="projects/article", at="2026-11-02T09:00:00Z"))
    store.create(Identity("teams/west", "teams", "West"))
    store.write_compilation(Compilation("teams/west", "Overview", "The budget is set.", by="teams/article"))
    return store


def facts(store: Store) -> tuple[FakeExtraction, dict[str, str]]:
    """The memo's facts, the Alder fact linked to Alder and the Birch fact to Birch, and their ids by their second
    word."""
    extraction = FakeExtraction()
    extraction.extract(MEMO)
    by_word = {f.text.split()[1]: f.id for f in extraction.facts()}
    store.link(by_word["Alder"], ["projects/alder"])
    store.link(by_word["Birch"], ["projects/birch"])
    return extraction, by_word


class ByWord(FakeDecider):
    """A decision model that scores a passage by the first of `scores`'s words its text holds, 0.5 when it holds
    none, and counts its calls in `asked`."""

    def __init__(self, scores: dict[str, float]):
        super().__init__()
        self.scores = scores

    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        self.answers = {p["id"]: next((s for word, s in self.scores.items() if word in p["text"]), 0.5)
                        for p in state["passages"]}
        return super().decide(state, questions)


class Ticks:
    """A seconds counter a test moves by hand."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# The scores that put the Birch fact first, the West team second, then Cedar, then Alder: the search order backwards.
BACKWARDS = {"finance": 0.9, "set.": 0.8, "closed": 0.3, "March": 0.1}


def ids(result: Any, word: dict[str, str]) -> list[str]:
    names = {id: name for name, id in word.items()}
    return [names.get(id, id) for id in (getattr(item, "id", None) or item.identity.id for item in result.items)]


def reranked(store: Store, decider: Any, **options: Any) -> tuple[StoreLookup, dict[str, str], DecisionReranker]:
    extraction, word = facts(store)
    reranker = DecisionReranker(decider, store=store, extraction=extraction, clock=lambda: NOW, **options)
    return StoreLookup(store, extraction, clock=lambda: NOW, rerank=reranker), word, reranker


def workspace(tmp_path: Path, settings: str, decider: Any) -> tuple[Cerebro, dict[str, str]]:
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    (root / "settings.yaml").write_text(settings)
    store = fill(MemoryStore())
    extraction, word = facts(store)
    return Cerebro(root, store=store, extraction=extraction, clock=lambda: NOW, decider=decider), word


# -- off by default --------------------------------------------------------------------------------------------------

def test_with_no_search_setting_a_search_makes_no_decider_call_and_keeps_its_search_order(tmp_path: Path) -> None:
    decider = ByWord(BACKWARDS)
    made, word = workspace(tmp_path, "budget:\n  daily_usd: 20\n", decider)
    result = made.lookup.query(YOU, "", matching="budget")
    assert made.lookup.rerank is None and decider.asked == []
    assert ids(result, word) == FUSED and result.warnings == []
    assert ids(made.lookup.query(YOU, "projects", matching="budget"), word) == ["projects/cedar", "projects/alder"]
    assert decider.asked == []


def test_the_search_block_is_read_with_rerank_off_and_twenty_scored_unless_it_says_otherwise() -> None:
    assert read_rerank({}) == read_rerank({"search": None}) == read_rerank({"search": {}}) == RerankSettings(False, 20)
    assert read_rerank({"search": {"rerank": True}}) == RerankSettings(True, RERANK_TOP_DEFAULT)
    assert read_rerank({"search": {"rerank": True, "rerank_top": 5}}) == RerankSettings(True, 5)
    assert read_rerank({"search": {"rerank": False, "rerank_top": None}}) == RerankSettings(False, 20)
    assert read_rerank({"search": {"rerank": None}}) == RerankSettings(False, 20)
    assert [read_rerank({"search": {"rerank": True, "rerank_top": n}}).top for n in (1, 50)] == [1, 50]
    with pytest.raises(RerankSettingsError, match="search.rerank is 'yes': write true or false"):
        read_rerank({"search": {"rerank": "yes"}})
    for bad in (0, 51, 2.5, True, "20"):
        with pytest.raises(RerankSettingsError, match=r"search.rerank_top is .*: write a whole number from 1 to 50"):
            read_rerank({"search": {"rerank": True, "rerank_top": bad}})
    with pytest.raises(RerankSettingsError) as refused:
        read_rerank({"search": "rerank"})
    assert str(refused.value) == ("settings.yaml search is not a map: write rerank: true and rerank_top: 20 under it, "
                                  "or leave it out")


def test_rerank_on_with_no_decision_model_builds_no_reranker_and_says_so(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("search:\n  rerank: true\n")
    made = Cerebro(root, store=fill(MemoryStore()), clock=lambda: NOW)
    assert made.lookup.rerank is None
    assert any(n.startswith("search.rerank is on, but this workspace has no decision model") for n in made.notices)


# -- the order -------------------------------------------------------------------------------------------------------

def test_with_rerank_on_the_top_results_come_in_the_models_order_and_the_rest_in_search_order(tmp_path: Path) -> None:
    decider = ByWord(BACKWARDS)
    made, word = workspace(tmp_path, "search:\n  rerank: true\n  rerank_top: 2\n", decider)
    result = made.lookup.query(YOU, "", matching="budget")
    assert ids(result, word) == ["projects/cedar", "projects/alder", "teams/west", "Birch"]  # only the top two move
    assert result.warnings == [] and len(decider.asked) == 1
    state, questions = decider.asked[0]
    assert [p["text"] for p in state["passages"]] == ["The launch moved to March. The budget is short.",
                                                     "The budget review is done, and the budget is closed."]
    assert state["passages"][0]["article"] == "Alder" and state["passages"][0]["heading"] == "Overview"
    assert state["search"] == "budget" and list(questions) == ["p1", "p2"]
    every = ByWord(BACKWARDS)
    whole, word = workspace(tmp_path / "every", "search:\n  rerank: true\n", every)
    assert ids(whole.lookup.query(YOU, "", matching="budget"), word) == FUSED[::-1]
    assert {"kind": "fact", "id": "p4", "text": "The Birch budget review waits on finance."} in \
        every.asked[0][0]["passages"]


def test_a_tie_keeps_the_search_order_and_a_collection_and_the_facts_are_reranked_too() -> None:
    store = fill(MemoryStore())
    lookup, word, _ = reranked(store, ByWord({"budget": 0.5}))
    assert ids(lookup.query(YOU, "", matching="budget"), word) == FUSED  # every score ties
    store = fill(MemoryStore())
    lookup, word, _ = reranked(store, ByWord({"March": 0.9, "closed": 0.1, "third": 0.9}))
    assert ids(lookup.query(YOU, "projects", matching="budget"), word) == ["projects/alder", "projects/cedar"]
    assert ids(lookup.query(YOU, "facts", matching="budget"), word) == ["Alder", "Birch"]  # Birch is first unranked


# -- the cap and the ledger ------------------------------------------------------------------------------------------

def test_each_call_is_recorded_under_rerank_and_past_its_cap_a_search_keeps_its_order_with_a_note() -> None:
    store = fill(MemoryStore())
    ledger = Ledger(store, Caps())
    assert ledger.caps.rerank == RERANK_DAILY_USD == 0.5
    decider = ByWord(BACKWARDS)
    lookup, word, reranker = reranked(store, decider, ledger=ledger, decision=JEV)
    assert ids(lookup.query(YOU, "", matching="budget"), word) == FUSED[::-1]
    (row,) = store.rows("spend")
    assert (row.data["layer"], row.data["run"], row.data["model"]) == ("rerank", "rerank", JEV.model)
    assert reranker.calls == 1
    ledger.record("rerank", NOW, JEV, TokenCount(input_tokens=20_000_000))  # $0.84 of a $0.50 cap
    result = lookup.query(YOU, "", matching="launch budget")  # other words, so nothing is cached
    assert ids(result, word) == ids(StoreLookup(store, lookup.extraction).query(YOU, "", matching="launch budget"),
                                    word)
    assert len(decider.asked) == 1 and reranker.calls == 1  # checked before: no call was made
    assert result.warnings == [("the best matches are in search order, not re-ranked: rerank spent $0.84 of its $0.5 "
                               "cap on 2026-11-17 (UTC): its model calls wait for 00:00 UTC, or raise "
                               "budget.rerank_daily_usd")]


def test_the_rerank_layer_has_its_own_cap_in_settings_and_stops_alone() -> None:
    assert read_caps({}).rerank == 0.5 and read_caps({"budget": {"rerank_daily_usd": 2}}).of("rerank") == 2.0
    with pytest.raises(BudgetError, match="budget.rerank_daily_usd is 0"):
        read_caps({"budget": {"rerank_daily_usd": 0}})
    ledger = Ledger(MemoryStore(), Caps())
    ledger.record("rerank", NOW, JEV, TokenCount(input_tokens=20_000_000))
    assert not ledger.may_spend("rerank", NOW, JEV) and ledger.may_spend("compilation", NOW, JEV)


# -- never slower than its bound, never a failure --------------------------------------------------------------------

def test_a_slow_decision_gives_the_search_order_with_a_note() -> None:
    ticks = Ticks()

    class Slow(ByWord):
        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            ticks.now += 6  # past the five seconds a search waits
            return super().decide(state, questions)

    store = fill(MemoryStore())
    decider = Slow(BACKWARDS)
    lookup, word, _ = reranked(store, decider, ticks=ticks)
    result = lookup.query(YOU, "", matching="budget")
    assert ids(result, word) == FUSED and len(decider.asked) == 1
    assert result.warnings == [("the best matches are in search order, not re-ranked: the decision model did not "
                               "answer within 5 seconds, the most a search waits for it: search again later")]


def test_a_decision_that_never_answers_is_not_waited_for_past_the_bound() -> None:
    let_go = threading.Event()

    class Stuck(FakeDecider):
        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            let_go.wait(5)
            return super().decide(state, questions)

    store = fill(MemoryStore())
    lookup, word, _ = reranked(store, Stuck(), limit=0.05)
    try:
        result = lookup.query(YOU, "", matching="budget")
    finally:
        let_go.set()
    assert ids(result, word) == FUSED and "did not answer within 0.05 seconds" in result.warnings[0]


def test_a_failing_decision_and_a_broken_one_each_give_the_search_order_with_a_note_and_never_raise() -> None:
    class Fails(FakeDecider):
        def __init__(self, error: Exception):
            super().__init__()
            self.error = error

        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            super().decide(state, questions)
            raise self.error

    for error, said in ((DecisionError("jev-1.13.0 gave a reply with no answers"), "no answers"),
                        (RuntimeError("the connection dropped"), "the connection dropped")):
        store = fill(MemoryStore())
        decider = Fails(error)
        lookup, word, _ = reranked(store, decider)
        result = lookup.query(YOU, "", matching="budget")
        assert ids(result, word) == FUSED and len(decider.asked) == 1
        assert result.warnings[0].startswith("the best matches are in search order, not re-ranked: the decision "
                                             "model failed: ") and said in result.warnings[0]
        assert ids(lookup.query(YOU, "projects", matching="budget"), word) == ["projects/cedar", "projects/alder"]


def test_a_passage_left_unscored_leaves_the_search_order_with_a_note() -> None:
    class Partial(FakeDecider):
        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            made = super().decide(state, questions)
            return Decision({k: v for k, v in made.answers.items() if k != "p2"}, "fake")

    store = fill(MemoryStore())
    lookup, word, _ = reranked(store, Partial())
    result = lookup.query(YOU, "", matching="budget")
    assert ids(result, word) == FUSED
    assert result.warnings == [("the best matches are in search order, not re-ranked: the decision model left "
                               "passage p2 of the search unscored")]


def test_a_decision_too_large_is_split_in_halves_and_each_half_is_recorded() -> None:
    class Small(ByWord):
        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            if len(questions) > self.most:
                self.asked.append((state, dict(questions)))
                raise DecisionTooLarge("too large: split it and ask again")
            return super().decide(state, questions)

    store = fill(MemoryStore())
    ledger = Ledger(store, Caps())
    decider = Small(BACKWARDS)
    decider.most = 1
    lookup, word, reranker = reranked(store, decider, ledger=ledger, decision=JEV)
    assert ids(lookup.query(YOU, "", matching="budget"), word) == FUSED[::-1]
    assert [len(q) for _, q in decider.asked] == [4, 2, 1, 1, 2, 1, 1] and reranker.calls == 4
    assert len(store.rows("spend")) == 4
    assert {state["contract"] for state, _ in decider.asked} == {decider.asked[0][0]["contract"]}
    assert decider.asked[0][0]["contract"].startswith("Someone searched")
    decider.most = 0  # not even one passage fits
    result = lookup.query(YOU, "", matching="launch budget")
    assert result.warnings[0].startswith("the best matches are in search order, not re-ranked: the decision model "
                                         "failed: too large")


# -- the cache -------------------------------------------------------------------------------------------------------

def test_a_cached_score_makes_no_call_and_the_cache_drops_its_oldest_past_its_bound() -> None:
    assert RERANK_CACHE_ENTRIES == 4096
    store = fill(MemoryStore())
    decider = ByWord(BACKWARDS)
    lookup, word, reranker = reranked(store, decider, decision=JEV, entries=4)
    first = lookup.query(YOU, "", matching="budget")
    again = lookup.query(YOU, "", matching="budget")
    assert ids(first, word) == ids(again, word) == FUSED[::-1] and len(decider.asked) == 1
    lookup.query(YOU, "projects", matching="launch")  # two more scores, so the two oldest of four are dropped
    assert len(decider.asked) == 2 and len(reranker._cache) == 4
    lookup.query(YOU, "", matching="budget")
    assert len(decider.asked) == 3
    assert [p["text"] for p in decider.asked[2][0]["passages"]] == ["The launch moved to March. The budget is short.",
                                                                    ("The budget review is done, and the budget is "
                                                                    "closed.")]


def test_the_cache_is_kept_by_model_and_by_the_words_searched() -> None:
    store = fill(MemoryStore())
    decider = ByWord(BACKWARDS)
    extraction, _ = facts(store)
    one = DecisionReranker(decider, store=store, extraction=extraction, decision=JEV)
    lookup = StoreLookup(store, extraction, rerank=one)
    lookup.query(YOU, "", matching="budget")
    lookup.query(YOU, "", matching="the budget")  # other words: asked afresh
    assert len(decider.asked) == 2
    one.decision = dataclasses.replace(JEV, model="jev-2.0.0")  # another model: asked afresh
    lookup.query(YOU, "", matching="budget")
    assert len(decider.asked) == 3


# -- the prompt, and what a caller sees ------------------------------------------------------------------------------

def test_the_judgment_reads_the_bundled_prompt_or_the_workspaces_own_copy(tmp_path: Path) -> None:
    store = fill(MemoryStore())
    decider = ByWord(BACKWARDS)
    lookup, _, reranker = reranked(store, decider)
    lookup.query(YOU, "", matching="budget")
    assert decider.asked[0][0]["contract"].startswith("Someone searched a team's wiki")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "rerank-judge.md").write_text("Score each passage for the search.", encoding="utf-8")
    reranker.root = tmp_path
    lookup.query(YOU, "", matching="budget")
    assert decider.asked[1][0]["contract"] == "Score each passage for the search."  # and not from the cache


def keys(value: Any) -> set[str]:
    """Every key and field name anywhere in a reply's data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, (list, tuple)):
        return {k for v in value for k in keys(v)}
    return set()


def test_no_score_reaches_a_caller_of_a_reranked_search(tmp_path: Path) -> None:
    decider = ByWord(BACKWARDS)
    made, _word = workspace(tmp_path, "budget:\n  daily_usd: 20\nsearch:\n  rerank: true\n", decider)
    for source in ("", "projects", "facts"):
        result = made.lookup.query(YOU, source, matching="budget")
        assert result.items and not any("score" in key for key in keys(result.items))

    async def calls() -> Any:
        async with Client(server(made, clock=lambda: NOW, log=False)) as client:
            return await client.call_tool("query", {"matching": "budget"})

    reply = anyio.run(calls)
    assert not any("score" in key for key in keys(reply.structured_content))
    assert "score" not in reply.content[0].text and "0.9" not in reply.content[0].text
    assert reply.content[0].text.index("teams/west") < reply.content[0].text.index("projects/alder")


# -- rules on the main path -----------------------------------------------------------------------------------

def test_no_hits_make_no_call_and_hits_that_cannot_be_read_keep_their_order_with_a_note() -> None:
    store = fill(MemoryStore())
    decider = ByWord(BACKWARDS)
    _, _, reranker = reranked(store, decider)
    assert reranker.order("budget", []).hits == [] and reranker("budget", []) == [] and decider.asked == []

    class Unreadable(MemoryStore):
        def article(self, id: str) -> Any:
            raise OSError("the store is locked")

    broken = DecisionReranker(decider, store=Unreadable())
    hits = [Hit("projects/alder#0", 1.0, {"article": "projects/alder"}), Hit("projects/cedar#0", 0.5,
                                                                              {"article": "projects/cedar"})]
    again = broken.order("budget", hits)
    assert again.hits == hits and decider.asked == []
    assert again.note == "the best matches are in search order, not re-ranked: its hits could not be read: the store "\
                         "is locked"


def test_a_model_with_no_known_price_is_not_called_while_the_rerank_cap_is_set() -> None:
    store = fill(MemoryStore())
    decider = ByWord(BACKWARDS)
    unpriced = dataclasses.replace(JEV, provider="fireworks", model="rune-unlisted")
    lookup, word, _ = reranked(store, decider, ledger=Ledger(store, Caps()), decision=unpriced)
    result = lookup.query(YOU, "", matching="budget")
    assert ids(result, word) == FUSED and decider.asked == []
    assert "has no known price, so rerank does not run it while a daily cap is set" in result.warnings[0]


def test_a_score_from_zero_to_one_stands_and_one_past_it_is_left_unscored_while_the_rest_are_kept() -> None:
    store = fill(MemoryStore())
    decider = ByWord({"finance": 1.0, "set.": 0.0, "closed": 0.0, "March": 0.0})
    lookup, word, reranker = reranked(store, decider)
    result = lookup.query(YOU, "", matching="budget")
    assert ids(result, word) == ["Birch", "projects/alder", "projects/cedar", "teams/west"] and result.warnings == []
    store = fill(MemoryStore())
    decider = ByWord({"March": 1.5})
    lookup, word, reranker = reranked(store, decider)
    result = lookup.query(YOU, "", matching="budget")
    assert ids(result, word) == FUSED and result.warnings == [
        ("the best matches are in search order, not re-ranked: the decision model left passage p1 of the search "
        "unscored")]
    assert len(reranker._cache) == 3  # the scores it gave are kept


def test_what_each_call_spent_is_totalled_and_each_question_names_its_passage() -> None:
    class Strict(ByWord):
        def __init__(self) -> None:
            super().__init__(BACKWARDS)
            self.strict = True

    store = fill(MemoryStore())
    decider = Strict()
    lookup, _, reranker = reranked(store, decider, ledger=Ledger(store, Caps()), decision=JEV)
    lookup.query(YOU, "", matching="budget")
    state, questions = decider.asked[0]
    assert [q.instructions for q in questions.values()] == [f"Does passage p{n} answer the search?" for n in (1, 2, 3, 4)]
    assert state["passages"][0] == {"id": "p1", "kind": "article part", "article": "Alder", "heading": "Overview",
                                    "text": "The launch moved to March. The budget is short."}
    sent = reranker.usage
    assert sent.input_tokens > 0 and sent.output_tokens == 4 and (sent.cache_read_tokens, sent.cache_write_tokens) == (0, 0)
    lookup.query(YOU, "", matching="launch budget")
    assert reranker.usage.output_tokens == 8 and reranker.usage.input_tokens > sent.input_tokens
    assert store.rows("spend")[0].data["usd"] > 0


def test_the_bound_is_waited_out_exactly_and_no_longer() -> None:
    import time

    class Stepping:
        """Seconds that move `step` each time they are read."""

        def __init__(self, step: float):
            self.now, self.step = 100.0, step

        def __call__(self) -> float:
            self.now += self.step
            return self.now

    let_go = threading.Event()

    class Stuck(FakeDecider):
        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            let_go.wait(5)
            return super().decide(state, questions)

    store = fill(MemoryStore())
    lookup, word, _ = reranked(store, Stuck(), ticks=Stepping(4.95), limit=5)  # 0.05 seconds left when it waits
    began = time.monotonic()
    try:
        result = lookup.query(YOU, "", matching="budget")
    finally:
        let_go.set()
    assert time.monotonic() - began < 1 and ids(result, word) == FUSED and "within 5 seconds" in result.warnings[0]

    class Late(ByWord):
        """A decision that takes a little real time, and some seconds on a counter of its own."""

        def __init__(self, ticks: Ticks | None = None):
            super().__init__(BACKWARDS)
            self.ticks = ticks

        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            threading.Event().wait(0.05)
            if self.ticks is not None:
                self.ticks.now += 5  # exactly the bound: still in time
            return super().decide(state, questions)

    store = fill(MemoryStore())
    lookup, word, _ = reranked(store, Late())
    assert ids(lookup.query(YOU, "", matching="budget"), word) == FUSED[::-1]
    ticks = Ticks()
    store = fill(MemoryStore())
    lookup, word, _ = reranked(store, Late(ticks), ticks=ticks)
    assert ids(lookup.query(YOU, "", matching="budget"), word) == FUSED[::-1]


def test_an_article_is_read_by_the_part_that_matched_and_a_fact_through_the_facts_layer() -> None:
    store = fill(MemoryStore())
    store.write_compilation(Compilation("projects/birch", "Risks", "The budget for the launch is at risk.", by="projects/article"))
    decider = ByWord(BACKWARDS)
    lookup, _word, reranker = reranked(store, decider)
    lookup.query(YOU, "", matching="risk")
    assert decider.asked[0][0]["passages"][0] == {"id": "p1", "kind": "article part", "article": "Birch",
                                                  "heading": "Risks", "text": "The budget for the launch is at risk."}
    lookup.query(YOU, "projects", matching="at risk")  # other words, so not from the cache
    assert decider.asked[1][0]["passages"][0]["heading"] == "Risks"
    alder = next(f for f in lookup.extraction.facts() if f.text.startswith("The Alder"))
    reranker.order("budget", [Hit(alder.id, 1.0), Hit("fact_unknown", 0.5)])  # a facts layer's hits, with no text
    assert decider.asked[2][0]["passages"] == [{"id": "p1", "kind": "fact", "text": alder.text},
                                               {"id": "p2", "kind": "fact", "text": ""}]
    gone = DecisionReranker(decider, store=MemoryStore())  # an article the store no longer holds: its title alone
    gone.order("budget", [Hit("projects/gone", 1.0, {"kind": "article", "title": "Gone", "heading": "Overview"}),
                          Hit("projects/gone#0", 0.5, {"article": "projects/gone"})])
    assert [(p["article"], p["heading"], p["text"]) for p in decider.asked[3][0]["passages"]] == [
        ("Gone", "", ""), ("projects/gone", "", "")]


def test_a_long_passage_is_cut_to_what_the_model_reads_and_never_mid_letter() -> None:
    store = MemoryStore()
    store.create(Identity("projects/fir", "projects", "Fir"))
    store.write_compilation(Compilation("projects/fir", "Overview", "budget " + "é" * 600, by="projects/article"))
    decider = ByWord({})
    lookup = StoreLookup(store, rerank=DecisionReranker(decider, store=store))
    result = lookup.query(YOU, "projects", matching="budget")
    assert result.warnings == []
    text = decider.asked[0][0]["passages"][0]["text"]
    assert text.startswith("budget é") and len(text.encode()) <= 800 and len(text.encode()) >= 798


# -- masking, the client's bound and the settings -------------------------------------------------------------------

def test_the_words_searched_and_each_passage_reach_the_model_masked() -> None:
    store = fill(MemoryStore())
    store.write_compilation(Compilation("teams/west", "Overview", "The budget is set by key sekret-0042.", by="teams/article"))
    seen: list[Any] = []

    class Seeing(ByWord):
        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            seen.append(state)
            return super().decide(state, questions)

    lookup, _, _ = reranked(store, Seeing(BACKWARDS), mask=lambda text: text.replace("sekret-0042", "[secret]"))
    lookup.query(YOU, "", matching="budget sekret-0042")
    (state,) = seen
    assert "sekret-0042" not in str(state) and "[secret]" in state["search"]
    assert any("[secret]" in p["text"] for p in state["passages"])


def test_a_re_ranking_decision_client_asks_once_and_gives_up_a_silent_connection_after_its_bound(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import models

    waited: list[float] = []

    def silent(request: Any, timeout: float) -> Any:
        waited.append(timeout)
        raise TimeoutError("the read timed out")

    monkeypatch.setattr(models._OPENER, "open", silent)
    bounded = models.HttpDecider(JEV, key=lambda: "key-for-a-test", sleep=lambda s: None).bounded(2.5)
    with pytest.raises((TimeoutError, OSError, DecisionError)):
        bounded.decide({"x": 1}, {"q": models.YesNo("Is it?")})
    assert waited == [2.5] and bounded.attempts == 1


def test_a_search_setting_the_workspace_cannot_run_on_is_refused_when_the_workspace_loads(tmp_path: Path) -> None:
    from cerebro.project import WorkspaceError

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("search:\n  rerank: true\n  rerank_top: 99\n")
    with pytest.raises(WorkspaceError, match="search.rerank_top is 99"):
        Cerebro(root, store=MemoryStore(), extraction=FakeExtraction())


def test_status_says_whether_matching_is_re_ranked(tmp_path: Path) -> None:
    on, _ = workspace(tmp_path, "search:\n  rerank: true\n  rerank_top: 7\n", ByWord({}))
    assert on.status()["rerank"] == {"asked": True, "on": True, "top": 7, "model": JEV.model}
    assert on.status()["caps"]["rerank"] == RERANK_DAILY_USD and on.status()["spent_today"]["rerank"] == 0
    off, _ = workspace(tmp_path / "off", "{}\n", ByWord({}))
    assert off.status()["rerank"] == {"asked": False, "on": False, "top": RERANK_TOP_DEFAULT, "model": ""}


def test_the_json_api_and_mcp_give_one_re_ranked_order_for_one_search(tmp_path: Path) -> None:
    from test_tokens import Session, serving

    cerebro, _ = workspace(tmp_path, "search:\n  rerank: true\n", ByWord(BACKWARDS))
    token = cerebro.make_token("desk-agent").token
    args = {"from": "projects", "matching": "budget"}
    with serving(cerebro) as client:
        over_api = client.post("/api/v1/query", json=args, headers={"Authorization": f"Bearer {token}"}).json()
        over_mcp = Session(client, token).call("query", args)["structuredContent"]
    order = [item["id"] for item in over_api["items"]]
    assert over_api == over_mcp and order == ["projects/cedar", "projects/alder"]  # the model's order, not the search's
