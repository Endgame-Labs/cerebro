"""The command line's look: the palette and the named styles pinned to their values, every piece in a light
and a dark terminal, plain output with no escape byte through a pipe and with NO_COLOR, and color through a
pseudo-terminal."""

from __future__ import annotations

import fcntl
import io
import math
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from cerebro.output import (
    PALETTE,
    SPINNER,
    STYLE_NAMES,
    Mark,
    Printer,
    Styles,
    color_profile,
    dark_from,
    elapsed,
    spinner,
    strip_escapes,
)

CHECKS = Path(__file__).resolve().parents[1] / "checks"
DATA = Path(__file__).parent / "data" / "tui"


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def env(**values: str) -> dict[str, str]:
    return values


# ---- the palette, one test per role ----------------------------------------------------------------------------

EXPECTED_PALETTE = {
    #          light: hex, 256, 16          dark: hex, 256, 16
    "success": (("#047857", 23, 2), ("#34d399", 43, 10)),
    "warning": (("#864720", 94, 3), ("#dd8c5e", 173, 11)),
    "failure": (("#b5455d", 125, 1), ("#d55973", 168, 9)),
    "accent": (("#004ba2", 25, 4), ("#399aff", 39, 12)),
    "muted": (("#587292", 60, 8), ("#587292", 243, 8)),
    "tint": (("#f4f6f9", 255, 7), ("#1e2229", 235, 8)),
}


@pytest.mark.parametrize("role", list(EXPECTED_PALETTE))
def test_each_role_takes_its_light_and_dark_values_and_their_256_and_16_color_fallbacks(role: str) -> None:
    light, dark = EXPECTED_PALETTE[role]
    got = PALETTE[role]
    assert (got.light.hex, got.light.c256, got.light.c16) == light
    assert (got.dark.hex, got.dark.c256, got.dark.c16) == dark


def test_the_palette_has_the_six_roles_and_no_more() -> None:
    assert set(PALETTE) == set(EXPECTED_PALETTE)


# The backgrounds each mode's 256-color indexes must read on.
DARK_BACKGROUNDS = ["#000000", "#1e1e1e", "#282c34", "#002b36", "#010c1e", "#0e233d"]
LIGHT_BACKGROUNDS = ["#ffffff", "#fdf6e3", "#eeeeee", "#f8fafc"]


def _rgb(hex_: str) -> tuple[float, float, float]:
    return int(hex_[1:3], 16) / 255, int(hex_[3:5], 16) / 255, int(hex_[5:7], 16) / 255


def _xterm(i: int) -> tuple[float, float, float]:
    if i >= 232:
        g = (8 + 10 * (i - 232)) / 255
        return g, g, g
    levels = (0, 95, 135, 175, 215, 255)
    i -= 16
    return levels[i // 36] / 255, levels[i // 6 % 6] / 255, levels[i % 6] / 255


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _worst_contrast(c: tuple[float, float, float], backgrounds: list[str]) -> float:
    def lum(x: tuple[float, float, float]) -> float:
        return 0.2126 * _linear(x[0]) + 0.7152 * _linear(x[1]) + 0.0722 * _linear(x[2])

    worst = math.inf
    for bg in backgrounds:
        a, b = lum(c), lum(_rgb(bg))
        worst = min(worst, (max(a, b) + 0.05) / (min(a, b) + 0.05))
    return worst


def _ok_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    def lab(c: tuple[float, float, float]) -> tuple[float, float, float]:
        r, g, bl = (_linear(v) for v in c)
        l_ = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * bl)
        m = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * bl)
        s = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * bl)
        return (0.2104542553 * l_ + 0.7936177850 * m - 0.0040720468 * s,
                1.9779984951 * l_ - 2.4285922050 * m + 0.4505937099 * s,
                0.0259040371 * l_ + 0.7827717662 * m - 0.8086757660 * s)

    x, y = lab(a), lab(b)
    return sum((p - q) ** 2 for p, q in zip(x, y, strict=True))


def test_each_256_color_index_is_the_nearest_that_reads_with_enough_contrast() -> None:
    for role in ("success", "warning", "failure", "accent", "muted"):
        for shade, backgrounds in ((PALETTE[role].light, LIGHT_BACKGROUNDS), (PALETTE[role].dark, DARK_BACKGROUNDS)):
            token = _rgb(shade.hex)
            bar = min(4.5, _worst_contrast(token, backgrounds))
            best = min((i for i in range(16, 256) if _worst_contrast(_xterm(i), backgrounds) >= bar),
                       key=lambda i: _ok_distance(token, _xterm(i)))
            assert shade.c256 == best, (role, shade.hex)


