"""Every named style and piece of the command line's look, in a light and a dark terminal, and the checklist on a
made-up list.

    uv run python checks/style_demo.py                  # print both themes here, in this terminal's colors
    uv run python checks/style_demo.py --plain          # the same pieces as plain text
    uv run python checks/style_demo.py --save <folder>  # save each frame as text and as an SVG screenshot

No workspace, no model and no network: every name in it is made up. The frames under tests/data/tui/ are this
script's, saved with --save.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from cerebro.checklist import DARK, LIGHT, Checklist, ChecklistApp, Group, MenuList, MenuRow, Model, Row, write_plain
from cerebro.checklist import Menu as MenuModel
from cerebro.output import PALETTE, SPINNER, STYLE_NAMES, Mark, Printer, Styles, strip_escapes

WIDTH = 80

# A light and a dark terminal's own colors, for the SVG frames: the backgrounds the palette is checked on.
LIGHT_TERMINAL = TerminalTheme(
    (255, 255, 255), (30, 30, 30),
    [(0, 0, 0), (181, 69, 93), (4, 120, 87), (134, 71, 32), (0, 75, 162), (128, 0, 128), (0, 128, 128), (244, 246, 249)],
    [(88, 114, 146), (213, 89, 115), (52, 211, 153), (221, 140, 94), (57, 154, 255), (255, 0, 255), (0, 255, 255),
     (255, 255, 255)],
)
DARK_TERMINAL = TerminalTheme(
    (30, 30, 30), (230, 230, 230),
    [(0, 0, 0), (181, 69, 93), (4, 120, 87), (134, 71, 32), (0, 75, 162), (128, 0, 128), (0, 128, 128), (192, 192, 192)],
    [(88, 114, 146), (213, 89, 115), (52, 211, 153), (221, 140, 94), (57, 154, 255), (255, 0, 255), (0, 255, 255),
     (255, 255, 255)],
)


class _Terminal(io.StringIO):
    """A stream that says it is a terminal, so the pieces draw styled into it."""

    def isatty(self) -> bool:
        return True


def made_up_list() -> Checklist:
    """A list of made-up rows under two regions, one group long enough to start closed, and one more group."""
    rows = [
        Row("Bramblecote Ltd", detail="Named in 2 notes, replies both ways in 6 threads, last 2026-10-08."),
        Row("Quillfeather & Sons", detail="Named in 1 note, last 2026-10-06."),
        Row("Larkspur Mills", checked=False, detail="Named in 1 note on 2026-09-18, nothing since."),
    ]
    rows += [Row(f"Harbor Row {n}", detail=f"In the files source, {n + 2} notes.") for n in range(1, 13)]
    rows += [
        Row("Juniper Ward", detail="Spoke in 3 calls, last 2026-10-02."),
        Row("Fenwick Ash", detail="On 2 meetings, last 2026-09-29."),
        Row("Otterly Goods", checked=False),
    ]
    return Checklist(
        rows=rows,
        groups=[
            Group("named in your notes  (2 notes)", [0, 1, 2], heading="Sample collection"),
            Group("from the files source  (local)", list(range(3, 15)), heading="Sample collection"),
            Group("spoke in your calls  (calls)", [15, 16], heading="Second collection"),
            Group("Left out: vendor (1)", [17]),
        ],
        head=["Sample list", "by a made-up helper"],
        counts=lambda checked: f" · about ${0.45 * sum(checked):.2f} to write the checked articles",
        after=["Left out 1: one vendor"],
    )


def made_up_menu() -> MenuList:
    return MenuList(
        head=["Sample menu", "Open a collection to look at its rows."],
        rows=[
            MenuRow("Sample collection", state="15 awaiting review"),
            MenuRow("Second collection", state="2 awaiting review", reason="from the calls source"),
            MenuRow("Empty collection", state="nothing new", inert=True),
        ],
        end="Done",
    )


def pieces(dark: bool, plain: bool = False, profile: str = "truecolor") -> str:
    """Every named style and every piece, drawn as a terminal of the given background and colors shows them."""
    stream = io.StringIO() if plain else _Terminal()
    clock = {"now": 0.0}
    env = {"COLORTERM": "truecolor"} if profile == "truecolor" else {"TERM": "xterm-256color"}
    p = Printer(stream, stream, plain=plain, getenv=env.get, dark=dark, width=lambda: WIDTH,
                now=lambda: clock["now"], tick=3600)
    st = p.st if p.styled_out else Styles(None)
    p.header("cerebro sample", ".../sample-wiki")
    p.line("The named styles:")
    for name in STYLE_NAMES:
        p.line(f"  {name:<15}" + st.paint(name, f"{name} text"))
    p.heading("Marks")
    for mark in Mark:
        p.item(1, mark, f"{mark.name.lower()} mark")
    p.line("  " + p.running_mark() + " the running mark a line printed once shows")
    p.line("  " + " ".join(st.paint("key", frame) for frame in SPINNER) + "  the spinner's frames, a tenth of a second each")
    p.heading("A table")
    p.table([["workspace", ".../sample-wiki"], ["articles", "12 articles, 3 awaiting review"],
             ["next", "run cerebro status --workspace .../sample-wiki to see them"]])
    p.heading("A tinted block")
    p.print_tinted("cerebro connect sample --workspace .../sample-wiki\nkind: files")
    p.heading("A link")
    p.line("  " + p.link("articles/sample/bramblecote-ltd.md", "/tmp/sample-wiki/articles/sample/bramblecote-ltd.md"))
    p.heading("A step")
    step = p.step("Reading sources")
    step.lines("local: 12 records", "calls: 3 records")
    clock["now"] = 12.4
    step.done("Read 15 records from 2 sources")
    p.warn("one source is slow: run cerebro status --workspace .../sample-wiki")
    p.err_line("cerebro: warning: a made-up warning line")
    p.line("")
    p.line(p.last_line(failed=True, run="cerebro trust .../sample-wiki"))
    p.line(p.last_line())
    p._state.checks.clear()
    p.next("cerebro serve --workspace .../sample-wiki")
    p.line(p.last_line())
    return stream.getvalue()


def checklist_frames(dark: bool, keys: tuple[str, ...] = ("down", "down", "space")) -> tuple[str, str]:
    """The checklist on the made-up list after keys, as a text frame with no colors and as an SVG screenshot."""
    styles = Styles("truecolor", dark)
    app = ChecklistApp(made_up_list(), styles, saved_theme=DARK if dark else LIGHT)

    async def drive() -> tuple[str, str]:
        async with app.run_test(size=(WIDTH, 30)) as pilot:
            await pilot.press(*keys)
            await pilot.pause()
            frame = "\n".join(strip_escapes(line) for line in app.view.frame())
            return frame + "\n", app.export_screenshot(title="Sample list")

    return asyncio.run(drive())


def screen(drawn: str) -> str:
    """What a terminal shows once drawn is written to it: a live line erased (a carriage return, then erase to the
    end of the line, the cursor moved up a row for each row more) is gone, as on a terminal."""
    rows, row = [""], 0
    for part in re.split(r"(\n|\x1b\[K|\x1b\[A)", drawn.replace("\r", "")):
        if part == "\n":
            row += 1
            if row == len(rows):
                rows.append("")
        elif part == "\x1b[K":  # always right after a carriage return here, so the whole row goes
            rows[row] = ""
        elif part == "\x1b[A":
            row = max(row - 1, 0)
        elif part:
            rows[row] += part
    return "\n".join(rows[: row + 1]) + ("\n" if drawn.endswith("\n") else "")


def svg(text: str, dark: bool, title: str) -> str:
    console = Console(width=WIDTH, record=True, file=io.StringIO(), force_terminal=True, color_system="truecolor")
    console.print(Text.from_ansi(text), soft_wrap=True)
    return console.export_svg(title=title, theme=DARK_TERMINAL if dark else LIGHT_TERMINAL)


def save(folder: Path) -> list[Path]:
    """Save every frame under folder: the pieces and the checklist in each theme, as text and as SVG, and the plain
    pieces and plain list as text."""
    folder.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for theme, dark in (("light", False), ("dark", True)):
        drawn = screen(pieces(dark))
        written[f"pieces-{theme}.txt"] = strip_escapes(drawn)
        written[f"pieces-{theme}.svg"] = svg(drawn, dark, f"The pieces, {theme} terminal")
        frame, shot = checklist_frames(dark)
        written[f"checklist-{theme}.txt"] = frame
        written[f"checklist-{theme}.svg"] = shot
    written["pieces-plain.txt"] = pieces(True, plain=True)
    written["checklist-plain.txt"] = write_plain(made_up_list())
    paths = []
    for name, body in written.items():
        path = folder / name
        path.write_text(body, encoding="utf-8")
        paths.append(path)
    return paths


def main(argv: list[str] | None = None, out: io.TextIOBase | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show every style and piece of the command line's look.")
    parser.add_argument("--plain", action="store_true", help="print the pieces and the list as plain text")
    parser.add_argument("--save", type=Path, help="save each frame under this folder")
    args = parser.parse_args(argv)
    out = out if out is not None else sys.stdout
    if args.save:
        for path in save(args.save):
            print(f"saved     {path}", file=out)
        return 0
    if args.plain:
        out.write(pieces(True, plain=True))
        out.write("\n" + write_plain(made_up_list()))
        return 0
    for theme, dark in (("light", False), ("dark", True)):
        out.write(f"\n== {theme} terminal ==\n\n")
        out.write(screen(pieces(dark)))
        model = Model(made_up_list())
        out.write("\n" + "\n".join(model.render(WIDTH, 40, True, Styles("truecolor", dark))) + "\n")
        menu = MenuModel(made_up_menu())
        out.write("\n" + "\n".join(menu.render(WIDTH, 20, True, Styles("truecolor", dark))) + "\n")
    out.write("\npalette   " + ", ".join(f"{role} {r.light.hex}/{r.dark.hex}" for role, r in PALETTE.items()) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
