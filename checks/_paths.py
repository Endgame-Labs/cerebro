"""What a live check prints of the paths a run takes at the edge of its hand-in and its room, one line per run that
took any, so a live run shows whether they fire: a reply taken as text, a run that handed in nothing, one whose room
or calls were spent or whose reply was cut with nothing handed in, a curator that met its floor, and a call refused for
room or for calls; and the lines of a pass's runs that ran and wrote nothing, which a check prints beside its failures."""

from __future__ import annotations

from typing import Any

from cerebro.run import CUT_NOTHING, FLOOR_HEAD, HANDED_NOTHING, SPENT_NOTHING, TOOK_REPLY

NO_ROOM = "not run: this run has no room left"
NO_CALLS = "not run: this run has made every tool call"


def paths(run: Any) -> str:
    """The paths `run` took, as one line, or "" when it took none."""
    said = [e.text for e in run.transcript if e.role == "tool"]
    floor = [t for t in said if FLOOR_HEAD in t]
    at_floor = sum(t.startswith("Your budget is nearly spent") for t in floor)  # a refusal is the line alone
    found = [*(["took its reply as its text"] if TOOK_REPLY in (run.note or "") else []),
             *(["handed in nothing"] if HANDED_NOTHING in (run.note or "") else []),
             *(["spent its room or calls and handed in nothing"] if SPENT_NOTHING in (run.note or "") else []),
             *(["was cut at its output limit and handed in nothing"] if CUT_NOTHING in (run.note or "") else []),
             *([f"met its floor, {at_floor} calls refused there"] if floor else []),
             *([f"{n} calls refused for room"] if (n := sum(t.startswith(NO_ROOM) for t in said)) else []),
             *([f"{n} calls refused for calls"] if (n := sum(t.startswith(NO_CALLS) for t in said)) else [])]
    return f"path      {run.agent} on {run.target}: {'; '.join(found)}" if found else ""


def ran_lines(result: Any, indent: str = "  ") -> list[str]:
    """A line for each run of a pass that ran and wrote nothing, no failure, so a live run's report shows every run
    that handed in nothing beside those that failed."""
    return [f"{indent}ran     {key}: wrote nothing: {why}" for key, why in result.ran.items()]


class Watching:
    """An agent run that keeps each run it gives, so a check that sees no runs of its own can print their paths."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.runs = inner, []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        done = self.inner.run(*args, **kwargs)
        self.runs.append(done)
        return done
