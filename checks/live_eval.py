"""One live run of a question eval on real models: the workspace `live_pass.py` builds, one real pass, then
`cerebro eval` on six questions about its two notes, one of them answered only by joining two facts. Not part of the
test suite: it calls real models and costs a few cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_eval.py

It prints the pass, the article, what `evals.lines` prints for the suite, and what the pass and the eval spent. It
passes when every question scores at least half its checks and the suite scores 0.75 or more.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from live_pass import AGENT, NOTES, show

from cerebro import evals, scaffold
from cerebro.app import Cerebro
from cerebro.article import passages

SUITE = """\
context: A launch the workspace's notes describe, for an eval of its article.
questions:
  - id: launch-date
    ask: When does the Halcyon launch happen now?
    read: projects/launch
    expect:
      article: projects/launch
      holds: ["8 December"]
      judge: The answer says the launch moved to 8 December.
  - id: why-it-moved
    ask: Why did the launch move?
    read: projects/launch
    expect:
      holds: ["payments review"]
      judge: The answer says the launch moved because the payments review was not finished.
  - id: review-owner
    ask: Who owns the payments review?
    find: Launch
    expect:
      article: projects/launch
      holds: [Tomas Weber]
  - id: blocker
    ask: What blocks sign-off?
    read: projects/launch
    expect:
      holds: [refund]
      judge: The answer says an open issue with refunds blocks sign-off of the payments review.
  - id: go-call-before-launch
    ask: How many days before the launch is the go or no-go call?
    read: projects/launch
    expect:
      judge: The answer gives the go or no-go call on 4 December and the launch on 8 December, four days apart.
  - id: no-budget-cut
    ask: Was the launch budget cut?
    read: projects/launch
    expect:
      lacks: ["budget was cut"]
      judge: The answer says the budget stays at $40,000.
"""


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, daily_usd=1, environ=os.environ)
        for rel, text in NOTES.items():
            (root / rel).write_text(text, encoding="utf-8")
        (root / "agents" / "projects").mkdir(parents=True)
        (root / "agents" / "projects" / "launch.md").write_text(AGENT, encoding="utf-8")
        suite_path = Path(folder) / "launch.yaml"
        suite_path.write_text(SUITE, encoding="utf-8")
        with Cerebro(root, trusted=True) as workspace:
            show(1, workspace.run_once())
            article = workspace.store.article("projects/launch")
            for compilation in (passages(article) if article else []):
                print(f"## {compilation.heading}\n{compilation.text}\n")
            report = evals.run(workspace, evals.load(suite_path), decider=workspace.decider, model=workspace.models["decision"],
                               ledger=workspace.ledger, cap_usd=workspace.ledger.caps.evaluation, clock=workspace.clock)
            for line in evals.lines(report):
                print(line)
            now = workspace.clock()
            print(f"spent: pass ${workspace.ledger.spent(now) - workspace.ledger.spent(now, 'evaluation'):.4f}, "
                  f"eval ${workspace.ledger.spent(now, 'evaluation'):.4f}")
    weak = [s.id for s in report.scored if s.score is not None and s.score < 0.5]
    if weak or report.score is None or report.score < 0.75:
        print(f"FAIL: score {report.score}, under half on {', '.join(weak) or 'none'}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
