"""The checkbox list a person answers in the terminal, beside the menu they open one row of and the pager they read.

It is drawn with Textual. A list is rows under group headers, the groups
under region headers, each row with its detail line under it. Up and down (or k and j) move, space toggles the row
the cursor is on, or every row under a header, `a` checks all or none, right or `l` opens a group, left or `h` closes
it, Enter takes the checked rows and Escape (or Ctrl-C) takes nothing; in a list that rejects, `r` names
the row to reject and ends it. A header's box is `[x]` when every row under
it is checked, `[ ]` for none and `[-]` for some, and a group of more than ten rows starts closed, its header counting
the checked rows. A list taller than the terminal scrolls, a line counting the rows out of view above or below.

`Model`, `Menu` and `Pager` are the logic and the frames, with no terminal. `ChecklistView`, `MenuView` and
`PagerView` are the Textual widgets that draw them, and `ask`, `choose` and `read` run each inline under the prompt.
Off a terminal, with `plain=True` or with `NO_COLOR` set, `ask` raises `NoRawKeys` and the caller prints the list
with `write_plain` and asks on a plain line.

The list knows nothing of what its rows are: it takes rows, groups and text and returns which rows are checked.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual._xterm_parser import XTermParser
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.theme import Theme
from textual.widget import Widget

from cerebro.errors import CerebroError
from cerebro.output import Printer, Styles, cut, is_terminal, visible_width

__all__ = [
    "DETAIL_INDENT",
    "FOLD_OVER",
    "PLAIN_ROWS",
    "Checklist",
    "ChecklistApp",
    "ChecklistView",
    "Field",
    "FieldAsk",
    "FieldBack",
    "FieldView",
    "Group",
    "Key",
    "Menu",
    "MenuApp",
    "MenuList",
    "MenuRow",
    "MenuView",
    "Model",
    "NoRawKeys",
    "Pager",
    "PagerApp",
    "PagerView",
    "Row",
    "ask",
    "choose",
    "count_of",
    "field",
    "hard_wrap",
    "legend",
    "read",
    "write_plain",
]

FOLD_OVER = 10
"""The most rows a group of a folding list starts open with."""
PLAIN_ROWS = 5
"""How many rows of a longer group the plain list prints."""
DETAIL_INDENT = "      "
"""Sets a row's detail under its label."""
AFTER_INDENT = "  "
"""Sets the lines after the selection line in from the margin."""
LEGEND_GAP = "   "


class ListError(CerebroError):
    """A list ended on an error inside it."""


class NoRawKeys(CerebroError):
    """The terminal cannot take raw keys, or plain text was asked for: print the list with write_plain and ask on a
    plain line instead."""


class Key(Enum):
    """A key the lists read."""

    UP = "up"
    DOWN = "down"
    TOGGLE = "toggle"
    ALL = "all"
    ENTER = "enter"
    ESC = "esc"
    OPEN = "open"
    CLOSE = "close"
    REJECT = "reject"


# ---- what a list shows ---------------------------------------------------------------------------------------------


@dataclass
class Row:
    """One row: its label, whether it starts checked, and its detail, a line under it the cursor never rests on.
    `more` are lines under the detail, and `more_off`, when set, the lines shown in their place while it is unchecked.
    """

    label: str
    checked: bool = True
    detail: str = ""
    more: Sequence[str] = ()
    more_off: Sequence[str] = ()

    def under(self) -> bool:
        return bool(self.detail or self.more)

    def more_line(self, k: int, on: bool) -> str:
        return self.more_off[k] if not on and k < len(self.more_off) else self.more[k]


@dataclass
class Group:
    """A header and the rows under it, by their place in `Checklist.rows`. `heading` is the header of the region a
    folding list shows the group in, or "": groups one after another with one region show under one header for it.
    A header holds the group's name, then what it draws on after two spaces, shown muted."""

    header: str
    rows: Sequence[int]
    heading: str = ""
    closed: bool = False  # it starts closed, however few rows it holds
    shown: bool = True  # its rows are drawn; a group not shown is toggled through its region's line alone


@dataclass
class Checklist:
    """What a checklist shows: `head` above the rows (the first line a title), the rows under their groups, `note`
    pinned under the rows, then the selection line with `counts(checked)` after it (the estimate text it is given),
    `after` under it, and the keys. `verb` is the action Enter takes, `esc` what the legend says Escape does. `fold`
    makes headers rows the cursor rests on, each with a box, and a group of more than FOLD_OVER rows starts closed.
    `toggled`, when set, is told the rows a key just set and every row's check, and returns every row's check from
    then on, so a row that sits under another can follow it. `reject`, when set, is told the row `r` is pressed on, and
    the list ends as Escape ends it."""

    rows: Sequence[Row]
    groups: Sequence[Group]
    head: Sequence[str] = ()
    note: Sequence[str] = ()
    counts: Callable[[list[bool]], str] | None = None
    after: Sequence[str] = ()
    verb: str = "Approve"
    esc: str = "Cancel and quit"
    fold: bool = True
    toggled: Callable[[list[int], bool, list[bool]], list[bool]] | None = None
    reject: Callable[[int], None] | None = None

    def opens_heading(self, gi: int) -> bool:
        g = self.groups[gi]
        return self.fold and bool(g.heading) and (gi == 0 or self.groups[gi - 1].heading != g.heading)


# ---- drawing -------------------------------------------------------------------------------------------------------


class _Draw:
    """The parts of a list drawn in the named styles, or as plain text with no styles."""

    _PARTS: ClassVar[dict[str, str]] = {
        "title": "section", "strong": "title", "muted": "muted", "key": "key", "checked": "success",
    }

    def __init__(self, styles: Styles | None) -> None:
        self.styles = styles if styles is not None and styles.on else None

    @property
    def on(self) -> bool:
        return self.styles is not None

    def __call__(self, part: str, s: str) -> str:
        return self.styles.paint(self._PARTS[part], s) if self.styles and s else s

    def key_help(self, key: str, what: str) -> str:
        return self("key", key) + self("muted", " " + what)


def legend(styles: Styles | None, *keys: tuple[str, str]) -> str:
    """The line of keys under a list, a menu or a pager: each key in the key style and what it does muted."""
    draw = _Draw(styles)
    return LEGEND_GAP.join(draw.key_help(k, what) for k, what in keys)


def count_of(n: int, of: int) -> str:
    """How a header counts its rows: how many are checked of how many, as 2,836 of 2,841."""
    return f"{n:,} of {of:,}"


