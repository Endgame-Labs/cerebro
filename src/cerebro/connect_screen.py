"""`cerebro connect` at a terminal: the lobby of a workspace's sources, what a source says before it asks and the
choice it asks first, and the list a person ticks for one.

The lobby lists each source of the workspace and each kind of source it could add, with whether it is connected.
Enter on a row hands its name back, and the command runs that source's
connect. Escape, or Enter on Done, ends it.

The picker shows what a connector offers for its source file, from `Connector.offer`, as a checkbox list. What the
file lists now starts ticked, and a value of the file that no row shows gets a ticked row of its own, first, so
nothing the person wrote is dropped unseen. Enter hands back the ticked values, the file's own first in its order,
then the others in the list's order. Escape hands back None, and nothing is written.

Neither screen reads the workspace or writes a file: each gets its rows and gives back a choice, and the command
does the rest. The terminal modules are imported only inside the functions that draw, so a command that never
draws never loads them. Every word that came from a source or a model goes through `shown`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .connector import Offer
from .connectors.connecting import Way
from .shown import shown

if TYPE_CHECKING:
    from .checklist import Checklist, MenuList
    from .output import Printer

NAME_CAP = 120  # a name or a line's words past this many characters are cut, with a note of how long they were
CONNECTED, NOT_CONNECTED, ATTENTION, NO_LOGIN, NOT_ADDED = (
    "connected", "not connected", "needs attention", "keeps no login", "not added")


@dataclass(frozen=True)
class Place:
    """A row of the lobby: a source by its name, or a kind of source the workspace has none of, with its state and,
    for one that needs attention or keeps no login, why."""

    name: str
    state: str
    reason: str = ""
    title: str = ""
    viewed: bool = True  # connect has a view for it: a login to ask for, a program's login to check, or local files


# -- the screen apart


class Apart:
    """The terminal's alternate screen, where lobbies and views of more than one screen draw: `take` moves
    there, or clears it once there, so what comes next draws from the top of a clear screen; `leave` brings the main
    screen back as it was. Nothing of it where standard error draws no styles."""

    def __init__(self, printer: Printer | None) -> None:
        self.printer = printer
        self.on = False
        self._kept: dict[int, Any] = {}

    def _write(self, s: str) -> None:
        try:
            self.printer.err.write(s)  # type: ignore[union-attr]
            self.printer.err.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):  # the terminal is gone: there is no screen to draw on
            pass

    def take(self) -> None:
        if self.printer is None or not self.printer.styled_err:
            return
        if not self.on:
            self._hold_signals()
        self._write(("" if self.on else "\x1b[?1049h") + "\x1b[H\x1b[2J")
        self.on = True

    def leave(self) -> None:
        if self.on:
            self._write("\x1b[?1049l")
            self.on = False
            self._let_signals_go()

    def _hold_signals(self) -> None:
        """While the screen is apart, SIGTERM, SIGHUP and SIGQUIT end the command through its `finally`, so the main
        screen comes back by every way out, with 130, 129 and 131, as a view drawn apart ends on them; a signal's
        default would leave the terminal on the alternate screen."""
        import signal

        def end(signum: int, frame: Any) -> None:
            raise SystemExit({signal.SIGTERM: 130, signal.SIGHUP: 129}.get(signum, 128 + signum))

        try:
            for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
                self._kept[sig] = signal.signal(sig, end)
        except ValueError:  # not the main thread, where no signal comes
            self._kept = {}

    def _let_signals_go(self) -> None:
        import signal

        for sig, handler in self._kept.items():
            signal.signal(sig, handler)
        self._kept = {}

    def fits(self, rows: int) -> bool:
        """Whether a view of `rows` rows fits the screen apart, which keeps no scrollback."""
        import shutil

        return rows < shutil.get_terminal_size().lines


# -- the lobby


def lobby_menu(places: Sequence[Place], printer: Printer, status: Sequence[str] = ()) -> MenuList:
    """The lobby as a menu: a title, lines that say what it is for, `status` under them (how the last source opened
    went, each line already drawn clean by the command), then a row for each place, and Done."""
    from .checklist import MenuList, MenuRow
    from .output import Mark

    # the lobby shows three states, connected, not connected and needs attention: a kind not added yet is not
    # connected, and a source that keeps no login reads as it is, so it is connected
    shown_as = {CONNECTED: (Mark.DONE, "connected"), NOT_CONNECTED: (Mark.FAILED, "not connected"),
                ATTENTION: (Mark.WARNING, "needs attention"), NO_LOGIN: (Mark.DONE, "connected"),
                NOT_ADDED: (Mark.FAILED, "not connected")}
    rows = [MenuRow(shown(p.title or p.name, NAME_CAP), shown_as[p.state][1], shown(p.reason, NAME_CAP),
                    printer.mark(shown_as[p.state][0])) for p in places]
    intro = (f"Pick one to connect. Come back any time with {printer.key('cerebro connect')}, or in the "
             f"{printer.key('sources/')} folder of the workspace.")
    head = ["Sources", intro, *status]
    return MenuList(rows, head, end="Done", open="select", done="done")


def lobby(places: Sequence[Place], printer: Printer, cursor: int = 0, status: Sequence[str] = ()) -> int | None:
    """Show the lobby inline and give the place of the row Enter opened, or None for Escape or Done. Raises
    `checklist.NoRawKeys` where the terminal takes no raw keys."""
    from .checklist import MENU_DONE, choose

    got = choose(lobby_menu(places, printer, status), printer, cursor)
    return None if got == MENU_DONE else got


# -- what a source says before it asks


def accented(printer: Printer, line: str) -> str:
    """A line a source says, each phrase between backticks in the accent and the backticks gone, so the
    settings, buttons and addresses it names stand out; plain, the words alone."""
    parts = line.split("`")
    return "".join(printer.accent(part) if i % 2 and part else part for i, part in enumerate(parts))


def explained(printer: Printer, lines: Sequence[str], block: str = "", after: Sequence[str] = ()) -> list[str]:
    """What a source says before its first question: its lines muted with
    the phrases it names accented, a blank line kept as it is, and `block` between them on a tint, to copy whole.
    Plain, each line is as it is and the block is printed at the margin, so a copy of it from its
    first character on reads as it should."""
    from .output import fold

    def muted(part: Sequence[str]) -> list[str]:  # drawn, each line wraps at its spaces to the terminal's width
        width = printer.width() if printer.styled_out else 0
        return [row for line in part
                for row in (fold(printer.dim(accented(printer, line)), width) if line else [line])]

    shown = (printer.tinted(block) if printer.styled_out else block.removesuffix("\n").split("\n")) if block else []
    return [*muted(lines), *shown, *muted(after)]


def ways_menu(title: str, ways: Sequence[Way], lines: Sequence[str] = ()) -> MenuList:
    """The choice a source asks first: a row for each way, with its reason, under the
    title when the source says nothing before it. A source that says lines prints its title and its lines above the
    menu, so the menu holds no head. Enter selects; Escape goes back."""
    from .checklist import MenuList, MenuRow

    return MenuList([MenuRow(w.title, reason=w.reason) for w in ways], [] if lines else [title], open="select",
                    done="back")


def plain_ways(ways: Sequence[Way]) -> str:
    """The prompt a plain line asks the choice with: each way's word, its first letter in
    brackets, two spaces apart, then `  > `, such as `[a]lpha  [b]eta  > `."""
    return "  ".join(f"[{w.word[:1]}]{w.word[1:]}" for w in ways) + "  > "


def way_of(ways: Sequence[Way], answer: str) -> int | None:
    """The place of the way a plain answer names, by its word or the word's first letter, or None."""
    said = answer.strip().lower()
    return next((i for i, w in enumerate(ways) if said and said in (w.word, w.word[:1])), None)


