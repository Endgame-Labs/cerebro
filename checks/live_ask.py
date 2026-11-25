"""A live check of the ask to hand in: a writer whose own instructions lead its model to answer in plain text, run N
times on real models, each in a fresh workspace on two made-up notes, so each run reaches the ask on its own. Not
part of the test suite: it calls real models and costs a few cents a run.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_ask.py --runs 5
    ... checks/live_ask.py --runs 5 --prompt my-ask.md   # any text in place of the bundled `prompts/unwritten.md`
    ... checks/live_ask.py --runs 4 --stand-in       # no model and no key: a scripted model, to see the check work

For each run it prints whether the run reached the ask,
what the model did there (handed in, declined, wrote again, remarked, or said nothing), what landed in the article, and
the run's line; then the counts, and a `spent $x` line that adds what every workspace's ledger holds, extraction,
the runs and grounding's calls together; with `--stand-in` no model is paid, and the line prices the stand-in's
counted tokens. It never prints a key, and it judges nothing: the counts are for a person to read.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _paths import Watching
from spend import dollars

from cerebro import prompts, scaffold
from cerebro.app import Cerebro
from cerebro.article import passages
from cerebro.run import NOTHING, RETURN, WRITE

NOTES = {
    "local/2026-11-10-orchard-sync.md": (
        "Project: Orchard\nDate: 2026-11-10\n\n"
        "Ilse Varga said the Orchard data migration moves from 25 November to 2 December, because the export from the "
        "old ledger is not finished.\nTeodor Lund owns the export and expects it done by 28 November.\n"
        "Questions go to orchard@northwind.example.\n"),
    "local/2026-11-14-orchard-risks.md": (
        "Project: Orchard\nDate: 2026-11-14\n\n"
        "Teodor Lund said the export found 40 rows with a broken date, and the migration cannot start until they are "
        "fixed.\nIlse Varga asked for a go or no-go call on 1 December.\n"),
}
# The agent's own words lead its model to answer in its reply, not with its hand-in, so the run reaches the ask.
AGENT = """---
evidence:
  - facts: { window: last_90_days }
heartbeat: [change]
---

# Instructions

Write where the Orchard migration stands for someone joining the project today:
the date, what holds it up, who owns what, and what happens next. Every
sentence cites a fact. Leave out anything you cannot cite.

Answer in your reply: write the article itself as your answer, in markdown,
under the headings below, so the person reads it as soon as you reply.

# Output

## Where it stands

