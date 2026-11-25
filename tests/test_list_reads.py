"""A list reads the bytes a terminal sends: Alt with Left, Right, Home, End, PageDown, Delete or an F-key is a CSI that does nothing; Escape with a character after
it in one read is Escape; and a read that ends on a lone Escape is Escape, however soon an Enter follows. What each
does is in the table. The bytes go through the list's own key reader, one
read at a time as the terminal's driver hands them on, into the list driven by Textual's pilot; no terminal is needed.
One test runs each list on a real pseudo-terminal, so the reader is the one the terminal's driver reads with."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import select
import subprocess
import sys
import time
from typing import Any

import pytest

from cerebro.checklist import MENU_DONE, Checklist, ChecklistApp, Group, MenuApp, MenuList, MenuRow, Row, _ByRead

E = b"\x1b"
CANCELLED = {"menu": MENU_DONE, "checklist": None}


def a_list(kind: str) -> Any:
    if kind == "menu":
        return MenuApp(MenuList(head=["Sample menu"], rows=[MenuRow("Alder"), MenuRow("Birch")], end="Done"))
    return ChecklistApp(Checklist(rows=[Row("Alder"), Row("Birch")], groups=[Group("sample", [0, 1])],
                                  head=["Sample list"]))


def pressed(kind: str, reads: list[bytes]) -> Any:
    """What a list returns when each read is handed to it as the terminal's driver hands one on, then an Enter in a
    read of its own, past Textual's 100 ms wait for more of a key: a list the keys left open takes that Enter, one
    they ended returns cancelled."""
    app = a_list(kind)

    async def outcome() -> None:
        reader = _ByRead()
        async with app.run_test() as pilot:
            for at, data in enumerate([*reads, b"\r"]):
                waited = []
                if at:
                    await asyncio.sleep(0.12)
                    waited = list(reader.tick())  # the driver ticks at least every 100 ms while it waits for a read
                for event in [*waited, *reader.feed(data.decode()), *reader.tick()]:  # and after each read
                    if app.is_running:
                        app._driver.send_message(event)
                await pilot.pause()

    asyncio.run(outcome())
    return MENU_DONE if kind == "menu" and app.return_value is None else app.return_value  # as `choose` gives it


# What a list does with each, one write each: "open" leaves the list open, "ends" ends it as Escape.
ALT_KEYS = {
    "Alt+Left": (E + b"[1;3D", "open"), "Alt+Right": (E + b"[1;3C", "open"), "Alt+Home": (E + b"[1;3H", "open"),
    "Alt+End": (E + b"[1;3F", "open"), "Alt+PageDown": (E + b"[6;3~", "open"), "Alt+Delete": (E + b"[3;3~", "open"),
    "Alt+F1": (E + b"[1;3P", "open"), "Ctrl+Alt+Left": (E + b"[1;7D", "open"), "Escape O": (E + b"O", "open"),
    "Escape j": (E + b"j", "ends"),
}


# Both lists read keys through one view's `on_key`: the menu takes every key, and the checklist one of each road there,
# a CSI ending in a letter, one ending in ~, Escape and O, and Escape and a letter.
ROADS = ("Alt+Left", "Alt+Delete", "Escape O", "Escape j")
PRESSED = [("menu", key) for key in ALT_KEYS] + [("checklist", key) for key in ROADS]


@pytest.mark.parametrize(("kind", "key"), PRESSED, ids=[f"{key}-{kind}" for kind, key in PRESSED])
def test_a_key_with_alt_leaves_the_list_open_or_ends_it_as_its_case_says(kind: str, key: str) -> None:
    data, outcome = ALT_KEYS[key]
    got = pressed(kind, [data])
    assert (got == CANCELLED[kind]) == (outcome == "ends"), (key, got)


@pytest.mark.parametrize("kind", ["menu", "checklist"])
def test_an_arrow_in_one_read_still_moves_and_enter_then_takes_the_row_it_moved_to(kind: str) -> None:
    got = pressed(kind, [E + b"[B"])
    assert got == (1 if kind == "menu" else [True, True]), got


OWN_TERMINAL = "import fcntl, sys, termios\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
# Each list as a command shows it, the words it draws once it reads, and what it gives when cancelled.
ON_A_TERMINAL = {
    "menu": (b" move", """from cerebro.checklist import MenuList, MenuRow, choose
from cerebro.output import Printer
print("RESULT", choose(MenuList(head=["Sample menu"], rows=[MenuRow("Alder"), MenuRow("Birch")], end="Done"),
                       Printer()), file=sys.stderr)
""", str(MENU_DONE)),
    "checklist": (b"Cancel and quit", """from cerebro.checklist import Checklist, Group, Row, ask
from cerebro.output import Printer
print("RESULT", ask(Checklist(rows=[Row("Alder"), Row("Birch")], groups=[Group("sample", [0, 1])],
                              head=["Sample list"]), Printer()), file=sys.stderr)
""", "None"),
}


@pytest.mark.parametrize("kind", list(ON_A_TERMINAL))
def test_on_a_terminal_escape_then_enter_20_ms_later_cancels_the_list(kind: str) -> None:
    """A list reads one read at a time: a read of a lone Escape is Escape, and the Enter after it goes on, however
    soon it comes. Textual alone waits 100 ms for more after an Escape."""
    ready, code, cancelled = ON_A_TERMINAL[kind]
    leader, follower = pty.openpty()
    child = subprocess.Popen([sys.executable, "-c", OWN_TERMINAL + code], stdin=follower, stdout=follower, stderr=follower,
                             env={"PATH": os.environ.get("PATH", ""), "TERM": "xterm-256color", "COLORFGBG": "15;0"},
                             start_new_session=True)
    os.close(follower)
    got, sent = b"", False
    end = time.monotonic() + 20
    try:
        while time.monotonic() < end and b"RESULT" not in got:
            if select.select([leader], [], [], 0.02)[0]:
                try:
                    chunk = os.read(leader, 65536)
                except OSError:
                    break
                got += chunk
                if b"\x1b]11;?" in chunk:
                    os.write(leader, b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")
                if b"\x1b[6n" in chunk:
                    os.write(leader, b"\x1b[1;1R")
            if not sent and ready in got:
                sent = True
                time.sleep(0.3)  # the list is drawn before it reads
                os.write(leader, E)
                time.sleep(0.02)
                os.write(leader, b"\r")
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=5)
        with contextlib.suppress(OSError):
            while select.select([leader], [], [], 0.1)[0] and (more := os.read(leader, 65536)):
                got += more
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(leader)
    text = got.decode("utf-8", "replace")
    assert "RESULT" in text, text[-500:]
    assert text.split("RESULT", 1)[1].split("\n", 1)[0].strip() == cancelled
