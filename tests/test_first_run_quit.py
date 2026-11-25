"""A list left with Escape or `q` says that nothing was recorded, the late list a first run leaves waiting comes up again
at the next run at a terminal, and the hand admit a list names is of a collection its rows were proposed to. Fakes
only: each curator's calls are scripted. Names are made up and neutral."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from test_first_run import (
    ADMITS,
    CURATOR,
    TOPIC,
    Script,
    articles,
    every_curator_weighs,  # noqa: F401  (a fixture)
    lay_out_workspace,
    open_it,
)

from cerebro import cli, first_run
from cerebro.first_run import Choices, Listed, Pile, Row, run
from cerebro.records import proposals

QUIT = "Nothing recorded. Run again when you are ready, or admit one with cerebro admit {}/<name>."


def late_list_left(root: Path, script: Script) -> tuple[list[str], object]:
    """A first run whose list is approved and whose late list is left with Escape: what it said, and the outcome."""
    said: list[str] = []
    workspace = open_it(lay_out_workspace(root, knowledge=True), script)
    out = run(workspace, choose=lambda pile: None if pile.late else Choices.starter(pile), say=said.append)
    workspace.close()
    return said, out


def test_escape_on_a_late_list_says_nothing_was_recorded_how_many_wait_and_to_run_again(tmp_path: Path) -> None:
    """Escape on a late list, and the line on what waits."""
    said, out = late_list_left(tmp_path, Script({"things/curator": list(ADMITS), "topics/curator": list(TOPIC)}))
    at = said.index(QUIT.format("topics"))
    assert said[at - 1:at + 3] == ["", QUIT.format("topics"), "", "1 proposal waiting · cerebro review"]
    assert out.result is not None and out.result.next == "cerebro serve --once"


def test_after_escape_on_a_late_list_the_next_run_at_a_terminal_shows_it_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run opens a late list that waits before its tick at a terminal: Escape records nothing and runs no pass, off a terminal the pass goes on, and approving it lands its
    article."""
    script = Script({"things/curator": list(ADMITS), "topics/curator": list(TOPIC)})
    late_list_left(tmp_path, script)
    monkeypatch.setattr(first_run, "_at_a_terminal", lambda: True)
    with open_it(tmp_path, script) as workspace:
        shown: list[Pile] = []
        ran = len(script.ran)
        out = run(workspace, choose=lambda pile: shown.append(pile))
        assert out is not None and out.quit and out.result is None and len(script.ran) == ran
        assert [listed.agent for listed in shown[0].lists] == ["topics/curator"] and shown[0].late
        assert out.lines == ["", QUIT.format("topics"), "", "1 proposal waiting · cerebro review",
                             "next      cerebro serve --once"]
        assert run(workspace, choose=None) is None  # off a terminal: the pass goes on and says the list waits
        out = run(workspace, choose=Choices.starter)
        assert out is not None and out.result is not None and "topics/renewals" in articles(workspace)
        assert not [p for p in proposals(workspace.store, "waiting") if p.by.name == "topics/curator"]


@pytest.mark.usefixtures("every_curator_weighs")  # alpha holds no record, and is asked all the same
def test_escape_names_a_collection_a_row_was_proposed_to_never_one_whose_curator_proposed_nothing(
        tmp_path: Path) -> None:
    """The hand admit is offered for the rows' own curators alone."""
    root = lay_out_workspace(tmp_path)
    (root / "agents" / "alpha").mkdir(parents=True)
    (root / "agents" / "alpha" / "curator.md").write_text(CURATOR)
    with open_it(root, Script({"things/curator": list(ADMITS), "alpha/curator": []})) as workspace:
        shown: list[Pile] = []
        out = run(workspace, choose=lambda pile: shown.append(pile))
        assert "alpha/curator" in [listed.agent for listed in shown[0].lists]  # asked, and proposed nothing
        assert out is not None and QUIT.format("things") in out.lines


def test_the_plain_accept_line_names_a_collection_a_row_was_proposed_to() -> None:
    pile = Pile([Listed("alpha/curator", "alpha", [], state="nothing"),
                 Listed("parts/curator", "parts", [Row("left-out:1", "parts/curator", "parts", "Bolt", "not a part",
                                                       left_out=True)]),
                 Listed("things/curator", "things", [Row("p_1", "things/curator", "things", "Alder Works")])])
    ran = CliRunner().invoke(click.command()(lambda: cli._accept_line(pile)), [], input="q\n")
    assert "Accept admits the ticked ones, and the rest wait. Add with cerebro admit things/<name>." in ran.output


def test_off_a_terminal_a_waiting_late_list_is_never_asked_from_a_pipe_and_the_pass_goes_on(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The late list opens only at a terminal or with --yes: --plain with its
    answers on a pipe would quit at the end of the input and never run its pass."""
    script = Script({"things/curator": list(ADMITS), "topics/curator": list(TOPIC)})
    late_list_left(tmp_path, script)
    monkeypatch.setattr(first_run, "_at_a_terminal", lambda: False)
    asked: list[Pile] = []
    with open_it(tmp_path, script) as workspace:
        assert run(workspace, choose=lambda pile: asked.append(pile)) is None and asked == []
