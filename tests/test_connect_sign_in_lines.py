"""What a browser sign-in says goes to stderr, so a person who sends stdout to
a file still sees the address to open. The browser step is a stand-in that says the sign-in's own lines and gives a
made-up login; the MCP session is a stand-in too. No browser and no network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_connect_lobby import lay_out_workspace
from test_connect_walks import at_a_plain_terminal, signing_in

from cerebro import cli
from cerebro.connectors.mcp_oauth import Login

PAGE = "http://127.0.0.1:9/authorize?state=stand-in"


def test_the_sign_in_address_and_its_waiting_lines_go_to_stderr(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.connect as connect_module

    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)

    def authorize(server: str, *, say: Any, **_: Any) -> Login:
        say("Opening your browser to sign in to mcp.granola.ai. If it does not open, go to:")
        say(PAGE)
        say("A sign-in return with a state this sign-in did not start was ignored. Still waiting for the browser.")
        return Login(server, f"{server}/token", "client-test", "tok-test-signed-in")

    monkeypatch.setattr(connect_module, "authorize", authorize)
    got = CliRunner().invoke(cli.main, ["connect", "granola", "--workspace", str(root)])
    assert got.exit_code == 0, got.output
    assert PAGE in got.stderr and "Still waiting" in got.stderr
    assert PAGE not in got.stdout and "Still waiting" not in got.stdout and "go to:" not in got.stdout
    assert "Connected to Granola" in got.stdout
