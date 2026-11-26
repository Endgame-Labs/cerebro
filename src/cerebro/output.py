"""How the command line looks: the palette, the named styles and the pieces a screen is drawn with.

The palette has a light and a dark step per role, each with a 256-color and a
16-color index for a terminal of fewer colors. A screen draws through a `Printer`: a header,
regions, items with a mark, tables, a tinted block, links, steps with a spinner that resolve to a mark and the time
they took, and the last line a command ends on.

Plain output stays plain. Off a terminal, with `plain=True` (as `--plain` asks), with `NO_COLOR` set or on a dumb
terminal, every piece prints as plain text with ASCII marks and no escape code, so a screen reader and a log file
read it cleanly. The checkbox list beside it is `cerebro.checklist`.

Nothing here knows what a screen is about: it takes text and returns or prints text.
"""

from __future__ import annotations

import os
import re
import select
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import IO, Any, Literal
from urllib.parse import quote

from rich.cells import cell_len

__all__ = [
    "PALETTE",
    "SPINNER",
    "STYLE_NAMES",
    "Mark",
    "Printer",
    "Profile",
    "Shade",
    "Step",
    "Styles",
    "color_profile",
    "dark_from",
    "elapsed",
    "is_terminal",
    "spinner",
    "strip_escapes",
    "terminal_dark",
    "visible_width",
]

Profile = Literal["truecolor", "256", "16"]
"""The colors a terminal takes: a profile of none is plain text."""

NAME = "cerebro"
"""The program's name, as a command a line names to run starts with it."""


# ---- the palette ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shade:
    """One step of a role: its hex token, and the 256-color and 16-color indexes a terminal of fewer colors gets."""

    hex: str
    c256: int
    c16: int

    def rgb(self) -> tuple[int, int, int]:
        return int(self.hex[1:3], 16), int(self.hex[3:5], 16), int(self.hex[5:7], 16)


@dataclass(frozen=True)
class Role:
    """A color of the palette: the shade it takes on a light background and on a dark one."""

    light: Shade
    dark: Shade

    def shade(self, dark: bool) -> Shade:
        return self.dark if dark else self.light


# The palette. On a dark background failure and muted
# read under 4.5:1, steps chosen by eye there. Each 256-color index is the one nearest its token by OKLab distance
# among those that read on every background of its mode at least as well as the token does (or at 4.5:1 where the
# token reads better), so a role is never harder to read on 256 colors, though its hue may drift.
#
#   success  green.700  | green.400
#   warning  orange.700 | orange.400
#   failure  red.600    | red.500
#   accent   blue.700   | blue.300, primary.fg in each mode
#   muted    gray.600   | gray.600, fg.muted on light, and on dark too
#   tint     the muted background a tinted block shows on: one small step off the terminal's own background
PALETTE: dict[str, Role] = {
    "success": Role(Shade("#047857", 23, 2), Shade("#34d399", 43, 10)),
    "warning": Role(Shade("#864720", 94, 3), Shade("#dd8c5e", 173, 11)),
    "failure": Role(Shade("#b5455d", 125, 1), Shade("#d55973", 168, 9)),
    "accent": Role(Shade("#004ba2", 25, 4), Shade("#399aff", 39, 12)),
    "muted": Role(Shade("#587292", 60, 8), Shade("#587292", 243, 8)),
    "tint": Role(Shade("#f4f6f9", 255, 7), Shade("#1e2229", 235, 8)),
}


@dataclass(frozen=True)
class _Named:
    role: str | None
    bold: bool = False


# The named styles every screen is drawn with. Bold is for the command's name, a region, the
# state a command ended in. Muted is for what supports a value: a path beside the header, a time, a detail, a label. A
# value is in the terminal's own color. The strong styles are a mark or word that must not be missed.
_NAMED: dict[str, _Named] = {
    "title": _Named(None, bold=True),
    "section": _Named(None, bold=True),
    "label": _Named("muted"),
    "value": _Named(None),
    "muted": _Named("muted"),
    "success": _Named("success"),
    "warning": _Named("warning"),
    "failure": _Named("failure"),
    "hint": _Named("muted"),
    "key": _Named("accent"),
    "strong_success": _Named("success", bold=True),
    "strong_warning": _Named("warning", bold=True),
    "strong_failure": _Named("failure", bold=True),
}
STYLE_NAMES: tuple[str, ...] = tuple(_NAMED)

RESET = "\x1b[0m"
_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")


def strip_escapes(s: str) -> str:
    """s with every escape sequence taken out: what a terminal shows of it."""
    return _ESCAPE.sub("", s)


def visible_width(s: str) -> int:
    """The columns s takes on a terminal, escape codes aside."""
    return cell_len(strip_escapes(s))


