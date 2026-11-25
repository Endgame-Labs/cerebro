"""A connect that cannot go on ends in one line, never a traceback, and keeps nothing: a
mail server that does not answer as IMAP, and a secrets folder that cannot be set up. The mail server and GitHub
are stand-ins; the folder that cannot be set up is a plain file where it belongs. No network."""

from __future__ import annotations

import imaplib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_connect_lobby import lay_out_workspace

from cerebro import cli


def test_a_mail_server_that_does_not_answer_as_imap_ends_connect_in_one_line(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    opened = cli.open_workspace

    def not_imap(host: str) -> Any:
        raise imaplib.IMAP4.abort("socket error: EOF")  # as IMAP4_SSL raises on a greeting that is not IMAP

    def open_workspace(r: Path | None) -> Any:
        workspace = opened(r)
        workspace.imap_connect = not_imap
        return workspace

    monkeypatch.setattr(cli, "open_workspace", open_workspace)
    got = CliRunner().invoke(cli.main, ["connect", "gmail", "--stdin", "--workspace", str(root)],
                             input="pat@example.test:madeup app pass word\n")
    assert got.exit_code == 1 and isinstance(got.exception, SystemExit), got.output
    assert "[x] Gmail did not verify: no answer" in got.output
    assert "did not answer as an IMAP server), so nothing was kept" in got.output
    assert not (root / "sources" / "gmail.yaml").exists()
    assert "madeup" not in got.output


def test_a_secrets_folder_that_cannot_be_set_up_ends_connect_in_one_line(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    blocked = tmp_path / "config-is-a-file"
    blocked.write_text("not a folder\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocked))
    got = CliRunner().invoke(cli.main, ["connect", "gong", "--stdin", "--workspace", str(root)],
                             input="madeup-access-AAAA:madeup-secret-BBBB\n")
    assert got.exit_code == 1, got.output
    assert isinstance(got.exception, SystemExit), repr(got.exception)
    assert got.output.startswith("Error: ") and len(got.output.strip().splitlines()) == 1
    assert "does not read (NotADirectoryError)" in got.output  # the trust list, read first, sits in the same folder
    assert not (root / "sources" / "gong.yaml").exists() and blocked.read_text() == "not a folder\n"