def _rows(s: str, width: int) -> int:
    n = visible_width(s)
    return 1 if width <= 0 or n <= width else (n + width - 1) // width


def _selection_line(checked: list[bool]) -> str:
    return f"{sum(checked)} of {len(checked)} selected"


# ---- the checklist -------------------------------------------------------------------------------------------------

_TEXT, _ROW, _DETAIL, _GROUP, _HEADING = range(5)


@dataclass
class _Line:
    kind: int
    text: str = ""
    at: int = 0
    more: int = 0
    depth: int = 0


class Model:
    """A list being answered: the cursor, which rows are checked, which groups are open, how far it has scrolled."""

    def __init__(self, checklist: Checklist) -> None:
        self.list = checklist
        self.checked = [r.checked for r in checklist.rows]
        # a group with no header has nothing to open it by, so it never starts closed, however many rows it holds
        self.open = [not checklist.fold or not g.header or len(g.rows) <= FOLD_OVER and not g.closed
                     for g in checklist.groups]
        self.cursor = 0
        self.top = 0
        self.lines: list[_Line] = []
        self.build()

    def build(self) -> None:
        """Lay out the body for the groups open now."""
        lst = self.list
        self.lines = []
        for gi, g in enumerate(lst.groups):
            if gi > 0:
                self.lines.append(_Line(_TEXT))
            depth = 1 if lst.fold and g.heading else 0
            if lst.opens_heading(gi):
                self.lines.append(_Line(_HEADING, g.heading, gi))
            if g.header:
                self.lines.append(_Line(_GROUP, g.header, gi, depth=depth))
                if lst.fold:
                    depth += 1
            if not self.open[gi] or not g.shown:
                continue
            for i in g.rows:
                self.lines.append(_Line(_ROW, at=i, depth=depth))
                if lst.rows[i].detail:
                    self.lines.append(_Line(_DETAIL, lst.rows[i].detail, i, depth=depth))
                for k in range(len(lst.rows[i].more)):
                    self.lines.append(_Line(_DETAIL, at=i, more=k + 1, depth=depth))

    def _spaced(self, styled: bool) -> list[_Line]:
        """The body as drawn: with styles, a row that has a line under it is set apart from the next by a blank."""
        if not styled:
            return self.lines
        out: list[_Line] = []
        for n, ln in enumerate(self.lines):
            if ln.kind == _ROW and out and n > 0:
                before = self._row_before(n)
                if before is not None and self.list.rows[before].under():
                    out.append(_Line(_TEXT))
            out.append(ln)
        return out

    def _row_before(self, n: int) -> int | None:
        """The row whose lines come right before body line n, when it is in the same group."""
        for ln in reversed(self.lines[:n]):
            if ln.kind == _DETAIL:
                continue
            return ln.at if ln.kind == _ROW else None
        return None

    def stops(self, lines: list[_Line] | None = None) -> list[int]:
        """The lines the cursor rests on, by their place in the body: the rows, and a folding list's headers."""
        lines = self.lines if lines is None else lines
        return [i for i, ln in enumerate(lines)
                if ln.kind == _ROW or self.list.fold and ln.kind in (_GROUP, _HEADING)]

    def under(self, ln: _Line) -> list[int]:
        """The rows a line toggles: its row, its group's rows, or the rows of every group of its region."""
        groups = self.list.groups
        if ln.kind == _ROW:
            return [ln.at]
        if ln.kind == _GROUP:
            return list(groups[ln.at].rows)
        if ln.kind == _HEADING:
            out: list[int] = []
            gi = ln.at
            while gi < len(groups) and groups[gi].heading == groups[ln.at].heading:
                out += groups[gi].rows
                gi += 1
            return out
        return []

    def _group_of(self, i: int) -> int:
        return next((gi for gi, g in enumerate(self.list.groups) if i in g.rows), -1)

    def _move_to(self, kind: int, at: int) -> None:
        for si, i in enumerate(self.stops()):
            if self.lines[i].kind == kind and self.lines[i].at == at:
                self.cursor = si
                return

    def at(self) -> _Line | None:
        """The line the cursor is on."""
        stops = self.stops()
        return self.lines[stops[self.cursor]] if stops else None

    def press(self, key: Key) -> tuple[bool, bool]:
        """Apply one key. It reports whether the list is done, and whether Enter took it."""
        stops = self.stops()
        at = self.lines[stops[self.cursor]] if stops else _Line(_TEXT)
        if key is Key.UP:
            self.cursor = max(self.cursor - 1, 0)
        elif key is Key.DOWN:
            self.cursor = max(min(self.cursor + 1, len(stops) - 1), 0)
        elif key is Key.TOGGLE:
            rows = self.under(at)
            self._set(rows, self._count(rows) < len(rows))
        elif key is Key.OPEN:
            if at.kind == _GROUP and not self.open[at.at]:
                self.open[at.at] = True
                self.build()
        elif key is Key.CLOSE:
            gi = at.at if at.kind == _GROUP else self._group_of(at.at) if at.kind == _ROW and self.list.fold else -1
            if gi >= 0 and self.list.groups[gi].header and self.open[gi]:
                self.open[gi] = False
                self.build()
                self._move_to(_GROUP, gi)
        elif key is Key.ALL:
            n, of = self.selected()
            self._set(list(range(len(self.checked))), n < of)
        elif key is Key.REJECT and self.list.reject is not None and at.kind == _ROW:
            self.list.reject(at.at)
            return True, False
        elif key is Key.ENTER:
            return True, True
        elif key is Key.ESC:
            return True, False
        return False, False

    def _set(self, rows: list[int], on: bool) -> None:
        for i in rows:
            self.checked[i] = on
        if self.list.toggled is None or not rows:
            return
        got = self.list.toggled(list(rows), on, list(self.checked))
        if len(got) == len(self.checked):
            self.checked = list(got)

    def _count(self, rows: list[int]) -> int:
        return sum(1 for i in rows if self.checked[i])

    def selected(self) -> tuple[int, int]:
        """How many rows are checked, of how many there are."""
        return sum(self.checked), len(self.checked)

    def taken(self) -> list[bool]:
        return list(self.checked)

    def _foot(self, draw: _Draw, width: int = 0) -> list[str]:
        lst = self.list
        n, of = self.selected()
        verb = f"{lst.verb} all" if n == of else f"{lst.verb} selected"
        action = draw("key", "⏎") + " " + draw("strong", verb) + LEGEND_GAP + draw.key_help("esc", lst.esc)
        out = [""]
        if lst.note:
            out += [*lst.note, ""]
        tail = lst.counts(list(self.checked)) if lst.counts else ""
        out.append(_selection_line(self.checked) + draw("muted", tail))
        out += [AFTER_INDENT + draw("muted", a) for a in lst.after]
        if draw.on:
            out.append("")
        keys = draw.key_help("↑↓", "move") + LEGEND_GAP + draw.key_help("space", "toggle") + LEGEND_GAP
        if lst.fold and any(g.header for g in lst.groups):
            keys += draw.key_help("→←", "open or close") + LEGEND_GAP
        keys += draw.key_help("a", "all or none")
        if lst.reject is not None:
            keys += LEGEND_GAP + draw.key_help("r", "reject")
        # one line, keys then action, where both fit the width
        if width and visible_width(keys) + len(LEGEND_GAP) + visible_width(action) <= width:
            return [*out, keys + LEGEND_GAP + action]
        return [*out, keys, action]

    def render(self, width: int, height: int, cursor: bool = True, styles: Styles | None = None) -> list[str]:
        """The list drawn to fit height lines: the head, the body scrolled so the cursor shows, with a line for the
        rows above or below the view, and the foot pinned under it. A note's line may wrap; every other is cut."""
        draw = _Draw(styles)
        lst = self.list
        head = [draw("title" if i == 0 else "muted", part) for i, h in enumerate(lst.head)
                for part in (wrap_head(h, width) if i < 2 and width > 1 else [h])] + [""]
        foot = self._foot(draw, width)

        def is_note(i: int) -> bool:
            return 1 <= i <= len(lst.note)

        foot_rows = sum(_rows(line, width) if is_note(i) else 1 for i, line in enumerate(foot))
        room = max(height - 1 - len(head) - foot_rows, 3)
        lines = self._spaced(draw.on)
        stops = self.stops(lines)
        at = stops[self.cursor] if stops else 0
        view = min(room, len(lines))
        end = at
        while end + 1 < len(lines) and lines[end + 1].kind == _DETAIL and end + 2 - at <= view - 2:
            end += 1
        if end >= self.top + view - 1:
            self.top = end - view + 2
        if at <= self.top:
            self.top = at - 1
        self.top = max(min(self.top, len(lines) - view), 0)
        while self.top > 0 and self.top + 1 < len(lines) and lines[self.top + 1].kind == _DETAIL:
            self.top += 1
        # a group's lines show only under its heading: the view opens on the heading where the cursor still fits under
        # it, or else on the next heading down to the cursor
        if self.top > 0:
            up = next((i for i in range(self.top + 1, -1, -1) if lines[i].kind == _HEADING
                       or lines[i].kind == _GROUP and not lst.groups[lines[i].at].heading), 0)
            if lines[up].kind == _HEADING and up <= self.top:
                down = next((i for i in range(self.top + 1, at + 1) if lines[i].kind == _HEADING), len(lines))
                if end <= max(up - 1, 0) + view - 2:
                    self.top = max(up - 1, 0)
                elif down <= len(lines) - view + 1:
                    self.top = down - 1
        last = min(self.top + view, len(lines))
        while last < len(lines) and lines[last - 1].kind == _DETAIL and last - 2 > max(end, at):
            last -= 1

        def rows_in(start: int, stop: int) -> int:
            n = 0
            for ln in lines[start:stop]:
                if ln.kind == _ROW:
                    n += 1
                elif ln.kind == _GROUP and not self.open[ln.at]:
                    n += len(lst.groups[ln.at].rows)
            return n

        body = []
        on = stops[self.cursor] if cursor and stops else -1
        for i in range(self.top, last):
            if i == self.top and self.top > 0:
                body.append("  " + draw("muted", f"… {rows_in(0, self.top + 1)} more"))
            elif i == last - 1 and last < len(lines):
                body.append("  " + draw("muted", f"… {rows_in(i, len(lines))} more"))
            else:
                body.append(self._line(lines[i], i == on, draw))
        out = [cut(line, width) for line in head + body]
        return out + [line if is_note(i) else cut(line, width) for i, line in enumerate(foot)]

    def _line(self, ln: _Line, on: bool, draw: _Draw) -> str:
        lst = self.list
        pad = "  " * ln.depth
        if ln.kind == _DETAIL:
            text = lst.rows[ln.at].more_line(ln.more - 1, self.checked[ln.at]) if ln.more else ln.text
            return DETAIL_INDENT + pad + draw("muted", text)
        if ln.kind == _TEXT:
            return ln.text
        if ln.kind in (_GROUP, _HEADING) and not lst.fold:
            return self._header(ln.text, draw)
        mark = draw("key", ">") + " " if on else "  "
        rows = self.under(ln)
        n = self._count(rows)
        box = draw("muted", "[ ]")
        if n == len(rows) and n > 0:
            box = draw("muted", "[") + draw("checked", "x") + draw("muted", "]")
        elif n > 0:
            box = draw("muted", "[") + draw("checked", "-") + draw("muted", "]")
        if ln.kind == _ROW:
            label = lst.rows[ln.at].label
            return mark + pad + box + " " + (draw("strong", label) if on else label)
        if ln.kind == _GROUP:
            fold, tail = "▾ ", ""
            if not self.open[ln.at]:
                fold, tail = "▸ ", "  " + draw("muted", count_of(n, len(rows)))
            return mark + pad + box + " " + fold + self._header(ln.text, draw) + tail
        return mark + pad + box + " " + draw("title", ln.text) + "  " + draw("muted", count_of(n, len(rows)))

    @staticmethod
    def _header(text: str, draw: _Draw) -> str:
        name, sep, rest = text.partition("  ")
        return draw("title", name) + "  " + draw("muted", rest) if sep else draw("title", text)


