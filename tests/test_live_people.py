"""The people live check, on the stand-ins its `--fake` runs: a people curator scripted to follow the rule, and the
counts the check prints from what discovery filed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CHECKS = Path(__file__).resolve().parents[1] / "checks"


def test_the_fake_run_counts_whom_discovery_proposed_and_why_each_other_was_left_out(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    monkeypatch.setattr(sys, "argv", ["live_people.py", "--fake"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import live_people

    assert live_people.main() == 0
    out = capsys.readouterr().out
    assert "placed by address proposed          4 of 4" in out
    assert "bare names proposed                 0 of 2" in out and "own side proposed                   0 of 3" in out
    assert "  left out Lina Brooks: " in out and out.rstrip().endswith("PASS")


def test_the_floor_passes_a_third_of_the_placed_attendees_and_any_one_wrong_person_proposed_fails(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    import live_people

    def trial(*names: str) -> dict[str, object]:
        return {"proposed": set(names), "left": {}, "spent": 0.0, "keys": {}}

    placed = list(live_people.PLACED)
    assert live_people.verdict([trial(*placed[:3]), trial()]) == []  # 3 of 8 placed: over the floor
    assert live_people.verdict([trial(placed[0]), trial()]) == [
        "placed attendees proposed 1 of 8, under 30%"]
    for wrong in (live_people.BARE[0], "Sam Okoye", next(iter(live_people.PERSONAL))):
        assert len(live_people.verdict([trial(*placed), trial(*placed, wrong)])) == 1  # one wrong person fails