# -- the picker


@dataclass(frozen=True)
class Picking:
    """A picker's list and the value each row writes, by its place in the rows."""

    checklist: Checklist
    values: tuple[str, ...]


def picking(offer: Offer, name: str) -> Picking:
    """The checkbox list for an offer to `sources/<name>.yaml`: the file's values no row shows first, ticked, then
    the offer's rows under their groups in the order they first come. A row starts ticked when the file lists it, or,
    while the file lists nothing, when the connector suggests it."""
    from .checklist import Checklist, Group, Row

    file = f"sources/{shown(name, NAME_CAP)}.yaml"
    listed = {v.casefold() for v in offer.current}
    offered = {c.value.casefold() for c in offer.choices}
    rows: list[Row] = []
    values: list[str] = []
    groups: dict[str, list[int]] = {}
    for value in offer.current:
        if value.casefold() not in offered:
            groups.setdefault(f"in {file}", []).append(len(rows))
            rows.append(Row(shown(value, NAME_CAP), True, f"in {file}, and not among what the login can see"))
            values.append(value)
            offered.add(value.casefold())
    for choice in offer.choices:
        groups.setdefault(shown(choice.group, 60), []).append(len(rows))
        checked = choice.value.casefold() in listed if listed else choice.checked
        rows.append(Row(shown(choice.label, NAME_CAP), checked, shown(choice.detail, NAME_CAP)))
        values.append(choice.value)
    # the list's title, `into <file>` under it, the verb on Enter and the Escape, and a list that never folds, so every
    # row shows however many there are
    head = [offer.head or f"{offer.many[:1].upper()}{offer.many[1:]} to read", f"into {file}"]
    note = [shown(line, 300) for line in offer.note]
    if not rows:
        note.append(f"The login can see no {offer.many}: list them under {offer.key}: in {file} by hand.")
    return Picking(Checklist(rows, [Group(h, r) for h, r in groups.items()], head, note, verb=offer.verb,
                             esc="Cancel and quit", fold=False), tuple(values))