def _foreground(shade: Shade, profile: Profile) -> str:
    if profile == "truecolor":
        r, g, b = shade.rgb()
        return f"38;2;{r};{g};{b}"
    if profile == "256":
        return f"38;5;{shade.c256}"
    n = shade.c16
    return str(30 + n) if n < 8 else str(90 + n - 8)


def _background(shade: Shade, profile: Profile) -> str:
    if profile == "truecolor":
        r, g, b = shade.rgb()
        return f"48;2;{r};{g};{b}"
    if profile == "256":
        return f"48;5;{shade.c256}"
    n = shade.c16
    return str(40 + n) if n < 8 else str(100 + n - 8)


class Styles:
    """The named styles on a terminal of `profile` colors with a light or dark background, or plain with no profile.

    `paint` draws text in a named style as an SGR sequence with the token's own values: the hex on a truecolor
    terminal, the token's 256-color or 16-color index on one of fewer colors. A plain `Styles` returns text as it
    is.
    """

    def __init__(self, profile: Profile | None, dark: bool = True) -> None:
        self.profile = profile
        self.dark = dark

    @property
    def on(self) -> bool:
        return self.profile is not None

    def opening(self, name: str) -> str:
        """The sequence that opens the named style, or "" for one with nothing to draw or with no colors."""
        named = _NAMED[name]
        if self.profile is None:
            return ""
        codes = []
        if named.bold:
            codes.append("1")
        if named.role is not None:
            codes.append(_foreground(PALETTE[named.role].shade(self.dark), self.profile))
        return f"\x1b[{';'.join(codes)}m" if codes else ""

    def tint_opening(self) -> str:
        """The sequence that opens the tint as the background, or "" with no colors."""
        if self.profile is None:
            return ""
        return f"\x1b[{_background(PALETTE['tint'].shade(self.dark), self.profile)}m"

    def paint(self, name: str, s: str) -> str:
        """s in the named style, line by line so no line is padded to the widest; s itself when plain or empty.

        A phrase inside s already drawn in a style of its own, such as a key, ends in a reset: the style opens again
        after it, so the text around the phrase keeps it."""
        open_ = self.opening(name)
        if not open_ or not s:
            return s
        return "\n".join(_nested(open_, line) if line else line for line in s.split("\n"))


def _nested(open_: str, s: str) -> str:
    return open_ + s.replace(RESET, RESET + open_) + RESET


# ---- the terminal --------------------------------------------------------------------------------------------------


_COLORTERM: dict[str, Profile] = {"24bit": "truecolor", "truecolor": "truecolor", "yes": "256", "true": "256"}
_FULL_COLOR_TERMS = frozenset(("alacritty", "contour", "rio", "wezterm", "xterm-ghostty", "xterm-kitty"))


def color_profile(getenv: Callable[[str], str | None]) -> Profile:
    """The colors a terminal takes, from the variables it sets: what `COLORTERM` says, else what `TERM` names, else
    the 16 colors every terminal has."""
    term = getenv("TERM") or ""
    if getenv("GOOGLE_CLOUD_SHELL") == "true":
        return "truecolor"
    said = _COLORTERM.get((getenv("COLORTERM") or "").lower())
    if said is None:
        return "truecolor" if term in _FULL_COLOR_TERMS else "256" if "256color" in term else "16"
    if said == "truecolor" and term.startswith("screen") and getenv("TERM_PROGRAM") != "tmux":
        return "256"  # a TERM of screen outside tmux is read as 256 colors: tmux names itself screen too
    return said


def is_terminal(stream: object) -> bool:
    """Whether stream is a terminal."""
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty and isatty())
    except (ValueError, OSError):
        return False


def _lightness(r: float, g: float, b: float) -> float:
    return (max(r, g, b) + min(r, g, b)) / 2


