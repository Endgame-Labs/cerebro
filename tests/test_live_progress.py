"""A pass's live progress: on a terminal one region, drawn again in place as each event of the pass lands; off one,
with --plain or with NO_COLOR, the plain lines byte for byte as recorded, with no escape byte."""

from __future__ import annotations

import io
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_cli import NOTE, EachHeading
from test_output import DATA, on_a_pty

from cerebro import cli
from cerebro.app import Cerebro
from cerebro.fakes import FakeExtraction
from cerebro.output import Printer, strip_escapes
from cerebro.progress import LiveProgress
from cerebro.runner import Progress

CHECKS = Path(__file__).resolve().parents[1] / "checks"
sys.path.insert(0, str(CHECKS))
import progress_demo

AGENT = ("---\nevidence:\n  - query: { from: facts, source: local }\nheartbeat: [change]\n---\n\nSummarize the notes.\n\n"
         "# Output\n\n## Summary\n")


def lay_out_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A blank workspace with one note and one writer on a fixed article, run by the scripted model."""
    extraction = FakeExtraction()
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, agent_run=EachHeading(), extraction=extraction,
                                                                clock=lambda: datetime(2026, 11, 18, 9, tzinfo=UTC)))
    root = tmp_path / "team"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    (root / "local" / "kickoff.md").write_text(NOTE)
    (root / "agents" / "digests").mkdir(parents=True)
    (root / "agents" / "digests" / "weekly.md").write_text(AGENT)
    return root


def plain(output: str, root: Path) -> str:
    """output with the workspace's folder named <ws>, and with no notice, which says what this machine keeps."""
    return "".join(line for line in output.replace(str(root.resolve()), "<ws>").splitlines(True)
                   if not line.startswith("notice "))


def printer(dark: bool = True, width: int = 80) -> Printer:
    return Printer(io.StringIO(), io.StringIO(), terminal=lambda stream: True, dark=dark, width=lambda: width,
                   getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))


# ---- off a terminal: the plain lines ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("flags", "env"), [([], {}), (["--plain"], {}), ([], {"NO_COLOR": "1"})])
def test_off_a_terminal_with_plain_or_no_color_a_pass_prints_its_plain_lines_byte_for_byte_as_recorded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str], env: dict[str, str]) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    passed = CliRunner(env=env).invoke(cli.main, ["serve", "--once", "--workspace", str(root), *flags])
    assert passed.exit_code == 0, passed.output
    assert plain(passed.output, root) == (DATA / "progress-plain.txt").read_text()
    assert "\x1b" not in passed.output


def _demo(tmp_path: Path, plain: bool, variables: dict[str, str]) -> str:
    code = (f"import sys\nsys.path.insert(0, {str(CHECKS)!r})\nimport progress_demo\n"
            f"sys.exit(progress_demo.stand_in_pass({plain}, pause=0))\n")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    got = on_a_pty(code, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"), "TMPDIR": str(tmp_path),
                          "CEREBRO_SECRETS_STORE": "file", "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
                          **variables}, seconds=60)
    return got.decode().replace("\r\n", "\n")


def _lines(output: str) -> list[str]:
    return sorted(re.sub(r"\S*cerebro-progress-demo-\S*", "<ws>", line) for line in output.splitlines()
                  if not line.startswith("notice "))


def test_on_a_pseudo_terminal_with_no_color_or_plain_a_pass_prints_the_plain_lines_with_no_escape_byte(
        tmp_path: Path) -> None:
    colorless = _demo(tmp_path / "a", False, {"TERM": "xterm-256color", "NO_COLOR": "1"})
    flagged = _demo(tmp_path / "b", True, {"TERM": "xterm-256color", "COLORFGBG": "15;0"})
    for got in (colorless, flagged):
        assert "\x1b" not in got and "stage     list: checking the sources\n" in got, got
        assert "ran       parts/writer: failed, TimeoutError: the stand-in model gave no answer\n" in got
        assert got.rstrip().endswith("Done: 2 written, 1 failed.")
    assert _lines(colorless) == _lines(flagged)  # the same lines; side by side, the compiles' may end in any order


