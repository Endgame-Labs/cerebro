"""The live check of the curators carrying out what a caller accepted, on the stand-ins its `--fake` runs: each of
its eight proposals is read as carried out as asked, and an id a curator names on another article is read as named
wrongly."""

from __future__ import annotations

from pathlib import Path

import pytest

CHECKS = Path(__file__).resolve().parents[1] / "checks"


def test_the_fake_run_reads_each_proposal_carried_out_and_a_wrong_id_as_wrong(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import live_applies

    from cerebro import fakes

    real = fakes.ScriptedRun

    def archive_named_elsewhere(reply: object, calls: list) -> object:
        wrong = [("archive", {"article": "accounts/kestrel-foods", "reason": "x", "proposals": c[1]["proposals"]})
                 for c in calls if c[0] == "archive"]
        return real(reply, wrong + calls)

    monkeypatch.setattr(fakes, "ScriptedRun", archive_named_elsewhere)
    assert live_applies.main(["--fake", "--runs", "1"]) == 0
    out = capsys.readouterr().out
    assert out.count("carried out 1/1, as asked 1/1") == 8
    assert "ids named wrongly: 1" in out
    assert "approved admit at its path and name: 1/1" in out
    assert "no second article for the account that had one: 1/1" in out
    assert "after the split, the learned key on the right article: 1/1" in out
    assert out.rstrip().endswith("PASS")
