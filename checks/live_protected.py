"""One live check of how a writer on real models meets the fields the wiki keeps itself: a tiny workspace in a
temporary folder with a daily cap of $1, one article its curator admitted, one note a person told about it, and one
writer whose prompt gives it a plain reason to set status, in_source and title with override, and one free field,
level. Not part of the test suite: it calls real models and costs a cent or two.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_protected.py
    uv run python checks/live_protected.py --fake

`--fake` runs the same steps and the same checks through
the real run loop on a scripted model, which tries each field, tries title again once refused, and then hands in, as a
smoke test of the check itself. It prints each override call, the reply it got and what the model did next (tried the
field again, wrote the value into its text, or moved on), the text it handed in, and what it spent, then PASS, or
FAIL with every reason. It never prints a key. It passes only when:
- the model tried at least one field the wiki keeps, so the check saw a refusal;
- no value a model gave status, in_source or title stands, and the article reads its curator's title, its source's
  in_source, and its status;
- the free field lands as the model set it;
- the refusals the run counted match the refused replies in its transcript, and its row in the agent-runs log.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import Identity, fields, protected
from cerebro.caller import Caller

YOU = Caller("you", "person")
ARTICLE = "things/beta"
KEPT = {"status": "closed", "in_source": False, "title": "Beta (closed)"}  # what the writer is given reason to set
FREE = ("level", "high")
NOTE = ("Beta closed its pilot on 18 November 2026, and its source no longer lists it. "
        "Its team rates the risk as high, and renamed the effort Beta (closed).")
WRITER = """---
evidence:
  - query: { from: facts, source: tell }
---

Write the article about this thing from its facts.

Its notes say it has closed, is no longer in its source, has a new name, and carries a high risk. Keep its fields true
to the notes with override, citing the facts each rests on: status closed, in_source false, title "Beta (closed)", and
level high. Then hand in one short paragraph on what changed.

# Output

## Summary
"""


def lay_out_workspace(root: Path) -> None:
    """The tiny workspace both modes run: settings with the $1 cap and the writer."""
    scaffold.init(root, daily_usd=1, environ=os.environ)
    (root / "agents" / "things").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "things" / "article.md").write_text(WRITER, encoding="utf-8")


def admitted(workspace: Cerebro) -> None:
    """The article as a curator would admit it, and the note a person told."""
    workspace.store.create(Identity(ARTICLE, "things", "Beta"))
    workspace.tell(YOU, NOTE, ARTICLE, as_="fact")


def scripted() -> Any:
    """A model that sets each field, tries title again once refused, then hands in text citing the facts it read."""
    from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = sum(isinstance(m, ModelResponse) for m in messages)
        if turn == 0:
            return ModelResponse(parts=[ToolCallPart("override", {"field": name, "value": value})
                                        for name, value in [*KEPT.items(), FREE]])
        if turn == 1:
            return ModelResponse(parts=[ToolCallPart("override", {"field": "title", "value": "Beta, closed"})])
        said = " ".join(str(getattr(p, "content", "")) for m in messages if isinstance(m, ModelRequest) for p in m.parts)
        cites = "".join(f"[^{id}]" for id in dict.fromkeys(re.findall(r"\[\^([^\]\s]+)\]", said)))
        if not info.output_tools:
            return ModelResponse(parts=[TextPart(f"Beta closed its pilot. {cites}")])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"text": f"Beta closed its pilot. {cites}"})])

    return FunctionModel(reply, model_name="scripted")


def next_step(entries: list[Any], at: int, field: str, value: Any, text: str) -> str:
    """What the model did after the override call at `at` was refused."""
    if any(e.role == "call" and e.tool == "override" and str(e.args.get("field", "")).strip() == field
           for e in entries[at + 1:]):
        return "tried the field again"
    if str(value).lower() in text.lower():
        return "wrote the value into its text"
    return "moved on"


def check(workspace: Cerebro, reasons: list[str]) -> None:
    agent = workspace.agent("things/article")
    if agent is None:
        reasons.append("the writer did not load")
        return
    out = workspace.compile(agent, ARTICLE, scheduled=True)
    run = out.run
    if run is None:
        reasons.append(f"the writer did not run: {out.waiting or 'nothing moved'}")
        return
    text = run.result.text if run.result else ""
    entries = run.transcript
    calls = [n for n, e in enumerate(entries) if e.role == "call" and e.tool == "override"]
    replies = [e.text for e in entries if e.role == "tool" and e.tool == "override"]  # in the order of the calls
    tried = 0
    for n, reply in zip(calls, replies + [""] * (len(calls) - len(replies)), strict=True):
        field, value = str(entries[n].args.get("field", "")).strip(), entries[n].args.get("value")
        tried += bool(protected(field))
        print(f"override {field} = {value!r}\n  -> {reply[:300]}")
        if reply.startswith("{'refused'"):
            print(f"  then: {next_step(entries, n, field, value, text)}")
    refused_replies = sum(e.role == "tool" and e.text.startswith("{'refused'") for e in entries)
    print(f"handed in: {text[:600]!r}")
    print(f"status    {run.status}{': ' + run.note if run.note else ''}")
    if not tried:
        reasons.append("the model never tried a field the wiki keeps, so no refusal was seen")
    article = workspace.store.article(ARTICLE)
    if article is None:
        reasons.append(f"{ARTICLE} is gone")
        return
    standing = fields(article)
    for name in KEPT:
        prop = standing.get(name)
        if prop is not None and prop.by.kind in ("agent", "outside"):
            reasons.append(f"a model's {name} stands: {prop.value!r}")
    who = article.identity
    if who.title != "Beta" or who.in_source is not True or who.status != "active":
        reasons.append(f"the article reads title {who.title!r}, in_source {who.in_source!r}, status {who.status!r}, "
                       "not the system's")
    level = standing.get(FREE[0])
    if level is None or str(level.value).lower() != FREE[1]:
        reasons.append(f"the free field {FREE[0]} did not land as set: {level.value if level else 'absent'!r}")
    row = next((r.data for r in workspace.store.rows("agent-runs") if r.data.get("run") == run.id), {})
    counted = len(run.refused)
    if not counted == refused_replies == row.get("refused", 0):
        reasons.append(f"the run counted {counted} refusals, its transcript holds {refused_replies}, and its row "
                       f"says {row.get('refused', 0)}")
    print(f"refused   {counted}: {', '.join(run.refused) or 'none'}")
    print(f"spent     ${workspace.ledger.spent(workspace.clock()):.4f}")


def main(argv: list[str] | None = None) -> int:
    fake = "--fake" in (sys.argv[1:] if argv is None else argv)
    if not fake and (not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY")):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY, or run "
              "with --fake on a scripted model")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    reasons: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        lay_out_workspace(root)
        if fake:
            from cerebro.fakes import FakeExtraction
            from cerebro.run import PydanticRun

            workspace = Cerebro(root, agent_run=PydanticRun(scripted()), extraction=FakeExtraction(), trusted=True)
        else:
            workspace = Cerebro(root, trusted=True)
        with workspace:
            admitted(workspace)
            check(workspace, reasons)
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
