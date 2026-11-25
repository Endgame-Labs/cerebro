"""A key is proven with one small call before it is kept: checked against the day's
budget first, a model with no known price refused under a daily cap before any call, what it spent recorded under
`key_proof`, and the key masked. The model is always a stand-in here; no test reaches a provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.budget import UnpricedModel
from cerebro.models import ModelSpec, TokenCount

KEY = "stand-in-proof-key-7f3a"


class Asked:
    """The proof's model, standing in: it answers ok and keeps every request."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def rewrite(self, system: str, request: str) -> tuple[str, TokenCount]:
        self.asked.append(request)
        return "ok", TokenCount(12, 1)


def workspace(tmp_path: Path, daily_usd: float | None) -> Cerebro:
    root = scaffold.init(tmp_path / "wiki", daily_usd=daily_usd, timezone="UTC", environ={}).root
    return Cerebro(root, trusted=True)


def test_a_key_is_proven_with_one_call_recorded_as_a_proof_and_masked_from_then_on(tmp_path: Path) -> None:
    with workspace(tmp_path, 5) as w:
        w.asker = asked = Asked()
        w.prove_key("extraction", ModelSpec("fireworks", "glm-5p3-flash", "low", "extraction"), KEY)
        assert asked.asked == ["ok"]
        rows = [row.data for row in w.store.rows("spend")]
        assert [(r["layer"], r["provider"], r["model"], r["run"]) for r in rows] == [
            ("extraction", "fireworks", "glm-5p3-flash", "key_proof")]
        assert KEY not in w.mask_every()(f"the key {KEY} answered")


def test_under_a_daily_cap_a_model_with_no_known_price_is_refused_before_any_call(tmp_path: Path) -> None:
    with workspace(tmp_path, 5) as w:
        w.asker = asked = Asked()
        with pytest.raises(UnpricedModel, match="no-such-model has no known price, so compilation does not run it"):
            w.prove_key("compilation", ModelSpec("fireworks", "no-such-model", "", "compilation"), KEY)
        assert asked.asked == [] and list(w.store.rows("spend")) == []


def test_with_no_cap_a_model_with_no_known_price_is_proven(tmp_path: Path) -> None:
    with workspace(tmp_path, None) as w:
        w.asker = Asked()
        w.prove_key("compilation", ModelSpec("fireworks", "no-such-model", "", "compilation"), KEY)
        assert [row.data.get("unpriced") for row in w.store.rows("spend")] == [True]


def runs_of(key: str, text: str, length: int = 6) -> list[str]:
    """Every run of `length` characters of `key` that `text` holds."""
    return [key[i:i + length] for i in range(len(key) - length + 1) if key[i:i + length] in text]


@pytest.mark.parametrize("pad", range(120, 260, 12))  # the key's 46 characters cross the cut at four places
def test_a_failed_proof_never_holds_a_run_of_the_key_whatever_the_provider_echoes_and_wherever_it_is_cut(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pad: int) -> None:
    """A provider whose error echoes the key, after padding that puts it at every place across the 300-character
    cut: nothing printed, kept or said, the proof's error, the lobby's line, its kept reason and the walk's last
    check, holds six characters of the key in a row."""
    from pydantic_ai.exceptions import ModelHTTPError

    from cerebro import guided

    key = "fw-standin-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    def echoing(*args: object, **kwargs: object) -> None:
        raise ModelHTTPError(401, "glm-5p3-flash", {"error": {"message": "x" * pad + " invalid key Bearer " + key}})

    monkeypatch.setattr("pydantic_ai.direct.model_request_sync", echoing)
    root = scaffold.init(tmp_path / "wiki", daily_usd=5, timezone="UTC", environ={}).root
    with Cerebro(root, trusted=True) as w, pytest.raises(Exception) as failed:
        w.prove_key("extraction", ModelSpec("fireworks", "glm-5p3-flash", "low", "extraction"), key)
    assert runs_of(key, str(failed.value)) == [], str(failed.value)
    assert "401" in str(failed.value) and "x" * 20 not in str(failed.value)  # the status, never the reply's body

    class Screen:
        def __init__(self) -> None:
            self.said: list[str] = []
            self.answers: list[object] = [0, key, None]

        def menu(self, menu: object, cursor: int = 0) -> object:
            self.said += [str(line) for line in getattr(menu, "head", [])]
            return self.answers.pop(0) if self.answers else None

        def text(self, *args: object, **kwargs: object) -> object:
            return self.answers.pop(0) if self.answers else None

        def read(self, lines: object) -> None:
            pass

        def say(self, line: str) -> None:
            self.said.append(line)

    screen = Screen()
    walk = guided.Walk(root, screen, lambda folder: Cerebro(folder, trusted=True), lobby=lambda: None,
                       first_run=lambda: None, home=tmp_path / "home", command="cerebro", environ={})
    walk.keys()
    session = walk._key_session()
    kept = " ".join([*screen.said, *walk.checks, *session.failed.values()])
    assert "did not verify" in kept and runs_of(key, kept) == [], kept


def test_the_lobby_keeps_and_says_no_run_of_the_key_whatever_a_proofs_error_holds() -> None:
    """The lobby's own guard, beside the mask: an error holding the key whole, or any piece of six characters or
    more of it, from any place in the key, keeps no such run in its line or its kept reason."""
    from cerebro.keys import unkeyed

    key = "tk-standin-0123456789abcdefghij"
    for start in range(len(key)):
        for end in range(start + 6, len(key) + 1):
            said = unkeyed(f"refused: ...{key[start:end]}... and again {key}", key)
            assert runs_of(key, said) == [], (start, end, said)
    assert unkeyed("the key was refused", key) == "the key was refused"
