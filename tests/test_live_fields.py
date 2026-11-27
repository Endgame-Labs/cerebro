"""checks/live_fields.py runs to its end on the stand-ins: a source's fields reach its deals' front matter through
the facts the deal agent reads, a change follows, an emptied field's fact is retired, and a pass with nothing new
moves no field and spends nothing on fields."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CHECKS = Path(__file__).resolve().parents[1] / "checks"


def test_the_fake_run_measures_the_fields_and_ends_with_its_three_numbers(capsys: pytest.CaptureFixture[str],
                                                                         monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    monkeypatch.setattr(sys, "argv", ["live_fields.py", "--fake", "--keep", str(tmp_path / "kept")])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import live_fields

    assert live_fields.main() == 0
    out = capsys.readouterr().out
    # the stand-in sets deals only, and drops the field its evidence says is retired
    assert "fields right 25/28; followed a change 2/2; emptied and followed 1/1; changed with no new evidence 0" in out
    assert "its fact is retired" in out and "spent model tokens on the pass with nothing changed: 0" in out
    assert "KEY" not in out.replace("FIREWORKS_API_KEY", "").replace("TYPESAFE_API_KEY", "")