def _xterm(i: int) -> tuple[int, int, int]:
    if i >= 232:
        g = 8 + 10 * (i - 232)
        return g, g, g
    levels = (0, 95, 135, 175, 215, 255)
    i -= 16
    return levels[i // 36], levels[i // 6 % 6], levels[i % 6]


def _dark_index(i: int) -> bool:
    """Whether the background COLORFGBG names by its index is dark: of the sixteen a theme sets, black, the six colors
    and bright black are dark, white and the bright colors light, whatever the theme makes of them."""
    if i == 7 or 9 <= i <= 15:
        return False
    if i < 16:
        return True
    r, g, b = _xterm(i)
    return _lightness(r / 255, g / 255, b / 255) < 0.5


def _parse_rgb(answer: str) -> tuple[float, float, float] | None:
    """The color in a terminal's answer to OSC 11, as in rgb:1e1e/1e1e/1e1e, each part of one to four hex digits."""
    at = answer.find("rgb:")
    if at < 0:
        return None
    body = re.split(r"[\x07\x1b]", answer[at + 4:], maxsplit=1)[0]
    parts = body.split("/")
    if len(parts) != 3:
        return None
    values = []
    for part in parts:
        if not 1 <= len(part) <= 4 or not re.fullmatch(r"[0-9a-fA-F]+", part):
            return None
        values.append(int(part, 16) / ((1 << (4 * len(part))) - 1))
    return values[0], values[1], values[2]


def dark_from(answer: str | None, getenv: Callable[[str], str | None]) -> bool:
    """Whether the background is dark: as the terminal's answer to OSC 11 says, else as COLORFGBG says, else dark."""
    if answer:
        rgb = _parse_rgb(answer)
        if rgb is not None:
            return _lightness(*rgb) < 0.5
    value = getenv("COLORFGBG") or ""
    if ";" in value:
        last = value.split(";")[-1]
        if last.isdigit() and int(last) < 256:
            return _dark_index(int(last))
    return True


_ASK_WAIT = 0.3
_asked: dict[str, bool] = {}


def _ask_background(fd: int, getenv: Callable[[str], str | None]) -> str | None:
    """The terminal's answer to OSC 11, asked with the cursor's place behind it, which every terminal answers, so one
    that does not know OSC 11 ends the wait at once. It asks nothing of screen or tmux, of a terminal it does not hold
    the foreground of, or of one with a key waiting, which stays for the reader it was typed for."""
    import termios

    term = getenv("TERM") or ""
    if term.startswith(("screen", "tmux")) or term == "dumb":
        return None
    try:
        if os.tcgetpgrp(fd) != os.getpgrp() or select.select([fd], [], [], 0)[0]:
            return None
        cooked = termios.tcgetattr(fd)
    except (OSError, termios.error):
        return None
    quiet = list(cooked)
    quiet[3] &= ~(termios.ECHO | termios.ICANON)
    quiet[6] = list(cooked[6])
    quiet[6][termios.VMIN], quiet[6][termios.VTIME] = 1, 0
    got = b""
    try:
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
        os.write(fd, b"\x1b]11;?\x1b\\\x1b[6n")
        end = time.monotonic() + _ASK_WAIT
        while not re.search(rb"\x1b\[\d+;\d+R", got):
            left = end - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 256)
            if not chunk:
                break
            got += chunk
    except OSError:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, cooked)
    text = got.decode("latin-1")
    return text if "rgb:" in text else None


def terminal_dark(stream: object, getenv: Callable[[str], str | None]) -> bool:
    """Whether the terminal stream writes to has a dark background: as it answers when asked, once a process, else as
    COLORFGBG says, else dark. A stream that is no terminal is not asked."""
    fileno = getattr(stream, "fileno", None)
    try:
        fd = fileno() if fileno else -1
    except (OSError, ValueError):
        fd = -1
    if fd < 0 or not os.isatty(fd):
        return dark_from(None, getenv)
    key = os.ttyname(fd)
    if key not in _asked:
        _asked[key] = dark_from(_ask_background(fd, getenv), getenv)
    return _asked[key]


# ---- marks, the spinner and times ----------------------------------------------------------------------------------


class Mark(IntEnum):
    """What a line says happened."""

    RUNNING = 0
    DONE = 1
    FAILED = 2
    WARNING = 3
    QUIET = 4

    def symbol(self, styled: bool) -> str:
        """The mark's glyph as a styled stream shows it, before its color, or its ASCII form."""
        return ("•", "✓", "✗", "!", "·")[self] if styled else ("[..]", "[ok]", "[x]", "[!]", "[-]")[self]

    @property
    def style(self) -> str:
        """The named style the mark shows in."""
        return ("key", "success", "failure", "strong_warning", "muted")[self]


SPINNER: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
SPIN_EVERY = 0.1
"""Seconds between two frames of a running step's spinner."""


def spinner(took: float) -> str:
    """The spinner's frame once took seconds have passed, the frame a running step shows at that time."""
    return SPINNER[int(max(took, 0) / SPIN_EVERY + 1e-9) % len(SPINNER)]


def elapsed(seconds: float) -> str:
    """A time the way the docs spell it: 0.4s under a second, then 12s, 3m 12s or 1h 4m."""
    d = max(seconds, 0.0)
    if d < 1:
        return f"{d:.1f}s"
    whole = int(d)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m {whole % 60}s"
    return f"{whole // 3600}h {whole // 60 % 60}m"


# ---- commands a line names -----------------------------------------------------------------------------------------

_RUN_COLON = re.compile(r"(?:^|[\s(])Run: ([^,;\n]+?)(?:,| then |;|\.(?:\s|$)|\n|$)")
_RUN_WORD = re.compile(r"(?:^|[\s(])(?:run|Run) (" + NAME + r" [^,;:\n]+?)(?:,| to | then | and |;|:|\.(?:\s|$)|\)|\n|$)")
_TRAILING = re.compile(r" · (" + NAME + r" [^,;:\n·]+)$")
_WARNING_WORD = "warning:"


