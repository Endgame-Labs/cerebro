"""What the runs of a live check spent, by role: curator, subagent, composer, and single-article agent.

The live checks import it to print one line per run and a table at the end. A run's dollars are its tokens at the
list price of the model it ran on, as the ledger prices them.
"""

from __future__ import annotations

import atexit
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from cerebro.models import cost
from cerebro.run import PydanticRun, Run

__all__ = ["Spend", "acts_line", "dollars", "fetches", "refusals", "role_of", "spent_line", "table", "watch"]

ROLES = {"curator": "curator", "once": "curator", "subagent": "subagent", "composer": "composer"}


def role_of(roles: dict[str, str], run: Run) -> str:
    """The role a run is counted under, from its agent's role."""
    return ROLES.get(roles.get(run.agent, ""), "single")


def dollars(run: Run) -> float:
    provider, _, model = run.usage.model.partition(":")
    return cost(provider, model, run.usage) if model else 0.0


def fetches(run: Run) -> int:
    """How many of a run's tool calls were a curator's fetch."""
    return sum(1 for entry in run.transcript if entry.role == "call" and entry.tool == "fetch")


def acts_line(runs: list[Run]) -> str:
    """Three counts over the runs: a curator's link calls, those whose reply added or took off something, and admits
    refused because the article is in the working set."""
    replies = [e for run in runs for e in run.transcript if e.role == "tool"]
    links = [e.text for e in replies if e.tool == "link"]
    changed = sum(1 for text in links if re.search(r"""["'](added|removed)["']""", text))  # a dict, as JSON or not
    in_set = sum(e.text.count("is in the working set already") for e in replies if e.tool == "admit")
    return (f"link calls {len(links)}, link calls that changed something {changed}, admits refused as in the "
            f"working set {in_set}")


def refusals(runs: list[Run]) -> Counter[str]:
    """Each refusal the runs' tools gave, by its first words with each path folded, each argument the model loop
    refused by name, and each call that gave `articles`."""
    said: Counter[str] = Counter()
    for e in (e for run in runs for e in run.transcript if e.role == "call" and "articles" in e.args):
        said[f"{e.tool}: called with articles"] += 1  # an argument admit does not take, which the model loop drops
    for e in (e for run in runs for e in run.transcript if e.role == "tool"):
        for line in re.findall(r"""['"]refused['"]: ['"]([^'"]+)""", e.text) + re.findall(
                r"""['"][^'"]+:[^'"]+['"]: ['"]((?:admit|link|leave_out|instruct): [^'"]+)""", e.text):
            words = re.sub(r"\b[\w.-]+/[\w./-]+", "<path>", line).split()
            said[" ".join(words[:8])] += 1
        for name in re.findall(r"(?:unexpected_keyword_argument|extra_forbidden)['\"][^)]*?['\"]loc['\"]: \(['\"](\w+)",
                               e.text):
            said[f"{e.tool or 'a tool'}: argument {name} refused"] += 1
    return said


def watch() -> None:
    """Keep each run a real model makes in this process, and print `acts_line` over them as the check ends."""
    seen: list[Run] = []
    real = PydanticRun.run

    def run(self: PydanticRun, *args: object, **kwargs: object) -> Run:
        done = real(self, *args, **kwargs)  # type: ignore[arg-type]
        seen.append(done)
        return done

    PydanticRun.run = run  # type: ignore[method-assign]
    atexit.register(lambda: print(f"\nacts: {acts_line(seen)}\nrefusals: " + ("; ".join(
        f"{n}x {words}" for words, n in refusals(seen).most_common()) or "none")))


def spent_line(run: Run) -> str:
    u = run.usage
    return (f"{u.requests} requests, {u.tool_calls} tool calls ({fetches(run)} fetch), {u.input_tokens} in ({u.cache_read_tokens} cached), "
            f"{u.output_tokens} out, ${dollars(run):.4f}")


@dataclass
class Spend:
    runs: int = 0
    requests: int = 0
    tool_calls: int = 0
    fetches: int = 0
    input_tokens: int = 0
    cached: int = 0
    output_tokens: int = 0
    dollars: float = 0.0


def table(roles: dict[str, str], runs: list[Run]) -> str:
    """Totals by role, and over every run, as a table to print."""
    by: dict[str, Spend] = defaultdict(Spend)
    for run in runs:
        for key in (role_of(roles, run), "all"):
            s = by[key]
            s.runs += 1
            s.requests += run.usage.requests
            s.tool_calls += run.usage.tool_calls
            s.fetches += fetches(run)
            s.input_tokens += run.usage.input_tokens
            s.cached += run.usage.cache_read_tokens
            s.output_tokens += run.usage.output_tokens
            s.dollars += dollars(run)
    rows = ["| role | runs | requests | tool calls | fetch calls | input | cached | output | dollars |", "|" + "---|" * 9]
    for key in ("curator", "subagent", "composer", "single", "all"):
        if key in by:
            s = by[key]
            rows.append(f"| {key} | {s.runs} | {s.requests} | {s.tool_calls} | {s.fetches} | {s.input_tokens} | {s.cached} | "
                        f"{s.output_tokens} | {s.dollars:.4f} |")
    return "\n".join(rows)