# ---- every named style, in light and dark, on each kind of terminal -----------------------------------------------

ROLE_OF = {"title": None, "section": None, "label": "muted", "value": None, "muted": "muted", "success": "success",
           "warning": "warning", "failure": "failure", "hint": "muted", "key": "accent",
           "strong_success": "success", "strong_warning": "warning", "strong_failure": "failure"}
BOLD = {"title", "section", "strong_success", "strong_warning", "strong_failure"}


def expected_opening(name: str, profile: str, dark: bool) -> str:
    codes = ["1"] if name in BOLD else []
    role = ROLE_OF[name]
    if role is not None:
        hex_, c256, c16 = EXPECTED_PALETTE[role][1 if dark else 0]
        if profile == "truecolor":
            codes.append(f"38;2;{int(hex_[1:3], 16)};{int(hex_[3:5], 16)};{int(hex_[5:7], 16)}")
        elif profile == "256":
            codes.append(f"38;5;{c256}")
        else:
            codes.append(str(30 + c16) if c16 < 8 else str(90 + c16 - 8))
    return f"\x1b[{';'.join(codes)}m" if codes else ""


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("profile", ["truecolor", "256", "16"])
def test_every_named_style_draws_with_its_expected_sequence_in_light_and_dark(profile: str, dark: bool) -> None:
    st = Styles(profile, dark)  # type: ignore[arg-type]
    assert set(STYLE_NAMES) == set(ROLE_OF)
    for name in STYLE_NAMES:
        opening = expected_opening(name, profile, dark)
        drawn = st.paint(name, "text")
        assert drawn == (opening + "text\x1b[0m" if opening else "text"), name


def test_the_tint_opens_as_the_background_in_its_shade_on_each_kind_of_terminal() -> None:
    assert Styles("truecolor", False).tint_opening() == "\x1b[48;2;244;246;249m"
    assert Styles("truecolor", True).tint_opening() == "\x1b[48;2;30;34;41m"
    assert Styles("256", False).tint_opening() == "\x1b[48;5;255m"
    assert Styles("256", True).tint_opening() == "\x1b[48;5;235m"
    assert Styles("16", False).tint_opening() == "\x1b[47m"
    assert Styles("16", True).tint_opening() == "\x1b[100m"


def test_a_style_opens_again_after_a_phrase_drawn_in_its_own_and_paints_line_by_line() -> None:
    st = Styles("256", True)
    inner = st.paint("key", "cerebro status")
    assert st.paint("muted", f"run {inner} now") == (
        "\x1b[38;5;243mrun \x1b[38;5;39mcerebro status\x1b[0m\x1b[38;5;243m now\x1b[0m")
    assert st.paint("muted", "one\n\ntwo") == "\x1b[38;5;243mone\x1b[0m\n\n\x1b[38;5;243mtwo\x1b[0m"


# ---- the terminal's colors and background --------------------------------------------------------------------------


@pytest.mark.parametrize(("variables", "profile"), [
    ({"COLORTERM": "truecolor"}, "truecolor"),
    ({"COLORTERM": "24bit", "TERM": "xterm-256color"}, "truecolor"),
    ({"COLORTERM": "truecolor", "TERM": "screen-256color"}, "256"),
    ({"COLORTERM": "truecolor", "TERM": "screen", "TERM_PROGRAM": "tmux"}, "truecolor"),
    ({"COLORTERM": "yes"}, "256"),
    ({"TERM": "xterm-256color"}, "256"),
    ({"TERM": "xterm-kitty"}, "truecolor"),
    ({"TERM": "xterm"}, "16"),
    ({}, "16"),
])
def test_the_colors_a_terminal_takes_follow_the_variables_it_sets(
        variables: dict[str, str], profile: str) -> None:
    assert color_profile(variables.get) == profile


def test_the_background_is_the_terminals_answer_else_colorfgbg_else_dark() -> None:
    nothing = env().get
    assert dark_from("\x1b]11;rgb:ffff/ffff/ffff\x1b\\", nothing) is False
    assert dark_from("\x1b]11;rgb:1e1e/1e1e/1e1e\x07", nothing) is True
    assert dark_from("\x1b]11;rgb:fd/f6/e3\x1b\\", env(COLORFGBG="15;0").get) is False  # the answer wins
    assert dark_from(None, env(COLORFGBG="0;15").get) is False
    assert dark_from(None, env(COLORFGBG="15;0").get) is True
    assert dark_from(None, env(COLORFGBG="0;default;7").get) is False
    assert dark_from(None, env(COLORFGBG="15;8").get) is True  # bright black is dark, whatever a theme makes of it
    assert dark_from(None, env(COLORFGBG="0;255").get) is False
    assert dark_from(None, env(COLORFGBG="0;232").get) is True
    assert dark_from("rgb:zz/00/00", nothing) is True
    assert dark_from(None, nothing) is True


