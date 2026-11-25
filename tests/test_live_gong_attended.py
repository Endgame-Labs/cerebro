"""The calls group's live check, on the stand-ins its `--fake` runs: a Gong source of thirty calls with ten
companies, the person on one, and a curator that asks for the calls the person took part in."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CHECKS = Path(__file__).resolve().parents[1] / "checks"


def test_the_fake_run_puts_only_the_attended_calls_account_in_the_calls_group(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    monkeypatch.setattr(sys, "argv", ["live_gong_attended.py", "--fake"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import live_gong_attended

    assert live_gong_attended.main() == 0
    out = capsys.readouterr().out
    assert "  calls group: 1 accounts, 0 from calls the person was not on" in out
    assert "  accounts/cobaltmill: " in out and "(from calls you attended in the last 90 days (gong))" in out


def test_took_part_me_keeps_the_one_company_of_ten_whose_call_the_person_was_on(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    import live_gong_attended

    gong = live_gong_attended.gong(Path("."))
    assert len(gong.ask("calls_by_domain").records) == 10  # anyone: every company the calls had
    mine = gong.ask("calls_by_domain", {"took_part": "me"}).records
    assert [r.id for r in mine] == ["cobaltmill.example"] and mine[0].fields["took_part"] == 1
