"""The first run's live check, on the stand-ins its `--fake` runs: it shows the late round's list as `--plain` does
and says what it decided there, on both of its runs, and on the second checks the row its curator left out, which is
ticked and admitted."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CHECKS = Path(__file__).resolve().parents[1] / "checks"


def test_the_fake_run_passes_and_shows_the_late_list_and_what_it_decided_on_both_runs(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    monkeypatch.setattr(sys, "argv", ["live_first_run.py", "--fake"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import live_first_run

    assert live_first_run.main() == 0
    out = capsys.readouterr().out
    assert out.count("[x] Brightwater Logistics  accounts") == 2  # the call sits under its account
    assert out.count(": late list: ") == 2
    assert "chosen    two declined: 3 ticked, 3 unticked under 2 lines" in out
    assert "admitted  two declined: Quillon Supply, left out by agents/accounts/curator.md and ticked" in out
    assert out.rstrip().endswith("PASS")


def test_a_part_with_nothing_in_it_says_nothing_to_check_not_pass_and_exits_0(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    import live_first_run

    assert live_first_run.verdict([], []) == (["PASS"], 0)
    lines, code = live_first_run.verdict([], ["two declined: the list left no row out, so no left-out row was checked"])
    assert code == 0 and lines[-1] == "NOTHING TO CHECK" and "PASS" not in lines
    assert "- two declined: the list left no row out, so no left-out row was checked" in lines
    assert live_first_run.verdict(["approved whole: nothing was written"], ["x"])[1] == 1  # a failure is a failure