def test_on_a_pseudo_terminal_a_pass_draws_one_region_in_place_and_leaves_it_whole_above_the_summary(
        tmp_path: Path) -> None:
    got = _demo(tmp_path, False, {"TERM": "xterm-256color", "COLORFGBG": "15;0"})
    assert "\x1b[A\x1b[K" in got  # drawn again in place
    assert "stage     " not in got and "\nran       " not in got  # the region takes the plain lines' place
    text = strip_escapes(got)
    last = text[text.rindex("Distilling"):]  # the region's last frame, drawn whole, then the summary
    assert last.index("Distilling") < last.index("Compiling") < last.index("3 of 3 complete • 0 in flight")
    assert "  ✓ local  5 records • 5 facts  " in last
    assert "  ✓ notes  articles/things/notes.md  " in last and "  ✓ review  articles/things/review.md  " in last
    assert "  ✗ writer  articles/parts/writer.md  " in last
    assert "    parts/writer: TimeoutError: the stand-in model gave no answer\n" in last
    # then the end: a table of what the pass did, and the last line, which names what to check; what the region
    # showed, as the listing, what was written and the writer's failure, is not said again
    assert last.index("0 in flight") < last.index("Sources checked   1") < last.index("Articles written  2")
    assert "listed    " not in last and "wrote     " not in last and "Done: " not in last
    flat = " ".join(last.split())  # lines broken at a space past the width read as one
    assert flat.count("the stand-in model gave no answer") == 2  # under its row, and in the last line's Check
    assert flat.endswith("Done with warnings. Check: parts/writer: TimeoutError: the stand-in model gave no answer")
    assert not re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", last)  # nothing in flight once it ends


# ---- the frames ------------------------------------------------------------------------------------------------------


def test_the_regions_frames_in_a_light_and_a_dark_terminal_are_the_saved_ones_and_read_the_same() -> None:
    light, dark = progress_demo.frames(dark=False), progress_demo.frames(dark=True)
    assert light == (DATA / "progress-light.ansi").read_text() and dark == (DATA / "progress-dark.ansi").read_text()
    assert light != dark and strip_escapes(light) == strip_escapes(dark)
    middle, end = strip_escapes(dark).split("\n\n-- the pass ends --\n\n")
    assert middle.splitlines()[-1] == "1 of 4 complete • 3 in flight • 9s"
    assert "  ⠏ things/beta  2s" in middle and "  ✓ alpha  articles/things/alpha.md  2s" in middle
    assert end.splitlines()[-1] == "4 of 4 complete • 0 in flight • 10s"
    assert all(len(line) < 80 for line in strip_escapes(dark).splitlines())


def test_the_region_shows_what_is_checked_before_a_count_opens_and_says_when_nothing_ran() -> None:
    clock = [0.0]
    screen = LiveProgress(printer(), now=lambda: clock[0], ticking=False)
    # the time in whole seconds, three spaces before it
    assert [strip_escapes(line) for line in screen.frame()] == ["⠋ Checking     sources   0s"]
    screen(Progress("check", "start", "notes"))
    clock[0] = 0.25
    assert [strip_escapes(line) for line in screen.frame()] == ["⠹ Checking     notes   0s"]
    screen(Progress("check", "end", "notes", mark="done"))
    assert [strip_escapes(line) for line in screen._frame(1.0, final=True)] == ["Checking     nothing to run"]


def test_the_agents_block_shows_what_each_curator_admitted_and_proposed_in_columns() -> None:
    clock = [0.0]
    screen = LiveProgress(printer(width=100), now=lambda: clock[0], ticking=False)
    screen(Progress("agents", "open", total=3))
    for key, text, cost, took in (("accounts/curator", "admitted 3, proposed 11", "$0.1642", 190),
                                  ("calls/curator", "admitted 25", "$0.1442", 102),
                                  ("opportunities/curator", "admitted 3", "$0.0576", 50)):
        screen(Progress("agents", "start", key))
        clock[0] += took
        screen(Progress("agents", "end", key, mark="done", text=text, cost=cost))
    screen(Progress("agents", "end", "risk/curator", mark="warning", text="stopped at its budget " + "x" * 120,
                    cost="$0.0010"))  # a warning's long words leave the other rows' columns where they are
    assert [strip_escapes(line) for line in screen.frame() if "s/curator" in line] == [
        "  ✓ accounts/curator         admitted 3, proposed 11    $0.1642    3m 10s",
        "  ✓ calls/curator            admitted 25                $0.1442    1m 42s",
        "  ✓ opportunities/curator    admitted 3                 $0.0576    0m 50s"]
    alone = LiveProgress(printer(width=100), now=lambda: 0.0, ticking=False)
    alone(Progress("agents", "open", total=1))
    alone(Progress("agents", "end", "risk/curator", mark="warning", text="stopped at its budget", cost="$0.0010"))
    assert any("stopped at its budget" in strip_escapes(line) for line in alone.frame())


