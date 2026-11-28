"""Every list moves with the arrows and with j and k, and takes these keys: space and a in a checklist, l and h to open and close a group, Enter, and a newline as Enter. Each
list is the one its command builds, driven through Textual's own test pilot; no terminal is needed."""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from typing import Any

import pytest

from cerebro.checklist import ChecklistApp, MenuApp, PagerApp

sys.path.insert(0, str(Path(__file__).parent))


def pressed(app: Any, *keys: str) -> list[Any]:
    """The cursor after each key, pressed in turn through the app's pilot, and the app's result at the end."""
    seen: list[Any] = []

    async def go() -> None:
        async with app.run_test() as pilot:
            for key in keys:
                await pilot.press(key)
                seen.append(app.view.model.cursor if hasattr(app.view.model, "cursor") else app.view.model.top)

    asyncio.run(go())
    return seen


def test_the_first_runs_list_moves_with_j_and_k_and_toggles_with_space() -> None:
    from cerebro.first_run import Listed, Pile
    from cerebro.first_run_list import checklist
    from cerebro.tree import Row, plain

    rows = [Row(f"p_{n}", "things/curator", "things", f"Thing {n}", path=f"things/t{n}") for n in range(3)]
    lst, _ = checklist(Pile([Listed("things/curator", "things", rows)], groups=[g for g in plain(rows, {})]))
    app = ChecklistApp(lst)
    before = list(app.view.model.checked)
    assert pressed(app, "l", "j", "j", "k", "space") == [0, 1, 2, 1, 1]  # opened, then moved through its rows
    assert app.view.model.checked != before


def test_the_connect_lobby_and_its_picker_move_with_j_and_k() -> None:
    from cerebro.connect_screen import Place, lobby_menu, picking
    from cerebro.connector import Choice, Offer
    from cerebro.output import Printer

    printer = Printer(io.StringIO(), io.StringIO(), plain=True, width=lambda: 80)
    lobby = lobby_menu([Place("notes", "connected"), Place("code", "not connected"), Place("chat", "not connected")],
                       printer)
    assert pressed(MenuApp(lobby), "j", "j", "k") == [1, 2, 1]
    offer = Offer("repos", "repository", "repositories",
                  (Choice("a/one", "a/one"), Choice("a/two", "a/two"), Choice("a/three", "a/three")))
    assert pressed(ChecklistApp(picking(offer, "code").checklist), "j", "j", "k") == [1, 2, 1]


def test_the_guided_inits_menus_and_the_keys_lobby_move_with_j_and_k() -> None:
    from cerebro import guided, keys
    from cerebro.models import DEFAULTS

    models = guided.models_menu(dict(DEFAULTS))
    assert pressed(MenuApp(models), "j", "j", "k") == [1, 2, 1]
    clients = guided.clients_menu([("Claude Desktop", "set up", ""), ("Claude Code", "not set up", ""),
                                   ("Codex", "not set up", "")])
    assert pressed(MenuApp(clients), "j", "k", "j", "j") == [1, 0, 1, 2]
    rows = keys.key_rows(dict(DEFAULTS), [], {})
    lobby = keys.menu(rows, keys.head())
    assert pressed(MenuApp(lobby), "j", "k") == [1, 0]


def test_the_pager_scrolls_with_j_and_k() -> None:
    app = PagerApp([f"line {n}" for n in range(200)])
    assert pressed(app, "j", "j", "k") == [1, 2, 1]


@pytest.mark.parametrize("key", ["enter", "ctrl+j"])
def test_enter_or_a_newline_opens_the_cursors_row(key: str) -> None:
    from cerebro.connect_screen import Place, lobby_menu
    from cerebro.output import Printer

    printer = Printer(io.StringIO(), io.StringIO(), plain=True, width=lambda: 80)
    app = MenuApp(lobby_menu([Place("notes", "connected"), Place("code", "not connected")], printer))
    got: list[Any] = []

    async def go() -> None:
        async with app.run_test() as pilot:
            await pilot.press("j", key)
        got.append(app.return_value)

    asyncio.run(go())
    assert got == [1]


# -- keys read beyond the named ones -------------------------------------------------------------------------------


@pytest.mark.parametrize(("down", "up"), [("alt+down", "alt+up"), ("ctrl+shift+down", "ctrl+shift+up")])
def test_an_arrow_with_a_modifier_moves_as_the_arrow_does_for_any_csi_a_or_b(down: str, up: str) -> None:
    app = MenuApp(_menu())
    assert pressed(app, down, down, up) == [1, 2, 1]
    pager = PagerApp([f"line {n}" for n in range(200)])
    assert pressed(pager, down, up) == [1, 0]


def test_ctrl_q_does_nothing_in_a_list() -> None:
    app = MenuApp(_menu())
    assert pressed(app, "ctrl+q", "j") == [0, 1]  # the list is still there, and moves


@pytest.mark.parametrize("key", ["alt+j", "alt+x"])
def test_escape_with_a_key_after_it_in_one_read_is_escape(key: str) -> None:
    app = ChecklistApp(_checklist())
    got: list[Any] = []

    async def go() -> None:
        async with app.run_test() as pilot:
            await pilot.press(key, "enter")  # an Enter the list takes only if the key before did not end it
        got.append(app.return_value)

    asyncio.run(go())
    assert got == [None]


def _menu() -> Any:
    from cerebro.connect_screen import Place, lobby_menu
    from cerebro.output import Printer

    printer = Printer(io.StringIO(), io.StringIO(), plain=True, width=lambda: 80)
    return lobby_menu([Place("notes", "connected"), Place("code", "not connected"), Place("chat", "not connected")],
                      printer)


def _checklist() -> Any:
    from cerebro.connect_screen import picking
    from cerebro.connector import Choice, Offer

    offer = Offer("repos", "repository", "repositories", (Choice("a/one", "a/one"), Choice("a/two", "a/two")))
    return picking(offer, "code").checklist
