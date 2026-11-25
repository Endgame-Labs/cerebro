"""The own side's live check, on the stand-ins its `--fake` runs: a workspace made by `init --email`, curators that
admit the outside organisations and people, and the checks on what is on disk."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CHECKS = Path(__file__).resolve().parents[1] / "checks"


def test_the_fake_run_passes_and_shows_each_article_with_its_keys(capsys: pytest.CaptureFixture[str],
                                                                   monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    monkeypatch.setattr(sys, "argv", ["live_own_side.py", "--fake", "--keep", str(tmp_path / "kept")])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import live_own_side

    assert live_own_side.main() == 0
    out = capsys.readouterr().out
    assert "article  accounts/brightwater-logistics: keys ['brightwater.example']" in out
    assert "article  accounts/tidewater-freight: keys ['tidewater.example']" in out
    shown = [line for line in out.splitlines() if line.startswith("article")]
    assert len(shown) == 5 and not any("ourco" in line.split(", ours")[0] for line in shown)
    assert not any(line.startswith(("article  people/bo-lin", "article  people/ann-lee")) for line in shown)
    assert out.rstrip().endswith("PASS") and (tmp_path / "kept" / "workspace.md").is_file()


def test_the_check_fails_an_article_keyed_by_the_own_side_or_for_a_colleague(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(CHECKS))
    import live_own_side

    from cerebro import scaffold

    root = scaffold.init(tmp_path / "wiki", environ={}, email=live_own_side.YOU).root
    articles = [{"id": "accounts/ourco", "front": {"title": "Ourco", "identity": {"domain": ["ourco.example"]}}},
                {"id": "people/bo-lin", "front": {"title": "Bo Lin", "identity": {"email": ["bo@ourco.example"]}}},
                {"id": "accounts/brightwater-logistics",
                 "front": {"identity": {"domain": ["brightwater.example"], "email": ["ann@ourco.example"]}}}]
    reasons = live_own_side.checked(root, articles)
    assert any(r.startswith("accounts/ourco is keyed by the own side") for r in reasons)
    assert any(r.startswith("people/bo-lin is named for the own side") for r in reasons)
    assert any(r.startswith("accounts/brightwater-logistics is keyed by the own side beside an outside") for r in reasons)
    assert "Tidewater Freight (tidewater.example) has no article" in reasons
