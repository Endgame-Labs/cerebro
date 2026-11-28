"""One live check of the Notes app on real models: a blank workspace whose `extensions.py` loads Notes, a note written
and revised through the workspace's MCP surface, and the app's article agent writing from the note's facts. Not part of
the test suite: it calls real models and costs a few cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_notes.py

The workspace is a temporary folder made as `cerebro
init` makes one, with its config folder beside it. The workspace's clock is the real time moved on by hand past the
agent's cooldown before the revision's pass. It prints each pass, the facts and the article with how many
sentences grounding marked, what the workspace spent, then PASS, or FAIL with every reason. It never prints a key. It
passes only when:
- the note reads back whole at once, before any pass;
- the pass makes more than one fact of the note, and the article is written, every citation in it a fact the
  workspace holds;
- after the note is revised, the old facts are gone from what the workspace serves and none of the new ones repeats
  them, and the article is rewritten from the new facts alone.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from _paths import ran_lines
from mcp import Client
from mcp.types import Implementation

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import MARK, cut, passages
from cerebro.evidence import CITE
from cerebro.ground import unmarked
from cerebro.runner import PassResult

NOTES = Path(__file__).resolve().parents[1] / "apps" / "notes"
ARTICLE = "projects/standing"
FIRST = ("We chose a weekly spend cap of $5,000 for the ad-spend project over a daily cap, because daily caps cut "
         "off weekend campaigns.\n\nMira Okafor wired the cap into the billing job on 17 November.\n\n"
         "Next, Mira adds an alert at 80% of the cap.\n")
SECOND = ("We moved the ad-spend project to a daily spend cap of $800 after all, because the weekly cap let one "
          "weekend overspend by $1,900.\n\nMira Okafor removed the weekly cap from the billing job on 18 November.\n")


async def _call(workspace: Cerebro, name: str, args: dict[str, Any]) -> Any:
    async with Client(workspace.mcp_server(), client_info=Implementation(name="live-notes", version="1")) as client:
        return await client.call_tool(name, args)


def call(workspace: Cerebro, name: str, args: dict[str, Any]) -> dict[str, Any]:
    reply = anyio.run(_call, workspace, name, args)
    if reply.is_error:
        raise SystemExit(f"FAIL\n- {name} over MCP failed: {reply.content[0].text}")
    return reply.structured_content


def show(n: int, result: PassResult) -> None:
    print(f"pass {n}: {result.read} records read, {result.extracted} facts extracted, {result.retracted} retracted, "
          f"{result.linked} linked, compiled {result.compiled}, {len(result.runs)} runs")
    for run in result.runs:
        print(f"  run {run.agent}: {run.status}{': ' + run.note if run.note else ''}, wrote {([h for h, _ in cut(run.result.text)] if run.result else [])}, "
              f"{run.usage.requests} requests, {run.usage.tool_calls} tool calls")
    for agent, why in result.skipped.items():
        print(f"  skipped {agent}: {why}")
    for line in ran_lines(result):
        print(line)
    for key, why in result.failed.items():
        print(f"  failed  {key}: {why}")
    for line in result.warnings:
        print(f"  warning {line}")


def check_article(workspace: Cerebro, when: str, reasons: list[str], only: set[str] | None = None) -> str:
    """The article's text, after checking it has a compilation and every citation in it is a fact the workspace holds,
    and, with `only`, a fact of `only`."""
    article = workspace.store.article(ARTICLE)
    held = {f.id for f in workspace.extraction.facts()}
    if article is None or not article.compilations:
        reasons.append(f"{when}: {ARTICLE} has no part")
        return ""
    for compilation in passages(article):
        print(f"## {compilation.heading}\n{compilation.text}\n")
        for id in CITE.findall(unmarked(compilation.text)):
            if id not in held:
                reasons.append(f"{when}: {compilation.heading}: [^{id}] is no fact the wiki holds")
            elif only is not None and id not in only:
                reasons.append(f"{when}: {compilation.heading}: [^{id}] is not a fact of the note as it stands")
    print(f"{when}: grounding marked {sum(s.text.count(MARK) for s in article.compilations)} sentences")
    return "\n".join(s.text for s in article.compilations)


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)  # the MCP client logs each request it makes
    reasons: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, daily_usd=1, environ=os.environ)
        (root / "extensions.py").write_text(f"from cerebro import load_app\n\nnotes = load_app({str(NOTES)!r})\n")
        shift = [timedelta()]
        with Cerebro(root, trusted=True, clock=lambda: datetime.now(UTC) + shift[0]) as workspace:
            print(f"agents: {[a.id for a in workspace.agents]}, refused: {workspace.refused}")
            written = call(workspace, "note", {"text": FIRST, "title": "Spend cap", "collection": "notes/projects/ad-spend"})
            name, record = written["id"], written["record"]
            read = call(workspace, "query", {"from": "note", "slug": "spend-cap"})
            if read["count"] != 1 or read["items"][0]["body"] != FIRST:
                reasons.append(f"the note did not read back whole at once: {read}")
            first = workspace.run_once()
            show(1, first)
            old = workspace.extraction.facts(record=record)
            print(f"facts of {name}: {len(old)}")
            for fact in old:
                print(f"- {fact.id} {fact.text}")
            if len(old) < 2:
                reasons.append(f"the note gave {len(old)} facts, not more than one")
            if any("cerebro:" in f.text for f in old):
                reasons.append("a fact names an internal address")
            if ARTICLE not in first.compiled:
                reasons.append(f"the first pass did not write {ARTICLE}")
            before = check_article(workspace, "after the first pass", reasons)

            revised = call(workspace, "note", {"text": SECOND, "id": name})
            print(f"revised {name} to revision {revised['revision']}")
            shift[0] = timedelta(minutes=10)  # past the agent's cooldown of 5 minutes
            second = workspace.run_once()
            show(2, second)
            new = workspace.extraction.facts(record=record)
            print(f"facts of {name} now: {len(new)}")
            for fact in new:
                print(f"- {fact.id} {fact.text}")
            gone = {f.id for f in old}
            if not new or {f.id for f in new} & gone:
                reasons.append("the revision's facts were not made anew: an old fact is still current")
            if any(f.status == "current" for f in workspace.extraction.facts(ids=list(gone))):
                reasons.append("an old fact is still current after the revision")
            if any("weekly cap of $5,000" in f.text or "alert at 80%" in f.text for f in new):
                reasons.append("a fact of the old text came back after the revision")
            if ARTICLE not in second.compiled:
                reasons.append(f"after the revision, {ARTICLE} was not rewritten")
            after = check_article(workspace, "after the revision", reasons, only={f.id for f in new})
            if after == before:
                reasons.append(f"after the revision, {ARTICLE} reads as it did")
            now = workspace.clock()
            print(f"spent: ${workspace.ledger.spent(now):.4f} (extraction ${workspace.ledger.spent(now, 'extraction'):.4f}, "
                  f"compilation ${workspace.ledger.spent(now, 'compilation'):.4f})")
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    from spend import watch

    watch()
    sys.exit(main())
