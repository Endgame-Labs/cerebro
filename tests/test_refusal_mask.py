"""A refusal any command prints is masked with every value the workspace's secrets store holds, read for the purpose
when the workspace did not open, and is printed as it is when no store can be read: the refusal never fails for want
of one."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cerebro import Cerebro, cli
from cerebro import review as reviewing
from cerebro.cli import main
from cerebro.errors import CerebroError

VALUE = "made-up-key-QZ7xV2pL9wK4"


def made(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    assert CliRunner().invoke(main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    kept = CliRunner().invoke(main, ["secrets", "set", "vault", "--workspace", str(root)], input=VALUE + "\n")
    assert kept.exit_code == 0, kept.output
    (root / "profile.md").write_text(f"---\nidentity:\n  email: [sam@quillmoor.example]\n  {VALUE}: [a]\n  {VALUE}: [b]\n"
                                     "---\n")


def test_a_refusal_that_quotes_a_stored_value_is_printed_masked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "ws"
    made(root, monkeypatch)
    done = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert done.exit_code == 1, done.output
    assert "duplicate key" in done.output and VALUE not in done.output, done.output


def test_mcp_on_a_profile_that_gives_a_key_twice_starts_and_serves_with_no_part_of_a_stored_value_shown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    root = tmp_path / "ws"
    made(root, monkeypatch)
    hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    done = CliRunner().invoke(main, ["mcp", "--workspace", str(root)], input=json.dumps(hello) + "\n")
    replies = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
    assert done.exit_code == 0 and [reply["id"] for reply in replies if "result" in reply] == [1], done.output
    seen = done.output + caplog.text
    assert not any(window in seen for window in windows(VALUE)), seen


def test_a_refusal_with_no_store_to_read_is_printed_as_it_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "ws"
    made(root, monkeypatch)
    (root / "settings.yaml").write_text("id: [not, an, id]\n" + (root / "settings.yaml").read_text())
    done = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert done.exit_code != 0 and "Traceback" not in done.output, done.output


SPACED = ["two  spaces9x", "tab\tvalue77", "line\nbreak77", "  edge-spaces77  "]  # each as the store holds it


def windows(value: str) -> set[str]:
    """Every 6-character run of a value, as kept and with its whitespace folded as a line folds it."""
    out = set()
    for form in (value.strip(), " ".join(value.split())):
        out |= {form[n:n + 6] for n in range(len(form) - 5)}
    return out


def kept_spaced(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    assert CliRunner().invoke(main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    store = Cerebro(root, trusted=True).secrets
    assert store is not None
    for n, value in enumerate(SPACED):
        store.set(f"spaced{n}", value)


REFUSALS = {
    "a refusal of the app": lambda said: CerebroError(said),
    "what waits on a person": lambda said: reviewing.NotATerminal(said),
    "what is not built": lambda said: NotImplementedError(said),
    "a refusal of the command line": lambda said: click.ClickException(said),
    "a refusal that names a folder": lambda said: cli._Whole(said),
}


@pytest.mark.parametrize("kind", list(REFUSALS))
@pytest.mark.parametrize("value", SPACED)
def test_a_stored_value_with_runs_of_whitespace_is_masked_in_every_refusal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, value: str) -> None:
    root = tmp_path / "ws"
    kept_spaced(root, monkeypatch)

    @click.command()
    @click.option("--workspace", "root", type=click.Path(path_type=Path), default=None)
    @cli._refusing
    def refuse(root: Path | None) -> None:
        raise REFUSALS[kind](f"the key {value} is given twice")

    done = CliRunner().invoke(refuse, ["--workspace", str(root)])
    assert done.exit_code in (1, 2) and "is given twice" in done.output, done.output
    assert not any(window in done.output for window in windows(value)), done.output


@pytest.mark.parametrize("value", SPACED)
def test_a_duplicated_key_in_profile_md_that_is_a_stored_value_with_whitespace_is_masked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    root = tmp_path / "ws"
    kept_spaced(root, monkeypatch)
    key = json.dumps(value)  # a quoted key holds the value's whitespace as it is
    (root / "profile.md").write_text(f"---\nidentity:\n  email: [sam@quillmoor.example]\n  {key}: [a]\n  {key}: [b]\n"
                                     "---\n")
    done = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert done.exit_code == 1 and "duplicate key" in done.output, done.output
    assert not any(window in done.output for window in windows(value)), done.output
