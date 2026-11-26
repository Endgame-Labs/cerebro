"""A first run on a terminal shows what it is doing: the sources read on a live step whose
sub-lines count what each listed, each curator of a round on a step of its own, queued, reviewing, then resolved to
what it proposed or why it failed, the approval on a step that resolves to what it admitted, and the build in the
pass's region, with a distill pass's records counted as it goes and a failure wrapped under its row. The clock is
driven by hand and the spinner stands still, so each frame is the same on every run."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
from test_first_run import every_curator_weighs  # noqa: F401  (a fixture)
from test_output import Terminal, env

from cerebro.output import Printer, strip_escapes
from cerebro.progress import FirstRunScreen, LiveProgress
from cerebro.runner import Progress

CHECKS = Path(__file__).resolve().parents[1] / "checks"


def screen_of(drawn: str) -> str:
    """What a terminal shows after drawn: each \\r goes to the line's start, \\x1b[K clears the rest of it, \\x1b[A
    goes up one, as the printer's live lines use them, with every style dropped."""
    rows: list[str] = [""]
    row = col = 0
    for token in re.findall(r"\x1b\[[0-9;]*[A-Za-z]|\r|\n|[^\x1b\r\n]+", drawn):
        if token == "\r":
            col = 0
        elif token == "\n":
            row, col = row + 1, 0
            if row == len(rows):
                rows.append("")
        elif token == "\x1b[K":
            rows[row] = rows[row][:col]
        elif token.startswith("\x1b[") and token.endswith("A"):
            row = max(row - int(token[2:-1] or 1), 0)
        elif token == "\x1b[J":  # the rest of the screen cleared, from the cursor
            rows[row] = rows[row][:col]
            del rows[row + 1:]
        elif token.startswith("\x1b["):
            continue
        else:
            rows[row] = rows[row][:col].ljust(col) + token + rows[row][col + len(token):]
            col += len(token)
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows)


def still(width: int = 100) -> tuple[FirstRunScreen, Terminal, list[float]]:
    """A first run's screen on a styled terminal whose clock stands until the test moves it."""
    clock = [0.0]
    stream = Terminal()
    p = Printer(stream, stream, getenv=env(TERM="xterm-256color").get, dark=True, width=lambda: width,
                now=lambda: clock[0], tick=3600)
    return FirstRunScreen(p, sources=["north", "east", "local", "west"], now=lambda: clock[0],
                          ticking=False, height=lambda: 60), stream, clock


# ---- reading the sources ---------------------------------------------------------------------------------------------


def test_reading_the_sources_counts_every_source_in_name_order_and_collapses_to_what_it_read() -> None:
    # four sources: every source from the start, 0 included, in
    # name order whichever lists first, redrawn as each lists, collapsed to one line that counts what it read
    screen, out, _ = still()
    screens = []
    screen(Progress("read", "start"))
    screens.append(screen_of(out.getvalue()))
    for name, n in (("north", 524), ("local", 0), ("west", 0), ("east", 25)):
        screen(Progress("check", "start", name))
        screen(Progress("check", "end", name, mark="done", total=n))
        screens.append(screen_of(out.getvalue()))
    screen(Progress("read", "end", total=549))
    screens.append(screen_of(out.getvalue()))
    assert screens == [
        "⠋ Reading sources  0.0s\n    east: 0 records\n    local: 0 records\n    north: 0 records\n    west: 0 records",
        "⠋ Reading sources  0.0s\n    east: 0 records\n    local: 0 records\n    north: 524 records\n    west: 0 records",
        "⠋ Reading sources  0.0s\n    east: 0 records\n    local: 0 records\n    north: 524 records\n    west: 0 records",
        "⠋ Reading sources  0.0s\n    east: 0 records\n    local: 0 records\n    north: 524 records\n    west: 0 records",
        "⠋ Reading sources  0.0s\n    east: 25 records\n    local: 0 records\n    north: 524 records\n    west: 0 records",
        "✓ Read 549 records from 4 sources  0.0s",
    ]


def test_a_source_that_fails_its_read_takes_the_warning_mark_with_why_and_one_record_reads_singular() -> None:
    screen, out, clock = still()
    screen(Progress("read", "start"))
    screen(Progress("check", "end", "east", mark="done", total=1))
    screen(Progress("check", "end", "west", mark="failed", why="ConnectError: refused"))
    clock[0] = 12.4
    screen(Progress("read", "end", total=1))
    assert screen_of(out.getvalue()) == "! Read 1 record from 1 source; west: ConnectError: refused  12s"


def test_a_read_that_lists_nothing_leaves_no_line() -> None:
    screen, out, _ = still()
    screen(Progress("read", "start"))
    screen(Progress("read", "end", total=0))
    assert screen_of(out.getvalue()) == ""