def test_each_word_from_a_source_or_a_model_is_drawn_clean() -> None:
    screen = LiveProgress(printer(), now=lambda: 1.0, ticking=False)
    for stage in ("distill", "compile", "agents"):
        screen(Progress(stage, "open", total=1))
        screen(Progress(stage, "start", f"a\x1b[31mred\x07/{stage}"))
        screen(Progress(stage, "end", f"a\x1b[31mred\x07/{stage}", mark="failed", text="b\x1b]8;;x\x1b\\",
                        why="c\x1b[2J\u202e"))
    drawn = "\n".join(screen.frame())
    for raw in ("\x1b[31m", "\x07", "\x1b]8", "\x1b[2J", "\u202e"):
        assert raw not in drawn, repr(raw)
    assert "\\x1b[31mred\\x07" in drawn and "c\\x1b[2J\\u202e" in drawn


def test_the_footer_fits_the_width_or_breaks_at_a_word_and_a_tall_region_shows_its_tail() -> None:
    clock = [0.0]
    out = io.StringIO()
    narrow = Printer(out, io.StringIO(), terminal=lambda stream: True, dark=True, width=lambda: 24,
                     getenv=lambda name: None)
    screen = LiveProgress(narrow, now=lambda: clock[0], ticking=False, height=lambda: 6, limit=2)
    screen(Progress("compile", "open", total=120))
    for n in range(5):
        screen(Progress("compile", "start", f"parts/p{n}"))
    clock[0] = 3725
    footer = [strip_escapes(line) for line in screen.frame()[-2:]]
    assert footer == ["0 of 120 complete", "5 in flight • 1h 2m"]  # broken between its parts
    assert all(len(line) < 24 for line in footer)
    assert "  … and 3 more" in [strip_escapes(line) for line in screen.frame()]
    screen.begin()
    drawn = out.getvalue().split("\r\x1b[K")[-1]
    assert len(drawn.split("\n")) == 5 and strip_escapes(drawn).endswith("5 in flight • 1h 2m")  # its tail
    for n in range(5):
        screen(Progress("compile", "end", f"parts/p{n}", mark="done"))
    screen.finish()
    whole = strip_escapes(out.getvalue().split("\x1b[K")[-1]).splitlines()
    # the last frame is whole, taller than the screen as it is, and a blank line parts it from what follows
    assert whole[0] == "Compiling" and len(whole) == 10 and whole[-1] == ""
    assert whole[1] == "  ✓ p0  articles/parts…" and whole[-3:-1] == ["5 of 120 complete", "0 in flight • 1h 2m"]


# ---- the pass's events -----------------------------------------------------------------------------------------------


def test_a_pass_hands_its_events_in_order_each_masked_and_draws_nothing_itself(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        events: list[Progress] = []
        workspace.mask = lambda line: line.replace("Vega", "****")  # type: ignore[method-assign]
        result = workspace.run_once(on=events.append)
    assert [(e.stage, e.kind, e.key, e.mark) for e in events] == [
        ("check", "start", "local", ""), ("check", "end", "local", "done"),
        ("distill", "open", "", ""), ("distill", "start", "local", ""), ("distill", "count", "local", ""),
        ("distill", "end", "local", "done"),
        ("compile", "open", "", ""), ("compile", "start", "digests/weekly", ""),
        ("compile", "end", "digests/weekly", "done")]
    assert (events[4].done, events[4].total, events[4].text) == (0, 1, "0 facts")  # how far through, as it goes
    assert events[5].text == "1 record • 2 facts" and events[6].total == 1
    assert events[-1].text == "articles/digests/weekly.md" and result.compiled == ["digests/weekly"]
    with cli.open_workspace(root) as workspace:
        (root / "local" / "kickoff.md").write_text(NOTE + "\nThe kickoff note changed.\n")
        workspace.mask = lambda line: line.replace("Vega", "****")  # type: ignore[method-assign]
        events = []
        workspace.run_once(on=events.append, only="local")
    assert all("Vega" not in f"{e.key} {e.text} {e.why}" for e in events)
