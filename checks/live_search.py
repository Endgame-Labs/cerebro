"""One live check of a search across everything on real models: the small workspace `live_pass.py` builds, one real
pass, then three queries with `matching` alone, asked the way an outside agent asks, through the workspace's MCP
surface. Not part of the test suite: it calls real models and costs a few cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_search.py

The workspace is a temporary folder made as `cerebro
init` makes one, with its config folder beside it, so nothing is left behind and no call is replayed. It prints each
reply's text as a model reads it, what the pass and the searches spent, then PASS, or FAIL with every reason. It never
prints a key. It passes only when:
- a search whose answer the article states puts the article in the first three items;
- a search whose answer only a fact holds, one the article does not state, returns that fact;
- a fact linked to an article in a reply is folded under it and never listed on its own as well;
- each item's id, and each folded fact's, reads back with `read`;
- a search about something the workspace does not hold comes back empty, or says plainly that nothing matched;
- what the searches spent, by Factoid's own meter, is in the ledger.

Its agent is `live_pass.py`'s, told to leave out the budget, so that one fact holds what the article does not.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import anyio
from live_pass import AGENT, ARTICLE, NOTES, show
from mcp import Client
from mcp.types import Implementation

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import cut, passages
from cerebro.budget import LOG

IN_ARTICLE = "Halcyon launch date"  # the article says when the launch is
IN_FACT = ("budget", "40,000")  # only a fact holds the launch budget: the agent is told to leave it out
# live_pass.py's agent, told to leave the budget out, so that a fact holds something the article does not
ONE_AGENT = AGENT.replace("cannot cite.", "cannot cite, and leave out the budget.")
NOT_HELD = "Lisbon office tax filing"  # nothing in the workspace


async def _call(workspace: Cerebro, name: str, args: dict[str, Any]) -> Any:
    async with Client(workspace.mcp_server(), client_info=Implementation(name="live-search", version="1")) as client:
        return await client.call_tool(name, args)


def call(workspace: Cerebro, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The reply's text, as a model reads it, and its data."""
    reply = anyio.run(_call, workspace, name, args)
    text = reply.content[0].text if reply.content else ""
    if reply.is_error:
        raise SystemExit(f"FAIL\n- {name} {args} over MCP failed: {text}")
    return text, reply.structured_content or {}


def search(workspace: Cerebro, words: str, reasons: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Ask `words` with `matching` alone, print the reply, and check its folding and that each id reads back. Gives
    the items and the warnings."""
    text, data = call(workspace, "query", {"matching": words})
    print(f'\nquery {{ "matching": "{words}" }}\n\n{text}\n')
    items = data.get("items", [])
    alone = {item["id"] for item in items if item["kind"] == "fact"}
    for item in items:
        if item["kind"] != "article":
            continue
        linked = set(workspace.store.facts_of(item["id"]))
        folded = {fact["id"] for fact in item.get("facts", [])}
        if folded - linked:
            reasons.append(f"{words!r}: {sorted(folded - linked)} folded under {item['id']}, which they are not linked "
                           "to")
        if twice := linked & alone:
            reasons.append(f"{words!r}: {sorted(twice)} listed on their own beside {item['id']}, which they link to")
    ids = [item["id"] for item in items] + [fact["id"] for item in items for fact in item.get("facts", [])]
    for n in range(0, len(ids), 20):
        _, read = call(workspace, "read", {"ids": ids[n:n + 20]})
        if read.get("missing"):
            reasons.append(f"{words!r}: read found none of {read['missing']}")
    return items, data.get("warnings", [])


def embed_tokens(workspace: Cerebro) -> int:
    """The input tokens Factoid's meter counts on its embedding model so far, to set beside what the ledger records."""
    rows = getattr(getattr(getattr(workspace.extraction, "app", None), "client", None), "meter", None)
    return int(rows.rows.get("embed", {}).get("input_tokens", 0)) if rows is not None else 0


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)  # the MCP client logs each request it makes
    reasons: list[str] = []
    retried = ""
    with tempfile.TemporaryDirectory() as folder:
        # the workspace's secrets folder, outside the workspace
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, daily_usd=1, environ=os.environ)
        for rel, text in NOTES.items():
            (root / rel).write_text(text, encoding="utf-8")
        (root / "agents" / "projects").mkdir(parents=True)
        (root / "agents" / "projects" / "launch.md").write_text(ONE_AGENT, encoding="utf-8")
        with Cerebro(root, trusted=True) as workspace:
            result = workspace.run_once()
            show(1, result)
            article = workspace.store.article(ARTICLE)
            if (article is None or not article.compilations) and result.runs and all(r.status == "done" for r in result.runs):
                retried = "the first run ended as done and its draft did not land, so the agent ran once more"
                print(f"  {retried}")
                again = workspace.compile(next(a for a in workspace.agents if a.id == ARTICLE), scheduled=True)
                print(f"  again: {again.run.status if again.run else 'skipped'}, wrote {([h for h, _ in cut(again.run.result.text)] if again.run.result else []) if again.run else []}")
                article = workspace.store.article(ARTICLE)
            if article is None or not article.compilations:
                print(f"FAIL\n- the pass wrote no {ARTICLE}")
                return 1
            body = "\n".join(s.text for s in article.compilations)
            for compilation in passages(article):
                print(f"## {compilation.heading}\n{compilation.text}\n")
            now = workspace.clock()
            passed, embedded = workspace.ledger.spent(now), embed_tokens(workspace)

            first, _ = search(workspace, IN_ARTICLE, reasons)
            top = [item["id"] for item in first[:3]]
            if ARTICLE not in top:
                reasons.append(f"{IN_ARTICLE!r}: {ARTICLE} is not in the first three items: {top}")

            words, answer = IN_FACT
            if answer in body:
                reasons.append(f"the article states {answer}, so {words!r} does not test a fact the article lacks")
            second, _ = search(workspace, words, reasons)
            facts = [item["text"] for item in second if item["kind"] == "fact"] + \
                [fact["text"] for item in second for fact in item.get("facts", [])]
            if not any(answer in text for text in facts):
                reasons.append(f"{words!r}: no fact that holds {answer} came back: {facts}")

            third, warnings = search(workspace, NOT_HELD, reasons)
            if third and not any("matched no" in w for w in warnings):
                reasons.append(f"{NOT_HELD!r}: {len(third)} items came back, and no warning says nothing matched")

            now = workspace.clock()
            tokens = embed_tokens(workspace) - embedded
            booked = [row.data["usd"] for row in workspace.store.rows(LOG) if row.data.get("run") == "search"]
            print(f"the searches: {tokens} embedding tokens by Factoid's own meter, {len(booked)} rows in the ledger "
                  f"under extraction, ${sum(booked):.7f}")
            print(f"spent: ${workspace.ledger.spent(now):.4f} (extraction ${workspace.ledger.spent(now, 'extraction'):.4f}, "
                  f"compilation ${workspace.ledger.spent(now, 'compilation'):.4f}); the pass ${passed:.4f}")
            if tokens and not booked:
                reasons.append(f"the searches spent {tokens} embedding tokens, and the ledger records none")
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    if retried:  # a pass on the second try, never shown as a clean one
        print(f"retried: {retried}")
    print("PASS")
    return 0


if __name__ == "__main__":
    from spend import watch

    watch()
    sys.exit(main())
