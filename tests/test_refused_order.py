"""A run's refused calls keep the order the model made them, not the order they finished, so an audit of the run
reads the same each time: two refused calls of one turn, the second of which finishes first."""

from __future__ import annotations

import threading

from suites.agent_run import AGENT, EVIDENCE
from test_run import _talker

from cerebro.agent import Budget
from cerebro.run import PydanticRun


def racing() -> tuple[list[object], list[str]]:
    """Two tools that refuse, the first of which finishes only once the second has, and what finished, in order."""
    second_done = threading.Event()
    finished: list[str] = []

    def first(about: str) -> dict[str, str]:
        """Look a thing up."""
        second_done.wait(5)  # it finishes only once the second call has
        finished.append("first")
        return {"refused": "nothing to look up"}

    def second(about: str) -> dict[str, str]:
        """Look another thing up."""
        finished.append("second")
        second_done.set()
        return {"refused": "nothing to look up either"}

    return [first, second], finished


TURNS = [[("first", {"about": "this"}), ("second", {"about": "that"})],
         [("write_article", {"text": "The review moved [^f_1]."})]]


def test_two_refused_calls_of_one_turn_are_named_in_the_order_made_when_the_second_finishes_first() -> None:
    tools, finished = racing()
    run = PydanticRun(_talker(TURNS, [])).run(AGENT, EVIDENCE, tools, Budget())
    assert finished == ["second", "first"]  # the second call did finish first
    assert run.refused == ["first", "second"]


def test_the_agent_runs_row_names_the_refused_tools_in_the_order_made() -> None:
    from cerebro.article import Identity
    from cerebro.compile import run_agent
    from cerebro.fakes import MemoryStore

    store = MemoryStore()
    store.create(Identity(AGENT.target, "reports", "Weekly"))
    tools, finished = racing()
    run_agent(AGENT, EVIDENCE, tools, agent_run=PydanticRun(_talker(TURNS, [])), store=store)
    [row] = [r.data for r in store.rows("agent-runs")]
    assert finished == ["second", "first"] and row["refused_tools"] == ["first", "second"]