# ---- the curators of a round -----------------------------------------------------------------------------------------


def round_of(screen: FirstRunScreen, *curators: tuple[str, str]) -> None:
    screen(Progress("discover", "open", total=len(curators)))
    for id, collection in curators:
        screen(Progress("discover", "queue", id, text=collection))


def test_the_round_names_its_collections_and_each_curator_waits_on_a_queued_step_its_start_takes_on() -> None:
    # the step names the round's collections
    screen, out, _ = still()
    round_of(screen, ("parts/curator", "parts"), ("places/curator", "places"))
    assert screen_of(out.getvalue()) == ("Running discovery for parts and places collections.\n"
                                         "⠋ Queued for parts collection  0.0s\n⠋ Queued for places collection  0.0s")
    screen(Progress("discover", "start", "parts/curator", text="parts"))
    assert "⠋ Reviewing sources for parts collection  0.0s\n⠋ Queued for places collection  0.0s" in screen_of(
        out.getvalue())
    screen(Progress("discover", "end", "parts/curator", mark="done", text="3 articles to parts collection"))
    screen(Progress("discover", "end", "places/curator", mark="budget"))
    shown = screen_of(out.getvalue())
    assert "✓ Proposed 3 articles to parts collection" in shown and "✗ Budget reached for places collection" in shown
    assert "Queued" not in shown and "⠋" not in shown


def test_curators_run_side_by_side_each_with_its_clock_and_resolve_to_what_they_did() -> None:
    # a curator's step names its collection, with a failure: three running, then two done and one failed
    screen, out, clock = still()
    ids = [("parts/curator", "parts"), ("places/curator", "places"), ("things/curator", "things")]
    round_of(screen, *ids)
    for id, coll in ids:
        screen(Progress("discover", "start", id, text=coll))
    running = screen_of(out.getvalue()).split("\n", 1)[1]
    assert running == ("⠋ Reviewing sources for parts collection  0.0s\n⠋ Reviewing sources for places collection  0.0s\n"
                       "⠋ Reviewing sources for things collection  0.0s")
    clock[0] = 64.0
    screen(Progress("discover", "end", "parts/curator", mark="done", text="2 articles to parts collection"))
    screen(Progress("discover", "end", "places/curator", mark="nothing"))
    screen(Progress("discover", "end", "things/curator", mark="failed",
                    text="Discovery for things collection did not finish", why="TimeoutError: no answer in 120s"))
    assert screen_of(out.getvalue()).split("\n")[1:] == [
        "✓ Proposed 2 articles to parts collection  1m 4s",
        "✓ Nothing new for places collection  1m 4s",  # names its collection
        "✗ Discovery for things collection did not finish  1m 4s",
        "    TimeoutError: no answer in 120s"]  # why, muted under its line


@pytest.mark.usefixtures("every_curator_weighs")  # parts is asked in round 1, before a parent of its parts is proposed
def test_a_later_rounds_curator_whose_evidence_did_not_move_ends_its_step_as_nothing_new(tmp_path: Path) -> None:
    from test_first_run_structure import Script, by_record, lay_out_workspace, open_it

    from cerebro.first_run import run

    class Later(Script):  # parts proposes in round 2 alone
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            if agent.id == "parts/curator" and agent.id in self.ran:
                self.calls[agent.id] = [by_record("P1")]
            return super().run(agent, evidence, tools, budget)

    screen, out, _ = still()
    with open_it(lay_out_workspace(tmp_path), Later({"things/curator": [by_record("T1")], "parts/curator": []})) as w:
        run(w, choose=lambda pile: None, on=screen)
    shown = screen_of(out.getvalue()).split("\n")
    # each round in its order, every step it started resolved: none left running, none gone with no line
    assert shown[shown.index("Running discovery for parts and things collections.") + 1:] == [
        "✓ Nothing new for parts collection  0.0s", "✓ Proposed 1 article to things collection  0.0s",
        "✓ Proposed 1 article to parts collection  0.0s", "✓ Nothing new for things collection  0.0s",
        "✓ Nothing new for parts collection  0.0s", "✓ Nothing new for things collection  0.0s",
        "✓ Reviewed 2 proposals  0.0s"]


def test_a_question_or_the_list_takes_the_screen_once_what_is_live_has_gone_after_a_blank_line() -> None:
    screen, out, _ = still()
    round_of(screen, ("parts/curator", "parts"), ("places/curator", "places"))
    screen(Progress("discover", "end", "parts/curator", mark="done", text="1 article to parts collection"))
    screen.settle()
    shown = screen_of(out.getvalue())
    assert "Queued" not in shown and shown.endswith("✓ Proposed 1 article to parts collection  0.0s")
    assert screen_of(out.getvalue() + "list").endswith("0.0s\n\nlist")  # the list stands apart from the steps


