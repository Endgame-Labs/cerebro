"""`cerebro connect` keeps a login and the source file that names it together or not at all: after any failure there
is neither a file that names a login the store does not hold, nor a login kept with no file, and a file that was
there before is back as it was, byte for byte. A browser sign-in whose check fails keeps nothing and never says
Connected.

Every provider is a stand-in: GitHub and Gong behind a mock transport, the browser step and the MCP session in this
process. The store's failures are stand-ins too: a keychain that refuses the write, a store file that cannot be
replaced, and Ctrl-C while the store is written. No key, no login and no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from click.testing import CliRunner
from test_connect_lobby import lay_out_workspace
from test_connect_walks import at_a_plain_terminal, signing_in

from cerebro import cli
from cerebro import secrets as secrets_module
from cerebro.connectors.mcp_oauth import Login
from cerebro.connectors.mcp_source_client import McpError, ToolRefused

GITHUB_TOKEN = "stand-in"  # the one token the recorded GitHub takes
GONG_KEY = "madeup-access-AAAA:madeup-secret-BBBB"
LINEAR_FILE = "# the team's own notes\r\nkind: linear\r\nteams: [ENG]\r\n"  # CRLF and a comment, kept byte for byte


def stores(root: Path) -> dict[str, str]:
    """Every name and value the workspace's store holds."""
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        return {name: workspace.secrets.get(name) for name in workspace.secrets.names()}


def files(root: Path) -> dict[str, bytes]:
    folder = root / "sources"
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()}


def failing_set(monkeypatch: pytest.MonkeyPatch, how: str, then: Any = None) -> list[str]:
    """Make every store write fail from here on, as `how` says; `then`, given the store, runs first. Reads still
    work. Gives the names whose write was tried."""
    tried: list[str] = []
    errors = {"keychain": lambda: secrets_module.SecretsError("the os keychain refused the write (stand-in)"),
              "file": lambda: PermissionError(13, "Permission denied (stand-in)"),
              "ctrl-c": KeyboardInterrupt}

    def refuse(self: Any, name: str, value: str) -> None:
        tried.append(name)
        if then is not None:
            then()
        raise errors[how]()

    monkeypatch.setattr(secrets_module.FileSecrets, "set", refuse)
    return tried


def failing_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the source file's write fail, as a full disk does."""
    import cerebro.connect as connect_module

    def full(*_: Any, **__: Any) -> None:
        raise OSError(28, "No space left on device (stand-in)")

    monkeypatch.setattr(connect_module, "write_source", full)


def nothing_kept_or_left(got: Any, root: Path, before_store: dict[str, str], before_files: dict[str, bytes],
                         code: int = 1) -> None:
    assert got.exit_code == code, got.output
    assert stores(root) == before_store
    assert files(root) == before_files
    assert "Traceback" not in got.output and "connection verified" not in got.output
    assert not any(line.startswith("Connected to") for line in got.output.splitlines())


# ---- the store fails after the file is written


@pytest.mark.parametrize("how", ["keychain", "file", "ctrl-c"])
def test_a_new_file_is_taken_back_when_the_login_cannot_be_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 how: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    before_store, before_files = stores(root), files(root)
    tried = failing_set(monkeypatch, how)
    got = CliRunner().invoke(cli.main, ["connect", "gong", "--stdin", "--workspace", str(root)],
                             input=GONG_KEY + "\n")
    assert tried == ["gong"]
    nothing_kept_or_left(got, root, before_store, before_files, 130 if how == "ctrl-c" else 1)
    if how != "ctrl-c":
        assert "the login could not be kept in" in got.output and "so nothing was kept" in got.output
        assert got.output.count("\n") <= 3  # one line, after the step


@pytest.mark.parametrize("how", ["keychain", "file", "ctrl-c"])
def test_a_file_that_gained_auth_is_put_back_byte_for_byte_when_the_login_cannot_be_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    (root / "sources" / "work.yaml").write_bytes(LINEAR_FILE.encode())
    (root / "sources" / "work.yaml").chmod(0o640)
    before_store, before_files = stores(root), files(root)
    failing_set(monkeypatch, how)
    got = CliRunner().invoke(cli.main, ["connect", "work", "--workspace", str(root)], input="b\n")
    nothing_kept_or_left(got, root, before_store, before_files, 130 if how == "ctrl-c" else 1)
    assert (root / "sources" / "work.yaml").stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("how", ["keychain", "file", "ctrl-c"])
def test_a_kept_login_and_its_file_stay_when_a_replacement_cannot_be_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    before_store, before_files = stores(root), files(root)  # code.yaml's token is kept already
    failing_set(monkeypatch, how)
    got = CliRunner().invoke(cli.main, ["connect", "code", "--replace", "--stdin", "--workspace", str(root)],
                             input=GITHUB_TOKEN + "\n")
    nothing_kept_or_left(got, root, before_store, before_files, 130 if how == "ctrl-c" else 1)


