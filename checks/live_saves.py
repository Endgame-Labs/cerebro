"""One live check of earlier saves on real models: `cerebro init --template gtm` in a temporary folder, one invented
discount policy under `local/knowledge/`, one pass, then the policy edited in place so its limit moves from 10% to
15%, and another pass. Not part of the test suite: it calls real models and costs some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_saves.py

A pass curates before it extracts, so the first
pass's knowledge curator sees no fact yet: the check admits the topic as a person would before the first pass. The workspace's clock is the real time, moved on a day by
hand before the second pass, so the save's change is past any cooldown. It prints each pass, the facts of every save,
the knowledge article, and what the workspace spent, then PASS, or FAIL with every reason. It never prints a key. It
passes only when:
- after the first pass a knowledge article states the 10% limit, citing a fact of the policy;
- after the edit, a facts query with `latest: false` gives the first save's facts marked superseded with their
  revision, and one without it gives the new save's alone;
- the text under the rewritten article's Current heading states 15%, and grounding marked each sentence of it that
  cites no current fact, or gives 10% without citing the first save beside the new one;
- somewhere in the article the old 10% limit is given citing a superseded fact of the first save beside a fact of the
  new one;
- every citation in the article names a fact the workspace holds.

It prints each sentence that gives the old limit beside the new one, and how many sentences grounding marked.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from _paths import ran_lines
from spend import spent_line

from cerebro import Caller, scaffold
from cerebro.app import Cerebro
from cerebro.article import MARK, Identity, passages
from cerebro.clock import stamp
from cerebro.evidence import CITE
from cerebro.ground import split_sentences, unmarked
from cerebro.runner import PassResult

DOC = "local/knowledge/discount-policy.md"
TOPIC = "knowledge/discount-policy"
SOURCE = """kind: files
window: { behind: all }
select:
  - glob: "local/knowledge/**/*.md"
    extract: facts
"""
FIRST = """Topic: Discount policy
Date: 2026-09-01

Reps may give a discount of up to 10% on an annual plan without approval.

A discount above 10% needs written approval from the sales lead before the quote goes out.

Discounts apply to annual plans only, never to monthly plans.
"""
SECOND = """Topic: Discount policy
Date: 2026-11-10

Reps may give a discount of up to 15% on an annual plan without approval.

A discount above 15% needs written approval from the sales lead before the quote goes out.

