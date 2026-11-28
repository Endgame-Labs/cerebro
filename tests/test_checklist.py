"""The checkbox list, the menu and the pager: their keys and frames, scripted through Textual's pilot with golden
frames, the plain list with no escape byte, and the list inline on a pseudo-terminal."""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

import pytest
from test_output import on_a_pty

from cerebro.checklist import (
    MENU_DONE,
    Checklist,
    ChecklistApp,
    Group,
    Key,
    Menu,
    MenuApp,
    MenuList,
    MenuRow,
    Model,
    NoRawKeys,
    Pager,
    PagerApp,
    Row,
    ask,
    count_of,
    write_plain,
)
from cerebro.output import Printer, Styles, strip_escapes

DATA = Path(__file__).parent / "data" / "tui"
SOURCES = Path(__file__).resolve().parents[1] / "src" / "cerebro"


def sample(long_group: int = 12) -> Checklist:
    rows = [Row("Bramblecote Ltd", detail="Named in 2 notes, last 2026-10-08."),
            Row("Quillfeather & Sons", detail="Named in 1 note, last 2026-10-06."),
            Row("Larkspur Mills", checked=False, detail="Named once on 2026-09-18, nothing since.")]
    rows += [Row(f"Harbor Row {n}") for n in range(1, long_group + 1)]
    rows += [Row("Otterly Goods", checked=False)]
    return Checklist(
        rows=rows,
        groups=[Group("named in your notes  (2 notes)", [0, 1, 2], heading="Sample collection"),
                Group("from the files source  (local)", list(range(3, 3 + long_group)), heading="Sample collection"),
                Group("Left out: vendor (1)", [3 + long_group])],
        head=["Sample list", "by a made-up helper"],
        counts=lambda checked: f" · about ${sum(checked)}.00 to write the checked rows",
        after=["Left out 1: one vendor"],
    )


def frame(model: Model, width: int = 80, height: int = 40) -> list[str]:
    return model.render(width, height)


def line_of(model: Model, label: str) -> str:
    return next(line for line in frame(model) if label in line)


def press(model: Model, *keys: Key) -> None:
    for key in keys:
        model.press(key)


# ---- the model -----------------------------------------------------------------------------------------------------


def test_every_row_starts_as_given_and_a_header_box_says_all_none_or_some() -> None:
    model = Model(sample())
    assert model.checked[:3] == [True, True, False]
    assert line_of(model, "Sample collection").startswith("> [-] Sample collection  14 of 15")
    assert "[-] ▾ named in your notes  (2 notes)" in line_of(model, "named in your notes")
    assert "[x] ▸ from the files source  (local)  12 of 12" in line_of(model, "files source")
    assert "[ ] ▾ Left out: vendor (1)" in line_of(model, "Left out")


def test_space_on_a_header_toggles_every_row_under_it_and_on_a_row_just_that_row() -> None:
    model = Model(sample())
    press(model, Key.DOWN, Key.TOGGLE)  # the group's header: some checked, so all become checked
    assert model.checked[:3] == [True, True, True]
    press(model, Key.TOGGLE)
    assert model.checked[:3] == [False, False, False]
    assert "[ ] ▾ named in your notes" in line_of(model, "named in your notes")
    press(model, Key.DOWN, Key.TOGGLE)
    assert model.checked[:3] == [True, False, False]
    press(model, Key.UP, Key.UP, Key.TOGGLE)  # the region: every row of its groups
    assert all(model.checked[:15]) and not model.checked[15]