@pytest.mark.parametrize("how", ["keychain", "file"])
def test_a_kept_browser_login_and_its_file_stay_when_a_new_sign_in_cannot_be_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    (root / "sources" / "work.yaml").write_text("kind: linear\nauth: oauth\n")
    old = Login("https://mcp.linear.app/mcp", "https://mcp.linear.app/token", "client-test", "tok-test-old")
    with cli.open_workspace(root) as workspace:
        workspace.secrets.set("linear", old.to_json())
    before_store, before_files = stores(root), files(root)
    failing_set(monkeypatch, how)
    got = CliRunner().invoke(cli.main, ["connect", "work", "--replace", "--workspace", str(root)])
    nothing_kept_or_left(got, root, before_store, before_files)


# ---- the file fails, and both fail


def test_a_file_that_cannot_be_written_keeps_nothing_on_a_first_connect_or_over_a_kept_login(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    (root / "sources" / "work.yaml").write_bytes(LINEAR_FILE.encode())
    with cli.open_workspace(root) as workspace:
        workspace.secrets.set("linear", "lin_api_madeup_kept")  # a kept key the browser way would replace
    before_store, before_files = stores(root), files(root)
    tried = failing_set(monkeypatch, "keychain")
    failing_write(monkeypatch)
    first = CliRunner().invoke(cli.main, ["connect", "gong", "--stdin", "--workspace", str(root)],
                               input=GONG_KEY + "\n")
    nothing_kept_or_left(first, root, before_store, before_files)
    over = CliRunner().invoke(cli.main, ["connect", "work", "--replace", "--workspace", str(root)], input="b\n")
    nothing_kept_or_left(over, root, before_store, before_files)
    assert tried == []  # the store is not written once the file is not
    assert "could not be written" in first.output and "could not be written" in over.output


def test_a_file_that_cannot_be_put_back_is_named_with_what_to_fix(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    path = root / "sources" / "gong.yaml"

    unlink = Path.unlink

    def undeletable(self: Path, missing_ok: bool = False) -> None:
        if self == path:
            raise PermissionError(1, "Operation not permitted (stand-in)")
        unlink(self, missing_ok=missing_ok)

    failing_set(monkeypatch, "keychain", lambda: monkeypatch.setattr(Path, "unlink", undeletable))
    got = CliRunner().invoke(cli.main, ["connect", "gong", "--stdin", "--workspace", str(root)],
                             input=GONG_KEY + "\n")
    assert got.exit_code == 1 and "Traceback" not in got.output
    assert "gong" not in stores(root)
    assert "sources/gong.yaml could not be put back as it was (PermissionError): delete it" in got.output


def test_a_file_edited_while_the_login_was_kept_is_left_as_the_person_wrote_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    path = root / "sources" / "work.yaml"
    path.write_text("kind: linear\n")
    failing_set(monkeypatch, "keychain", lambda: path.write_text("kind: linear\nteams: [OPS]\nauth: oauth\n"))
    got = CliRunner().invoke(cli.main, ["connect", "work", "--workspace", str(root)], input="b\n")
    assert got.exit_code == 1 and "linear" not in stores(root)
    assert path.read_text() == "kind: linear\nteams: [OPS]\nauth: oauth\n"
    assert "could not be put back as it was (it changed meanwhile)" in got.output


# ---- a browser sign-in whose check fails


def refusing(session: Any, how: str) -> None:
    def call(*tools: str) -> Any:
        def answer(tool: str, arguments: Any) -> Any:
            if how == "refused":
                raise ToolRefused("stand-in", tool, "invalid_token: the login was refused")
            raise McpError("stand-in: the server answered 500 to the probe")
        return answer

    session.reader = call


@pytest.mark.parametrize("how", ["refused", "no answer"])
@pytest.mark.parametrize(("kind", "typed"), [("linear", "b\n"), ("granola", "")])
def test_a_sign_in_whose_check_fails_keeps_nothing_and_never_says_connected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, typed: str, how: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    _, session = signing_in(monkeypatch)
    refusing(session, how)
    before_store, before_files = stores(root), files(root)
    got = CliRunner().invoke(cli.main, ["connect", kind, "--workspace", str(root)], input=typed)
    nothing_kept_or_left(got, root, before_store, before_files)
    assert f"[x] {kind.capitalize()} did not verify" in got.output
    assert "so nothing was kept" in got.output and "kept the login" not in got.output


def test_an_mcp_sign_in_whose_server_refuses_it_exits_1_with_nothing_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    _, session = signing_in(monkeypatch)

    def refused(where: Any) -> Any:
        raise McpError("the server refused the login (stand-in)")

    monkeypatch.setattr(session, "__call__", refused, raising=False)
    (root / "sources" / "tools.yaml").write_text("kind: mcp\nurl: https://tools.example/mcp\nauth: oauth\n")
    opened = cli.open_workspace

    def open_workspace(r: Path | None) -> Any:
        workspace = opened(r)
        workspace.mcp_open = refused
        return workspace

    monkeypatch.setattr(cli, "open_workspace", open_workspace)
    before_store, before_files = stores(root), files(root)
    got = CliRunner().invoke(cli.main, ["connect", "tools", "--workspace", str(root)])
    nothing_kept_or_left(got, root, before_store, before_files)


def test_a_sign_in_whose_check_fails_over_a_kept_login_keeps_the_kept_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    _, session = signing_in(monkeypatch)
    (root / "sources" / "work.yaml").write_text("kind: linear\nauth: oauth\n")
    old = Login("https://mcp.linear.app/mcp", "https://mcp.linear.app/token", "client-test", "tok-test-old")
    with cli.open_workspace(root) as workspace:
        workspace.secrets.set("linear", old.to_json())
    refusing(session, "refused")
    before_store, before_files = stores(root), files(root)
    got = CliRunner().invoke(cli.main, ["connect", "work", "--replace", "--workspace", str(root)])
    nothing_kept_or_left(got, root, before_store, before_files)


# ---- a store that fails partway, and Ctrl-C as the file lands


class HalfKeychain:
    """A keychain that writes an item, then refuses the write of the store's list of names, as a locked or full
    keychain can between two writes."""

    priority = 5
    items: ClassVar[dict[tuple[str, str], str]] = {}
    tried: ClassVar[list[str]] = []
    refuse_list = False

    def get_password(self, service: str, user: str) -> Any:
        return self.items.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.tried.append(user)
        if user == secrets_module.INDEX and HalfKeychain.refuse_list:
            raise RuntimeError("keychain locked (stand-in)")
        self.items[(service, user)] = value

    def delete_password(self, service: str, user: str) -> None:
        self.items.pop((service, user), None)


def only_the_stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make HalfKeychain the store's keychain, whatever this run's environment says: the keychain is chosen only where
    CEREBRO_SECRETS_STORE is unset, so it is unset for this test alone, and the one road the store has to a keychain,
    `secrets._default_backend`, gives the stand-in. keyring itself is pointed at its backend that fails, and each of
    its own ways in raises, so no real keychain can be reached if a road around the stand-in were ever opened."""
    import keyring
    import keyring.core

    monkeypatch.delenv(secrets_module.STORE_ENV, raising=False)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")

    def no_keychain(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a real keychain was reached")

    for name in ("get_keyring", "set_keyring", "get_password", "set_password", "delete_password", "get_credential"):
        for module in (keyring, keyring.core):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, no_keychain)
    monkeypatch.setattr("cerebro.secrets._default_backend", HalfKeychain)


def test_a_keychain_that_fails_partway_holds_nothing_new_and_no_file_is_left(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    HalfKeychain.items, HalfKeychain.tried, HalfKeychain.refuse_list = {}, [], False
    only_the_stand_in(monkeypatch)
    root = lay_out_workspace(tmp_path, monkeypatch)
    before_files = files(root)
    HalfKeychain.refuse_list = True
    got = CliRunner().invoke(cli.main, ["connect", "gong", "--stdin", "--workspace", str(root)],
                             input=GONG_KEY + "\n")
    assert got.exit_code == 1 and "so nothing was kept" in got.output, got.output
    assert "gong" in HalfKeychain.tried  # the stand-in was the store: it took the key, then refused its list
    assert files(root) == before_files
    with cli.open_workspace(root) as workspace, pytest.raises(secrets_module.SecretNotFound):
        workspace.secrets.get("gong")


@pytest.mark.parametrize("adding", [True, False])
def test_ctrl_c_as_the_file_lands_takes_it_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adding: bool) -> None:
    import os

    root = lay_out_workspace(tmp_path, monkeypatch)
    name = "gong" if adding else "work"
    if not adding:
        at_a_plain_terminal(monkeypatch)
        signing_in(monkeypatch)
        (root / "sources" / "work.yaml").write_bytes(LINEAR_FILE.encode())
    before_store, before_files = stores(root), files(root)
    real = os.replace

    def replace(a: Any, b: Any, *args: Any, **kwargs: Any) -> None:
        real(a, b, *args, **kwargs)
        if str(b).endswith(f"sources/{name}.yaml"):
            raise KeyboardInterrupt  # the signal arrives as the rename returns

    monkeypatch.setattr(os, "replace", replace)
    args = ["connect", "gong", "--stdin"] if adding else ["connect", "work"]
    got = CliRunner().invoke(cli.main, [*args, "--workspace", str(root)], input=GONG_KEY + "\n" if adding else "b\n")
    monkeypatch.setattr(os, "replace", real)
    assert got.exit_code == 130
    assert stores(root) == before_store and files(root) == before_files
