"""No facts layer Cerebro builds, in a check or in the engine, lets Factoid load its own key files into the process
environment: only the workspace's reader supplies a key. Factoid loads `~/.factoid/.env`, or `$FACTOID_HOME/.env`,
whenever its own provider is asked for a key."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cerebro.connector import Record
from cerebro.facts import ExtractionFailed, FactoidExtraction
from cerebro.models import ModelSpec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
import facts_live

SENTINEL = "CEREBRO_TEST_FACTOID_SENTINEL"
SPEC = ModelSpec("fireworks", "glm-5p3-flash", "low")
NOTE = Record(id="memo", source="notes", revision="1", body="Vega leads the review.")


@pytest.fixture
def factoid_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Factoid home whose key file holds a harmless line, which a call must never load."""
    import factoid.models

    home = tmp_path / "factoid-home"
    home.mkdir()
    (home / ".env").write_text(f"{SENTINEL}=from-factoids-own-key-file\n")
    monkeypatch.setenv("FACTOID_HOME", str(home))
    monkeypatch.setattr(factoid.models, "_keys_loaded", False)
    monkeypatch.setenv(SENTINEL, "")
    monkeypatch.delenv(SENTINEL)  # so the test ends with the variable gone, whatever a call loaded
    return home


def _call(handler: FactoidExtraction) -> None:
    """Make one extraction call on Factoid's own model client, its HTTP answered by a stand-in that refuses."""
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused by the stand-in", request=request)

    http = handler.app.client._http
    http._transport, http._mounts = httpx.MockTransport(refuse), {}
    try:
        handler.extract(NOTE)
    except ExtractionFailed:
        pass  # no key, or refused: either way the call was made, which is what loads a key file
    finally:
        handler.close()


BUILDERS: dict[str, Callable[[Path], Any]] = {
    "checks/facts_live.py": lambda folder: facts_live.build(folder, SPEC),
    "FactoidExtraction with no key reader": lambda folder: FactoidExtraction(folder, SPEC),
}


@pytest.mark.parametrize("name", BUILDERS)
def test_a_call_never_loads_factoids_own_key_file_into_the_environment(
        name: str, factoid_home: Path, tmp_path: Path) -> None:
    folder = tmp_path / "facts"
    folder.mkdir()
    _call(BUILDERS[name](folder))
    assert os.environ.get(SENTINEL) is None, name
