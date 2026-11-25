"""The guided init, on stand-ins: no model call, no network, no key and no real client's config.

    uv run python checks/init_demo.py                  # walk the guided init in this terminal
    uv run python checks/init_demo.py --save <folder>  # save each step's frames, light, dark and plain

The walk runs the real `cerebro init` in a new temp folder. Its config, trust and secrets stay in that folder too,
and so does the home folder the MCP clients' config files are written under: Claude Desktop's, Cursor's and Codex's
files are written there, and Claude Code's `claude mcp add` is a stand-in that writes the scratch `.claude.json`.
A key's proof is a stand-in too: a key that starts `bad` does not verify, and any other does. The sources step is connect's lobby on the connect demo's stand-in GitHub and Slack, so any token typed is kept in the
temp folder and checked against nothing real. The first run is a stand-in that says what would run, since a real one
calls the models. The temp folder is removed when the demo ends; it prints the folder's files first.

The frames under tests/data/tui/ are `--save`'s: the real `cerebro init`, run on a new workspace in a temp folder on
the same stand-ins and answered by a script, each menu it opened and each line it asked, in order, on a 100-column
terminal. A question added to the walk or taken from it changes the frames, and the test that holds them fails.
"""

from __future__ import annotations

import argparse
import atexit
import io
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import click
import httpx

from cerebro.models import DEFAULTS, ModelSpec
from cerebro.output import Printer

sys.path.insert(0, str(Path(__file__).parent))
import connect_demo

WIDTH = 100  # the width the frames are drawn at
SPECS = {**DEFAULTS, "compilation": ModelSpec("anthropic", "claude-sonnet-5-5", "", "compilation")}


def row(title: str):
    """An answer to a menu: the place of its row titled `title`, so a script does not hang on a row's place."""
    def place(menu) -> int:
        titles = [r.title for r in menu.rows]
        if title not in titles:
            raise AssertionError(f"{menu.head[0]} has no row {title!r}: {titles}")
        return titles.index(title)

    return place


# What the frames' walk answers, in the order the walk asks: a menu's row, a line's text ("" takes its default), or
# None for Escape or Done. The walk asks each, and so decides each frame.
ANSWERS = [
    row("blank"),                                                      # a new workspace
    "Rowan Vale", "rowan@quillmoor.example", "", "",                   # you, the company and its domain by default
    "5", "",                                                           # the budget, and the time zone by default
    row("Compilation"), row("anthropic"), row("claude-sonnet-5-5"), None,  # models: compilation on Anthropic
    row("Fireworks"), "ok-6666-7777-8888",                        # keys: Fireworks proven and kept,
    row("Anthropic"), "bad-0000-1111-2222",                             # Anthropic's first key refused,
    row("Anthropic"), "ok-3333-4444-5555", None,                  # its second kept; Typesafe's is in the env
    row("Claude Desktop"), row("Install for me"),                      # clients: Claude Desktop written,
    row("Codex"), None, None,                                          # Codex, held for another folder, left
]                                                                      # the first run starts with no question


class Recording:
    """The walk's screen, standing in for the terminal: it answers from `answers` and keeps every menu it was shown
    and every line it was asked, as the terminal shows it."""

    def __init__(self, answers: list) -> None:
        self.answers, self.shown = list(answers), []

    def _next(self, menu=None):
        if not self.answers:
            raise AssertionError(f"the walk asked more than the frames' script answers: {menu.head[0] if menu else ''}")
        answer = self.answers.pop(0)
        return answer(menu) if callable(answer) else answer

    def menu(self, menu, cursor: int = 0):
        self.shown.append(("menu", menu, cursor))
        return self._next(menu)

    def text(self, prompt: str, default: str = "", hidden: bool = False, source: str = ""):
        from cerebro import guided

        self.shown.append(("line", guided.prompt_line(prompt, default, hidden, source) + ": "))
        answer = self._next()
        return default if answer == "" else answer

    def read(self, lines) -> None:
        self.shown.append(("steps", list(lines)))

    def say(self, line: str) -> None:
        pass