# ---- the approval ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("n", "end", "want"), [
    (6, {"mark": "done", "text": "Accepted 6 articles for things collection: its curator makes them as the build runs"},
     "✓ Accepted 6 articles for things collection: its curator makes them as the build runs  0.0s"),
    (46, {"mark": "done", "text": "Accepted 46 articles for 2 collections: their curators make them as the build runs",
          "why": "parts: 25 articles\nthings: 21 articles"},
     ("✓ Accepted 46 articles for 2 collections: their curators make them as the build runs  0.0s\n"
      "    parts: 25 articles\n    things: 21 articles")),
])
def test_an_approval_is_recorded_on_a_step_that_resolves_to_what_it_admitted(n: int, end: dict[str, str],
                                                                               want: str) -> None:
    # an approval accepts articles for its collections
    screen, out, _ = still()
    screen(Progress("approve", "start", total=n))
    assert screen_of(out.getvalue()) == f"\n⠋ Recording {n} approvals  0.0s"
    screen(Progress("approve", "end", **end))
    assert screen_of(out.getvalue()) == "\n" + want


@pytest.mark.parametrize(("n", "noun"), [(1, "proposal"), (122, "proposals")])
def test_the_list_is_arranged_on_a_step_that_resolves_to_what_it_reviewed(n: int, noun: str) -> None:
    screen, out, clock = still()
    screen(Progress("arrange", "start", total=n))
    assert screen_of(out.getvalue()) == f"⠋ Reviewing {n} {noun}  0.0s"
    clock[0] = 64.0
    screen(Progress("arrange", "end", total=n))
    assert screen_of(out.getvalue()) == f"✓ Reviewed {n} {noun}  1m 4s"


def test_the_approvals_end_counts_the_articles_it_accepted_by_collection_in_name_order() -> None:
    from cerebro.first_run import Applied, Choices, Listed, Pile, Row, _admitted

    pile = Pile([Listed("t/curator", "things", [Row(f"o{n}", "t/curator", "things", f"O{n}")
                                                       for n in range(21)]),
                 Listed("p/curator", "parts", [Row(f"c{n}", "p/curator", "parts", f"C{n}") for n in range(25)])])
    every = [row.id for row in pile.rows()]
    one_each = Choices([[id] for id in every])
    assert _admitted(pile, one_each, Applied(accepted=every)) == {
        "mark": "done", "text": "Accepted 46 articles for 2 collections: their curators make them as the build runs",
        "why": "parts: 25 articles\nthings: 21 articles"}
    assert _admitted(pile, Choices([["c1", "c2"], ["c3"]]), Applied(accepted=["c1", "c2", "c3"])) == {
        "mark": "done", "text": "Accepted 2 articles for parts collection: its curator makes them as the build runs"}
    assert _admitted(pile, one_each, Applied()) == {}


# ---- the build's region ----------------------------------------------------------------------------------------------


def region(width: int = 100) -> tuple[LiveProgress, list[float]]:
    clock = [0.0]
    stream = Terminal()
    p = Printer(stream, stream, getenv=env(TERM="xterm-256color").get, dark=True, width=lambda: width,
                now=lambda: clock[0], tick=3600)
    return LiveProgress(p, now=lambda: clock[0], ticking=False, height=lambda: 60), clock


def test_a_distill_pass_under_way_counts_its_records_done_of_those_it_goes_through() -> None:
    # the region at 25s and 35s: the row reads as the one it leaves
    screen, clock = region()
    screen(Progress("distill", "open", total=2))
    clock[0] = 10.0
    screen(Progress("distill", "start", "parts"))
    screen(Progress("distill", "count", "parts", total=2, done=0, text="0 facts"))
    clock[0] = 25.0
    assert [strip_escapes(line) for line in screen.frame()] == [
        "Distilling", "  ⠋ parts  0 of 2 records • 0 facts  15s", "", "0 of 2 complete • 1 in flight • 25s"]
    screen(Progress("distill", "count", "parts", total=2, done=1, text="3 facts"))
    clock[0] = 70.0
    screen(Progress("distill", "end", "parts", mark="done", text="2 records • 5 facts"))
    assert [strip_escapes(line) for line in screen.frame()] == [
        "Distilling", "  ✓ parts  2 records • 5 facts  1m 0s", "", "1 of 2 complete • 0 in flight • 1m 10s"]