def command_in(message: str) -> str:
    """The command message says to run, after "Run:" or after "run" when it is a command of this program, or ""."""
    for pattern in (_RUN_COLON, _RUN_WORD):
        found = pattern.search(message)
        if found:
            return found.group(1).strip()
    return ""


def _commands(st: Styles, s: str) -> str:
    """s with each command it names to run in the key style, and the dot before a command that ends it muted."""
    if not st.on or "\x1b" in s:
        return s
    out = []
    for line in s.split("\n"):
        trailing = _TRAILING.search(line)
        if trailing:
            out.append(line[: trailing.start()] + st.paint("muted", " ·") + " " + st.paint("key", trailing.group(1)))
            continue
        cmd = command_in(line)
        at = line.find(cmd) if cmd else -1
        out.append(line[:at] + st.paint("key", cmd) + line[at + len(cmd):] if at >= 0 else line)
    return "\n".join(out)


# ---- the printer ---------------------------------------------------------------------------------------------------


@dataclass
class _State:
    next: str = ""
    checks: list[str] = field(default_factory=list)


class Printer:
    """One command's output: its lines on `out`, notes, warnings and the last line on `err`.

    A stream takes escape codes when it is a terminal and plain text was not asked for (`plain=True`, `NO_COLOR` set
    or `TERM=dumb`). Off a terminal every piece prints plain, with ASCII marks and nothing redrawn. `terminal`,
    `getenv`, `dark`, `width` and `now` stand in for the process's own, as a test's do.
    """

    def __init__(
        self,
        out: IO[str] | None = None,
        err: IO[str] | None = None,
        *,
        plain: bool = False,
        terminal: Callable[[object], bool] | None = None,
        getenv: Callable[[str], str | None] | None = None,
        dark: bool | None = None,
        width: Callable[[], int] | None = None,
        now: Callable[[], float] | None = None,
        tick: float = SPIN_EVERY,
        height: Callable[[], int] | None = None,
    ) -> None:
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        getenv = getenv or os.environ.get
        is_term = terminal or is_terminal
        self.plain = plain or bool(getenv("NO_COLOR")) or getenv("TERM") == "dumb"
        self.terminal_out, self.terminal_err = is_term(self.out), is_term(self.err)
        self.styled_out = self.terminal_out and not self.plain
        self.styled_err = self.terminal_err and not self.plain
        if self.styled_out or self.styled_err:
            if dark is None:
                dark = terminal_dark(self.out if self.styled_out else self.err, getenv) if terminal is None \
                    else dark_from(None, getenv)
            self.st = Styles(color_profile(getenv), dark)
        else:
            self.st = Styles(None)
        self._width = width
        self._height = height
        self._drawn_at = 0  # the width the live rows were last drawn at
        self.now = now or time.monotonic
        self.tick = tick
        self._lock = threading.RLock()
        self._steps: list[Step] = []
        self._drawn: list[int] = []
        self._open = False
        self._tail = ""
        self._state = _State()

    # -- styles of a phrase

    def _paint_out(self, name: str, s: str) -> str:
        return self.st.paint(name, s) if self.styled_out else s

    def bold(self, s: str) -> str:
        """s in bold on a styled stdout: the command's name, a region, what matters."""
        return self._paint_out("title", s)

    def dim(self, s: str) -> str:
        """s muted on a styled stdout: a time, a detail, what supports a value."""
        return self._paint_out("muted", s)

    def label(self, s: str) -> str:
        """s as a table's first column shows it."""
        return self._paint_out("label", s)

    def key(self, s: str) -> str:
        """s as a command to run or a key to press shows."""
        return self._paint_out("key", s)

    accent = key
    """s in the accent color: a command named inside a sentence, a button's label, an address that is no link."""

    def mark(self, mark: Mark) -> str:
        """mark as stdout shows it: its glyph in its color, or its ASCII form."""
        return self._paint_out(mark.style, mark.symbol(self.styled_out))

    def running_mark(self) -> str:
        """The running mark a line printed once shows: the spinner still, at its frame ⠴, or the ASCII form."""
        return self.st.paint("key", SPINNER[5]) if self.styled_out else Mark.RUNNING.symbol(False)

    def link(self, text: str, target: str) -> str:
        """text as a terminal link to target (an OSC 8 hyperlink in the accent color), a path as a file:// address;
        text itself off a styled stdout."""
        if not self.styled_out or not text:
            return text
        if os.path.isabs(target):
            target = "file://" + quote(target.replace(os.sep, "/"))
        return f"\x1b]8;;{target}\x1b\\{self.st.paint('key', text)}\x1b]8;;\x1b\\"

    # -- the terminal

    def width(self) -> int:
        """The width lines on stdout are fitted to: the terminal's, or 80 off a terminal or when it does not say."""
        if self._width is not None:
            return self._width()
        if self.terminal_out:
            try:
                return os.get_terminal_size(self.out.fileno()).columns or 80
            except (OSError, ValueError, AttributeError):
                return 80
        return 80

    def height(self) -> int:
        """The rows of the terminal stdout is, or 24 when it does not say."""
        if self._height is not None:
            return self._height()
        try:
            return os.get_terminal_size(self.out.fileno()).lines or 24
        except (OSError, ValueError, AttributeError):
            return 24

    # -- writing

    def _write(self, stream: IO[str], s: str) -> None:
        if stream is self.out:
            # A styled stdout shows one blank line where the lines printed would leave two.
            while self.styled_out and self._tail == "\n\n" and s.startswith("\n"):
                s = s[1:]
            if not s:
                return
            self._tail = (self._tail + s)[-2:]
        if self._open and stream is self.out:
            s = self._unlive() + s + self._live_text()
        elif self._open and self.terminal_out and self.terminal_err and stream is self.err:
            self.out.write(self._unlive())
            self.out.flush()
            stream.write(s)
            stream.flush()
            self.out.write(self._live_text())
            self.out.flush()
            return
        stream.write(s)
        stream.flush()

    def line(self, s: str = "") -> None:
        """Print s and a newline on stdout."""
        with self._lock:
            self._write(self.out, s + "\n")

    def lines(self, *lines: str) -> None:
        for s in lines:
            self.line(s)

    def block(self, text: str) -> None:
        """Print text on stdout as it is, with a newline added when it ends without one; nothing for no text."""
        if text:
            with self._lock:
                self._write(self.out, text if text.endswith("\n") else text + "\n")

    def note(self, s: str) -> None:
        """Print s on stderr, muted on a styled terminal: a line that is not the command's output."""
        with self._lock:
            self._write(self.err, (self.st.paint("muted", s) if self.styled_err else s) + "\n")

    def err_line(self, s: str) -> None:
        """Print s on stderr. On a styled stderr a line that opens with the program's name shows the name muted, a
        warning's word in the warning style and a command it names to run in the key style."""
        with self._lock:
            self._write(self.err, (self._prefixed(s) if self.styled_err else s) + "\n")

    def _prefixed(self, s: str) -> str:
        st = self.st
        if "\x1b" in s:
            return s
        lead = s[: len(s) - len(s.lstrip("\n"))]
        s = s[len(lead):]
        name = NAME + ":"
        if not s.startswith(name + " "):
            return lead + _commands(st, s)
        rest = s[len(name) + 1:]
        head = st.paint("muted", name) + " "
        if rest.startswith(_WARNING_WORD + " "):
            return lead + head + st.paint("strong_warning", _WARNING_WORD) + " " + _commands(
                st, rest[len(_WARNING_WORD) + 1:])
        return lead + head + _commands(st, rest)

    def warn(self, s: str) -> None:
        """Print "warning: " and s on stderr, and keep s as what to check, so the command ends done with warnings."""
        with self._lock:
            word = self.st.paint("strong_warning", _WARNING_WORD) if self.styled_err else _WARNING_WORD
            self._write(self.err, word + " " + (_commands(self.st, s) if self.styled_err else s) + "\n")
            self._state.checks.append(s)

    # -- pieces

    def header(self, title: str, about: str = "") -> None:
        """The command's header, on a terminal only: its name in bold, then what it acts on, muted, two spaces apart.
        A styled stdout sets it apart from what follows with a blank line."""
        if not self.terminal_out:
            return
        self.line(self.bold(title) + ("  " + self.dim(about) if about else ""))
        if self.styled_out:
            self.line("")

    def heading(self, title: str) -> None:
        """A heading inside a command, in bold on a styled stdout, after a blank line: one, never two."""
        self.line("")
        self.line(self._paint_out("section", title))

    def item(self, level: int, mark: Mark, text: str) -> None:
        """A line with a mark in front, indented by level steps of two spaces: a step's result, a row of a list."""
        self.line("  " * level + self.mark(mark) + " " + text)

    def table(self, rows: Sequence[Sequence[str]]) -> None:
        """Rows of cells, each column padded to its widest cell and two spaces between, the last cell unpadded. On a
        styled terminal the first column is muted as labels are, a last cell too wide for the terminal wraps under its
        column, and a command it names to run shows in the key style."""
        if not rows:
            return
        cols = max(len(r) for r in rows)
        widths = [0] * cols
        for r in rows:
            for i, cell in enumerate(r[:-1]):
                widths[i] = max(widths[i], visible_width(cell))
        width = self.width()
        out = []
        for r in rows:
            text, at = "", 0
            for i, cell in enumerate(r):
                if i == len(r) - 1:
                    text += _commands(self.st, _wrap(cell, at, width)) if self.styled_out else cell
                    break
                pad = widths[i] + 2 - visible_width(cell)
                text += (self.label(cell) if i == 0 else cell) + " " * pad
                at += widths[i] + 2
            out.append(text.rstrip() if not self.styled_out else text)
        self.block("\n".join(out))

    def tinted(self, text: str) -> list[str]:
        """text as one block, a line of it a row. On a styled stdout the block starts at the margin on the tint, each
        line with two tinted spaces before it and at least two after, padded to the block's width (the widest line
        and its four spaces, never wider than the terminal), in the terminal's own foreground. No line break is ever
        put inside a line, so a copy of the block is its lines. Plain, each line is set two spaces in, as it is."""
        lines = text.removesuffix("\n").split("\n")
        open_ = self.st.tint_opening() if self.styled_out else ""
        if not open_:
            return ["  " + line for line in lines]
        cols = max(self.width(), 1)
        side = 2
        block = min(max(visible_width(line) for line in lines) + 2 * side, cols)
        rows = []
        for line in lines:
            body = "  " + line.replace(RESET, RESET + open_)
            rows.append(open_ + body + " " * _tint_pad(side + visible_width(line), block, cols, side) + RESET)
        return rows

    def print_tinted(self, text: str) -> None:
        self.lines(*self.tinted(text))

    # -- steps

    def step(self, label: str, level: int = 0) -> Step:
        """Start a step named label, indented by level steps of two spaces. It takes the live line over from any
        step still running there."""
        return self._start(label, level, along=False)

    def step_along(self, label: str) -> Step:
        """Start a step beside the steps still running: each keeps a live row until it resolves."""
        return self._start(label, 0, along=True)

    def _start(self, label: str, level: int, along: bool) -> Step:
        step = Step(self, label, "  " * level)
        if not self.styled_out:
            return step
        with self._lock:
            unlive = self._unlive()
            self._steps = [*self._steps, step] if along else [step]
            self._open = True
            self.out.write(unlive + self._live_text())
            self.out.flush()
        step._spin()
        step._quiet = _quiet_input()
        return step

    def _live_text(self) -> str:
        now = self.now()
        rows = [row for step in self._steps for row in step.frame(now)]
        width = self.width()
        # live rows taller than the screen show their tail, as the progress region does: a redraw cannot reach a row
        # scrolled off the top, so a taller drawing would leave a copy of its first rows in the scrollback each time
        rows = [_fit(row, width) for row in rows][-max(self.height() - 1, 1):]
        self._drawn, self._drawn_at = [visible_width(row) for row in rows], width
        return "\n".join(rows)

    def _unlive(self) -> str:
        return "\r\x1b[K" + "\x1b[A\x1b[K" * max(_climb(self._drawn, self._drawn_at, self.width()) - 1, 0)

    def _redraw(self) -> None:
        if self._open:
            self.out.write(self._unlive() + self._live_text())
            self.out.flush()

    def _drop(self, step: Step) -> None:
        self._steps = [s for s in self._steps if s is not step]
        if not self._steps and self._open:
            self.out.write(self._unlive())
            self._open, self._drawn = False, []

    # -- the last line

    def next(self, what: str) -> None:
        """Say what to do next, for the done line. The last call wins."""
        self._state.next = what

    def check(self, s: str) -> None:
        """Keep s as something to check without printing it, so the command ends done with warnings."""
        self._state.checks.append(s)

    def last_line(self, *, failed: bool = False, run: str = "") -> str:
        """The command's last line: done and what's next, done with warnings and what to check, or failed and the
        one command to run. It is styled when stderr takes escape codes."""
        st = self.st if self.styled_err else Styles(None)

        def state(mark: Mark, word: str) -> str:
            strong = {Mark.DONE: "strong_success", Mark.FAILED: "strong_failure"}.get(mark, "strong_warning")
            return st.paint(mark.style, mark.symbol(st.on)) + " " + st.paint(strong, word)

        if failed:
            run = " ".join(part.strip() for part in run.split("\n") if part.strip())
            line = state(Mark.FAILED, "Failed.")
            return line + " " + st.paint("hint", "Run:") + " " + st.paint("key", run) if run else line
        if self._state.checks:
            check = _commands(st, " ".join(self._state.checks[0].split("\n")))
            if (n := len(self._state.checks) - 1) > 0:
                check += st.paint("muted", f" (and {n} more warning{'' if n == 1 else 's'} above)")
            return state(Mark.WARNING, "Done with warnings.") + " " + st.paint("hint", "Check:") + " " + check
        line = state(Mark.DONE, "Done.")
        if self._state.next:
            nxt = " ".join(self._state.next.split("\n"))
            nxt = st.paint("key", nxt) if nxt.startswith(NAME + " ") else _commands(st, nxt)
            line += " " + st.paint("hint", "Next:") + " " + nxt
        return line

    def end(self, *, failed: bool = False, run: str = "") -> None:
        """Print the last line on stderr."""
        with self._lock:
            self._write(self.err, self.last_line(failed=failed, run=run) + "\n")


