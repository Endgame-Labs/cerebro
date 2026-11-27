"""The list of proposed articles at a terminal: one tree, as `tree` arranges it.

One line per top-level group, under it each collection of what sits under it with a count and a checkbox, single
articles when a line is opened, then "Not grouped (n)" and the candidates the curators left out, "Left out (n)",
unticked, each with its curator's reason. Ticking a line ticks everything under it, and unticking it unticks everything
under it. It draws what the pile says and decides nothing: Escape gives None, and records nothing. Every string from
a workspace's files or a model passes through `shown`. The command line imports this module only to draw it, so
Textual loads only then.
"""
from __future__ import annotations

import textwrap
from collections.abc import Callable
from typing import TYPE_CHECKING

from .first_run import CLOSED_OVER, NAME_CAP, NOTE_CAP, SHOWN_ROWS, Choices, Group, Listed, Pile, said_by
from .shown import shown

if TYPE_CHECKING:
    from .checklist import Checklist
    from .output import Printer

WRAP = 76  # a note or a line under the selection is broken at a word to fit 80 columns with the widget's indent
REASON_WRAP = 68  # a reason under a row is broken at a word to fit 80 columns
TITLE = "Working set proposal"


def _one(text: object, cap: int | None) -> str:
    """A string from a workspace's files or a model on one line, escaped and cut as `shown` does."""
    return shown(" ".join(str(text).split()), cap)


def _reason(text: str) -> list[str]:
    """A reason whole, escaped and never cut, broken at a word over as many lines as it takes to fit under a row."""
    one = _one(text, None)
    return textwrap.wrap(one, REASON_WRAP, break_long_words=True, break_on_hyphens=False) if one else []


def _label(g: Group) -> str:
    """A group's name, with the other names its rows go by."""
    names = list(dict.fromkeys(_one(row.name, NAME_CAP) for row in g.rows)) or [_one(g.name, NAME_CAP)]
    return names[0] + (f"  same as {', '.join(names[1:])}" if len(names) > 1 else "") + (
        "  of the workspace's own side" if any(row.own for row in g.rows) else "")


def _lines(g: Group) -> list[str]:
    """The lines under a group's name: its lead's reason, who proposed it when more than one proposal did, and how
    many held records its admit takes, when more than one."""
    out = _reason(g.lead.reason) if g.lead else []
    if len(g.rows) > 1:
        out.append(f"{len(g.rows)} proposals, by {', '.join(sorted({_one(r.agent, NAME_CAP) for r in g.rows}))}")
    if (binds := len({held for row in g.rows for held in row.binds})) > 1:
        out.append(f"{binds} records")
    return out


def _collections(top: Group) -> list[tuple[str, list[Group]]]:
    """What sits under a group, by collection, the collections in one fixed order."""
    out: dict[str, list[Group]] = {}
    for g in top.under:
        out.setdefault(g.collection, []).append(g)
    return sorted(out.items())


def _top_line(top: Group) -> str:
    return f"{_label(top)}  {_one(top.collection, NAME_CAP)}" + ("" if top.rows else ", in the wiki")


def _state(listed: Listed) -> str:
    """What a curator's part says of itself, or "" for one that proposed rows."""
    count = len(listed.proposed())
    state = {"failed": "did not finish", "budget": "budget reached"}.get(listed.state, "")
    if not state and count and not listed.note:
        return ""
    state = state or (f"{count} proposed" if count else "proposed nothing")
    return f"{_one(listed.collection, NAME_CAP)} collection: {state}" + (
        f": {_one(listed.note, NOTE_CAP)}" if listed.note else "")


def notes(pile: Pile) -> list[str]:
    """The lines under the tree: why it is plain, why discovery stopped, each curator that did not finish, and what
    of the workspace's own side is not listed."""
    return [_one(text, NOTE_CAP * 2) for text in (pile.note, pile.stopped, *(_state(listed) for listed in pile.lists),
                                                 *(listed.unlisted for listed in pile.lists)) if text]


def _parts(pile: Pile) -> tuple[list[Group], list[Group]]:
    """The groups with something under them, or an article, and the rest, which are not grouped."""
    return [g for g in pile.groups if g.under], [g for g in pile.groups if not g.under]


def selected(groups: list[Group], on: set[int]) -> str:
    """The selection line: how many articles are ticked of how many."""
    if not groups:
        return "Nothing to propose: the wiki starts empty"
    return f"{sum(id(g) in on for g in groups)} of {len(groups)} selected"


def _every(pile: Pile) -> list[Group]:
    """Every group that makes an article, in the order the screen shows it."""
    return [g for top in [*pile.groups, *pile.left()] for g in (top, *top.under) if g.rows]