# ---- marks, the spinner and times ----------------------------------------------------------------------------------


def test_the_marks_and_the_spinner_have_their_glyphs_and_ascii_forms() -> None:
    assert [m.symbol(True) for m in Mark] == ["•", "✓", "✗", "!", "·"]
    assert [m.symbol(False) for m in Mark] == ["[..]", "[ok]", "[x]", "[!]", "[-]"]
    assert SPINNER == ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    assert spinner(0) == "⠋" and spinner(0.35) == "⠸" and spinner(1.0) == "⠋" and spinner(-1) == "⠋"


def test_a_time_is_spelled_as_the_docs_spell_it() -> None:
    assert [elapsed(s) for s in (0.42, 12.9, 192, 3840, -1)] == ["0.4s", "12s", "3m 12s", "1h 4m", "0.0s"]


# ---- the pieces ----------------------------------------------------------------------------------------------------


def styled(dark: bool = True, width: int = 80) -> tuple[Printer, Terminal]:
    stream = Terminal()
    return Printer(stream, stream, getenv=env(TERM="xterm-256color").get, dark=dark, width=lambda: width), stream


def test_the_header_heading_and_items_draw_on_a_terminal() -> None:
    p, out = styled(dark=False)
    p.header("cerebro status", ".../sample-wiki")
    p.heading("Sources")
    p.item(1, Mark.DONE, "local")
    p.item(1, Mark.WARNING, "calls")
    assert out.getvalue() == (
        "\x1b[1mcerebro status\x1b[0m  \x1b[38;5;60m.../sample-wiki\x1b[0m\n\n"
        "\x1b[1mSources\x1b[0m\n"
        "  \x1b[38;5;23m✓\x1b[0m local\n"
        "  \x1b[1;38;5;94m!\x1b[0m calls\n")


def test_a_table_mutes_its_labels_and_wraps_a_long_last_cell_under_its_column() -> None:
    p, out = styled(width=40)
    p.table([["workspace", ".../sample-wiki"], ["next", "run cerebro status to see the articles awaiting review"]])
    shown = strip_escapes(out.getvalue()).splitlines()
    assert shown[0] == "workspace  .../sample-wiki"
    assert shown[1].startswith("next       run cerebro status") and len(shown) == 3
    assert shown[2].startswith("           ")
    assert "\x1b[38;5;243mworkspace\x1b[0m" in out.getvalue()
    assert "\x1b[38;5;39mcerebro status\x1b[0m" in out.getvalue()


def test_a_tinted_block_pads_each_line_to_the_widest_and_never_past_the_terminal() -> None:
    p, _ = styled(dark=False, width=30)
    rows = p.tinted("kind: files\npath: local/**/*.md")
    assert rows == [
        "\x1b[48;5;255m  kind: files          \x1b[0m",
        "\x1b[48;5;255m  path: local/**/*.md  \x1b[0m",
    ]
    wide = p.tinted("x" * 40)
    assert strip_escapes(wide[0]) == "  " + "x" * 40 + " " * 18  # wraps once; its last row filled to the edge


def test_a_link_is_an_osc_8_hyperlink_in_the_accent_and_a_path_is_a_file_address() -> None:
    p, _ = styled()
    assert p.link("notes", "/tmp/a b.md") == "\x1b]8;;file:///tmp/a%20b.md\x1b\\\x1b[38;5;39mnotes\x1b[0m\x1b]8;;\x1b\\"
    assert p.link("site", "https://example.com").startswith("\x1b]8;;https://example.com\x1b\\")


def test_a_step_shows_a_spinner_and_resolves_to_its_mark_and_time_with_its_sub_lines() -> None:
    clock = {"now": 0.0}
    stream = Terminal()
    p = Printer(stream, stream, getenv=env(TERM="xterm-256color").get, dark=True, width=lambda: 80,
                now=lambda: clock["now"], tick=3600)
    step = p.step("Reading sources")
    step.lines("local: 3 records")
    clock["now"] = 3.2
    step.done("Read 3 records")
    drawn = stream.getvalue()
    assert drawn.startswith("\r\x1b[K\x1b[38;5;39m⠋\x1b[0m Reading sources  \x1b[38;5;243m0.0s\x1b[0m")
    assert drawn.endswith("\x1b[38;5;43m✓\x1b[0m Read 3 records  \x1b[38;5;243m3s\x1b[0m\n"
                          "    \x1b[38;5;243mlocal: 3 records\x1b[0m\n")