def write_plain(checklist: Checklist) -> str:
    """The list as it starts, each row checked or not, as plain text: for a terminal that cannot take raw keys, for
    plain output, and for a run that asks nothing. A folding list prints the first PLAIN_ROWS rows of a longer group
    and counts the rest."""
    lst = checklist
    checked = [r.checked for r in lst.rows]
    out = [*lst.head]
    for gi, g in enumerate(lst.groups):
        out.append("")
        if lst.opens_heading(gi):
            n = of = 0
            for sg in lst.groups[gi:]:
                if sg.heading != g.heading:
                    break
                of += len(sg.rows)
                n += sum(1 for i in sg.rows if checked[i])
            out.append(f"{g.heading}  {count_of(n, of)}")
        rows = list(g.rows)
        cut_at = PLAIN_ROWS if lst.fold and len(rows) > FOLD_OVER else len(rows)
        if g.header:
            tail = f"  {count_of(sum(1 for i in rows if checked[i]), len(rows))}" if cut_at < len(rows) else ""
            out.append(g.header + tail)
        for i in rows[:cut_at]:
            row = lst.rows[i]
            out.append(f"  {'[x]' if checked[i] else '[ ]'} {row.label}")
            if row.detail:
                out.append(DETAIL_INDENT + row.detail)
            out += [DETAIL_INDENT + row.more_line(k, checked[i]) for k in range(len(row.more))]
        if cut_at < len(rows):
            out.append(f"  … {len(rows) - cut_at:,} more")
    if lst.note:
        out += ["", *lst.note]
    tail = lst.counts(checked) if lst.counts else ""
    out += ["", _selection_line(checked) + tail]
    out += [AFTER_INDENT + a for a in lst.after]
    return "\n".join(out) + "\n"