def test_a_group_of_more_than_ten_rows_starts_closed_and_opens_and_closes_with_the_arrows_and_l_and_h() -> None:
    model = Model(sample())
    assert "Harbor Row 1" not in "\n".join(frame(model))
    press(model, Key.DOWN, Key.DOWN, Key.DOWN, Key.DOWN, Key.DOWN)  # past the three rows to the closed header
    assert model.at().text.startswith("from the files source")
    press(model, Key.OPEN)
    assert "Harbor Row 12" in "\n".join(frame(model)) and "▾ from the files source" in "\n".join(frame(model))
    press(model, Key.DOWN, Key.DOWN, Key.CLOSE)  # left on a row closes its group and puts the cursor on its header
    assert "Harbor Row 1" not in "\n".join(frame(model)) and model.at().text.startswith("from the files source")
    ten = Model(sample(long_group=10))
    assert "Harbor Row 10" in "\n".join(frame(ten))


def test_a_checks_all_or_none() -> None:
    model = Model(sample())
    press(model, Key.ALL)
    assert model.checked == [True] * 16
    press(model, Key.ALL)
    assert model.checked == [False] * 16


def test_up_and_down_stop_at_either_end() -> None:
    model = Model(sample())
    press(model, Key.UP)
    assert model.cursor == 0
    press(model, *[Key.DOWN] * 50)
    assert model.at().kind == 1 and model.list.rows[model.at().at].label == "Otterly Goods"


def test_enter_returns_what_is_checked_and_escape_returns_nothing() -> None:
    model = Model(sample())
    assert model.press(Key.ENTER) == (True, True)
    assert model.press(Key.ESC) == (True, False)


def test_the_footer_counts_the_selection_shows_the_estimate_it_is_given_and_the_key_hints() -> None:
    model = Model(sample())
    assert frame(model)[-5:] == [
        "",
        "14 of 16 selected · about $14.00 to write the checked rows",
        "  Left out 1: one vendor",
        "↑↓ move   space toggle   →← open or close   a all or none",
        "⏎ Approve selected   esc Cancel and quit",
    ]
    press(model, *[Key.DOWN] * 50, Key.TOGGLE)
    press(model, Key.UP, Key.UP, Key.UP, Key.TOGGLE)  # past the two headers to Larkspur Mills
    assert "16 of 16 selected" in "\n".join(frame(model))
    assert "⏎ Approve all   esc Cancel and quit" in frame(model)


def test_a_list_taller_than_the_terminal_scrolls_with_a_line_counting_the_rows_out_of_view() -> None:
    model = Model(sample())
    press(model, Key.DOWN, Key.DOWN, Key.DOWN, Key.DOWN, Key.DOWN, Key.OPEN)
    short = model.render(80, 16)
    assert len(short) <= 16
    assert any(re.fullmatch(r"  … \d+ more", line) for line in short)
    press(model, *[Key.DOWN] * 8)
    scrolled = model.render(80, 16)
    assert scrolled[3].startswith("  … ") and any(line.startswith(">") for line in scrolled)


def test_a_row_that_hangs_off_another_follows_it_through_toggled() -> None:
    def follow(rows: list[int], on: bool, checked: list[bool]) -> list[bool]:
        if 0 in rows:
            checked[1] = on
        return checked

    lst = sample()
    lst.toggled = follow
    model = Model(lst)
    press(model, Key.DOWN, Key.DOWN, Key.TOGGLE)
    assert model.checked[:2] == [False, False]


def test_with_styles_a_row_with_a_detail_is_set_apart_and_the_keys_from_the_selection() -> None:
    drawn = [strip_escapes(line) for line in Model(sample()).render(80, 40, True, Styles("truecolor", True))]
    at = drawn.index("          Named in 2 notes, last 2026-10-08.")
    assert drawn[at + 1] == "" and drawn[at + 2].endswith("[x] Quillfeather & Sons")
    assert drawn[-3] == ""


def test_a_line_wider_than_the_terminal_is_cut_with_an_ellipsis() -> None:
    lst = sample()
    lst.rows[0].detail = "a very long detail " * 10
    drawn = Model(lst).render(40, 40)
    detail = next(line for line in drawn if "a very long" in line)
    assert len(detail) == 40 and detail.endswith("…")