_QUIET: list[Any] = []  # the terminal's modes as they were before the first live step turned its echo off, and a count


def _quiet_input() -> bool:
    """While a step runs at a terminal, what is typed is not drawn: its echo is off, so a key pasted again while
    the first is checked never shows. Only where stdin is the terminal in the foreground; its modes come back when the
    last step ends, and at exit whatever way the command ends. Whether it turned the echo off."""
    try:
        import termios

        fd = sys.stdin.fileno()
        if not os.isatty(fd) or os.tcgetpgrp(fd) != os.getpgrp():
            return False
        if _QUIET:
            _QUIET[1] += 1
            return True
        modes = termios.tcgetattr(fd)
        quiet = list(modes)
        quiet[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
    except (ImportError, OSError, ValueError, AttributeError):
        return False
    _QUIET[:] = [modes, 1]
    if not getattr(_quiet_input, "registered", False):
        import atexit

        atexit.register(_loud_input, True)
        _quiet_input.registered = True  # type: ignore[attr-defined]
    return True


def _loud_input(all_: bool = False) -> None:
    """A live step ended: the last one gives the terminal its echo back."""
    if not _QUIET:
        return
    _QUIET[1] = 0 if all_ else _QUIET[1] - 1
    if _QUIET[1] > 0:
        return
    modes = _QUIET[0]
    _QUIET.clear()
    try:
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, modes)
    except (ImportError, OSError, ValueError, AttributeError):
        pass


