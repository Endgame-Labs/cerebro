"""The live check of protected fields, proven on its `--fake` mode: the real run loop on a scripted model, no model
call. It passes as the engine stands, and it fails when a value a model set in a protected field lands, which takes
both guards off: override's refusal and the rule that reads a field."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
import live_protected


def test_the_fake_run_passes_and_shows_each_refusal_and_what_the_model_did_next(
        capsys: pytest.CaptureFixture[str]) -> None:
    assert live_protected.main(["--fake"]) == 0
    said = capsys.readouterr().out
    assert said.rstrip().endswith("PASS"), said
    assert "override status = 'closed'\n  -> {'refused': \"override: status is the system's" in said
    assert "override level = 'high'\n  -> set level" in said
    assert "then: tried the field again" in said and "refused   4:" in said


def test_the_check_fails_when_a_models_value_in_a_protected_field_lands(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.article
    import cerebro.compile

    monkeypatch.setattr(cerebro.compile, "field_refused", lambda article, name, value: "")  # override refuses nothing,
    monkeypatch.setattr(cerebro.article, "stands", lambda name: True)  # and every written value stands
    assert live_protected.main(["--fake"]) == 1
    said = capsys.readouterr().out
    assert "FAIL" in said and "the model never tried" not in said
    assert "- a model's status stands: 'closed'" in said and "the run counted 0 refusals" not in said, said