def test_the_plain_list_matches_its_golden_frame_and_holds_no_escape_byte() -> None:
    plain = write_plain(sample())
    assert "\x1b" not in plain
    assert plain == (DATA / "sample-plain.txt").read_text()
    assert "  [x] Harbor Row 5\n  … 7 more\n" in plain


def test_count_of_writes_thousands_with_commas() -> None:
    assert count_of(2836, 2841) == "2,836 of 2,841"


def test_ask_refuses_plain_output_and_a_stream_that_is_no_terminal() -> None:
    with pytest.raises(NoRawKeys, match="write_plain"):
        ask(sample(), Printer(io.StringIO(), io.StringIO()))


# ---- the menu and the pager ----------------------------------------------------------------------------------------


def menu() -> MenuList:
    return MenuList(head=["Sample menu", "Open a collection."], rows=[
        MenuRow("Sample collection", state="15 awaiting review"),
        MenuRow("Empty collection", state="nothing new", inert=True),
        MenuRow("Second collection", state="2 awaiting review", reason="from calls", below=["one line under it"]),
    ], end="Done")


def test_a_menu_moves_past_an_inert_row_opens_the_cursors_row_and_ends_on_its_end_row() -> None:
    m = Menu(menu())
    m.press(Key.DOWN)
    assert m.cursor == 2
    assert m.press(Key.ENTER) == (True, True)
    m.press(Key.DOWN)
    assert m.on_end() and m.press(Key.ENTER) == (True, False)
    assert m.render(80, 24) == [
        "Sample menu", "Open a collection.", "",
        "  Sample collection   15 awaiting review",
        "  Empty collection    nothing new",
        "  Second collection   2 awaiting review    from calls",
        "                      one line under it",
        "", "> Done", "", "↑↓ move   ⏎ open   esc done"]


def test_a_pager_shows_text_that_fits_whole_and_scrolls_taller_text_a_line_at_a_time() -> None:
    assert Pager(["one", "two"]).render(80, 24) == ["one", "two", "", "esc back"]
    pager = Pager([f"line {n}" for n in range(30)])
    assert pager.render(80, 10) == [*(f"line {n}" for n in range(9)), "↑↓ scroll   esc back"]
    pager.press(Key.DOWN)
    assert pager.render(80, 10)[0] == "line 1"
    for _ in range(40):
        pager.press(Key.DOWN)
    assert pager.render(80, 10)[-2] == "line 29"
    assert pager.press(Key.ESC) == (True, False)


# ---- through Textual's pilot ---------------------------------------------------------------------------------------


def drive(app, *steps: tuple[str, ...]) -> list[list[str]]:
    """Run app headless, press each step's keys, and keep the frame after each step."""

    async def run() -> list[list[str]]:
        frames = []
        async with app.run_test(size=(80, 30)) as pilot:
            for keys in steps:
                await pilot.press(*keys)
                await pilot.pause()
                frames.append([strip_escapes(line) for line in app.view.frame()])
        return frames

    return asyncio.run(run())


def test_the_pilot_scripts_the_keys_and_the_widgets_state_and_frame_follow_each() -> None:
    app = ChecklistApp(sample(), Styles("256", True))
    frames = drive(app, ("down",), ("space",), ("j", "j", "space"), ("j", "j", "l"), ("A",), ("enter",))
    model = app.view.model
    assert model.checked == [True] * 16
    assert frames[0][3].startswith("  [-] Sample collection") and frames[0][4].startswith("> ")
    assert "[x] ▾ named in your notes" in frames[1][4]
    assert "[ ] Quillfeather & Sons" in "\n".join(frames[2])
    assert any("Harbor Row 1" in line for line in frames[3]) and "  … 6 more" in frames[3]
    assert "\n".join(frames[3]) + "\n" == (DATA / "pilot-opened.txt").read_text()
    assert not any(line.startswith(">") for line in frames[5])  # the last frame has no cursor
    assert app.return_value == model.checked