Discounts apply to annual plans only, never to monthly plans.
"""
OLD, NEW = re.compile(r"\b10\s?%|\b10 percent"), re.compile(r"\b15\s?%|\b15 percent")


def show(n: int, result: PassResult) -> None:
    print(f"pass {n}: {result.read} records read, {result.extracted} facts extracted, {result.linked} linked, "
          f"compiled {result.compiled}")
    for run in result.runs:
        print(f"  run {run.agent}: {run.status}{': ' + run.note if run.note else ''}, {spent_line(run)}")
    for line in ran_lines(result):
        print(line)
    for key, why in result.failed.items():
        print(f"  failed  {key}: {why}")
    for line in result.warnings:
        print(f"  warning {line}")


def units(text: str) -> list[str]:
    """Each sentence of the prose, and each table row whole, as grounding reads them."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ">", "```")) or set(line) <= set("|-: "):
            continue
        if line.startswith("|"):
            out.append(line)
            continue
        out += [s.text for s in split_sentences(re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line))]
    return out


def article(workspace: Cerebro) -> tuple[str, dict[str, str]]:
    """The knowledge article that cites the policy, as its id and parts, or ("", {})."""
    mine = {f.id for f in workspace.extraction.facts(record=DOC, latest=False)}
    for article in workspace.store.query("knowledge"):
        parts = {s.heading: s.text for s in passages(article)}
        if mine & set(CITE.findall("\n".join(parts.values()))):
            return article.identity.id, parts
    return "", {}


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    reasons: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, template="gtm", daily_usd=2, domains=["ourco.example"], environ=os.environ)
        (root / "sources" / "local.yaml").write_text(SOURCE, encoding="utf-8")
        (root / DOC).parent.mkdir(parents=True)
        (root / DOC).write_text(FIRST, encoding="utf-8")
        shift = [timedelta()]
        with Cerebro(root, trusted=True, clock=lambda: datetime.now(UTC) + shift[0]) as workspace:
            # The first pass curates before it extracts, so a person admits the topic, bound by the note's Topic line.
            workspace.store.create(Identity(TOPIC, "knowledge", "Discount policy", {"topic": ["Discount policy"]},
                                        admitted_by=Caller("you", "person"), admitted_at=stamp(workspace.clock())))
            show(1, workspace.run_once())
            first = workspace.extraction.facts(record=DOC)
            for fact in first:
                print(f"- {fact.id} {fact.text}")
            article_id, parts = article(workspace)
            print(f"\n# {article_id or 'no knowledge article cites the policy'}")
            for heading, text in parts.items():
                print(f"## {heading}\n{text}\n")
            if not article_id or not OLD.search("\n".join(parts.values())):
                reasons.append("after the first pass, no knowledge article states the 10% limit citing the policy")

            (root / DOC).write_text(SECOND, encoding="utf-8")
            shift[0] = timedelta(days=1)
            show(2, workspace.run_once())
            agent = Caller("live-saves", "person")
            every = workspace.lookup.query(agent, "facts", {"record": DOC, "latest": False}).items
            latest = workspace.lookup.query(agent, "facts", {"record": DOC}).items
            for fact in every:
                print(f"- {fact.id} [{fact.status}{', revision ' + fact.revision if fact.revision else ''}"
                      f"{', superseded ' + fact.superseded_at if fact.superseded_at else ''}] {fact.text}")
            old = {f.id: f for f in every if f.status == "superseded"}
            if not old or not all(f.revision.startswith("sha256:") and f.superseded_at for f in old.values()):
                reasons.append(f"latest: false gave no superseded fact of the first save marked with its revision: "
                               f"{[(f.id, f.status, f.revision) for f in every]}")
            if not any(OLD.search(f.text) for f in old.values()):
                reasons.append("no superseded fact holds the old 10% limit")
            if {f.id for f in latest} & set(old) or not any(NEW.search(f.text) for f in latest):
                reasons.append("a query without latest: false did not give the new save's facts alone")

            article_id, parts = article(workspace)
            print(f"\n# {article_id or 'no knowledge article cites the policy'}")
            for heading, text in parts.items():
                print(f"## {heading}\n{text}\n")
            current = {f.id for f in workspace.extraction.facts()}
            held = current | {f.id for f in workspace.extraction.facts(latest=False)}
            now_text = parts.get("Current", "")
            if not NEW.search(now_text):
                reasons.append("the text under the rewritten article's Current heading does not state the 15% limit")
            for unit in units(now_text):
                cited = set(CITE.findall(unmarked(unit)))
                beside = bool(cited & set(old)) and bool(cited & current)  # what was, beside the new
                if (OLD.search(unit) and not beside or not cited & current) and MARK not in unit:
                    reasons.append(f"Current gives the old limit, or rests on no current fact, unmarked: {unit!r}")
            history = [u for heading, text in parts.items() for u in units(text)
                       if set(CITE.findall(u)) & set(old) and set(CITE.findall(u)) & current
                       and (OLD.search(u) or any(OLD.search(old[i].text) for i in CITE.findall(u) if i in old))]
            if not history:
                reasons.append("the article does not give the old 10% limit citing the first save beside the "
                               "new one")
            for line in history:
                print(f"history: {line}")
            print(f"grounding marked {sum(text.count(MARK) for text in parts.values())} sentences")
            for id in CITE.findall(unmarked("\n".join(parts.values()))):  # a mark is grounding's, and cites nothing
                if id not in held:
                    reasons.append(f"[^{id}] is no fact the wiki holds")
            kept = workspace.store.superseded(record=DOC)
            size = sum(len(str(row["fact"])) for row in kept)
            print(f"store: {len(kept)} superseded facts of the policy, about {size // max(len(kept), 1)} bytes each")
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
    sys.exit(main())