# ---- the menu ------------------------------------------------------------------------------------------------------


@dataclass
class MenuRow:
    """One row of a menu: its title, a word for where it stands and a reason, drawn muted after the word. `mark` is a
    glyph drawn before the word, already in its color. An `inert` row is drawn muted and the cursor never lands on
    it. `below` are lines under the row, muted, set in under its state."""

    title: str
    state: str = ""
    reason: str = ""
    mark: str = ""
    inert: bool = False
    below: Sequence[str] = ()


@dataclass
class MenuList:
    """What a menu shows: `head` above the rows (a title, then an intro), the rows, and `end`, the title of an action
    row under them after a blank line, which Enter ends the menu on. `open` and `done` are what the legend says Enter
    and Escape do."""

    rows: Sequence[MenuRow]
    head: Sequence[str] = ()
    end: str = ""
    open: str = "open"
    done: str = "done"
    end_takes: bool = False  # Enter on the end row gives its place, len(rows), apart from Escape


MENU_DONE = -1
"""What a menu returns when it ends with no row opened: Escape, or Enter on the end row."""


class Menu:
    """A menu being answered: up and down (or k and j) move past inert rows, Enter opens the cursor's row, Escape or
    Enter on the end row ends it."""

    def __init__(self, menu: MenuList, cursor: int = 0) -> None:
        self.list = menu
        self.cursor = max(min(cursor, len(menu.rows) if menu.end else len(menu.rows) - 1), 0)  # the end row too
        self.top = 0
        self._settle()

    def _lands(self, i: int) -> bool:
        if i == len(self.list.rows):
            return bool(self.list.end)
        return 0 <= i < len(self.list.rows) and not self.list.rows[i].inert

    def _settle(self) -> None:
        if self._lands(self.cursor):
            return
        for i in [*range(self.cursor + 1, len(self.list.rows)), *range(self.cursor - 1, -1, -1)]:
            if self._lands(i):
                self.cursor = i
                return
        if self._lands(len(self.list.rows)):
            self.cursor = len(self.list.rows)

    def on_end(self) -> bool:
        return bool(self.list.end) and self.cursor == len(self.list.rows)

    def press(self, key: Key) -> tuple[bool, bool]:
        """Apply one key. It reports whether the menu is done, and whether Enter opened the cursor's row."""
        rows = self.list.rows
        last = len(rows) if self.list.end else len(rows) - 1
        if key is Key.UP:
            self.cursor = next((i for i in range(self.cursor - 1, -1, -1) if self._lands(i)), self.cursor)
        elif key is Key.DOWN:
            self.cursor = next((i for i in range(self.cursor + 1, last + 1) if self._lands(i)), self.cursor)
        elif key is Key.ENTER:
            if self.on_end():
                return True, self.list.end_takes
            if rows and rows[self.cursor].inert:
                return False, False
            return bool(rows), bool(rows)
        elif key is Key.ESC:
            return True, False
        return False, False

    def render(self, width: int, height: int, cursor: bool = True, styles: Styles | None = None) -> list[str]:
        """The menu drawn to fit height lines: the head, the rows scrolled so the cursor shows, each with its lines
        below it, a line for the rows out of view, the end row after a blank line, and the keys."""
        draw = _Draw(styles)
        lst = self.list
        head = [draw("title" if i == 0 else "muted", part) for i, h in enumerate(lst.head)
                for part in (wrap_head(h, width) if i < 2 and width > 1 else [h])] + [""]
        foot = ["", legend(styles, ("↑↓", "move"), ("⏎", lst.open), ("esc", lst.done))]
        end: list[str] = []
        if lst.end:
            on = cursor and self.on_end()
            end = ["", (draw("key", ">") + " " + draw("strong", lst.end)) if on else "  " + lst.end]
        titles = max((cell_len(r.title) for r in lst.rows), default=0)
        marks = max((visible_width(r.mark) for r in lst.rows), default=0)
        indent = 2 + titles + len(LEGEND_GAP) + (marks + 1 if marks else 0)
        # a row's lines below it wrap at their spaces under its state, so a long note is read whole, never cut
        belows = [[part for line in r.below for part in wrap_head(line, max(width - indent, 20))] for r in lst.rows]
        body = [(i, k) for i, r in enumerate(lst.rows) for k in range(len(belows[i]) + 1)]
        n = len(body)
        view = min(max(height - 1 - len(head) - len(end) - len(foot), max(3 - len(end), 1)), n)
        at = body.index((min(self.cursor, len(lst.rows) - 1), 0)) if lst.rows else 0
        last = at
        while last + 1 < n and body[last + 1][1] > 0:
            last += 1
        marked = view >= 3
        if marked:
            if last >= self.top + view - 1:
                self.top = last - view + 2
            if at <= self.top:
                self.top = at - 1
        elif at < self.top:
            self.top = at
        elif last >= self.top + view:
            self.top = last - view + 1
        self.top = max(min(self.top, n - view), 0)
        while marked and self.top > 0 and self.top + 1 < n and body[self.top + 1][1] > 0:
            self.top += 1
        to = min(self.top + view, n)
        while marked and to < n and to - 2 > last and body[to - 1][1] > 0:
            to -= 1

        def state(r: MenuRow) -> str:
            word = draw("muted", r.state)
            return word if marks == 0 else r.mark + " " * (marks - visible_width(r.mark)) + " " + word

        states = max((visible_width(state(r)) for r in lst.rows), default=0)
        out = list(head)
        for i in range(self.top, to):
            row, below = body[i]
            if marked and i == self.top and self.top > 0:
                out.append("  " + draw("muted", f"… {sum(1 for _, k in body[: self.top + 1] if k == 0)} more"))
            elif marked and i == to - 1 and to < n:
                out.append("  " + draw("muted", f"… {sum(1 for _, k in body[i:] if k == 0)} more"))
            elif below:
                out.append(" " * indent + draw("muted", belows[row][below - 1]))
            else:
                r = lst.rows[row]
                pointer, label = "  ", draw("muted", r.title) if r.inert else r.title
                if cursor and row == self.cursor:
                    pointer, label = draw("key", ">") + " ", draw("strong", r.title)
                if marks == 0 and not r.state and not r.reason:
                    out.append(pointer + label)
                    continue
                line = pointer + label + " " * (titles - cell_len(r.title)) + LEGEND_GAP + state(r)
                if r.reason:
                    line += " " * (states - visible_width(state(r))) + LEGEND_GAP + draw("muted", r.reason)
                out.append(line)
        return [cut(line, width) for line in out + end + foot]