def test_escape_ends_the_list_with_nothing_and_ctrl_c_does_too() -> None:
    for key in ("escape", "ctrl+c"):
        app = ChecklistApp(sample())
        drive(app, ("space", key))
        assert app.return_value is None


def test_the_menu_and_the_pager_run_as_widgets_too() -> None:
    menu_app = MenuApp(menu())
    drive(menu_app, ("down", "enter"))
    assert menu_app.return_value == 2
    done = MenuApp(menu())
    drive(done, ("escape",))
    assert done.return_value is None or done.return_value == MENU_DONE
    pager_app = PagerApp([f"line {n}" for n in range(50)])
    frames = drive(pager_app, ("down", "down"), ("escape",))
    assert frames[0][0] == "line 2"
    assert pager_app.return_value is None


def test_the_list_runs_inline_on_a_pseudo_terminal_redraws_in_place_on_a_resize_and_returns_the_checked_rows() -> None:
    code = """
import os, signal, sys
from cerebro.checklist import Checklist, ChecklistApp, Group, Row, ask
from cerebro.output import Printer
# the terminal is resized once the list is drawn, as dragging its window's edge does
ChecklistApp.on_mount = lambda app: app.call_after_refresh(os.kill, os.getpid(), signal.SIGWINCH)
lst = Checklist(rows=[Row("Bramblecote Ltd", detail="Named in 2 notes."), Row("Larkspur Mills", checked=False)],
                groups=[Group("named in your notes", [0, 1], heading="Sample list")], head=["Sample list"])
print("RESULT", ask(lst, Printer()), file=sys.stderr)
"""
    keys = ((b"Approve selected", 1, b"jj "), (b"Approve selected", 2, b"\r"))
    got = on_a_pty(code, {"TERM": "xterm-256color", "COLORFGBG": "15;0"}, keys=keys)
    assert b"RESULT [False, False]" in got, got[-400:]
    assert b"\x1b[2J" not in got  # no screen clear, which a terminal keeps in its scrollback as a copy of the list
    assert b"\x1b[38;5;39;49m" in got  # the dark accent at its 256-color index, on the terminal's own background
    assert b"\x1b[38;5;43;49m" in got  # the dark success of a checked box


def test_the_widget_and_output_modules_hold_no_word_of_what_their_rows_are_about() -> None:
    words = re.compile(r"\b(proposals?|curators?|admits?|admitted|deals?|accounts?|brain|page)\b", re.IGNORECASE)
    for name in ("checklist.py", "output.py"):
        found = words.findall((SOURCES / name).read_text())
        assert not found, (name, found)


def test_on_a_pseudo_terminal_with_no_color_the_list_refuses_raw_keys_and_prints_plain() -> None:
    code = """
import sys
from cerebro.checklist import Checklist, Group, NoRawKeys, Row, ask, write_plain
from cerebro.output import Printer
lst = Checklist(rows=[Row("Bramblecote Ltd")], groups=[Group("named in your notes", [0])], head=["Sample list"])
p = Printer()
try:
    ask(lst, p)
except NoRawKeys:
    p.block(write_plain(lst))
"""
    got = on_a_pty(code, {"TERM": "xterm-256color", "NO_COLOR": "1"})
    assert b"\x1b" not in got and b"[x] Bramblecote Ltd" in got


def test_with_stderr_sent_to_a_file_the_list_refuses_raw_keys_since_textual_draws_on_stderr() -> None:
    code = """
import os
from cerebro.checklist import Checklist, Group, NoRawKeys, Row, ask
from cerebro.output import Printer
os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
lst = Checklist(rows=[Row("Bramblecote Ltd")], groups=[Group("named in your notes", [0])])
try:
    ask(lst, Printer())
except NoRawKeys:
    print("NO RAW KEYS")
"""
    assert b"NO RAW KEYS" in on_a_pty(code, {"TERM": "xterm-256color"})