def test_a_failure_threads_under_its_row_wrapped_at_its_spaces_and_a_line_said_goes_under_the_groups() -> None:
    screen, clock = region(width=40)
    screen(Progress("compile", "open", total=1))
    screen(Progress("compile", "start", "parts/gamma"))
    clock[0] = 30.0
    screen(Progress("compile", "end", "parts/gamma", mark="failed",
                    why="parts/writer: TimeoutError: the model gave no answer in 120s"))
    screen.say("took over the leases of pid 4242")
    assert [strip_escapes(line) for line in screen.frame()] == [
        "Compiling",
        "  ✗ gamma  articles/parts/gamma.md  30s",
        "    parts/writer: TimeoutError: the",
        "    model gave no answer in 120s",
        "",
        "took over the leases of pid 4242",
        "",
        "1 of 1 complete • 0 in flight • 30s"]


def test_a_region_under_lines_stands_apart_by_a_blank_line_and_leaves_one_after_its_last_frame() -> None:
    screen, _ = region()
    screen.above = True
    screen.begin()
    screen(Progress("compile", "open", total=1))
    screen(Progress("compile", "end", "a/b", mark="done", text="articles/a/b.md"))
    screen.finish()
    drawn = strip_escapes(screen.p.out.getvalue())  # type: ignore[attr-defined]
    assert drawn.startswith("\n") and drawn.endswith("1 of 1 complete • 0 in flight • 0s\n\n")


def test_the_build_is_the_passs_region_and_a_line_the_steps_show_is_not_said_again() -> None:
    screen, out, clock = still()
    screen.say("stage     discover: reading the sources")
    screen.say("note      took over the leases of pid 4242")
    assert screen_of(out.getvalue()) == "note      took over the leases of pid 4242"
    screen(Progress("build", "start"))
    screen(Progress("compile", "open", total=1))
    screen(Progress("compile", "start", "parts/gamma"))
    clock[0] = 5.0
    screen(Progress("compile", "end", "parts/gamma", mark="done", text="articles/parts/gamma.md"))
    screen(Progress("build", "end"))
    assert screen_of(out.getvalue()).split("\n")[1:] == [
        "", "Compiling", "  ✓ gamma  articles/parts/gamma.md  5s", "", "1 of 1 complete • 0 in flight • 5s"]


# ---- the engine hands the events --------------------------------------------------------------------------------------


def test_a_first_run_hands_its_screen_each_step_in_order_and_changes_nothing_it_decides(tmp_path: Path) -> None:
    sys.path.insert(0, str(CHECKS))
    from live_first_run import stand_in

    from cerebro import first_run
    from cerebro.first_run import Choices

    events: list[Progress] = []
    out = first_run.run(stand_in(tmp_path / "a"), choose=Choices.starter, on=events.append)
    plain = first_run.run(stand_in(tmp_path / "b"), choose=Choices.starter)
    assert out is not None and plain is not None
    assert [len(a.accepted) for a in out.applied] == [len(a.accepted) for a in plain.applied]
    assert sorted(out.result.compiled) == sorted(plain.result.compiled)  # type: ignore[union-attr]
    said = [(e.stage, e.kind) for e in events]
    assert said[:2] == [("read", "start"), ("check", "start")]
    assert said.index(("read", "end")) < said.index(("discover", "open")) < said.index(("approve", "start"))
    assert said.index(("discover", "open")) < said.index(("arrange", "start")) < said.index(("arrange", "end"))
    assert said.index(("arrange", "end")) < said.index(("approve", "start"))
    assert said.index(("approve", "end")) < said.index(("build", "start")) < said.index(("compile", "open"))
    assert said[-1] == ("build", "end")
    queued = [e.key for e in events if e.stage == "discover" and e.kind == "queue"]
    ended = [e.key for e in events if e.stage == "discover" and e.kind == "end" and e.mark]
    assert queued and ended[:len(queued)] == queued  # round 1's each resolves, in the round's order
    read = next(e for e in events if e.stage == "read" and e.kind == "end")
    assert read.total == 6 and any(e.stage == "check" and e.kind == "end" and e.total == 6 for e in events)
    approved = next(e for e in events if e.stage == "approve" and e.kind == "end")
    assert approved.mark == "done" and approved.text.startswith("Accepted ")


# ---- the end of a run on a terminal ----------------------------------------------------------------------------------


def test_a_failed_curators_why_wraps_at_its_spaces_under_its_line() -> None:
    screen, out, _ = still(width=40)
    screen(Progress("discover", "start", "parts/curator", text="parts"))
    screen(Progress("discover", "end", "parts/curator", mark="failed", text="Discovery for parts collection did not "
                    "finish", why="TimeoutError: the stand-in model gave no answer in 120s"))
    rows = screen_of(out.getvalue()).split("\n")
    assert rows == ["✗ Discovery for parts collection",  # room left for its time, at the end of its last line
                    "  did not finish  0.0s",
                    "    TimeoutError: the stand-in model",
                    "    gave no answer in 120s"]
    assert all(len(row) < 40 for row in rows)