def walked(printer: Printer, folder: Path) -> list:
    """The real `cerebro init` on a new workspace in `folder`, at a terminal that draws with `printer`, answered by
    ANSWERS on the stand-ins: what it showed and asked, in order."""
    from click.testing import CliRunner

    from cerebro import cli, clock, guided

    home = folder / "home"
    (home / ".codex").mkdir(parents=True)  # Codex holds this workspace's name for another folder
    (home / ".codex" / "config.toml").write_text('[mcp_servers.cerebro-wiki]\ncommand = "cerebro"\n'
                                                 'args = ["mcp", "--workspace", "/srv/old/wiki"]\n')
    screen = Recording(ANSWERS)

    def lobby(*args, **kwargs) -> None:  # connect's own lobby, framed by checks/connect_demo.py: a note is added
        screen.shown.append(("lobby", None))
        (folder / "wiki" / "local").mkdir(exist_ok=True)
        (folder / "wiki" / "local" / "kickoff.md").write_text("The launch review moved to March 3.\n")

    @click.command()
    def first_pass(**kwargs: object) -> None:  # serve --once, standing in: it calls no model
        screen.shown.append(("first run", None))

    kept = {name: getattr(cli, name) for name in ("_drawing", "_client_home", "_client_environ", "_claude",
                                                   "_prove_on", "_lobby", "serve")}
    kept_screen, kept_zone = guided.TerminalScreen, clock.local_zone
    cli._drawing = lambda plain: printer
    cli._client_home = lambda: home
    cli._client_environ = lambda: {"TYPESAFE_API_KEY": "stand-in"}  # a key in the environment, never read for real
    cli._claude = lambda: stand_in_claude(home)
    cli._prove_on = lambda where: stand_in_proof  # no key is ever sent to a provider
    cli._lobby = lobby
    cli.serve = first_pass
    guided.TerminalScreen = lambda printer, echo: screen
    clock.local_zone = lambda *a, **k: "Europe/Berlin"
    try:
        got = CliRunner().invoke(cli.main, ["init", str(folder / "wiki")],
                                 env={"XDG_CONFIG_HOME": str(folder / "config"), "CEREBRO_SECRETS_STORE": "file"})
    finally:
        for name, value in kept.items():
            setattr(cli, name, value)
        guided.TerminalScreen, clock.local_zone = kept_screen, kept_zone
    if got.exit_code != 0 or screen.answers:
        raise AssertionError(f"the walk did not take the frames' script whole: exit {got.exit_code}, "
                             f"{len(screen.answers)} answers left: {got.output[-600:]}{got.exception!r}")
    return screen.shown


