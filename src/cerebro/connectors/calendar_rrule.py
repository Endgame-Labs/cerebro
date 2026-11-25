"""Recurrence rules, of the shapes calendars write for meetings, read by hand.

A rule is daily, weekly, monthly or yearly, with `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY`, `BYMONTHDAY`, `BYMONTH`,
`BYSETPOS` and `WKST`. Any other part, such as `FREQ=HOURLY` or `BYYEARDAY`, is refused with `RuleError`, and the
event then stands as its first occurrence alone. Times here are wall clock readings, naive datetimes, so a weekly
meeting at 15:00 stays at 15:00 across a change of daylight saving time; the caller turns each into an instant in
its zone. A walk is bounded, so no rule can make a read run long.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..errors import CerebroError

__all__ = ["DayRule", "Rule", "RuleError", "days_in", "parse_rule", "parse_wall"]

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
MAX_PERIODS = 200_000


class RuleError(CerebroError, ValueError):
    """A recurrence rule this connector does not expand. The message names the part it does not read."""


def parse_wall(value: str) -> tuple[datetime, bool] | None:
    """`20261015T170000`, `20261015T1700` or `20261015` as a wall clock reading, and whether it ended in `Z`."""
    value = value.strip()
    utc = value.endswith(("Z", "z"))
    value = value.rstrip("Zz")
    shapes = {8: "%Y%m%d", 15: "%Y%m%dT%H%M%S", 13: "%Y%m%dT%H%M"}
    shape = shapes.get(len(value))
    if shape is None:
        return None
    try:
        return datetime.strptime(value, shape), utc and len(value) != 8
    except ValueError:
        return None


def days_in(day: datetime) -> int:
    """How many days the month of `day` has."""
    following = datetime(day.year + day.month // 12, day.month % 12 + 1, 1)
    return (following - timedelta(days=1)).day


def _months_on(start: datetime, months: int) -> datetime:
    """The first of the month `months` after `start`'s, at `start`'s clock."""
    index = start.year * 12 + start.month - 1 + months
    return start.replace(year=index // 12, month=index % 12 + 1, day=1)


def _ints(value: str, limit: int, part: str) -> list[int]:
    out = []
    for text in value.split(","):
        try:
            number = int(text.strip())
        except ValueError:
            raise RuleError(f"{part}={value}") from None
        if number == 0 or abs(number) > limit:
            raise RuleError(f"{part}={value}")
        out.append(number)
    return out


@dataclass(frozen=True)
class DayRule:
    """One `BYDAY` entry: a weekday, Monday 0, with its place in the month or year when it has one, as `2MO`."""

    ord: int
    weekday: int


@dataclass
class Rule:
    """A recurrence rule read. `until` is a wall reading, an instant in UTC when `until_utc`, and a whole day kept
    when `until_day`."""

    freq: str = ""
    interval: int = 1
    count: int = 0
    until: datetime | None = None
    until_utc: bool = False
    until_day: bool = False
    byday: list[DayRule] = field(default_factory=list)
    bymonthday: list[int] = field(default_factory=list)
    bymonth: list[int] = field(default_factory=list)
    bysetpos: list[int] = field(default_factory=list)
    wkst: int = 0

    def beyond(self, wall: datetime, instant: Callable[[datetime], datetime]) -> bool:
        """Whether an occurrence is past `UNTIL`. `instant` turns a wall reading into a UTC instant, naive."""
        if self.until is None:
            return False
        if self.until_utc:
            return instant(wall) > self.until
        if self.until_day:
            return wall >= self.until + timedelta(days=1)
        return wall > self.until

    def each(self, start: datetime, stop: datetime, beyond: Callable[[datetime], bool],
             emit: Callable[[datetime], bool]) -> None:
        """Walk the occurrences from `start` in order, up to `COUNT` and `UNTIL` and before `stop`, and give each
        to `emit` until it says False."""
        count = 0
        try:
            for i in range(MAX_PERIODS):
                begins, candidates = self._period(start, i)
                if begins >= stop:
                    return
                for candidate in candidates:
                    if candidate < start:
                        continue
                    if beyond(candidate) or candidate >= stop:
                        return
                    count += 1
                    if self.count and count > self.count:
                        return
                    if not emit(candidate):
                        return
        except (OverflowError, ValueError):  # a walk past the last year a date can hold
            return

    def between(self, start: datetime, low: datetime, high: datetime) -> list[datetime]:
        """The occurrences from `start` within `[low, high)`, `UNTIL` read as a wall reading. How a time zone block's
        changes of offset are found."""
        found: list[datetime] = []

        def keep(wall: datetime) -> bool:
            if wall >= low:
                found.append(wall)
            return True

        self.each(start, high, lambda wall: self.beyond(wall, lambda w: w), keep)
        return found

    # -- one period
    def _period(self, start: datetime, i: int) -> tuple[datetime, list[datetime]]:
        n = i * self.interval
        if self.freq == "DAILY":
            day = start + timedelta(days=n)
            if not self._month_ok(day) or (self.bymonthday and _signed_day(day, self.bymonthday)
                                           not in self.bymonthday) or (self.byday and not self._weekday_in(day)):
                return day, []
            return day, [day]
        if self.freq == "WEEKLY":
            back = (start.weekday() - self.wkst) % 7
            week = start - timedelta(days=back) + timedelta(days=7 * n)
            out = []
            for d in range(7):
                day = week + timedelta(days=d)
                if not self._month_ok(day):
                    continue
                if (not self.byday and day.weekday() == start.weekday()) or (self.byday and self._weekday_in(day)):
                    out.append(day)
            return week, out
        if self.freq == "MONTHLY":
            first = _months_on(start, n)
            if self.bymonth and first.month not in self.bymonth:
                return first, []
            return first, self._pick(self._month_days(first, start))
        year = start.replace(year=start.year + n, month=1, day=1)
        if not self.bymonth and self.byday and not self.bymonthday and any(d.ord for d in self.byday):
            return year, self._pick(self._year_days(year))
        out = []
        for month in sorted(self.bymonth or [start.month]):
            out += self._month_days(year.replace(month=month), start)
        return year, sorted(set(out))

    def _month_ok(self, day: datetime) -> bool:
        return not self.bymonth or day.month in self.bymonth

    def _weekday_in(self, day: datetime) -> bool:
        return any(d.weekday == day.weekday() for d in self.byday)

    def _month_days(self, first: datetime, start: datetime) -> list[datetime]:
        """The days of the month from `first` that `BYMONTHDAY` and `BYDAY` name, at `start`'s clock. With neither,
        `start`'s day of the month, which a month without it skips."""
        n = days_in(first)
        if not self.bymonthday and not self.byday:
            return [first.replace(day=start.day)] if start.day <= n else []
        out = []
        for d in range(1, n + 1):
            day = first.replace(day=d)
            if self.byday and not self._day_matches(day, n):
                continue
            if self.bymonthday and d not in self.bymonthday and d - n - 1 not in self.bymonthday:
                continue
            out.append(day)
        return out

    def _day_matches(self, day: datetime, n: int) -> bool:
        for d in self.byday:
            if d.weekday != day.weekday():
                continue
            if d.ord == 0 or (d.ord > 0 and (day.day - 1) // 7 + 1 == d.ord) or (
                    d.ord < 0 and (n - day.day) // 7 + 1 == -d.ord):
                return True
        return False

    def _year_days(self, year: datetime) -> list[datetime]:
        total = (year.replace(year=year.year + 1) - year).days
        out = []
        for d in range(total):
            day = year + timedelta(days=d)
            for rule in self.byday:
                if rule.weekday == day.weekday() and (rule.ord == 0 or (rule.ord > 0 and d // 7 + 1 == rule.ord) or (
                        rule.ord < 0 and (total - 1 - d) // 7 + 1 == -rule.ord)):
                    out.append(day)
                    break
        return out

    def _pick(self, candidates: list[datetime]) -> list[datetime]:
        """`BYSETPOS` over one period's candidates."""
        candidates = sorted(candidates)
        if not self.bysetpos:
            return candidates
        picked = set()
        for position in self.bysetpos:
            index = position - 1 if position > 0 else len(candidates) + position
            if 0 <= index < len(candidates):
                picked.add(candidates[index])
        return sorted(picked)


def _signed_day(day: datetime, listed: list[int]) -> int:
    """The day of the month as the rule spells it, positive or negative, whichever of the two it lists."""
    return day.day if day.day in listed else day.day - days_in(day) - 1


def parse_rule(value: str) -> Rule:
    """Read an `RRULE` value. Raises `RuleError` for a rule this connector does not expand."""
    rule = Rule()
    for part in value.strip().split(";"):
        key, eq, text = part.partition("=")
        if not eq:
            continue
        key, text = key.strip().upper(), text.strip()
        if key == "FREQ":
            rule.freq = text.upper()
            if rule.freq not in FREQS:
                raise RuleError(f"FREQ={text} is not read")
        elif key in ("INTERVAL", "COUNT"):
            try:
                number = int(text)
            except ValueError:
                raise RuleError(f"{key}={text}") from None
            if number < 1 or (key == "INTERVAL" and number > 1000):
                raise RuleError(f"{key}={text}")
            setattr(rule, key.lower(), number)
        elif key == "UNTIL":
            read = parse_wall(text)
            if read is None:
                raise RuleError(f"UNTIL={text}")
            rule.until, rule.until_utc = read
            rule.until_day = len(text.rstrip("Zz")) == 8
        elif key == "BYDAY":
            for entry in text.split(","):
                entry = entry.strip().upper()
                if len(entry) < 2 or entry[-2:] not in WEEKDAYS:
                    raise RuleError(f"BYDAY={text}")
                place = 0
                if len(entry) > 2:
                    try:
                        place = int(entry[:-2])
                    except ValueError:
                        raise RuleError(f"BYDAY={text}") from None
                    if place == 0 or abs(place) > 53:
                        raise RuleError(f"BYDAY={text}")
                rule.byday.append(DayRule(place, WEEKDAYS[entry[-2:]]))
        elif key == "BYMONTHDAY":
            rule.bymonthday = _ints(text, 31, key)
        elif key == "BYMONTH":
            rule.bymonth = _ints(text, 12, key)
        elif key == "BYSETPOS":
            rule.bysetpos = _ints(text, 366, key)
        elif key == "WKST":
            if text.upper() not in WEEKDAYS:
                raise RuleError(f"WKST={text}")
            rule.wkst = WEEKDAYS[text.upper()]
        else:
            raise RuleError(f"{key} is not read")
    if not rule.freq:
        raise RuleError("no FREQ")
    return rule