# ---- the pager -----------------------------------------------------------------------------------------------------


class Pager:
    """A view of text with nothing to choose, read until Escape. Text that fits is drawn whole, a blank line and
    `esc back` under it. Taller text shows as many lines as fit above the legend, pinned on the last row as
    `↑↓ scroll   esc back`, and up and down (or k and j) move it a line at a time, stopping at either end. Each line is
    written whole, a line wider than the screen the terminal's to wrap, and counted in the rows it takes."""

    def __init__(self, lines: Callable[[int], list[str]] | Sequence[str]) -> None:
        self._lines = lines if callable(lines) else (lambda _width, fixed=list(lines): fixed)
        self.top = 0

    def render(self, width: int, height: int, styles: Styles | None = None) -> list[str]:
        lines = list(self._lines(width))
        if sum(_rows(line, width) for line in lines) + 2 <= height:
            self.top = 0
            return [*lines, "", legend(styles, ("esc", "back"))]
        room = max(height - 1, 1)
        used, last_top = 0, 0
        for i in range(len(lines) - 1, -1, -1):
            used += _rows(lines[i], width)
            if used > room:
                last_top = i + 1
                break
        self.top = max(min(self.top, last_top), 0)
        out: list[str] = []
        used = 0
        for line in lines[self.top:]:
            r = _rows(line, width)
            if used + r > room and out:
                break
            out.append(line)
            used += r
        return [*out, legend(styles, ("↑↓", "scroll"), ("esc", "back"))]

    def press(self, key: Key) -> tuple[bool, bool]:
        if key is Key.UP:
            self.top = max(self.top - 1, 0)
        elif key is Key.DOWN:
            self.top += 1
        return key is Key.ESC, False


# ---- the field ---------------------------------------------------------------------------------------------------


@dataclass
class FieldAsk:
    """One answer a view asks for at its prompt: the prompt, the
    default Enter takes on an empty line and where it comes from, or a hint in brackets when there is no default; a
    hidden answer echoes nothing. A `yes_no` prompt answers at y or n, and Enter takes `default_yes`. `enter` and `esc`
    are what the legend under the prompt says those keys do: `submit` and `back` unless a view says otherwise."""

    prompt: str
    default: str = ""
    source: str = ""
    hint: str = ""
    hidden: bool = False
    yes_no: bool = False
    default_yes: bool = False
    enter: str = "submit"
    esc: str = "back"


FIELD_TAKEN, FIELD_BACK, FIELD_CANCEL = "taken", "back", "cancel"
MAX_FIELD_BYTES = 64 * 1024  # the most bytes a line at a view's prompt holds: text that would pass it is not taken


class Field:
    """A view's prompt being answered: the prompt with its hint, the line typed after it,
    then a blank line and the legend of its keys. Text typed or pasted is added, its control characters left out;
    Backspace erases a character and Ctrl-U the line. Enter takes the line, or the default on an empty one; with no
    default an empty Enter does nothing. Escape goes back and Ctrl-C cancels. At a yes or no prompt y or n answer at
    once and every other key but Enter, Escape and Ctrl-C does nothing. `state` is None while it waits, then
    FIELD_TAKEN with `answer`, FIELD_BACK or FIELD_CANCEL."""

    def __init__(self, ask: FieldAsk) -> None:
        self.ask = ask
        self.typed = ""
        self.state: str | None = None
        self.answer = ""

    def hint(self) -> str:
        a = self.ask
        if a.yes_no:
            return "[Y/n]" if a.default_yes else "[y/N]"
        if a.default:
            return f"[{a.default}, {a.source}]" if a.source else f"[{a.default}]"
        return f"[{a.hint}]" if a.hint else ""

    def label(self, draw: _Draw) -> str:
        hint = self.hint()
        return self.ask.prompt + (" " + draw("muted", hint) if hint else "") + ": "

    def _take(self, answer: str) -> bool:
        self.state, self.answer = FIELD_TAKEN, answer
        return True

    def type(self, text: str) -> bool:
        """Add typed or pasted text; at a yes or no prompt, answer at its first y or n. Whether it ended."""
        text = "".join(c for c in text if c >= " " and c != "\x7f")
        if self.ask.yes_no:
            for c in text:
                if c in "yY":
                    return self._take("y")
                if c in "nN":
                    return self._take("n")
            return False
        if len((self.typed + text).encode()) > MAX_FIELD_BYTES:
            return False
        self.typed += text
        return False

    def erase(self) -> None:
        if not self.ask.yes_no:
            self.typed = self.typed[:-1]

    def kill(self) -> None:
        if not self.ask.yes_no:
            self.typed = ""

    def enter(self) -> bool:
        if self.ask.yes_no:
            return self._take("y" if self.ask.default_yes else "n")
        typed = self.typed.strip()
        if typed:
            return self._take(typed)
        if self.ask.default:
            return self._take(self.ask.default)
        return False

    def back(self) -> bool:
        self.state = FIELD_BACK
        return True

    def cancel(self) -> bool:
        self.state = FIELD_CANCEL
        return True

    def shown(self) -> str:
        return "" if self.ask.hidden else self.typed

    def render(self, width: int, height: int, cursor: bool = True, styles: Styles | None = None) -> list[str]:
        """The prompt and what is typed after it, then a blank line and the legend; once
        answered, the prompt's line alone, with the answer when it is not hidden; once gone back, nothing."""
        draw = _Draw(styles)
        if self.state == FIELD_BACK:
            return []
        if self.state == FIELD_TAKEN:
            return [self.label(draw) + ("" if self.ask.hidden else self.answer)]
        keys = legend(styles, ("⏎", self.ask.enter), ("esc", self.ask.esc))
        return [*hard_wrap(self.label(draw) + self.shown(), max(width - 1, 2)), "", cut(keys, max(width - 1, 2))]