def test_the_passs_warnings_thread_under_their_rows_three_at_most_and_the_rest_are_counted() -> None:
    screen, _clock = region()
    screen(Progress("compile", "open", total=2))
    screen(Progress("compile", "end", "parts/gamma", mark="done", text="articles/parts/gamma.md"))
    screen(Progress("compile", "end", "parts/delta", mark="failed", why="parts/writer: TimeoutError: no answer"))
    lines = [f"parts/writer on parts/gamma: Summary: dropped {n}: it cites nothing" for n in range(5)]
    lines += ["parts/writer on parts/delta: TimeoutError: no answer", "things/other: a line of no row here"]
    shown = screen.thread(lines)
    assert shown == {*lines[:3], lines[5]}  # past the cap, and of no row, it is said after the region
    frame = [strip_escapes(line) for line in screen.frame()]
    assert frame[:9] == ["Compiling", "  ✓ gamma  articles/parts/gamma.md  0s",
                         "    Summary: dropped 0: it cites nothing", "    Summary: dropped 1: it cites nothing",
                         "    Summary: dropped 2: it cites nothing", "    and 2 more",
                         "  ✗ delta  articles/parts/delta.md  0s", "    parts/writer: TimeoutError: no answer", ""]


def test_a_pass_ends_on_a_terminal_with_a_table_and_says_only_what_its_region_did_not(
        capsys: pytest.CaptureFixture[str]) -> None:
    from cerebro import cli
    from cerebro.runner import PassResult

    screen, out, _ = still()
    screen(Progress("build", "start"))
    screen(Progress("compile", "open", total=1))
    screen(Progress("compile", "end", "parts/gamma", mark="done", text="articles/parts/gamma.md"))
    screen(Progress("build", "end"))
    result = PassResult(listed={"notes": 3}, read=3, extracted=7, compiled=["parts/gamma"],
                        warnings=["parts/writer on parts/gamma: Summary: dropped one: it cites nothing",
                                  "things/other: a warning of no row"])
    cli._print_end(screen, result, fails=False)
    shown = screen_of(out.getvalue())
    assert "    Summary: dropped one: it cites nothing" in shown
    for row in ("Sources checked   1", "Records landed    3", "Facts appended    7", "Articles written  1"):
        assert row in shown, row
    assert "Agents run" not in shown  # a count of none is left out
    # each said once, under its row or on stderr, and the first named again by the last line
    assert shown.count("dropped one") == 2 and shown.count("a warning of no row") == 1
    assert "cerebro: warning: things/other: a warning of no row" in shown
    last = " ".join(shown[shown.rindex("!"):].split())  # the last line, broken at a space past the width
    assert last == "! Done with warnings. Check: parts/writer on parts/gamma: Summary: dropped one: it cites nothing (and " \
                   "1 more above)"
    assert all(len(row) < 100 for row in shown.split("\n"))
    assert "wrote     " not in shown and "listed    " not in shown


def test_what_waits_for_the_person_is_what_the_last_line_names_and_nothing_follows_it(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from test_first_run import ADMITS, YOU, Script, lay_out_workspace, open_it, settled

    from cerebro import cli
    from cerebro.first_run import run

    def choose(pile: Any) -> Any:  # Alder and Initech ticked, and a no to Initech given while the list is open
        initech = next(r.id for r in pile.rows() if r.name == "Initech")
        workspace.call(YOU, "decide", {"reject": [{"id": initech, "reason": "not ours"}]})
        return settled(pile, {r.id for r in pile.rows() if r.name in ("Alder Works", "Initech")})

    screen, out, _ = still()
    admits = [*ADMITS[:2], ("admit", {"record": {"source": "crm", "id": "001C"}, "reason": "One call.", "group": "you own (crm)"})]
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": admits})) as workspace:
        cli._print_first_run(run(workspace, on=screen, choose=choose), workspace, screen=screen)
    shown = screen_of(out.getvalue()).split("\n")
    assert any(line.startswith("notice    p_") and line.endswith(" is already rejected") for line in shown[:-2]), shown
    assert "Check: 1 more is waiting. Run cerebro review" in " ".join(shown[-2:]) and capsys.readouterr().out == ""


def test_ctrl_c_during_a_first_run_says_one_line_and_exits_130_with_no_traceback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from cerebro import cli, first_run
    from cerebro.app import Cerebro

    root = tmp_path / "w"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1", "--timezone", "UTC"]).exit_code == 0
    monkeypatch.setattr(cli, "open_workspace", lambda path: Cerebro(path, trusted=True))

    def stopped(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(first_run, "run", stopped)
    ran = CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)])
    assert ran.exit_code == 130, ran.output
    assert "Traceback" not in ran.output and "Aborted" not in ran.output
    assert ran.output.rstrip().endswith("Stopped. Run cerebro serve --once to go on from where it stopped.")


