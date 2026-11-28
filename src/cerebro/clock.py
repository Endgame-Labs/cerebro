"""The one place the time is read. Everything else is handed a clock, so what is due can be tested at any time.

A clock is a function that gives an aware datetime. The app object holds one and passes it on. `seconds` is the one
other reading: seconds on a counter that only moves forward, for pacing requests to a rate, never a time of day.
`local_zone` reads the machine's own time zone by its IANA name, for `init` to offer. `pinned` moves a moment to the
day `CEREBRO_TODAY` names, the one today that windows, a writer and the grounding judge read, so a demo's dates stay
in their windows. Leases, schedules and stamps keep the real clock.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import CerebroError

__all__ = ["Clock", "is_zone", "local_zone", "pinned", "seconds", "stamp", "system_clock"]

LOCALTIME = Path("/etc/localtime")
TIMEZONE_FILE = Path("/etc/timezone")
TODAY = "CEREBRO_TODAY"

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """The machine's time, in UTC."""
    return datetime.now(UTC)


def seconds() -> float:
    """Seconds on the machine's monotonic counter: only the gap between two readings means anything."""
    return time.monotonic()


def stamp(moment: datetime) -> str:
    """A moment as ISO 8601 UTC text to the second, such as 2026-11-17T09:00:00Z."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def pinned(moment: datetime) -> datetime:
    """`moment` on the day `CEREBRO_TODAY` names, at the same time of day in its own zone, or `moment` when it is
    unset. A value that is not a date is refused with what to write."""
    given = os.environ.get(TODAY, "")
    if not given:
        return moment
    try:
        day = date.fromisoformat(given)
    except ValueError:
        raise CerebroError(f"{TODAY} is {given!r}: write a date such as 2026-11-20") from None
    return moment.replace(year=day.year, month=day.month, day=day.day)


def is_zone(name: str) -> bool:
    """Whether the system's zone data, or the `tzdata` package where it is installed, knows `name`, older link names
    such as US/Pacific included."""
    if not name or name.startswith("/") or name != name.strip():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _from_link(target: str) -> str:
    """The zone name in a link into a zoneinfo folder, as macOS and Linux point `/etc/localtime`, or ""."""
    if "zoneinfo/" not in target:
        return ""
    name = target.rsplit("zoneinfo/", 1)[1]
    for prefix in ("posix/", "right/"):
        name = name.removeprefix(prefix)
    return name


def local_zone(environ: Mapping[str, str] | None = None, localtime: Path = LOCALTIME,
               timezone_file: Path = TIMEZONE_FILE) -> str | None:
    """The machine's own time zone by its IANA name, found in order: `TZ` when it names a zone, the link behind
    `/etc/localtime`, then `/etc/timezone`. None when none gives a name the zone data knows."""
    env = os.environ if environ is None else environ
    found = [env.get("TZ", "").removeprefix(":")]
    try:
        found.append(_from_link(os.readlink(localtime)))
    except OSError:
        pass
    try:
        found.append(timezone_file.read_text(encoding="utf-8").strip().splitlines()[0])
    except (OSError, UnicodeDecodeError, IndexError):
        pass
    return next((name for name in found if is_zone(name)), None)
