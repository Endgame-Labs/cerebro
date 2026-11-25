"""`cerebro`'s command line, run as a child process by test_serve_signals, on stand-ins for the model and the facts
layer: no network and no model call.

- `SERVE_CHILD_HOLD` naming a file: an agent's run makes that file and then never returns, as a call deaf to a stop.
- `SERVE_CHILD_RUN` giving seconds: an agent's run takes that long, as a model call does, and a stop cancels it as it
  cancels a real one, its request counted as spent; only for the agent `SERVE_CHILD_RUN_FOR` names, when it is set.
- `SERVE_CHILD_DEAF` giving seconds: a run that no stop cuts short, as a call that cannot be cancelled, returns the
  moment a stop has given its leases back, or after that many seconds without a stop; the stop then waits a fifth of a
  second before it goes on, so whatever the late run would write has its moment to land. `SERVE_CHILD_RUNS` naming a
  file: each run that starts adds a line to it.
- `SERVE_CHILD_SLOW` giving seconds, or a range such as `0-4`: the facts of a record that says "slowly" take that long
  to extract, as a model's extraction during a `tell` does.
- `SERVE_CHILD_RAISE`: the facts layer raises an error with that text whenever it is asked for facts, so a whole pass
  fails with it, a refusal with `SERVE_CHILD_RAISE_KIND=cerebro`. `SERVE_CHILD_SERVER_RAISE`: the HTTP server fails
  with that text as it starts.
- `SERVE_CHILD_JOIN` and `SERVE_CHILD_STOP_WAIT` giving seconds: the stop's bound (`cli.JOIN`) and each open request's
  wait (`mcp.STOP_WAIT`) are that long, so a test that runs into them waits less than a real stop does."""

from __future__ import annotations

import os
import random
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cerebro import Cerebro, cli, mcp
from cerebro.agent import Agent, Budget
from cerebro.budget import cancelling
from cerebro.clock import stamp, system_clock
from cerebro.connector import Record
from cerebro.errors import CerebroError
from cerebro.evidence import Evidence
from cerebro.facts import Extracted, Fact
from cerebro.fakes import FakeExtraction, ScriptedRun
from cerebro.run import STOPPED, AgentRun, Run, Usage, block_model_requests

LEASES_BACK = threading.Event()  # set as a stop gives this process's leases back


def _let_go(workspace: Cerebro) -> int:
    """The stop's own `let_go`, after which a deaf run returns, and has a fifth of a second to land what it wrote."""
    given = _real_let_go(workspace)
    if os.environ.get("SERVE_CHILD_DEAF"):
        LEASES_BACK.set()
        time.sleep(0.2)  # under the stop's own wait for its ending, so the stop still says it gave the leases back
    return given


_real_let_go = cli.let_go
cli.let_go = _let_go
if os.environ.get("SERVE_CHILD_JOIN"):
    cli.JOIN = float(os.environ["SERVE_CHILD_JOIN"])
if os.environ.get("SERVE_CHILD_STOP_WAIT"):
    mcp.STOP_WAIT = float(os.environ["SERVE_CHILD_STOP_WAIT"])  # type: ignore[assignment]


class Scripted(AgentRun):
    """Writes each heading the agent's `# Output` names, citing every fact it was given, or holds for good."""

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        hold = os.environ.get("SERVE_CHILD_HOLD")
        if hold:
            Path(hold).write_text(agent.id)
            threading.Event().wait()
        if runs := os.environ.get("SERVE_CHILD_RUNS"):
            with open(runs, "a") as log:
                log.write(agent.id + "\n")
        if deaf := float(os.environ.get("SERVE_CHILD_DEAF", "0")):
            LEASES_BACK.wait(deaf)
        slow = float(os.environ.get("SERVE_CHILD_RUN", "0"))
        if slow and os.environ.get("SERVE_CHILD_RUN_FOR", agent.id) == agent.id:
            woke = threading.Event()
            with cancelling(woke.set):
                if woke.wait(slow):
                    at = stamp(system_clock())
                    return Run("run_stopped", agent.id, agent.target, STOPPED, started_at=at, ended_at=at,
                               usage=Usage("scripted", requests=1, input_tokens=1000))

        def write(prompt: str) -> dict[str, str]:
            facts = re.findall(r"\[\^(fact_\w+)\] ([^(\n]+)", prompt)
            text = " ".join(f"{said.strip()} [^{fact}]" for fact, said in facts) or "Nothing yet."
            return {heading: text for heading in agent.output or ["Summary"]}

        return ScriptedRun(write).run(agent, evidence, tools, budget)


class Slow(FakeExtraction):
    """Extracts a record that says "slowly" in `SERVE_CHILD_SLOW` seconds, and every other at once."""

    def extract(self, record: Record) -> Extracted:
        if "slowly" in (record.body or ""):
            low, _, high = os.environ.get("SERVE_CHILD_SLOW", "0").partition("-")  # seconds, or a range to draw from
            time.sleep(random.uniform(float(low), float(high)) if high else float(low))
        return super().extract(record)

    def facts(self, **kwargs: Any) -> list[Fact]:
        if said := os.environ.get("SERVE_CHILD_RAISE"):
            raise (CerebroError if os.environ.get("SERVE_CHILD_RAISE_KIND") == "cerebro" else RuntimeError)(said)
        return super().facts(**kwargs)


if __name__ == "__main__":
    block_model_requests()
    extraction = Slow()
    cli.open_workspace = lambda root: Cerebro(root, agent_run=Scripted(), extraction=extraction)
    if failing := os.environ.get("SERVE_CHILD_SERVER_RAISE"):
        import cerebro.mcp

        def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError(failing)

        cerebro.mcp.serve = broken  # type: ignore[assignment]
    sys.exit(cli.main())