def test_a_step_off_a_terminal_prints_its_resolved_line_alone_with_an_ascii_mark() -> None:
    out = io.StringIO()
    p = Printer(out, out, getenv=env().get)
    step = p.step("Reading sources")
    step.lines("local: 3 records")
    step.fail("Could not read local")
    p.step("Nothing to do").skip()
    assert out.getvalue() == "[x] Could not read local\n    local: 3 records\n[-] Nothing to do\n"


def test_the_last_line_names_the_state_and_what_comes_next_in_each_of_the_three_states() -> None:
    p, _ = styled()
    p.next("cerebro serve --workspace .../sample-wiki")
    assert strip_escapes(p.last_line()) == "✓ Done. Next: cerebro serve --workspace .../sample-wiki"
    assert p.last_line().startswith("\x1b[38;5;43m✓\x1b[0m \x1b[1;38;5;43mDone.\x1b[0m \x1b[38;5;243mNext:\x1b[0m")
    assert strip_escapes(p.last_line(failed=True, run="cerebro trust .")) == "✗ Failed. Run: cerebro trust ."
    p.warn("one source is slow")
    p.check("another")
    assert strip_escapes(p.last_line()) == "! Done with warnings. Check: one source is slow (and 1 more warning above)"
    plain = Printer(io.StringIO(), io.StringIO(), getenv=env().get)
    plain.next("cerebro status")
    assert plain.last_line() == "[ok] Done. Next: cerebro status"
    assert plain.last_line(failed=True) == "[x] Failed."


def test_a_line_on_stderr_mutes_the_programs_name_and_marks_a_warnings_word() -> None:
    p, out = styled()
    p.err_line("cerebro: warning: run cerebro status, then look")
    assert out.getvalue() == ("\x1b[38;5;243mcerebro:\x1b[0m \x1b[1;38;5;173mwarning:\x1b[0m run "
                              "\x1b[38;5;39mcerebro status\x1b[0m, then look\n")


def test_a_styled_stdout_never_shows_two_blank_lines_in_a_row() -> None:
    p, out = styled()
    p.header("cerebro sample")
    p.heading("First")
    assert out.getvalue().count("\n\n\n") == 0


# ---- plain stays plain ---------------------------------------------------------------------------------------------


def every_piece(p: Printer) -> None:
    p.header("cerebro sample", ".../sample-wiki")
    p.heading("A heading")
    for mark in Mark:
        p.item(1, mark, mark.name.lower())
    p.line(p.bold("bold") + p.dim(" dim") + p.label(" label") + p.key(" key"))
    p.line(p.running_mark() + " " + p.link("article", "/tmp/a.md"))
    p.table([["workspace", ".../sample-wiki"], ["next", "run cerebro status to see what is awaiting review"]])
    p.print_tinted("cerebro connect sample")
    step = p.step("Reading sources")
    step.lines("local: 3 records")
    step.done()
    p.step_along("Beside it").warn()
    p.note("a note")
    p.warn("a warning: run cerebro status")
    p.err_line("cerebro: warning: a warning line")
    p.end()
    p.end(failed=True, run="cerebro trust .")


@pytest.mark.parametrize("how", ["off a terminal", "plain asked", "NO_COLOR", "TERM=dumb"])
def test_every_piece_prints_no_escape_byte_when_plain(how: str) -> None:
    stream = io.StringIO() if how == "off a terminal" else Terminal()
    variables = {"NO_COLOR": "1"} if how == "NO_COLOR" else {"TERM": "dumb"} if how == "TERM=dumb" else {}
    p = Printer(stream, stream, plain=how == "plain asked", getenv={"TERM": "xterm-256color", **variables}.get)
    every_piece(p)
    assert "\x1b" not in stream.getvalue()
    assert "[ok]" in stream.getvalue()


def test_the_demo_prints_every_piece_and_the_plain_list_through_a_pipe_with_no_escape_byte() -> None:
    shown = subprocess.run([sys.executable, str(CHECKS / "style_demo.py"), "--plain"], capture_output=True,
                           env={"PATH": os.environ.get("PATH", ""), "TERM": "xterm-256color"}, timeout=25, check=True)
    assert b"\x1b" not in shown.stdout and b"\x1b" not in shown.stderr
    assert b"[ok] done" in shown.stdout and b"selected" in shown.stdout