# ---- tall and narrowed screens ---------------------------------------------------------------------------------------


def test_live_steps_taller_than_the_screen_show_their_tail_so_no_redraw_leaves_a_copy_behind() -> None:
    stream = Terminal()
    p = Printer(stream, stream, getenv=env(TERM="xterm-256color").get, dark=True, width=lambda: 100,
                now=lambda: 0.0, tick=3600, height=lambda: 5)
    steps = [p.step_along(f"Queued for part{n} collection") for n in range(12)]
    drawn = stream.getvalue().split("\r\x1b[K")[-1]
    assert len(drawn.split("\n")) == 4 and "part11" in drawn and "part7 " not in drawn  # the tail, a row to spare
    for step in steps:
        step.clear()


def test_a_terminal_narrowed_under_live_steps_keeps_the_lines_above_them() -> None:
    from cerebro.output import _climb

    assert _climb([60, 60], 100, 100) == 2 and _climb([60, 60], 100, 50) == 2  # narrowed: a row each, as drawn
    assert _climb([60, 60], 50, 50) == 4  # drawn at this width: each as it wraps now
    width = [100]
    stream = Terminal()
    p = Printer(stream, stream, getenv=env(TERM="xterm-256color").get, dark=True, width=lambda: width[0],
                now=lambda: 0.0, tick=3600)
    p.line("Running discovery for parts and things collections.")
    step = p.step_along("Reviewing sources for parts collection, which takes a while to read")
    width[0] = 30
    step.relabel("Reviewing sources for parts collection")
    assert stream.getvalue().count("\x1b[A") == 0  # one live row: it climbs none, and the line above stays
    step.clear()


def test_a_resolved_step_breaks_at_a_space_on_a_narrow_terminal() -> None:
    stream = Terminal()
    p = Printer(stream, stream, getenv=env(TERM="xterm-256color").get, dark=True, width=lambda: 40,
                now=lambda: 0.0, tick=3600)
    p.step("Reviewing").done("Proposed 6 articles to opportunities collection, and 2 more to parts")
    shown = screen_of(stream.getvalue())
    assert all(len(row) < 40 for row in shown.split("\n")), shown
    assert "opportunities" in shown and "collection," in shown  # no word cut


def test_a_fault_of_the_drawing_stops_the_live_display_once_and_never_reaches_the_run() -> None:
    screen, out, _ = still()
    screen(Progress("build", "start"))

    def broken(*args: object, **kwargs: object) -> list[str]:
        raise RuntimeError("the drawing broke")

    assert screen.region is not None
    screen.region._frame = broken  # type: ignore[method-assign]
    screen(Progress("compile", "open", total=1))  # raises inside: the screen takes it
    screen(Progress("compile", "start", "parts/gamma"))
    screen.say("ran       parts/writer on parts/gamma: wrote articles/parts/gamma.md")
    screen.settle()
    screen.finish()
    drawn = strip_escapes(out.getvalue())
    assert screen.broken == "RuntimeError: the drawing broke"
    assert drawn.count("The live display stopped (RuntimeError: the drawing broke); the rest prints as plain "
                       "lines.") == 1
    assert drawn.rstrip().endswith("ran       parts/writer on parts/gamma: wrote articles/parts/gamma.md")


@pytest.mark.parametrize("under_init", [True, False])
def test_inside_init_the_run_stands_apart_from_the_walks_summaries_by_a_blank_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], under_init: bool) -> None:
    # inside init: a blank line under the Ready block, then the question; once it gives way, a blank line, then the run
    import click
    from click.testing import CliRunner

    from cerebro import cli, first_run
    from cerebro.app import Cerebro

    root = tmp_path / "w"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1", "--timezone", "UTC"]).exit_code == 0
    monkeypatch.setattr(cli, "open_workspace", lambda path: Cerebro(path, trusted=True))
    screen, out, _ = still()
    monkeypatch.setattr(cli, "_first_run_screen", lambda workspace, plain, **kwargs: screen)

    def reading(workspace: object, **kwargs: object) -> None:
        screen(Progress("read", "start"))
        raise KeyboardInterrupt

    monkeypatch.setattr(first_run, "run", reading)
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(first_run, "begins", lambda workspace: True)
    monkeypatch.setattr(click, "prompt", lambda text, **kwargs: print(f"{text}: ", end="", file=sys.stderr) or "")
    capsys.readouterr()
    with pytest.raises(click.exceptions.Exit):
        click.Context(cli.serve).invoke(cli.serve, root=root, once=True, port=None, host=None, allowed=(),
                                        under_init=under_init)
    assert capsys.readouterr().err.startswith("\nWhat do you want to see first?" if under_init else "What")
    drawn = out.getvalue()
    assert drawn.startswith("\n\r\x1b[K") == under_init  # a blank line, then the step, only under init
    assert "Reading sources" in drawn


