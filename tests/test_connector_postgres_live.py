"""The Postgres engine against a real server, run only where `CEREBRO_TEST_POSTGRES_DSN` names one and psycopg is
installed (`cerebro[postgres]`); skipped everywhere else, so the suite stays offline. The connection string should
log in as a role made as `docs/sources.md` shows: one that can only read. Nothing here writes: every table is made in
the query by `values` or `generate_series`, and each refusal checks that the server, not the text check, refused it.
`CEREBRO_TEST_POSTGRES_SUPERUSER_DSN`, where set, names a superuser's login, which the engine must refuse.
"""

from __future__ import annotations

import os
import time

import pytest

from cerebro.connectors.database import DatabaseError, Limits, QueryRefused
from cerebro.connectors.postgres import PostgresEngine

DSN = os.environ.get("CEREBRO_TEST_POSTGRES_DSN", "")
SUPERUSER = os.environ.get("CEREBRO_TEST_POSTGRES_SUPERUSER_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="CEREBRO_TEST_POSTGRES_DSN is not set, so no Postgres server is "
                                               "named: set it to a read-only role's connection string to run these")


@pytest.fixture
def engine() -> PostgresEngine:
    pytest.importorskip("psycopg", reason="psycopg is not installed: install cerebro[postgres] to run these")
    return PostgresEngine(lambda: DSN, "sources/live.yaml", item="live-dsn")


def test_a_select_reads_rows_with_each_type_mapped(engine: PostgresEngine) -> None:
    rows = engine.run("select 1 as n, 1.50::numeric as amount, 'won'::text as stage, true as open, "
                      "'2026-11-17 09:30:00+00'::timestamptz as at, '{\"a\": [1, 2]}'::jsonb as doc, "
                      "array[1, 2] as list, decode('00ff', 'hex') as raw, null::int as nothing, "
                      "'12345678-1234-5678-1234-567812345678'::uuid as id", Limits())
    assert rows.columns == ["n", "amount", "stage", "open", "at", "doc", "list", "raw", "nothing", "id"]
    assert rows.rows == [[1, 1.5, "won", True, "2026-11-17T09:30:00+00:00", '{"a":[1,2]}', "[1,2]", "<2 bytes>",
                          None, "12345678-1234-5678-1234-567812345678"]]


def test_rows_past_the_limit_are_not_read(engine: PostgresEngine) -> None:
    rows = engine.run("select g from generate_series(1, 100000) as g", Limits(rows=3))
    assert rows.rows == [[1], [2], [3]] and rows.more


@pytest.mark.parametrize("query", [
    "create temp table cerebro_live_probe (x int)",
    "select set_config('transaction_read_only', 'off', true)",
    "with gone as (delete from pg_catalog.pg_class where false returning 1) select * from gone",
])
def test_the_server_refuses_a_write_the_text_check_would_have_caught(engine: PostgresEngine, query: str) -> None:
    with pytest.raises((QueryRefused, DatabaseError)):
        engine._read(query, Limits())  # past the text check, to see the server's own read-only layers hold


def test_a_query_past_its_time_is_stopped_by_the_server(engine: PostgresEngine) -> None:
    with pytest.raises(DatabaseError, match="ran past 1 seconds and was stopped"):
        engine._read("select pg_sleep(5)", Limits(seconds=1))


def test_a_wrong_password_never_reaches_the_line() -> None:
    pytest.importorskip("psycopg", reason="psycopg is not installed: install cerebro[postgres] to run these")
    wrong = "postgresql://cerebro_nobody:not-the-password-xyz@" + (DSN.split("@", 1)[-1] if "@" in DSN else "")
    with pytest.raises(DatabaseError) as failed:
        PostgresEngine(lambda: wrong, "sources/live.yaml", item="live-dsn").run("select 1", Limits(seconds=5))
    assert "not-the-password-xyz" not in str(failed.value)


def test_infinite_dates_and_money_come_back_as_text(engine: PostgresEngine) -> None:
    rows = engine.run("select 'infinity'::timestamp as a, '-infinity'::timestamptz as b, 'infinity'::date as c, "
                      "'2026-11-17'::date as d, '12.50'::money as m", Limits())
    assert rows.rows[0][:4] == ["infinity", "-infinity", "infinity", "2026-11-17"]
    assert isinstance(rows.rows[0][4], str) and "12.50" in rows.rows[0][4]  # as the server's lc_monetary writes it


def test_a_row_far_past_the_size_limit_is_stopped_at_the_server_before_it_is_sent(engine: PostgresEngine) -> None:
    with pytest.raises(DatabaseError, match="made a row longer than 1048576 bytes, and the server stopped it"):
        engine._read("select repeat('x', 50000000) as v from generate_series(1, 50)", Limits())
    rows = engine.run("select g from generate_series(1, 5) as g order by g desc", Limits())
    assert rows.rows == [[5], [4], [3], [2], [1]]  # the wrap keeps the query's order


def test_the_time_limit_holds_across_fetches(engine: PostgresEngine) -> None:
    started = time.monotonic()
    with pytest.raises(DatabaseError, match="ran past 2 seconds and was stopped"):
        engine.run("select (select count(*) from generate_series(1, 100000 + g)) as n "
                   "from generate_series(1, 501) as g", Limits(seconds=2))
    assert time.monotonic() - started < 3


@pytest.mark.skipif(not SUPERUSER, reason="CEREBRO_TEST_POSTGRES_SUPERUSER_DSN is not set")
def test_a_superuser_login_is_refused_before_the_query_is_sent() -> None:
    pytest.importorskip("psycopg", reason="psycopg is not installed: install cerebro[postgres] to run these")
    engine = PostgresEngine(lambda: SUPERUSER, "sources/live.yaml", item="live-dsn")
    with pytest.raises(DatabaseError, match="logs in as a superuser") as refused:
        engine.run("select 1", Limits())
    assert SUPERUSER not in str(refused.value)