class Step:
    """One piece of work a command shows while it runs. On a styled stdout it is a live line with a spinner and the
    time so far, redrawn every tenth of a second, and it resolves to a line with a mark and the time it took. Elsewhere
    it prints its resolved line alone, with an ASCII mark. Sub-lines under it are drawn muted, four spaces in."""

    def __init__(self, printer: Printer, label: str, indent: str) -> None:
        self.p = printer
        self.label = label
        self.indent = indent
        self.subs: list[str] = []
        self.start = printer.now()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._ended = False
        self._quiet = False  # whether this step turned the terminal's echo off

    def _spin(self) -> None:
        def turn() -> None:
            while not self._done.wait(self.p.tick):
                try:
                    with self.p._lock:
                        if self.p._steps and self.p._steps[0] is self:
                            self.p._redraw()
                except OSError as error:
                    gone(error)
                    return
                except Exception:  # noqa: BLE001 - a fault of the drawing stops the spinner, never the command
                    return

        def gone(error: OSError) -> None:
            """The terminal the step draws on is gone, as when its window closes or an SSH session drops with no
            SIGHUP to say so: the command ends with 129, as on SIGHUP, rather than wait out the work for no one."""
            import errno

            if error.errno == errno.EIO:
                os._exit(129)

        self._thread = threading.Thread(target=turn, name="cerebro-step", daemon=True)
        self._thread.start()

    def frame(self, now: float) -> list[str]:
        """The step's live rows at now: its line, then its sub-lines."""
        took = now - self.start
        st = self.p.st
        rows = [self.indent + st.paint("key", spinner(took)) + " " + self.label + "  " + st.paint("muted", elapsed(took))]
        rows += [self.indent + "    " + st.paint("muted", sub) for sub in self.subs]
        return rows

    def lines(self, *lines: str) -> None:
        """Put lines under the step as its sub-lines, in place of those it had."""
        with self.p._lock:
            self.subs = list(lines)
            if self in self.p._steps:
                self.p._redraw()

    def relabel(self, label: str) -> None:
        """Name the running step label from now on."""
        with self.p._lock:
            self.label = label
            if self in self.p._steps:
                self.p._redraw()

    def done(self, text: str = "") -> int:
        return self.end(Mark.DONE, text)

    def fail(self, text: str = "") -> None:
        self.end(Mark.FAILED, text)

    def warn(self, text: str = "") -> None:
        self.end(Mark.WARNING, text)

    def skip(self, text: str = "") -> None:
        self.end(Mark.QUIET, text)

    def collapse(self, mark: Mark, text: str = "") -> None:
        """Resolve as end does, leaving the sub-lines off: a count that sums them says it all."""
        self.end(mark, text, collapse=True)

    def _stop(self) -> bool:
        if self._ended:
            return False
        self._ended = True
        self._done.set()
        if self._thread is not None:
            self._thread.join()
        if getattr(self, "_quiet", False):
            self._quiet = False
            _loud_input()
        return True

    def clear(self) -> None:
        """End the step and leave no line of it."""
        if not self._stop():
            return
        with self.p._lock:
            if self in self.p._steps:
                self.p._drop(self)
                self.p._redraw()

    def end(self, mark: Mark, text: str = "", *, collapse: bool = False) -> int:
        """Resolve the step with mark and text, or its label when text is empty; the rows it printed."""
        if not self._stop():
            return 0
        p = self.p
        with p._lock:
            text = text or self.label
            took = p.now() - self.start
            subs = "" if collapse else "".join(self.indent + "    " + p.dim(sub) + "\n" for sub in self.subs)
            if p.styled_out:
                if self in p._steps:
                    p._drop(self)
                # broken at a space where it would pass the terminal's width with its time after it
                text = _wrap(text, len(self.indent) + 2, max(p.width() - len(elapsed(took)) - 2, 1))
                said = self.indent + p.mark(mark) + " " + text + "  " + p.dim(elapsed(took)) + "\n" + subs
            else:
                said = self.indent + mark.symbol(False) + " " + text + "\n" + subs
            p._write(p.out, said)
            return _climb([visible_width(line) for line in said.split("\n")[:-1]], 0, p.width())