# ---- the late list --------------------------------------------------------------------------------------------------


def test_the_late_list_opens_under_the_builds_region_drawn_a_last_time_with_its_own_warnings_under_its_rows(
        ) -> None:
    # a run: the region's finish draws it a last time, whole, with a blank line, and only
    # then does the late curator's list open, under it, with its own header and nothing more
    screen, out, clock = still()
    screen(Progress("build", "start"))
    screen(Progress("compile", "open", total=2))
    screen(Progress("compile", "start", "parts/gamma"))
    screen(Progress("compile", "end", "parts/gamma", mark="done", text="articles/parts/gamma.md"))
    screen(Progress("compile", "end", "parts/delta", mark="fresh", text="nothing it reads has moved"))
    screen(Progress("agents", "open", total=1))
    screen(Progress("agents", "start", "things/curator"))
    clock[0] = 4.0
    screen(Progress("agents", "end", "things/curator", mark="done", text="proposed 2", cost="$0.0000"))
    screen(Progress("build", "end", why="parts/writer on parts/gamma: Summary: dropped one: it cites nothing"))
    screen.settle()  # the list takes the screen
    shown = screen_of(out.getvalue() + "Working set proposal")
    assert not re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", shown)  # nothing turns as the list opens
    assert shown.endswith("1 of 1 complete • 0 in flight • 4s\n\nWorking set proposal")
    assert "    Summary: dropped one: it cites nothing" in shown  # the build's own warning, under its own row
    assert "delta" not in shown  # nothing moved: no line for a fresh article
    # a later build's region, or the run's end, does not take the first build's warnings again
    screen(Progress("build", "start"))
    screen(Progress("compile", "open", total=1))
    screen(Progress("compile", "end", "parts/gamma", mark="done", text="articles/parts/gamma.md"))
    threaded = screen.close(["parts/writer on parts/gamma: Summary: dropped one: it cites nothing"])
    assert threaded == {"parts/writer on parts/gamma: Summary: dropped one: it cites nothing"}
    assert screen_of(out.getvalue()).count("dropped one") == 1



def test_inside_init_the_first_approval_takes_the_discovery_round_off_the_screen() -> None:
    # a block begins once the Ready block gave way, and the approval drops it: what it approved
    # comes first, one blank line under what stood above the run, and the rows of the round go
    clock = [0.0]
    stream = Terminal()
    stream.write("✗ No MCP clients installed\n  Install MCP clients later with cerebro init.\n")
    p = Printer(stream, stream, getenv=env(TERM="xterm-256color").get, dark=True, width=lambda: 100,
                now=lambda: clock[0], tick=3600, height=lambda: 40)
    screen = FirstRunScreen(p, sources=["notes"], ticking=False, height=lambda: 40, region=True)
    p.line("")
    screen(Progress("read", "start"))
    screen(Progress("check", "end", "notes", mark="done", total=3))
    screen(Progress("read", "end", total=3))
    screen(Progress("discover", "open", total=2))
    for id, coll in (("parts/curator", "parts"), ("things/curator", "things")):
        screen(Progress("discover", "queue", id, text=coll))
    screen(Progress("discover", "end", "parts/curator", mark="done", text="2 articles to parts collection"))
    screen(Progress("discover", "end", "things/curator", mark="failed", text="Discovery for things collection did "
                    "not finish", why="TimeoutError: no answer"))
    screen.settle()
    screen.list_shown(True)  # drawn on its own screen, which cleared it
    screen(Progress("approve", "start", total=2))
    screen(Progress("approve", "end", mark="done", text="Admitted 2 articles to parts collection"))
    assert screen_of(stream.getvalue()) == ("✗ No MCP clients installed\n  Install MCP clients later with cerebro init."
                                           "\n\n✓ Admitted 2 articles to parts collection  0.0s")


def test_outside_init_the_round_stays_and_a_single_blank_line_parts_it_from_the_approval() -> None:
    screen, out, _ = still()
    screen(Progress("discover", "start", "parts/curator", text="parts"))
    screen(Progress("discover", "end", "parts/curator", mark="done", text="2 articles to parts collection"))
    screen.settle()
    screen.list_shown(True)
    screen(Progress("approve", "start", total=2))
    screen(Progress("approve", "end", mark="done", text="Admitted 2 articles to parts collection"))
    assert screen_of(out.getvalue()) == ("✓ Proposed 2 articles to parts collection  0.0s\n\n"
                                        "✓ Admitted 2 articles to parts collection  0.0s")