def wrap_head(s: str, width: int) -> list[str]:
    """A head line broken into rows of at most width columns at its spaces: a word
    wider than width alone, such as a long path, is cut into rows of width."""
    rows: list[str] = []
    for para in s.split("\n"):
        row = ""
        for i, word in enumerate(para.split(" ")):
            if visible_width(word) > width:
                if i > 0:
                    rows.append(row)
                parts = hard_wrap(word, width)
                rows.extend(parts[:-1])
                row = parts[-1]
                continue
            if i == 0:
                row = word
            elif visible_width(row) + 1 + visible_width(word) <= width:
                row += " " + word
            else:
                rows.append(row)
                row = word
        rows.append(row)
    return rows


def hard_wrap(s: str, width: int) -> list[str]:
    """s cut into rows of width columns as a terminal wraps it, each row keeping the styles open where it starts."""
    from cerebro.output import _ESCAPE, RESET

    rows: list[str] = []
    row, used, held, at = "", 0, "", 0
    while at < len(s):
        escape = _ESCAPE.match(s, at)
        if escape:
            row += escape.group()
            held = "" if escape.group() in (RESET, "\x1b[m") else held + escape.group()
            at = escape.end()
            continue
        w = cell_len(s[at])
        if used + w > width and used:
            rows.append(row + (RESET if held else ""))
            row, used = held, 0
        row += s[at]
        used += w
        at += 1
    rows.append(row)
    return rows


# ---- on Textual ----------------------------------------------------------------------------------------------------

_KEYS: list[BindingType] = [
    Binding("up,k", "press('up')", show=False),
    Binding("down,j", "press('down')", show=False),
    Binding("space", "press('toggle')", show=False),
    Binding("a,A", "press('all')", show=False),
    Binding("right,l", "press('open')", show=False),
    Binding("left,h", "press('close')", show=False),
    Binding("r,R", "press('reject')", show=False),
    Binding("enter,ctrl+j", "press('enter')", show=False),  # a newline is Enter too
    Binding("escape,ctrl+c", "press('esc')", show=False),
]

