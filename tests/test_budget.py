"""The two daily caps and the spend ledger, on the in-memory store and on SQLite."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from cerebro.budget import LOG, BudgetError, BudgetSpent, Caps, HeldForRoom, Ledger, UnpricedModel, read_caps
from cerebro.fakes import MemoryStore
from cerebro.models import ModelSpec
from cerebro.run import Usage
from cerebro.store import SQLiteStore, Store

NOON = datetime(2026, 11, 17, 12, tzinfo=UTC)
FLASH = ModelSpec("fireworks", "glm-5p3-flash", "low", "extraction")
GLM = ModelSpec("fireworks", "glm-5p3", "high", "compilation")
DOLLAR_ON_GLM = Usage(input_tokens=714_286)  # at $1.40 a million input tokens, just over $1


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    return MemoryStore() if request.param == "memory" else SQLiteStore(tmp_path / "cerebro.db")


def test_caps_read_one_per_layer_and_todays_daily_usd_as_a_cap_on_both() -> None:
    assert read_caps({"budget": {"extraction_daily_usd": 5, "compilation_daily_usd": 15.5}}) == Caps(5, 15.5)
    assert read_caps({"budget": {"daily_usd": 20}}) == Caps(total=20)
    assert read_caps({"budget": {"extraction_daily_usd": 0.5, "daily_usd": 1e9}}) == Caps(0.5, total=1e9)
    assert read_caps({}) == read_caps({"budget": {}}) == read_caps({"budget": {"daily_usd": None}}) == Caps()


@pytest.mark.parametrize(("block", "names"), [
    ({"extraction_daily_usd": 0}, "budget.extraction_daily_usd is 0, which would stop every model call"),
    ({"compilation_daily_usd": -1}, "budget.compilation_daily_usd is -1"),
    ({"daily_usd": "ten"}, "budget.daily_usd is 'ten': write a number"),
    ({"daily_usd": True}, "budget.daily_usd is True"),
    ({"daily_usd": 2e9}, "budget.daily_usd is 2000000000.0: write an amount from"),
])
def test_a_bad_cap_is_refused_with_a_line_that_names_its_key(block: dict[str, object], names: str) -> None:
    with pytest.raises(BudgetError, match=names):
        read_caps({"budget": block})


def test_past_its_cap_a_layer_stops_and_the_other_carries_on(store: Store) -> None:
    ledger = Ledger(store, Caps(extraction=0.5, compilation=2))
    assert ledger.may_spend("compilation", NOON, GLM)
    ledger.record("compilation", NOON, GLM, DOLLAR_ON_GLM, run="run_1")
    assert ledger.may_spend("compilation", NOON, GLM)
    ledger.record("compilation", NOON, GLM, DOLLAR_ON_GLM, run="run_2")
    assert not ledger.may_spend("compilation", NOON + timedelta(minutes=1), GLM)
    with pytest.raises(BudgetSpent, match=r"compilation spent \$2.00 of its \$2 cap on 2026-11-17 \(UTC\)"):
        ledger.check("compilation", NOON, GLM)
    assert ledger.may_spend("extraction", NOON, FLASH)
    assert ledger.spent(NOON, "compilation") == pytest.approx(2, abs=1e-5)
    assert ledger.spent(NOON, "extraction") == 0
    exactly = Ledger(store, Caps(compilation=ledger.spent(NOON, "compilation")))
    assert not exactly.may_spend("compilation", NOON)
    row = store.rows(LOG)[0].data
    assert row == {"at": "2026-11-17T12:00:00Z", "day": "2026-11-17", "layer": "compilation",
                   "usd": pytest.approx(1, abs=1e-5), "provider": "fireworks", "model": "glm-5p3", "run": "run_1"}


def test_the_next_utc_day_starts_over_whatever_zone_the_time_is_in(store: Store) -> None:
    ledger = Ledger(store, Caps(compilation=1))
    ledger.record("compilation", NOON, GLM, DOLLAR_ON_GLM)
    ledger.record("compilation", NOON, GLM, DOLLAR_ON_GLM)
    pacific = timezone(timedelta(hours=-8))
    late_evening = datetime(2026, 11, 17, 15, 59, tzinfo=pacific)  # 23:59 UTC, the same day
    assert not ledger.may_spend("compilation", late_evening)
    assert ledger.may_spend("compilation", late_evening + timedelta(minutes=1))  # 00:00 UTC, the next day


def test_a_file_with_only_todays_daily_usd_stops_both_layers_together(store: Store) -> None:
    ledger = Ledger(store, read_caps({"budget": {"daily_usd": 1.5}}))
    ledger.record("compilation", NOON, GLM, DOLLAR_ON_GLM)
    assert ledger.may_spend("extraction", NOON)
    ledger.record("extraction", NOON, GLM, DOLLAR_ON_GLM)
    with pytest.raises(BudgetSpent, match="raise budget.daily_usd"):
        ledger.check("extraction", NOON)
    assert not ledger.may_spend("compilation", NOON)


def test_an_unpriced_model_is_refused_under_a_cap_and_kept_at_zero_without_one(store: Store) -> None:
    preview = ModelSpec("fireworks", "rune-preview", job="decision")
    with pytest.raises(UnpricedModel, match="rune-preview has no known price"):
        Ledger(store, Caps(extraction=5)).check("extraction", NOON, preview)
    assert not Ledger(store, Caps(extraction=5)).may_spend("extraction", NOON, preview)
    assert Ledger(store, Caps(compilation=5)).may_spend("extraction", NOON, preview)
    row = Ledger(store, Caps()).record("extraction", NOON, preview, Usage(input_tokens=1000))
    assert row["usd"] == 0 and row["unpriced"] is True
    assert Ledger(store, Caps()).spent(NOON) == 0


def test_runs_that_finish_at_once_lose_none_of_each_others_spend(tmp_path: Path) -> None:
    path = tmp_path / "cerebro.db"
    ledgers = [Ledger(SQLiteStore(path), Caps(compilation=1000)) for _ in range(4)]
    record: Callable[[int], object] = lambda i: ledgers[i % 4].record(
        "compilation", NOON, GLM, DOLLAR_ON_GLM, run=f"run_{i}")
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(record, range(40)))
    assert ledgers[0].spent(NOON, "compilation") == pytest.approx(40, abs=1e-3)
    assert len({row.data["run"] for row in ledgers[1].store.rows(LOG)}) == 40


def test_a_time_with_no_zone_or_an_unknown_layer_is_refused(store: Store) -> None:
    ledger = Ledger(store, Caps())
    with pytest.raises(BudgetError, match="has no time zone"):
        ledger.may_spend("extraction", datetime(2026, 11, 17, 12))
    with pytest.raises(BudgetError, match="'decision' is not a layer that spends"):
        ledger.record("decision", NOON, GLM, DOLLAR_ON_GLM)


def test_a_cost_is_kept_on_the_utc_day_it_was_spent() -> None:
    from datetime import datetime, timedelta, timezone

    from cerebro.budget import Caps, Ledger
    from cerebro.fakes import MemoryStore
    from cerebro.models import ModelSpec
    from cerebro.run import Usage

    evening = datetime(2026, 11, 17, 20, 0, tzinfo=timezone(timedelta(hours=-8)))
    row = Ledger(MemoryStore(), Caps()).record("compilation", evening, ModelSpec("fireworks", "glm-5p3"), Usage())
    assert row["day"] == "2026-11-18" and row["at"] == "2026-11-18T04:00:00Z"


def test_runs_side_by_side_reserve_their_budget_so_they_cannot_all_pass_the_cap_together() -> None:
    ledger, at = Ledger(MemoryStore(), Caps(compilation=1.0)), datetime(2026, 11, 17, 9, tzinfo=UTC)
    model = ModelSpec("fireworks", "glm-5p3")
    first = ledger.reserve("compilation", at, model, 2_000_000)  # the first run under the cap always starts
    with pytest.raises(HeldForRoom, match="held for room under the cap, runs next pass: compilation runs under way hold"):
        ledger.reserve("compilation", at, model, 2_000_000)
    ledger.record("compilation", at, model, Usage(input_tokens=1000))
    ledger.release(first)
    ledger.release(ledger.reserve("compilation", at, model, 2_000_000))  # once settled, the next one starts


def test_an_estimate_is_held_between_nothing_and_the_whole_budget_and_only_on_its_own_day() -> None:
    at, model = datetime(2026, 11, 17, 9, tzinfo=UTC), ModelSpec("fireworks", "glm-5p3")
    ledger = Ledger(MemoryStore(), Caps(compilation=1.0))
    ledger.reserve("compilation", at, model, 100_000, usd=10.0)  # held at its whole budget, $0.14, not $10
    ledger.reserve("compilation", at, model, 100_000)
    ledger = Ledger(MemoryStore(), Caps(compilation=1.0))
    ledger.reserve("compilation", at, model, 2_000_000, usd=-5.0)  # held at nothing, never below
    ledger.reserve("compilation", at, model, 2_000_000)
    with pytest.raises(BudgetSpent, match="runs under way hold"):
        ledger.reserve("compilation", at, model, 100_000)
    ledger.reserve("compilation", at + timedelta(days=1), model, 2_000_000)  # the next UTC day holds none of it


def test_reservations_are_held_against_the_total_cap_too_as_init_writes_it() -> None:
    ledger, at = Ledger(MemoryStore(), Caps(total=1.0)), datetime(2026, 11, 17, 9, tzinfo=UTC)
    model = ModelSpec("fireworks", "glm-5p3")
    ledger.reserve("compilation", at, model, 2_000_000)
    with pytest.raises(BudgetSpent, match=r"runs under way hold .* of the \$1 daily cap"):
        ledger.reserve("extraction", at, model, 2_000_000)


def test_an_extraction_that_fails_after_its_model_calls_still_books_what_they_cost() -> None:
    from cerebro.budget import Metered
    from cerebro.connector import Record
    from cerebro.facts import Extracted, ExtractionFailed
    from cerebro.fakes import FakeExtraction

    class FailsLate(FakeExtraction):
        def extract(self, record: Record) -> Extracted:
            raise ExtractionFailed("factoid failed after two calls", Extracted([], input_tokens=50_000))

    store, at = MemoryStore(), datetime(2026, 11, 17, 9, tzinfo=UTC)
    metered = Metered(FailsLate(), Ledger(store, Caps()), ModelSpec("fireworks", "glm-5p3-flash"), lambda: at)
    with pytest.raises(ExtractionFailed):
        metered.extract(Record(id="n1", source="notes", revision="1", body="A note."))
    assert store.rows(LOG)[0].data["usd"] > 0


def test_a_facts_search_books_what_its_handler_says_it_spent_under_extraction_and_nothing_when_it_spent_none() -> None:
    from cerebro.budget import Metered
    from cerebro.connector import Record
    from cerebro.facts import Extracted
    from cerebro.fakes import FakeExtraction
    from cerebro.search import Hit

    class Embeds(FakeExtraction):
        def search_spent(self, text: str, top_k: int = 10, *, source: str | None = None) -> tuple[list[Hit],
                                                                                                    Extracted]:
            return self.search(text, top_k, source=source), Extracted([], input_tokens=1_000_000)

    store = MemoryStore()
    for handler in (FakeExtraction(), Embeds()):
        handler.extract(Record(id="n1", source="notes", revision="1", body="The budget is short."))
        hits = Metered(handler, Ledger(store, Caps()), FLASH, lambda: NOON).search("budget")
        assert len(hits) == 1
    rows = [row.data for row in store.rows(LOG)]
    assert [(row["layer"], row["run"], row["usd"]) for row in rows] == [("extraction", "search", 0.15)]


def test_a_facts_search_that_fails_after_its_model_calls_still_books_what_they_cost() -> None:
    from cerebro.budget import Metered
    from cerebro.facts import Extracted, ExtractionFailed
    from cerebro.fakes import FakeExtraction

    class FailsLate(FakeExtraction):
        def search_spent(self, text: str, top_k: int = 10, **options: object) -> tuple[list, Extracted]:
            raise ExtractionFailed("the index went away after the embedding", Extracted([], input_tokens=50_000))

    ledger, at = Ledger(MemoryStore(), Caps()), datetime(2026, 11, 17, 9, tzinfo=UTC)
    metered = Metered(FailsLate(), ledger, ModelSpec("fireworks", "glm-5p3-flash"), lambda: at)
    with pytest.raises(ExtractionFailed):
        metered.search("pricing")
    assert ledger.spent(at, "extraction") > 0


def test_an_extraction_books_each_priced_model_on_its_own_row_and_an_unpriced_one_on_the_extraction_models() -> None:
    from cerebro.facts import Extracted

    store, at = MemoryStore(), datetime(2026, 11, 17, 9, tzinfo=UTC)
    ledger = Ledger(store, Caps())
    spent = {"fireworks/glm-5p3-flash": (1_000_000, 200_000), "typesafe/jev-1.13.0": (500_000, 0),
             "fireworks/qwen3-embedding-8b": (300_000, 0)}
    ledger.record_extracted(at, FLASH, Extracted([], 1_800_000, 200_000, models=spent), "n1")
    rows = {f"{r.data['provider']}/{r.data['model']}": r.data["usd"] for r in store.rows(LOG)}
    # glm-5p3-flash $0.15 in, $0.50 out; jev $0.042 in; qwen3-embedding-8b $0.10 in
    assert rows == pytest.approx({"fireworks/glm-5p3-flash": 0.15 + 0.1, "typesafe/jev-1.13.0": 0.021,
                                  "fireworks/qwen3-embedding-8b": 0.03})
    assert ledger.spent(at, "extraction") == pytest.approx(0.25 + 0.021 + 0.03)

    store = MemoryStore()
    Ledger(store, Caps()).record_extracted(at, FLASH, Extracted([], 1_000_000, models={
        "openai/text-embedding-3-small": (1_000_000, 0)}))
    assert [(r.data["model"], r.data["usd"]) for r in store.rows(LOG)] == [("glm-5p3-flash", pytest.approx(0.15))]
