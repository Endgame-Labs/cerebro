"""A pass's live progress, on stand-ins: no model call, no network and no key.

    uv run python checks/progress_demo.py                  # a stand-in pass, drawn live in this terminal
    uv run python checks/progress_demo.py --plain          # the same pass as the plain lines a pipe gets
    uv run python checks/progress_demo.py --save <folder>  # save the region's frames, light and dark

The live pass makes a workspace in a temp folder, and keeps its config and secrets there too, with five notes, two writers on fixed articles and one writer that
fails. A stand-in facts layer and a stand-in model run it slowly enough to watch, through the same `serve --once`
a person runs. The frames under tests/data/tui/ are `--save`'s: a scripted pass, drawn on an 80-column terminal.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cerebro.output import Printer
from cerebro.progress import LiveProgress
from cerebro.runner import Progress

WIDTH = 80
FOLDERS = ("local",)

# A scripted pass: each event with the seconds that pass after it. The names are made up.
SCRIPT: list[tuple[Progress, float]] = [
    (Progress("check", "start", "notes"), 0.4),
    (Progress("check", "end", "notes", mark="done"), 0.0),
    (Progress("check", "start", "mail"), 0.3),
    (Progress("check", "end", "mail", mark="failed", why="ConnectError: the server refused the connection"), 0.0),
    (Progress("agents", "open", total=1), 0.0),
    (Progress("agents", "start", "things/curator"), 2.1),
    (Progress("agents", "end", "things/curator", mark="done", text="admitted 2", cost="$0.0124"), 0.0),
    (Progress("distill", "open", total=1), 0.0),
    (Progress("distill", "start", "notes"), 3.2),
    (Progress("distill", "end", "notes", mark="done", text="5 records • 14 facts"), 0.0),
    (Progress("compile", "open", total=4), 0.0),
    (Progress("compile", "start", "things/alpha"), 0.2),
    (Progress("compile", "start", "things/beta"), 0.3),
    (Progress("compile", "start", "parts/gamma"), 0.1),
    (Progress("compile", "start", "parts/delta"), 1.9),
    (Progress("compile", "end", "things/alpha", mark="done", text="articles/things/alpha.md"), 0.6),
]
REST: list[tuple[Progress, float]] = [
    (Progress("compile", "end", "parts/gamma", mark="failed", why="parts/writer: TimeoutError: no answer in 120s"), 0.4),
    (Progress("compile", "end", "parts/delta", mark="skipped", text="nothing it reads has moved"), 0.8),
    (Progress("compile", "end", "things/beta", mark="done", text="articles/things/beta.md"), 0.0),
]


def frames(dark: bool) -> str:
    """The region in the middle of the scripted pass and once it ended, as an 80-column terminal draws it."""
    clock = [0.0]
    printer = Printer(io.StringIO(), io.StringIO(), terminal=lambda stream: True, dark=dark, width=lambda: WIDTH,
                      getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))
    screen = LiveProgress(printer, now=lambda: clock[0], ticking=False, height=lambda: 40)
    for event, seconds in SCRIPT:
        screen(event)
        clock[0] += seconds
    middle = screen.frame()
    for event, seconds in REST:
        screen(event)
        clock[0] += seconds
    end = screen._frame(clock[0], final=True)
    return "\n".join(middle) + "\n\n-- the pass ends --\n\n" + "\n".join(end) + "\n"


def stand_in_pass(plain: bool, pause: float = 1.0) -> int:
    """Run `serve --once` in this terminal on a stand-in workspace, slowly enough to watch: each stand-in waits
    `pause` times its usual while, so 0 runs it at once, as a test does."""
    from click.testing import CliRunner

    import cerebro.trust
    from cerebro import cli
    from cerebro.agent import Agent, Budget
    from cerebro.app import Cerebro
    from cerebro.evidence import Evidence
    from cerebro.fakes import FakeExtraction, ScriptedRun
    from cerebro.run import AgentRun, Run

    class Slow(AgentRun):
        """A stand-in model that writes each heading its agent names, after a while, and fails on one agent."""

        def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
            time.sleep(pause * {"parts/writer": 2.5, "things/notes": 1.5}.get(agent.id, 1.0))
            if agent.id == "parts/writer":
                raise TimeoutError("the stand-in model gave no answer")
            def cites(prompt: str) -> dict[str, str]:  # each heading cites every fact it was given, word for word
                facts = re.findall(r"\[\^(fact_\w+)\] ([^(\n]+)", prompt)
                said = " ".join(f"{text.strip()} [^{fid}]" for fid, text in facts) or "Nothing yet."
                return {heading: said for heading in agent.output or ["Summary"]}

            return ScriptedRun(cites).run(agent, evidence, tools, budget)

    class SlowFacts(FakeExtraction):
        def extract(self, record: Any) -> Any:
            time.sleep(pause * 0.6)
            return super().extract(record)

    folder = Path(tempfile.mkdtemp(prefix="cerebro-progress-demo-"))
    root = folder / "demo"
    # nothing outside the temp folder: its config, trust and secrets go under it, and no keychain is touched
    os.environ["XDG_CONFIG_HOME"] = str(folder / "config")
    os.environ["CEREBRO_SECRETS_STORE"] = "file"
    real_trusted = cerebro.trust.trusted
    cerebro.trust.trusted = lambda path, *args, **kwargs: (Path(path).resolve().is_relative_to(folder.resolve())
                                                           or real_trusted(path, *args, **kwargs))
    made = CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1", "--timezone", "UTC"])
    if made.exit_code != 0:
        print(made.output, file=sys.stderr)
        return 1
    for n, line in enumerate(["Alpha moves to March.", "Beta ships in May.", "Gamma waits on Delta.",
                              "Delta is unchanged.", "Alpha and Beta share a review."]):
        (root / "local" / f"note-{n}.md").write_text(f"Date: 2026-11-1{n}\n\n{line}\n", encoding="utf-8")
    agent = "---\nevidence:\n  - query: {{ from: facts, source: local }}\nheartbeat: [change]\n---\n\n{}\n\n# Output\n\n## Summary\n"
    for name, about in (("things/notes", "Summarize the notes."), ("parts/writer", "Write the parts."),
                        ("things/review", "Write the review.")):
        (root / "agents" / name).parent.mkdir(parents=True, exist_ok=True)
        (root / "agents" / f"{name}.md").write_text(agent.format(about), encoding="utf-8")
    facts = SlowFacts()
    cli.open_workspace = lambda path: Cerebro(path, agent_run=Slow(), extraction=facts,
                                          clock=lambda: datetime.now(UTC))
    args = ["serve", "--once", "--workspace", str(root), *(["--plain"] if plain else [])]
    try:
        cli.main(args, standalone_mode=False)
    except SystemExit as stop:
        return int(stop.code or 0)
    return 0


def main(argv: list[str] | None = None, out: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--save", type=Path)
    args = parser.parse_args(argv)
    if args.save is not None:
        args.save.mkdir(parents=True, exist_ok=True)
        for theme, dark in (("light", False), ("dark", True)):
            (args.save / f"progress-{theme}.ansi").write_text(frames(dark), encoding="utf-8")
        return 0
    return stand_in_pass(args.plain)


if __name__ == "__main__":
    sys.exit(main())
