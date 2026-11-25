"""A Gong that is busy, failing or silent while a key is proven never refuses the key: connect says which it was,
keeps a key kept already as it was, and keeps no new one. Gong is a stand-in behind a mock transport; the keys are
made up."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from test_connect_lobby import lay_out_workspace

from cerebro import cli

KEPT = "madeup-access-CCCC:madeup-secret-DDDD"
NEW = "madeup-access-EEEE:madeup-secret-FFFF"


class Gong:
    """Gong's workspaces take the key; its list of calls answers `calls`: a status, or "timeout" for no answer."""

    def __init__(self, calls: int | str):
        self.calls = calls

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/workspaces":
            return httpx.Response(200, json={"workspaces": []})
        if self.calls == "timeout":
            raise httpx.ReadTimeout("stand-in timeout", request=request)
        if self.calls == "cut":
            return httpx.Response(200, text='{"records": {"totalRec')
        assert isinstance(self.calls, int)
        return httpx.Response(self.calls, headers={"Retry-After": "2"}, json={"errors": ["stand-in"]})


SAID = {429: "is busy (status 429)", 500: "answered status 500", 503: "answered status 503",
        "timeout": "did not answer (ReadTimeout)", 408: "answered status 408", "cut": "returned invalid json"}


def kept(root: Path) -> str | None:
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        return workspace.secrets.get("gong") if "gong" in workspace.secrets.names() else None


@pytest.mark.parametrize("calls", [429, 500, 503, "timeout", 408, "cut"])
def test_a_kept_gong_key_gong_did_not_refuse_is_kept_and_not_said_refused(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch,
                                                                          calls: int | str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch, Gong(calls))  # type: ignore[arg-type]
    (root / "sources" / "calls.yaml").write_text("kind: gong\n")
    with cli.open_workspace(root) as workspace:
        workspace.secrets.set("gong", KEPT)
    got = CliRunner().invoke(cli.main, ["connect", "calls", "--workspace", str(root)])
    assert got.exit_code == 1, got.output
    assert "refused" not in got.output and "--replace" not in got.output
    assert SAID[calls] in got.output and "kept as gong stays as it was" in got.output
    assert kept(root) == KEPT
    assert "madeup-secret" not in got.output


@pytest.mark.parametrize("calls", [429, 500, "timeout"])
def test_a_new_gong_key_gong_did_not_refuse_is_not_kept_and_not_said_refused(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch,
                                                                             calls: int | str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch, Gong(calls))  # type: ignore[arg-type]
    got = CliRunner().invoke(cli.main, ["connect", "gong", "--stdin", "--workspace", str(root)], input=NEW + "\n")
    assert got.exit_code == 1, got.output
    assert "refused" not in got.output and SAID[calls] in got.output and "so nothing was kept" in got.output
    assert kept(root) is None and not (root / "sources" / "gong.yaml").exists()


def test_a_kept_gong_key_gong_refuses_still_names_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch, Gong(401))  # type: ignore[arg-type]
    (root / "sources" / "calls.yaml").write_text("kind: gong\n")
    with cli.open_workspace(root) as workspace:
        workspace.secrets.set("gong", KEPT)
    got = CliRunner().invoke(cli.main, ["connect", "calls", "--workspace", str(root)])
    assert got.exit_code == 1 and "was refused" in got.output and "--replace" in got.output
    assert kept(root) == KEPT
