"""Windows: named spans of time that a filter's value may be, as `due: last_90_days`.

A window is read at a moment and in a time zone, both given by the caller: nothing here reads the clock. The names:
- `last_N_days`, `next_N_days` and `before_last_N_days`, for a whole N from 1 to 999999. The last N days run from
  the same moment N days back up to now, the next N days from now to N days on, and `before_last_N_days` holds
  every time before the last N days start, so the two never overlap.
- `this_quarter`, `last_quarter` and `next_quarter`, calendar quarters in the given zone.
- `this_fiscal_quarter`, `last_fiscal_quarter`, `next_fiscal_quarter`, `this_fiscal_year`, `last_fiscal_year` and
  `next_fiscal_year`, counted from the month the fiscal year starts. With no start given they are an error.

A time with an offset falls in a window when its instant does. A date is a whole day in the zone, and falls in a
window when any part of it does, but in `before_last_N_days` only when all of it does: the day N days back, part of
which the last N days hold, belongs to `last_N_days` alone. A value written as a span Cerebro does not read, such
as `last_month`, is an error that names the windows it reads, never text that quietly matches nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

from .errors import CerebroError

__all__ = ["NAMES", "Span", "WindowError", "holds", "is_window", "read_time", "shaped", "span"]

NAMES = ("last_N_days, next_N_days or before_last_N_days for a whole N from 1 to 999999, this_quarter, last_quarter "
         "or next_quarter, or this_fiscal_quarter, last_fiscal_quarter, next_fiscal_quarter, this_fiscal_year, "
         "last_fiscal_year or next_fiscal_year")
DAYS = re.compile(r"(last|next|before_last)_([1-9][0-9]{0,5})_days")
QUARTERS = {"this_quarter": 0, "last_quarter": -1, "next_quarter": 1}
FISCAL = {"this_fiscal_quarter": ("q", 0), "last_fiscal_quarter": ("q", -1), "next_fiscal_quarter": ("q", 1),
          "this_fiscal_year": ("y", 0), "last_fiscal_year": ("y", -1), "next_fiscal_year": ("y", 1)}
SHAPED = re.compile(r"(last|next|this|before_last|before|previous|prior)_([0-9]+_)?(days?|weeks?|months?|quarters?|"
                    r"years?|fiscal_quarters?|fiscal_years?|fq|fy)")


class WindowError(CerebroError, ValueError):
    """A value is written as a window Cerebro does not read, or a fiscal window has no fiscal start."""


@dataclass(frozen=True)
class Span:
    """The times a window holds: from `start`, inclusive, to `end`, exclusive. `start` is None for all time before.
    `whole_days` says a date falls in it only when the whole day does, as in `before_last_N_days`."""

    name: str
    start: datetime | None
    end: datetime
    whole_days: bool = False


def is_window(value: object) -> bool:
    """Whether a value is a window Cerebro reads, such as `last_30_days` or `this_fiscal_year`."""
    key = value.strip().lower() if isinstance(value, str) else ""
    return key in QUARTERS or key in FISCAL or DAYS.fullmatch(key) is not None


def shaped(value: object) -> bool:
    """Whether a value is written as a span of time, read or not, such as `last_30_days` or `last_month`."""
    return isinstance(value, str) and SHAPED.fullmatch(value.strip().lower()) is not None


def _months(moment: datetime, months: int) -> datetime:
    """The first of the month `months` from `moment`'s month, at midnight in its zone."""
    index = moment.year * 12 + moment.month - 1 + months
    return moment.replace(year=index // 12, month=index % 12 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def span(name: str, now: datetime, zone: tzinfo, fiscal_start: int | None = None, missing: str | None = None) -> Span:
    """The span the window `name` holds at `now` in `zone`. `fiscal_start` is the month, 1 to 12, the fiscal year
    starts in. Raises `WindowError` for a name that is not a window, and a fiscal window with no fiscal start, whose
    line ends with `missing` when given, in place of saying workspace.md states none."""
    if now.tzinfo is None:
        raise WindowError("a window is read at a time with a zone, and this one has none: pass an aware datetime")
    key = name.strip().lower()
    now = now.astimezone(zone)
    if days := DAYS.fullmatch(key):
        n = timedelta(days=int(days[2]))
        earliest, latest = datetime(1, 1, 2, tzinfo=UTC), datetime(9999, 12, 30, tzinfo=UTC)
        back = now - n if now - earliest > n else earliest  # a window past the calendar's edge stops a day inside it
        ahead = now + n if latest - now > n else latest
        if days[1] == "last":
            return Span(key, back, now)
        if days[1] == "next":
            return Span(key, now, ahead)
        return Span(key, None, back, whole_days=True)
    quarter = _months(now, -((now.month - 1) % 3))
    if key in QUARTERS:
        start = _months(quarter, 3 * QUARTERS[key])
        return Span(key, start, _months(start, 3))
    if key not in FISCAL:
        raise WindowError(f"{name!r} is not a window: use {NAMES}")
    if fiscal_start is None or not 1 <= fiscal_start <= 12:
        raise WindowError(f"{key} needs the fiscal year's start, and " + (missing or "workspace.md states none: add a "
                          "rule such as \"The fiscal year starts February 1.\""))
    into = (now.month - fiscal_start) % 12
    year = _months(now, -into)
    unit, step = FISCAL[key]
    start = _months(year, 12 * step) if unit == "y" else _months(year, into // 3 * 3 + 3 * step)
    return Span(key, start, _months(start, 12 if unit == "y" else 3))


def read_time(value: object, zone: tzinfo) -> tuple[datetime, bool] | None:
    """A value as a moment, and whether it is a whole date: a date is the start of its day in `zone`, and a time
    with no offset is read in `zone`. None for a value that is neither."""
    if not isinstance(value, str) or len(value.strip()) < 10:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return datetime(day.year, day.month, day.day, tzinfo=zone), True
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=zone)), False


def holds(window: Span, value: object, zone: tzinfo) -> bool:
    """Whether a value, or one item of a list, falls in the window. A value that is no date or time does not."""
    for item in value if isinstance(value, list) else [value]:
        read = read_time(item, zone)
        if read is None:
            continue
        moment, whole = read
        if whole:
            end = moment + timedelta(days=1)  # wall-clock arithmetic: the next midnight in the zone
            if window.whole_days:
                found = end <= window.end
            else:
                found = moment < window.end and (window.start is None or end > window.start)
        else:
            found = moment < window.end and (window.start is None or moment >= window.start)
        if found:
            return True
    return False