def _climb(drawn: list[int], at: int, width: int) -> int:
    """The screen rows live rows drawn `drawn` columns wide at width `at` take now, at `width`: each its rows at the
    width now, where the terminal is as wide or wider; narrowed, one row each, the rows
    they were cut to fit, so a terminal that does not wrap them again, as xterm, keeps the lines above them. One that
    does wrap them again may keep a row of the last drawing then."""
    width = max(width, 1)
    if at and width < at:
        return len(drawn)
    return sum(max((w + width - 1) // width, 1) for w in drawn)


def _tint_pad(n: int, width: int, cols: int, side: int) -> int:
    """The spaces after a line of n columns that fill its last screen row to the block's width."""
    if n + side <= width:
        return width - n
    last = n % cols
    return width - last if last else 0


def _fit(s: str, cols: int) -> str:
    """s cut to fit a line of cols, with an ellipsis where it was cut: the live line never wraps."""
    room = cols - 1
    if room <= 0 or visible_width(s) <= room:
        return s
    return cut(s, room)


def fold(s: str, width: int, indent: str = "") -> list[str]:
    """A paragraph broken at its spaces into rows of at most width columns, each row after the first set in by
    `indent`, the styles open where a row breaks closed at its end and opened again on the next, so a word is never
    cut in two as a terminal's own wrap cuts it. A word wider than the room stays whole on a row of its own. A line
    that fits, or a width too small to wrap at, comes back as it is."""
    room = width - visible_width(indent)
    if width <= 0 or room < 20 or visible_width(s) <= width:
        return [s]
    rows: list[str] = []
    row, used, held = "", 0, ""
    for word in s.split(" "):
        w = visible_width(word)
        limit = width if not rows else room
        if row and used + 1 + w > limit:
            rows.append(row + (RESET if held else ""))
            row, used = held, 0
        elif row or used:
            row += " "
            used += 1
        row += word
        used += w
        for escape in _ESCAPE.finditer(word):
            code = escape.group()
            if code.startswith("\x1b["):
                held = "" if code in (RESET, "\x1b[m", "\x1b[39m", "\x1b[22m") else held + code
    rows.append(row)
    return [rows[0], *(indent + r for r in rows[1:])]


def cut(s: str, width: int) -> str:
    """s shortened to width columns as a terminal shows them, with an ellipsis where it was cut, keeping the styles
    of what it keeps; s itself when it fits."""
    if width <= 1 or visible_width(s) <= width:
        return s
    out, used, at, styled = "", 0, 0, False
    while at < len(s):
        escape = _ESCAPE.match(s, at)
        if escape:
            out += escape.group()
            styled = True
            at = escape.end()
            continue
        if used + cell_len(s[at]) > width - 1:
            break
        out += s[at]
        used += cell_len(s[at])
        at += 1
    return out + "…" + (RESET if styled else "")


def _wrap(s: str, at: int, cols: int) -> str:
    """s broken at spaces so each line fits cols when it starts at column at, the lines after the first indented to
    at. A word longer than the room stays whole, and so does the command s names to run."""
    room = cols - at - 1
    if room < 20 or visible_width(s) <= room:
        return s
    cmd = command_in(s)
    if cmd:
        s = s.replace(cmd, cmd.replace(" ", "\x00"), 1)
    lines: list[str] = []
    line = ""
    for word in s.split():
        if not line:
            line = word
        elif visible_width(line) + 1 + visible_width(word) > room:
            lines.append(line)
            line = word
        else:
            line += " " + word
    lines.append(line)
    return ("\n" + " " * at).join(lines).replace("\x00", " ")
