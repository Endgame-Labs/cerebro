"""Windows: the edges of each span, read at a given time in a given zone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from cerebro.window import WindowError, holds, shaped, span

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def test_the_day_n_days_back_belongs_to_last_n_days_and_not_to_before_last_n_days() -> None:
    last, before = span("last_30_days", NOW, UTC), span("before_last_30_days", NOW, UTC)
    for day, in_last in (("2026-10-18", True), ("2026-10-17", False), ("2026-11-17", True), ("2026-11-18", False)):
        assert holds(last, day, UTC) is in_last
    assert not holds(before, "2026-10-18", UTC) and holds(before, "2026-10-17", UTC)
    assert holds(last, "2026-10-18T12:00:00Z", UTC) and not holds(last, "2026-10-18T11:59:59Z", UTC)
    assert holds(before, "2026-10-18T11:59:59Z", UTC) and holds(before, "1999-01-01", UTC)
    assert holds(span("next_14_days", NOW, UTC), ["tbd", "2026-12-01"], UTC)  # one item of a list
    assert not holds(span("next_14_days", NOW, UTC), "2026-12-01T12:00:00Z", UTC)  # the end is not held
    assert not holds(last, "not a date", UTC)


def test_at_midnight_the_day_before_the_window_starts_is_wholly_before_it() -> None:
    midnight = datetime(2026, 11, 17, tzinfo=UTC)
    last, before = span("last_30_days", midnight, UTC), span("before_last_30_days", midnight, UTC)
    assert not holds(last, "2026-10-17", UTC) and holds(before, "2026-10-17", UTC)
    assert holds(last, "2026-10-18", UTC) and not holds(before, "2026-10-18", UTC)
    tokyo = ZoneInfo("Asia/Tokyo")  # a time with no offset, and a date, are read in the zone
    assert holds(span("last_7_days", NOW, tokyo), "2026-11-17T20:00:00", tokyo)
    assert not holds(span("last_7_days", NOW, tokyo), "2026-11-18", tokyo)


def test_quarters_are_calendar_quarters_in_the_zone_and_fiscal_ones_count_from_their_start() -> None:
    this, nxt = span("this_quarter", NOW, UTC), span("next_quarter", NOW, UTC)
    assert (this.start, this.end) == (datetime(2026, 10, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
    assert (nxt.start, nxt.end) == (datetime(2027, 1, 1, tzinfo=UTC), datetime(2027, 4, 1, tzinfo=UTC))
    assert span("last_quarter", NOW, UTC).start == datetime(2026, 7, 1, tzinfo=UTC)
    tokyo = ZoneInfo("Asia/Tokyo")
    new_year = datetime(2026, 12, 31, 20, 0, tzinfo=UTC)  # already 2027 in Tokyo
    assert span("this_quarter", new_year, tokyo).start == datetime(2027, 1, 1, tzinfo=tokyo)
    fiscal = span("this_fiscal_quarter", NOW, UTC, fiscal_start=2)
    assert (fiscal.start, fiscal.end) == (datetime(2026, 11, 1, tzinfo=UTC), datetime(2027, 2, 1, tzinfo=UTC))
    assert span("last_fiscal_year", NOW, UTC, fiscal_start=2).start == datetime(2025, 2, 1, tzinfo=UTC)
    year = span("this_fiscal_year", NOW, UTC, fiscal_start=12)
    assert (year.start, year.end) == (datetime(2025, 12, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    assert span("next_fiscal_quarter", NOW, UTC, fiscal_start=1).start == datetime(2027, 1, 1, tzinfo=UTC)
    with pytest.raises(WindowError, match="needs the fiscal year's start, and workspace.md states none"):
        span("next_fiscal_year", NOW, UTC)


def test_a_value_shaped_like_a_window_it_does_not_read_is_an_error_that_names_the_windows() -> None:
    assert shaped("last_month") and shaped("next_90_days") and not shaped("open")
    with pytest.raises(WindowError, match="'last_month' is not a window: use last_N_days"):
        span("last_month", NOW, UTC)
    with pytest.raises(WindowError):
        span("last_0_days", NOW, UTC)


def test_a_window_longer_than_the_calendar_stops_at_its_edge() -> None:
    from datetime import UTC, datetime

    from cerebro.window import span

    now = datetime(2026, 11, 17, 12, tzinfo=UTC)
    from zoneinfo import ZoneInfo

    from cerebro.window import holds

    for zone in (UTC, ZoneInfo("America/Los_Angeles"), ZoneInfo("Asia/Tokyo")):
        assert holds(span("last_999999_days", now, zone), "0005-01-01", zone)
        assert holds(span("next_999999_days", now, zone), "4000-01-01", zone)
        assert not holds(span("before_last_999999_days", now, zone), "2026-01-01", zone)


def test_a_plain_date_is_a_day_in_the_workspaces_zone_not_utc() -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from cerebro.window import holds, span

    tokyo = ZoneInfo("Asia/Tokyo")
    week = span("last_7_days", datetime(2026, 11, 16, 16, tzinfo=UTC), tokyo)  # 01:00 on the 17th in Tokyo
    assert holds(week, "2026-11-10", tokyo) and not holds(week, "2026-11-09", tokyo)