def chosen(offer: Offer, picked: Picking, checked: Sequence[bool]) -> list[str]:
    """The values Enter writes: the file's own that stay ticked, in the file's order and as written, then each other
    ticked row's value in the list's order, each once."""
    ticked = [v for v, on in zip(picked.values, checked, strict=True) if on]
    keys = {v.casefold() for v in ticked}
    out = [v for v in offer.current if v.casefold() in keys]
    seen = {v.casefold() for v in out}
    for value in ticked:
        if value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return out


def pick(offer: Offer, name: str, printer: Printer, answer: Callable[[str], str] | None = None) -> list[str] | None:
    """Show the picker inline and give the values to write, or None for Escape. Where the terminal takes no raw keys,
    the list prints as plain text, and `answer`, given a prompt, reads `a` to accept the ticks as they stand or
    `q` to write nothing, asking again on anything else; with no `answer` nothing is asked and None comes back."""
    from .checklist import NoRawKeys, ask, write_plain

    picked = picking(offer, name)
    try:
        checked = ask(picked.checklist, printer)
    except NoRawKeys:
        printer.out.write(write_plain(picked.checklist))
        printer.out.write(f"\nAccept writes the checked rows as the {offer.key} list. Edit the file to pick others.\n")
        printer.out.flush()
        while True:  # a or accept writes, q or quit writes nothing, anything else asks again
            try:
                said = answer("[a]ccept  [q]uit  > ").strip().lower() if answer is not None else "q"
            except EOFError:  # input that ends writes nothing; Ctrl-C goes on up and ends the command
                return None
            if said in ("a", "accept", "q", "quit"):
                break
        if said in ("q", "quit"):
            return None
        checked = [row.checked for row in picked.checklist.rows]
    return None if checked is None else chosen(offer, picked, checked)


def written_line(offer: Offer, name: str, values: Sequence[str] | None) -> str:
    """The line that closes a pick: what the file lists now, or that nothing was written."""
    file = f"sources/{shown(name, NAME_CAP)}.yaml"
    if values is None:
        return f"Nothing written: {file} is as it was."
    listed = ", ".join(shown(v, NAME_CAP) for v in values) if values else f"no {offer.one}"
    return f"{file} lists {listed}."