def checklist(pile: Pile, reject: Callable[[int], None] | None = None) -> tuple[Checklist, list[Group]]:
    """The pile as the checkbox list shows it, and the group of each row by its place."""
    from .checklist import Checklist
    from .checklist import Group as Heading
    from .checklist import Row as Line

    rows: list[Line] = []
    heads: list[Heading] = []
    of: list[Group] = []

    def add(g: Group) -> int:
        first, *rest = _lines(g) or [""]
        rows.append(Line(_label(g), checked=g.tick, detail=first, more=rest))
        of.append(g)
        return len(rows) - 1

    grouped, loose = _parts(pile)
    for top in grouped:
        heading = _top_line(top)
        at = add(top) if top.rows else None
        if at is not None:
            heads.append(Heading("", [at], heading=heading, shown=False))  # the heading's line is this article
        for collection, under in _collections(top):
            heads.append(Heading(f"{_one(collection.capitalize(), NAME_CAP)} ({len(under)})",
                                 [add(g) for g in under], heading=heading, closed=True))
    if loose:
        heads.append(Heading(f"Not grouped ({len(loose)})", [add(g) for g in loose], closed=True))
    if left := pile.left():
        heads.append(Heading(f"Left out ({len(left)})", [add(g) for g in left], closed=True))

    place = {id(g): n for n, g in enumerate(of)}
    over = {n: place[id(g.over)] for n, g in enumerate(of) if g.over is not None and id(g.over) in place}

    def above(i: int) -> list[int]:
        out = []
        while (i := over.get(i, -1)) >= 0:
            out.append(i)
        return out

    def toggled(changed: list[int], on: bool, checked: list[bool]) -> list[bool]:
        if not on:  # unticking a row unticks what sits under it, however deep
            checked = [c and not any(i in above(j) for i in changed) for j, c in enumerate(checked)]
        return checked

    agents = [f"agents/{_one(listed.agent, NAME_CAP)}.md" for listed in pile.lists if listed.rows]
    return Checklist(rows=rows, groups=heads, head=[TITLE, "by " + ", ".join(agents)],
                     note=[line for text in notes(pile) for line in textwrap.wrap(text, WRAP)],
                     after=[_one(line, None) for line in said_by(pile).values()], verb="Approve",
                     toggled=toggled, reject=reject), of


def draw(pile: Pile, printer: Printer | None = None,
         reject: Callable[[Group, str], bool] | None = None) -> Choices | None:
    """Show the pile and give the choices, or None for Escape. With `reject`, `r` on a row asks for a reason on a
    prompt, and Escape there goes back, rejecting nothing; `reject` gives whether it was rejected, and the list
    opens again without it. It raises the widget's NoRawKeys where the terminal cannot take raw keys, or plain text
    was asked for, so the caller shows the plain list."""
    from .checklist import FieldAsk, FieldBack, ask, field
    from .output import Printer

    printer = printer or Printer()
    while True:
        asked: list[int] = []
        lst, of = checklist(pile, asked.append if reject is not None else None)
        got = ask(lst, printer)
        if not asked:
            return None if got is None else Choices.of(pile, [g for g, on in zip(of, got, strict=True) if on])
        g = of[asked[0]]
        try:
            why = field(FieldAsk(f"Reason to reject {_one(g.name, NAME_CAP)}"), printer)
        except FieldBack:
            continue
        if reject is not None and reject(g, why):
            _drop(pile, g)


def _drop(pile: Pile, gone: Group) -> None:
    """Take a rejected group off the pile."""
    for listed in pile.lists:
        listed.rows = [row for row in listed.rows if row not in gone.rows]
    pile.groups = [g for g in pile.groups if g is not gone]
    for top in pile.groups:
        top.under = [g for g in top.under if g is not gone]


def plain(pile: Pile) -> list[str]:
    """The pile as plain lines, opened, for --yes, --plain and a terminal that cannot take raw keys: each line with
    its box, a group of more than ten rows by its first five. Nothing here loads the widget."""
    on = {id(g) for g in _every(pile) if g.tick}

    def box(groups: list[Group]) -> str:
        n = sum(id(g) in on for g in groups if g.rows)
        return "[x]" if n and n == len([g for g in groups if g.rows]) else "[-]" if n else "[ ]"

    def rows(groups: list[Group], indent: str, collection: bool = False) -> list[str]:
        out = []
        for g in groups[:SHOWN_ROWS] if len(groups) > CLOSED_OVER else groups:
            tail = f"  {_one(g.collection, NAME_CAP)}" if collection else ""
            out.append(f"{indent}{box([g])} {_label(g)}{tail}")
            out += [f"{indent}      {line}" for line in _lines(g)]
        if len(groups) > CLOSED_OVER:
            out.append(f"{indent}… {len(groups) - SHOWN_ROWS} more")
        return out

    agents = [f"agents/{_one(listed.agent, NAME_CAP)}.md" for listed in pile.lists if listed.rows]
    out = [TITLE, "by " + ", ".join(agents), ""]
    grouped, loose = _parts(pile)
    for top in grouped:
        out.append(f"{box([top, *top.under])} {_top_line(top)}")
        out += [f"      {line}" for line in (_lines(top) if top.rows else [])]
        for collection, under in _collections(top):
            out.append(f"    {box(under)} {_one(collection.capitalize(), NAME_CAP)} ({len(under)})")
            out += rows(under, "        ")
    if loose:
        out.append(f"{box(loose)} Not grouped ({len(loose)})")
        out += rows(loose, "    ", collection=True)
    if left := pile.left():
        out.append(f"[ ] Left out ({len(left)})")
        out += rows(left, "    ", collection=True)
    if said := notes(pile):
        out += ["", *said]
    out += ["", selected(_every(pile), on)]
    out += [f"  {line}" for line in said_by(pile).values()]
    return out