class _KeysView[T](Widget, can_focus=True):
    """A widget that draws a model's frame at the terminal's size and hands it the keys pressed; when the model is
    done it draws a last frame with no cursor and ends the app with what the model returns."""

    DEFAULT_CSS = """
    _KeysView { height: auto; }
    """
    BINDINGS: ClassVar[list[BindingType]] = _KEYS

    def __init__(self, model: Any, styles: Styles | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.model = model
        self.styles_ = styles
        self.done = False

    def frame(self) -> list[str]:
        width, height = self.app.size
        return self._render_lines(width, height, not self.done)

    def _render_lines(self, width: int, height: int, cursor: bool) -> list[str]:
        raise NotImplementedError

    def render(self) -> Text:
        return Text("\n").join(Text.from_ansi(line, no_wrap=True) for line in self.frame())

    def get_content_height(self, container: Any, viewport: Any, width: int) -> int:
        return len(self.frame())

    def _result(self, entered: bool) -> T | None:
        raise NotImplementedError

    def on_key(self, event: events.Key) -> None:
        """Keys a terminal sends that no binding names: an arrow up or down with any modifier moves as the arrow
        does, since a CSI ending in A or B is an arrow; and an Escape with a character after it in one read, which
        Textual gives as alt and that character, is Escape, since an Escape not followed by [ or O starts no sequence
        and stands alone. Any other key Textual names with alt, as Alt with Left, Home or F1, is a CSI that no key
        here means, and so does nothing."""
        held, _, key = event.key.rpartition("+")
        if held and key in ("up", "down"):
            press = key
        elif "alt" in held.split("+") and not _CSI_KEY.fullmatch(key) and event.key != "alt+shift+o":
            press = "esc"  # Escape and a character; Escape and O begins an SS3, which waits for more
        else:
            return
        event.stop()
        event.prevent_default()
        self.action_press(press)

    def action_press(self, key: str) -> None:
        if self.done:
            return
        finished, entered = self.model.press(Key(key))
        if finished:
            self.done = True
            result = self._result(entered)
            self.refresh(layout=True)
            self.call_after_refresh(self.app.exit, result)
            return
        self.refresh(layout=True)


class ChecklistView(_KeysView[list[bool]]):
    """The checklist as a Textual widget: it ends the app with the checked rows, by their place in the rows, or with
    None for Escape."""

    DEFAULT_CSS = "ChecklistView { height: auto; }"

    def __init__(self, checklist: Checklist, styles: Styles | None = None, **kwargs: Any) -> None:
        super().__init__(Model(checklist), styles, **kwargs)

    def _render_lines(self, width: int, height: int, cursor: bool) -> list[str]:
        return self.model.render(width, height, cursor, self.styles_)

    def _result(self, entered: bool) -> list[bool] | None:
        return self.model.taken() if entered else None


class MenuView(_KeysView[int]):
    """A menu as a Textual widget: it ends the app with the opened row's place, or MENU_DONE."""

    DEFAULT_CSS = "MenuView { height: auto; }"

    def __init__(self, menu: MenuList, styles: Styles | None = None, cursor: int = 0, **kwargs: Any) -> None:
        super().__init__(Menu(menu, cursor), styles, **kwargs)

    def _render_lines(self, width: int, height: int, cursor: bool) -> list[str]:
        return self.model.render(width, height, cursor, self.styles_)

    def _result(self, entered: bool) -> int:
        return self.model.cursor if entered else MENU_DONE


class PagerView(_KeysView[None]):
    """A pager as a Textual widget, read until Escape."""

    DEFAULT_CSS = "PagerView { height: auto; }"

    def __init__(self, lines: Callable[[int], list[str]] | Sequence[str], styles: Styles | None = None,
                 **kwargs: Any) -> None:
        super().__init__(Pager(lines), styles, **kwargs)

    def _render_lines(self, width: int, height: int, cursor: bool) -> list[str]:
        # a line wider than the screen takes the rows the terminal would wrap it into, as the pager counts them, so the
        # legend under the text is never pushed out of the view
        return [row for line in self.model.render(width, height, self.styles_) for row in hard_wrap(line, width)]

    def _result(self, entered: bool) -> None:
        return None


class FieldView(_KeysView[str], inherit_bindings=False):
    """A view's prompt as a Textual widget, which reads these keys raw: text typed or pasted,
    Backspace, Ctrl-U, Enter, Escape and Ctrl-C. No list key moves anything here: j, k, a and space are text."""

    DEFAULT_CSS = "FieldView { height: auto; }"
    BINDINGS: ClassVar[list[BindingType]] = []  # every key is read in order in on_key: a binding would jump the queue
    _KEYS: ClassVar[dict[str, str]] = {"enter": "enter", "ctrl+j": "enter", "ctrl+m": "enter", "escape": "back",
                                       "ctrl+c": "cancel", "backspace": "erase", "ctrl+h": "erase", "ctrl+u": "kill"}

    def __init__(self, ask: FieldAsk, styles: Styles | None = None, **kwargs: Any) -> None:
        super().__init__(Field(ask), styles, **kwargs)

    def _render_lines(self, width: int, height: int, cursor: bool) -> list[str]:
        return self.model.render(width, height, cursor, self.styles_)

    def _result(self, entered: bool) -> str | None:
        return self.model.state

    def _after(self, finished: bool) -> None:
        if finished:
            self.done = True
            self.refresh(layout=True)
            self.call_after_refresh(self.app.exit, self.model.state)
            return
        self.refresh(layout=True)

    def action_field(self, what: str) -> None:
        if self.done:
            return
        model: Field = self.model
        finished = {"enter": model.enter, "back": model.back, "cancel": model.cancel}.get(what, lambda: False)()
        if what == "erase":
            model.erase()
        elif what == "kill":
            model.kill()
        self._after(finished)

    def action_press(self, key: str) -> None:
        """No list key moves anything at a prompt."""

    def on_key(self, event: events.Key) -> None:
        what = self._KEYS.get(event.key)
        event.stop()
        event.prevent_default()  # every other key, an arrow, Home, Tab or an F-key among them, is no key here
        if what is None and (not event.is_printable or not event.character or len(event.character) != 1):
            return
        if self.done:
            return
        if what is not None:
            self.action_field(what)
        else:
            self._after(self.model.type(event.character or ""))

    def on_paste(self, event: events.Paste) -> None:
        """A paste is taken whole, its control characters left out; a line end that ends it is Enter."""
        if self.done:
            return
        event.stop()
        body = event.text.rstrip("\r\n")
        finished = self.model.type(body)
        if not finished and len(body) < len(event.text):
            finished = self.model.enter()
        self._after(finished)


# The themes a frame is saved in: a light and a dark terminal's own background and foreground. On a real terminal the
# app draws on the terminal's own colors (ansi_color) and these are not used.
LIGHT = Theme(name="cerebro-light", primary="#004ba2", background="#ffffff", foreground="#1e1e1e", dark=False)
DARK = Theme(name="cerebro-dark", primary="#399aff", background="#1e1e1e", foreground="#e6e6e6", dark=True)


# The keys Textual names that a terminal sends as a CSI with a modifier, as Alt with Left: each is a CSI, not an Escape.
_CSI_KEY = re.compile(r"left|right|home|end|pageup|pagedown|insert|delete|f\d+")


class _ByRead(XTermParser):
    """Textual's key reader, with a read that ends on a lone Escape read as Escape, whatever the next read brings
    and however soon: Textual would wait 100 ms for more, and an Enter
    pressed in that time would turn a cancel into an approve. An arrow split across two reads is then Escape. A paste
    is read whole."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pasting = False

    def feed(self, data: str) -> Iterable[Any]:
        yield from super().feed(data)
        start, end = data.rfind("\x1b[200~"), data.rfind("\x1b[201~")
        self._pasting = start > end if start != end else self._pasting
        if data.endswith("\x1b") and not self._pasting and self._timeout_time is not None:
            self._timeout_time = 0.0  # the tick the driver runs after each read ends the wait now


# the modes that make a terminal report focus changes and mouse events, which no view here reads
_NO_REPORTS = re.compile(r"\x1b\[\?(?:1000|1002|1003|1004|1006|1015)[hl]")


class _Reading:
    """The `os` module as Textual's inline driver reads the terminal with, but for its `read`: a read that gives
    nothing, or fails as a terminal that is gone fails, calls `gone` once and gives nothing, so the view ends."""

    def __init__(self, gone: Callable[[], None]) -> None:
        self._gone = gone

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def read(self, fd: int, n: int) -> bytes:
        try:
            data = os.read(fd, n)
        except OSError:
            data = b""
        if not data:
            self._gone()
        return data


class _InlineApp[T](App[T]):
    """An app that hosts one view inline, under the prompt, on the terminal's own colors."""

    CSS = """
    Screen { background: ansi_default; color: ansi_default; }
    Screen:inline { height: auto; border: none; padding: 0; }
    """
    ENABLE_COMMAND_PALETTE = False
    INLINE_PADDING = 0  # the view draws on the line the cursor is on, with no blank line above
    # Ctrl-Q does nothing in a list: Textual's own binding would end it as Escape does
    BINDINGS: ClassVar[list[BindingType]] = [Binding("ctrl+q", "nothing", show=False, priority=True)]

    def action_nothing(self) -> None:
        pass

    def __init__(self, view: _KeysView[T], *, saved_theme: Theme | None = None) -> None:
        super().__init__(ansi_color=saved_theme is None)
        self.view = view
        self._saved_theme = saved_theme

    def compose(self) -> ComposeResult:
        yield self.view

    def run(self, *args: Any, **kwargs: Any) -> T | None:
        """The list run inline. SIGTERM, SIGHUP or SIGQUIT ends it with the terminal given back, then ends the
        command with 130, 129 or 131; an error inside it ends the command with one line and exit 1."""
        import asyncio
        import signal

        from textual.drivers import linux_inline_driver

        linux_inline_driver.XTermParser = _ByRead  # the inline driver reads keys with it
        caught: list[int] = []

        def stop(signum: int, frame: Any) -> None:
            caught.append(signum)
            with contextlib.suppress(RuntimeError):  # before the loop runs there is nothing to end yet
                (self._loop or asyncio.get_running_loop()).call_soon_threadsafe(self.exit)

        def hung_up() -> None:
            """The terminal is gone, as when its window closes or an SSH session drops: in raw mode a read gives
            nothing only then, every time, and Textual's reader would read it again in a busy loop for good. The
            command ends there with 129, as on SIGHUP: there is no screen left to give back, and a view asks before
            anything is written, so nothing is left half done."""
            os._exit(129)

        linux_inline_driver.os = _Reading(hung_up)  # type: ignore[attr-defined]
        # no focus or mouse reporting: the views need neither, and a terminal that reports them, as VS Code's
        # does, then sends a sequence at every focus change and every mouse move, which it repaints the panel for.
        # No screen clear either, which the driver writes on each resize and many terminals keep in the scrollback.
        driver_class = linux_inline_driver.LinuxInlineDriver
        driver_write = driver_class.write

        def write(driver: Any, data: str) -> None:
            driver_write(driver, _NO_REPORTS.sub("", data).replace("\x1b[2J", ""))

        driver_class.write = write  # type: ignore[method-assign]

        kept = {}
        with contextlib.suppress(ValueError):  # not the main thread, where no signal comes
            for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
                kept[sig] = signal.signal(sig, stop)
        try:
            got = super().run(*args, **kwargs)
        finally:
            driver_class.write = driver_write  # type: ignore[method-assign]
            linux_inline_driver.os = os  # type: ignore[attr-defined]
            for sig, handler in kept.items():
                signal.signal(sig, handler)
        if caught:
            raise SystemExit({signal.SIGTERM: 130, signal.SIGHUP: 129}.get(caught[0], 128 + caught[0]))
        if self.return_code and self._exception is not None:
            raise ListError(f"the list ended on an error: {type(self._exception).__name__}: {self._exception}")
        return got

    def _fatal_error(self) -> None:
        self._close_messages_no_wait()  # the error is said in one line once the terminal is back, not as a traceback

    def on_mount(self) -> None:
        if self._saved_theme is not None:
            self.register_theme(self._saved_theme)
            self.theme = self._saved_theme.name
            self.screen.styles.background = self._saved_theme.background
            self.screen.styles.color = self._saved_theme.foreground
        self.view.focus()


class ChecklistApp(_InlineApp[list[bool]]):
    def __init__(self, checklist: Checklist, styles: Styles | None = None, *, saved_theme: Theme | None = None):
        super().__init__(ChecklistView(checklist, styles), saved_theme=saved_theme)


class MenuApp(_InlineApp[int]):
    def __init__(self, menu: MenuList, styles: Styles | None = None, *, cursor: int = 0,
                 saved_theme: Theme | None = None):
        super().__init__(MenuView(menu, styles, cursor), saved_theme=saved_theme)


class PagerApp(_InlineApp[None]):
    def __init__(self, lines: Callable[[int], list[str]] | Sequence[str], styles: Styles | None = None, *,
                 saved_theme: Theme | None = None):
        super().__init__(PagerView(lines, styles), saved_theme=saved_theme)


def _raw_keys(printer: Printer) -> None:
    """Refuse a list where it cannot take keys: plain text asked for, or a stdout, or the process's own stdin and
    stderr, that are no terminal. Textual reads the process's stdin and draws on its stderr, and the last frame stays
    on stdout."""
    if printer.plain or not printer.terminal_out or not is_terminal(sys.__stdin__) or not is_terminal(sys.__stderr__):
        raise NoRawKeys("the terminal cannot take raw keys: print the list with write_plain and ask on a plain line")


def ask(checklist: Checklist, printer: Printer, gone_once_taken: bool = False) -> list[bool] | None:
    """Show the list inline under the prompt and return every row's check once Enter is pressed, by its place in the
    rows, or None for Escape. It raises NoRawKeys off a terminal and when plain text was asked for. The list's last
    frame stays on the screen, but with `gone_once_taken` an answer Enter took takes the list away,
    so what it approved comes next; Escape still leaves it."""
    _raw_keys(printer)
    styles = printer.st if printer.styled_out else None
    if not gone_once_taken:
        return ChecklistApp(checklist, styles).run(inline=True, inline_no_clear=True)
    view = ChecklistView(checklist, styles)
    got = _InlineApp(view).run(inline=True)
    if got is None:  # Escape: the list as it was left, drawn once more where it stood
        import shutil

        size = shutil.get_terminal_size()
        for line in view.model.render(size.columns, size.lines, False, styles):
            printer.err.write(line + "\n")
        printer.err.flush()
    return got


def choose(menu: MenuList, printer: Printer, cursor: int = 0) -> int:
    """Show the menu inline and return the place of the row Enter opened, or MENU_DONE. It raises NoRawKeys as ask
    does."""
    _raw_keys(printer)
    got = MenuApp(menu, printer.st if printer.styled_out else None, cursor=cursor).run(inline=True)
    return MENU_DONE if got is None else got


def read(lines: Callable[[int], list[str]] | Sequence[str], printer: Printer) -> None:
    """Show text in the pager inline until Escape. It raises NoRawKeys as ask does."""
    _raw_keys(printer)
    PagerApp(lines, printer.st if printer.styled_out else None).run(inline=True)


class FieldBack(CerebroError):
    """Escape at a view's prompt: nothing was taken, and the view goes back."""


def field(ask: FieldAsk, printer: Printer) -> str:
    """Ask at a view's prompt on the terminal: the prompt with the
    legend of its keys under it until Enter, Escape or Ctrl-C. Gives the answer, `y` or `n` at a yes or no prompt,
    and leaves the prompt's line on the screen with the answer on it, unless hidden, the legend gone. Escape raises
    FieldBack with nothing left on the screen, and Ctrl-C raises KeyboardInterrupt. It raises NoRawKeys as ask does,
    before anything is drawn."""
    _raw_keys(printer)
    styles = printer.st if printer.styled_out else None
    view = FieldView(ask, styles)
    got = _InlineApp(view).run(inline=True)
    if got == FIELD_CANCEL or got is None:
        raise KeyboardInterrupt
    if got == FIELD_BACK:
        raise FieldBack("back from the prompt")
    model: Field = view.model
    line = model.render(10_000, 1, False, styles)[0]
    printer.err.write(line + "\n")
    printer.err.flush()
    return model.answer