# ---- color through a pseudo-terminal -------------------------------------------------------------------------------

CHILD = """
import sys
from cerebro.output import Mark, Printer
p = Printer()
p.header("cerebro sample", ".../sample-wiki")
p.item(0, Mark.DONE, "done")
p.next("cerebro status")
p.end()
"""


# The child's first line: it leads a session of its own, and takes the pseudo-terminal as its controlling terminal, as
# a shell's terminal is, so it holds the terminal's foreground.
OWN_TERMINAL = "import fcntl, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"


def on_a_pty(code: str, variables: dict[str, str], answer: bytes | None = None, seconds: float = 20,
             keys: tuple[tuple[object, ...], ...] = (), size: tuple[int, int] = (24, 80)) -> bytes:
    """What a child running code prints on an 80 by 24 pseudo-terminal, which answers the background question with
    answer, and the cursor's place, as a terminal does. Each of keys is typed once what the child printed holds its
    marker the given number of times."""
    leader, follower = pty.openpty()
    fcntl.ioctl(follower, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
    child = subprocess.Popen([sys.executable, "-c", OWN_TERMINAL + code], stdin=follower, stdout=follower,
                             stderr=follower, env={"PATH": os.environ.get("PATH", ""), **variables},
                             start_new_session=True)
    os.close(follower)
    got = b""
    script = list(keys)
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            ready, _, _ = select.select([leader], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(leader, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                got += chunk
                if b"\x1b]11;?" in chunk and answer is not None:
                    os.write(leader, answer)
                if b"\x1b[6n" in chunk:
                    os.write(leader, b"\x1b[1;1R")
                if script and got.count(script[0][0]) >= script[0][1]:
                    if len(script[0]) > 3:  # a pause first, for a view that must be reading keys before they come
                        time.sleep(script[0][3])
                    os.write(leader, script.pop(0)[2])
            elif child.poll() is not None:
                break
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(leader)
    return got


def test_on_a_pseudo_terminal_the_pieces_draw_in_color_with_the_256_color_indexes() -> None:
    got = on_a_pty(CHILD, {"TERM": "xterm-256color", "COLORFGBG": "0;15"})
    assert b"\x1b[1mcerebro sample\x1b[0m" in got
    assert b"\x1b[38;5;23m\xe2\x9c\x93\x1b[0m done" in got  # light success, from COLORFGBG's white background
    assert b"\x1b[1;38;5;23mDone.\x1b[0m" in got


def test_on_a_pseudo_terminal_the_terminals_own_answer_decides_light_or_dark() -> None:
    got = on_a_pty(CHILD, {"COLORTERM": "truecolor", "COLORFGBG": "15;0"}, answer=b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")
    assert b"\x1b]11;?" in got
    assert b"\x1b[38;2;4;120;87m" in got  # light success on a white background, though COLORFGBG says dark


def test_on_a_pseudo_terminal_no_color_prints_no_escape_byte() -> None:
    got = on_a_pty(CHILD, {"TERM": "xterm-256color", "NO_COLOR": "1"})
    assert b"\x1b" not in got and b"[ok] Done. Next: cerebro status" in got


# ---- the demo ------------------------------------------------------------------------------------------------------


def test_the_demo_runs_end_to_end_in_both_themes_and_saves_its_frames(tmp_path: Path) -> None:
    sys.path.insert(0, str(CHECKS))
    try:
        import style_demo
    finally:
        sys.path.remove(str(CHECKS))
    out = io.StringIO()
    assert style_demo.main([], out) == 0
    assert out.getvalue().count("== light terminal ==") == 1 and "== dark terminal ==" in out.getvalue()
    assert "\x1b[38;2;4;120;87m" in out.getvalue() and "\x1b[38;2;52;211;153m" in out.getvalue()
    assert style_demo.main(["--save", str(tmp_path)], io.StringIO()) == 0
    saved = sorted(p.name for p in tmp_path.iterdir())
    assert len(saved) == 10 and all((DATA / name).exists() for name in saved)
    for name in saved:
        if name.endswith(".txt"):
            assert (tmp_path / name).read_text() == (DATA / name).read_text(), name
            assert "\x1b" not in (tmp_path / name).read_text()
    assert re.search(r'fill="#ffffff"', (tmp_path / "checklist-light.svg").read_text())
    assert re.search(r'fill="#1e1e1e"', (tmp_path / "checklist-dark.svg").read_text())