def frames(dark: bool | None) -> str:
    """The real walk's every screen and line, as a 100-column terminal draws them, or as plain text when `dark` is
    None: each menu it opened, each set of steps it showed and each line it asked, in the order it did."""
    from cerebro.checklist import Menu, hard_wrap
    from cerebro.output import strip_escapes

    if dark is None:
        p = Printer(io.StringIO(), io.StringIO(), plain=True, width=lambda: WIDTH)
    else:
        p = Printer(io.StringIO(), io.StringIO(), terminal=lambda stream: True, dark=dark, width=lambda: WIDTH,
                    getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))
    folder = Path(tempfile.mkdtemp(prefix="cerebro-init-frames-")).resolve()
    try:
        shown = walked(p, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    out: list[str] = []
    for kind, what, *rest in shown:
        if kind == "menu":
            drawn = Menu(what, rest[0]).render(WIDTH, 30, styles=None if dark is None else p.st)
            out += [f"-- {strip_escapes(what.head[0])} --", "", *(strip_escapes(line) if dark is None else line
                                                                  for line in drawn), ""]
        elif kind == "steps":
            out += ["-- the steps it shows --", "", *what, ""]
        elif kind == "lobby":
            out += ["-- the sources: connect's own lobby, framed by checks/connect_demo.py --", ""]
        elif kind == "first run":  # discovery starts with no question; the choice after it is the run's own screen
            out += ["-- the first run: cerebro serve --once starts, with no question --", ""]
        elif len(out) > 1 and out[-2].endswith(": "):  # a line asked straight after another joins its group
            out[-1:] = [*hard_wrap(what, WIDTH - 1), ""]
        else:  # a line longer than the terminal wraps, as the terminal wraps it
            out += ["-- asked --", *hard_wrap(what, WIDTH - 1), ""]
    return "\n".join(line.replace(str(folder), "<folder>") for line in out) + "\n"


def stand_in_proof(row, value: str) -> None:
    """A key's proof call, standing in: it calls no provider. A key that starts `bad` is refused, as a provider's 401
    would refuse it, and any other verifies."""
    from cerebro.errors import CerebroError

    if value.startswith("bad"):
        raise CerebroError(f"{row.title} answered 401: the key was not accepted")


def stand_in_claude(home: Path):
    """`claude mcp add`, standing in: it writes the entry into the scratch home's .claude.json and runs nothing."""

    def run(argv: Sequence[str]) -> tuple[int, str]:
        path = home / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data.setdefault("mcpServers", {})[argv[5]] = {"type": "stdio", "command": argv[7], "args": list(argv[8:])}
        path.write_text(json.dumps(data, indent=2) + "\n")
        return 0, ""

    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--save", type=Path)
    args = parser.parse_args(argv)
    if args.save is not None:
        args.save.mkdir(parents=True, exist_ok=True)
        for theme, dark in (("light", False), ("dark", True)):
            (args.save / f"init-{theme}.ansi").write_text(frames(dark), encoding="utf-8")
        (args.save / "init-plain.txt").write_text(frames(None), encoding="utf-8")
        return 0
    import cerebro.trust
    from cerebro import cli
    from cerebro.app import Cerebro

    folder = Path(tempfile.mkdtemp(prefix="cerebro-init-demo-")).resolve()
    home = folder / "home"
    root = folder / "wiki"

    def show() -> None:
        print("\nThe demo wrote these, and removes them now:")
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):  # the config folder's files among them
            print(f"  {path.relative_to(folder)}")
        shutil.rmtree(folder, ignore_errors=True)

    atexit.register(show)
    os.environ["XDG_CONFIG_HOME"] = str(folder / "config")
    os.environ["CEREBRO_SECRETS_STORE"] = "file"
    real_trusted = cerebro.trust.trusted
    cerebro.trust.trusted = lambda path, *a, **k: (Path(path).resolve().is_relative_to(folder)
                                                   or real_trusted(path, *a, **k))
    client = httpx.Client(transport=httpx.MockTransport(connect_demo.stand_in))

    def open_workspace(path: Path | None) -> Cerebro:
        workspace = Cerebro(path, http=client)
        workspace.asker = connect_demo.StandInModel()
        return workspace

    cli.open_workspace = open_workspace
    cli._client_home = lambda: home
    cli._client_environ = dict  # the clients' own variables, as CLAUDE_CONFIG_DIR, are never read from here
    cli._claude = lambda: stand_in_claude(home)
    cli._prove_on = lambda where: stand_in_proof  # no key is ever sent to a provider

    @click.command()
    def first_pass(**kwargs: object) -> None:
        click.echo("first run would run here: cerebro serve --once reads the sources and shows the articles it "
                   "proposes. The demo calls no model, so it stops.")

    cli.serve = first_pass
    try:
        cli.main(["init", str(root)], standalone_mode=False)
    except SystemExit as stop:
        return int(stop.code or 0)
    except click.ClickException as error:
        error.show()
        return error.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