## What happens next
"""
ARTICLE = "projects/orchard"
HEADINGS = ("Where it stands", "What happens next")


def asked_line(root: Path) -> str:
    """The first sentence of the ask the run uses, the workspace's ejected one or the bundled one, to find it in a
    transcript."""
    return prompts.load("unwritten", root).split(". ")[0].strip()


def at_the_ask(run: Any, line: str) -> tuple[bool, str]:
    """Whether the run reached the ask, and what the model did in the reply after it."""
    entries = run.transcript
    at = next((n for n, e in enumerate(entries) if e.role in ("tool", "user") and line in e.text), None)
    if at is None:
        return False, ""
    for entry in entries[at + 1:]:
        if entry.role == "call" and entry.tool in (WRITE, RETURN):
            return True, "handed in"
        if entry.role == "call" and entry.tool == NOTHING:
            return True, f"declined: {' '.join(str(entry.args.get('reason', '')).split())[:160]}"
        if entry.role == "model" and entry.text.strip():
            said = entry.text
            if any(f"## {heading}" in said for heading in HEADINGS):
                return True, "wrote again, under its headings"
            return True, f"remarked: {' '.join(said.split())[:160]}"
        if entry.role == "call":
            return True, f"called {entry.tool}"
    return True, "said nothing"


def stand_in(n: int, root: Path) -> Any:
    """A scripted model for `--stand-in`: the article in plain text, then, at the ask, by run: a hand-in, a decline,
    the article again, a remark."""
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from cerebro.run import PydanticRun

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        opening = str(messages[0].parts[-1].content)
        fact = opening.split("[^", 1)[1].split("]", 1)[0] if "[^" in opening else "fact_none"
        article = (f"## Where it stands\n\nThe migration moved to 2 December [^{fact}].\n\n"
                   f"## What happens next\n\nA go or no-go call comes first [^{fact}].")
        if not any(isinstance(m, ModelResponse) for m in messages):
            return ModelResponse(parts=[TextPart(article)])
        return ModelResponse(parts=[
            [ToolCallPart(WRITE, {"text": article})],
            [ToolCallPart(NOTHING, {"reason": "Nothing new since my last article."})],
            [TextPart(article)],
            [TextPart("Done.")]][n % 4])

    return PydanticRun(FunctionModel(reply, model_name="stand-in"), root=root)  # the ejected ask too


def one(n: int, folder: Path, prompt: str | None, standing: bool) -> dict[str, Any]:
    """One run in a fresh workspace: init, the notes, the agent, one pass. What it reached, did and landed."""
    root = folder / f"run-{n + 1}"
    scaffold.init(root, daily_usd=1, environ=os.environ)
    for rel, text in NOTES.items():
        (root / rel).write_text(text, encoding="utf-8")
    (root / "agents" / "projects").mkdir(parents=True)
    (root / "agents" / "projects" / "orchard.md").write_text(AGENT, encoding="utf-8")
    if prompt is not None:
        (root / "prompts").mkdir(exist_ok=True)
        (root / "prompts" / "unwritten.md").write_text(prompt, encoding="utf-8")
    extra: dict[str, Any] = {}
    if standing:
        from cerebro.fakes import FakeExtraction

        extra = {"extraction": FakeExtraction(), "agent_run": stand_in(n, root)}
    with Cerebro(root, trusted=True, **extra) as workspace:
        watching = Watching(workspace.agent_run)
        workspace.agent_run = watching
        result = workspace.run_once()
        runs = [r for r in watching.runs if r.target == ARTICLE]
        run = runs[-1] if runs else None
        reached, did = at_the_ask(run, asked_line(root)) if run is not None else (False, "")
        article = workspace.store.article(ARTICLE)
        landed = "\n".join(f"## {p.heading}\n{p.text}" for p in passages(article)) if article else ""
        spent = workspace.ledger.spent(workspace.clock())
    return {"run": run, "reached": reached, "did": did, "landed": landed, "result": result, "spent": spent}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=3, help="how many runs, each in its own workspace")
    parser.add_argument("--prompt", type=Path, help="a file whose text is the ask, in place of the bundled one")
    parser.add_argument("--stand-in", action="store_true", help="a scripted model and no extraction model: no key")
    parser.add_argument("--keep", type=Path, help="keep the workspaces in this folder")
    args = parser.parse_args(argv)
    if not args.stand_in and (not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY")):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    if args.stand_in:
        from cerebro.run import block_model_requests

        block_model_requests()
    prompt = args.prompt.read_text("utf-8") if args.prompt else None
    print(f"the ask: {args.prompt or 'bundled'}")
    print((prompt or prompts.load("unwritten")).strip())
    seen: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as scratch:
        folder = args.keep or Path(scratch)
        folder.mkdir(parents=True, exist_ok=True)
        os.environ["XDG_CONFIG_HOME"] = str(folder / "config")  # the workspaces' secrets folder, outside them
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        for n in range(args.runs):
            got = one(n, folder, prompt, args.stand_in)
            seen.append(got)
            run = got["run"]
            print(f"\nrun {n + 1}: reached the ask: {'yes' if got['reached'] else 'no'}"
                  + (f"; at the ask it {got['did']}" if got["reached"] else ""))
            if run is None:
                print("  no run of the writer: " + "; ".join(f"{k}: {v}" for k, v in got["result"].failed.items()))
                continue
            print(f"  run: {run.status}{': ' + run.note if run.note else ''}, {run.usage.requests} requests, "
                  f"${dollars(run):.4f}")
            for line in got["result"].warnings:
                print(f"  warning {line}")
            print("  landed:" + ("\n    " + got["landed"].replace("\n", "\n    ") if got["landed"] else " nothing"))
        if args.keep is None:
            shutil.rmtree(folder / "config", ignore_errors=True)
    reached = [g for g in seen if g["reached"]]
    did = Counter(g["did"].split(":")[0] for g in reached)
    print(f"\n{len(reached)} of {len(seen)} runs reached the ask; at it: "
          + (", ".join(f"{k} {v}" for k, v in sorted(did.items())) or "none"))
    print(f"{sum(bool(g['landed']) for g in seen)} of {len(seen)} runs left text in the article")
    print(f"spent ${sum(g['spent'] for g in seen):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
