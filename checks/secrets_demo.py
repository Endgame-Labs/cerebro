"""`cerebro secrets` alone, on stand-ins: no model call, no network, and no keychain.

    uv run python checks/secrets_demo.py                  # the keys lobby in this terminal
    uv run python checks/secrets_demo.py --save <folder>  # save its frames, light, dark and plain

The lobby runs the real `cerebro secrets` on a workspace made in a new temp folder. Its config, trust and secrets
stay in that folder: the store is the file store there, never the keychain. A key's proof call is a stand-in that
calls no provider: a key that starts `bad` does not verify, as a provider's refusal would say, and any other does. The
temp folder is removed when the demo ends; it prints the folder's files first, never a value.

The frames under tests/data/tui/ are `--save`'s: the lobby, a provider opened, the lines after a key verified and after
one did not, and the table a plain terminal shows, on an 80-column terminal with made-up values.
"""

from __future__ import annotations

import argparse
import atexit
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from cerebro.models import DEFAULTS, ModelSpec

WIDTH = 80
SPECS = {**DEFAULTS, "compilation": ModelSpec("anthropic", "claude-sonnet-5-5", "", "compilation")}


def stand_in_proof(row: Any, value: str) -> None:
    """A key's proof call, standing in: it calls no provider. A key that starts `bad` is refused as a provider's
    401 would refuse it."""
    from cerebro.errors import CerebroError

    if value.startswith("bad"):
        raise CerebroError(f"{row.title} answered 401: the key was not accepted")


def opened(dark: bool | None) -> list[str]:
    """A provider's view as the lobby draws it, by the lobby's own code, on one 80-column terminal's lines."""
    from cerebro import guided, keys
    from cerebro.output import Printer

    drawn = io.StringIO()
    if dark is None:
        p = Printer(drawn, drawn, plain=True, width=lambda: WIDTH)
    else:
        p = Printer(drawn, drawn, terminal=lambda stream: True, dark=dark, width=lambda: WIDTH,
                    getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))
    keys.view(guided.TerminalScreen(p, drawn.write), "Anthropic", "Pasted contents are not shown and are stored in "
              "a file only your user can read.", drawn=True)
    return drawn.getvalue().split("\n")[:-1]


def frames(dark: bool | None) -> str:
    """The lobby's frames on made-up values, as an 80-column terminal draws them, or as plain text when `dark` is
    None, then a provider's view and the plain terminal's table."""
    from cerebro import keys
    from cerebro.checklist import Menu
    from cerebro.output import Mark, Printer, strip_escapes

    p = Printer(io.StringIO(), io.StringIO(), terminal=lambda stream: True, dark=bool(dark), width=lambda: WIDTH,
                getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))
    mark = (lambda m: m.symbol(False)) if dark is None else p.mark
    none = keys.key_rows(SPECS, [], {})
    one = keys.key_rows(SPECS, ["fireworks"], {"TYPESAFE_API_KEY": "set"})
    screens = [
        ("the lobby", keys.menu(none, keys.head(), "", mark)),
        ("a key verified", keys.menu(one, keys.head(),
                                     f"{mark(Mark.DONE)} Fireworks connection verified, key added to secrets store",
                                     mark)),
        ("a key that did not verify", keys.menu(one, keys.head(), f"{mark(Mark.FAILED)} Anthropic did not verify: "
                                                "Anthropic answered 401: the key was not accepted", mark)),
    ]
    out = []
    for title, menu in screens:
        drawn = Menu(menu).render(WIDTH, 30, styles=None if dark is None else p.st)
        lines = [strip_escapes(line) for line in drawn] if dark is None else list(drawn)
        out += [f"-- {title} --", "", *lines, ""]
    out += ["-- a provider opened, on a screen of its own --", *opened(dark), "Anthropic API key: ",
            "", "-- at a terminal that draws nothing --", "", *keys.table(none),
            "Set which? Names, with commas [Enter to skip]: ", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--save", type=Path)
    args = parser.parse_args(argv)
    if args.save is not None:
        args.save.mkdir(parents=True, exist_ok=True)
        for theme, dark in (("light", False), ("dark", True)):
            (args.save / f"secrets-{theme}.ansi").write_text(frames(dark), encoding="utf-8")
        (args.save / "secrets-plain.txt").write_text(frames(None), encoding="utf-8")
        return 0
    import click

    import cerebro.trust
    from cerebro import cli

    folder = Path(tempfile.mkdtemp(prefix="cerebro-secrets-demo-")).resolve()
    root = folder / "wiki"

    def show() -> None:
        print("\nThe demo wrote these, and removes them now:")
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            print(f"  {path.relative_to(folder)}")
        shutil.rmtree(folder, ignore_errors=True)

    atexit.register(show)
    os.environ["XDG_CONFIG_HOME"] = str(folder / "config")
    os.environ["CEREBRO_SECRETS_STORE"] = "file"
    for name in ("FIREWORKS_API_KEY", "TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(name, None)
    real_trusted = cerebro.trust.trusted
    cerebro.trust.trusted = lambda path, *a, **k: (Path(path).resolve().is_relative_to(folder)
                                                   or real_trusted(path, *a, **k))
    cli._prove_on = lambda where: stand_in_proof
    from cerebro import scaffold

    scaffold.init(root, timezone="UTC", environ={})
    try:
        cli.main(["secrets", "--workspace", str(root)], standalone_mode=False)
    except SystemExit as stop:
        return int(stop.code or 0)
    except click.ClickException as error:
        error.show()
        return error.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