def test_the_late_lists_approval_stands_one_blank_line_under_the_regions_last_frame() -> None:
    screen, out, _ = still()
    screen(Progress("build", "start"))
    screen(Progress("compile", "open", total=1))
    screen(Progress("compile", "end", "parts/gamma", mark="done", text="articles/parts/gamma.md"))
    screen(Progress("build", "end"))
    screen.settle()
    screen.list_shown(True)
    screen(Progress("approve", "start", total=1))
    screen(Progress("approve", "end", mark="done", text="Admitted 1 article to parts collection"))
    assert screen_of(out.getvalue()).endswith("1 of 1 complete • 0 in flight • 0s\n\n"
                                              "✓ Admitted 1 article to parts collection  0.0s")



def test_a_curator_of_no_collection_with_nothing_to_propose_keeps_the_bare_line() -> None:
    screen, out, _ = still()
    screen(Progress("discover", "start", "helper", text=""))
    screen(Progress("discover", "end", "helper", mark="nothing"))
    assert screen_of(out.getvalue()) == "✓ Nothing new  0.0s"


@pytest.mark.parametrize(("curators", "line"), [
    (1, "One more list: a curator that could not propose before the build proposes now."),
    (2, "One more list: curators that could not propose before the build propose now.")])
def test_a_late_list_is_announced_under_the_finished_region_and_the_line_goes_with_the_list(
        curators: int, line: str) -> None:
    screen, out, _ = still()
    screen(Progress("build", "start"))
    screen(Progress("compile", "open", total=1))
    screen(Progress("compile", "end", "parts/gamma", mark="done", text="articles/parts/gamma.md"))
    screen(Progress("build", "end"))
    screen(Progress("late", "open", total=curators))
    screen.say(line)  # the run says it too, for a screen that prints its lines: this one shows it once
    assert screen_of(out.getvalue()).endswith(f"1 of 1 complete • 0 in flight • 0s\n\n{line}")
    screen(Progress("arrange", "start", total=1))
    screen(Progress("arrange", "end", total=1))
    screen.list_shown(True)  # drawn and cleared by its own screen
    screen(Progress("approve", "start", total=1))
    screen(Progress("approve", "end", mark="done", text="Admitted 1 article to parts collection"))
    shown = screen_of(out.getvalue())
    assert line not in shown
    assert shown.endswith("1 of 1 complete • 0 in flight • 0s\n\n✓ Admitted 1 article to parts collection  0.0s")


def test_on_a_narrow_terminal_the_late_lists_line_and_the_review_under_it_go_with_the_list_whole() -> None:
    screen, out, clock = still(width=30)
    screen(Progress("build", "start"))
    screen(Progress("build", "end"))
    screen(Progress("late", "open", total=2))
    screen(Progress("arrange", "start", total=122))
    clock[0] = 64.0
    screen(Progress("arrange", "end", total=122))
    assert screen_of(out.getvalue()).endswith("✓ Reviewed 122\n  proposals  1m 4s")  # two rows at this width
    screen.list_shown(True)
    screen(Progress("approve", "start", total=1))
    screen(Progress("approve", "end", mark="done", text="Admitted 1 article"))
    assert screen_of(out.getvalue()).endswith("nothing to run\n\n✓ Admitted 1 article  0.0s")


@pytest.mark.parametrize(("width", "rows"), [(20, 6), (80, 2)])
def test_the_late_lists_approval_erases_every_screen_row_its_line_and_the_review_took(width: int, rows: int) -> None:
    screen, out, _ = still(width=width)
    screen(Progress("build", "start"))
    screen(Progress("build", "end"))
    screen(Progress("late", "open", total=2))
    screen(Progress("arrange", "start", total=1))
    screen(Progress("arrange", "end", total=1))
    screen.list_shown(True)
    screen(Progress("approve", "start", total=1))
    assert f"\r\x1b[{rows}A\x1b[J" in out.getvalue()


def test_off_a_live_screen_the_run_says_the_line_just_before_the_late_list(tmp_path: Path) -> None:
    sys.path.insert(0, str(CHECKS))
    from live_first_run import stand_in

    from cerebro import first_run
    from cerebro.first_run import Choices, late_line

    said: list[str] = []

    def choose(pile: Any) -> Any:
        said.append(f"<list late={pile.late}>")
        return Choices.starter(pile)

    out = first_run.run(stand_in(tmp_path / "a"), choose=choose, say=said.append)
    assert out is not None and out.late is not None
    assert said[said.index("<list late=True>") - 1] == late_line(len(out.late.lists))
    assert said.count(late_line(1)) + said.count(late_line(2)) == 1  # not above the first list
