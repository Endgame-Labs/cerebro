"""What the keys lobby and `secrets set` say is true: a proof Ctrl-C cancels on plain lines fails
with a line of its own and the next key is asked; Escape alone cancels a typed key and an arrow in it is left out; a
typed key is stripped as a piped one is; a Ctrl-C once the provider has answered ends one of two ways, each said; a key
that fills the terminal's line is refused; a name, its exit code and tab completion. Every key is made up and
every proof a stand-in; the store is the file store under the test's folder."""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cerebro import cli
from cerebro.app import Cerebro
from cerebro.errors import CerebroError

KEY = "sk-madeup-ZZTOP-123456"
OLD = "sk-madeup-OLD-000000"


@pytest.fixture(autouse=True)
def scratch_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    for name in ("FIREWORKS_API_KEY", "TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def made(tmp_path: Path) -> Path:
    root = (tmp_path / "wiki").resolve()
    got = CliRunner().invoke(cli.main, ["init", str(root), "--timezone", "UTC", "--daily-usd", "5"])
    assert got.exit_code == 0, got.output
    return root


def held(root: Path) -> dict[str, str]:
    with Cerebro(root) as workspace:
        assert workspace.secrets is not None
        return {name: workspace.secrets.get(name) for name in workspace.secrets.names()}


def set_piped(root: Path, name: str, value: str) -> Any:
    return CliRunner().invoke(cli.main, ["secrets", "set", name, "--workspace", str(root)], input=value)


# ---- a cancelled proof on plain lines --------------------------------------------------------------------------------

class Cancelled:
    """A proof Ctrl-C stops on its first call, as the terminal's interrupt lands in it; later calls pass."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, row: Any, value: str) -> None:
        self.calls.append(row.provider)
        if len(self.calls) == 1:
            raise KeyboardInterrupt


@pytest.mark.parametrize("connected", [False, True], ids=["not added", "replacing a connected key"])
def test_on_plain_lines_a_proof_ctrl_c_cancels_fails_with_its_own_line_and_the_next_key_is_asked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connected: bool) -> None:
    """On plain lines the cancelled proof is a failed one, `[x] <Provider> did not
    verify: ...`, and the next named key is asked; the summary follows and the command exits 0."""
    root = made(tmp_path)
    if connected:
        assert set_piped(root, "fireworks", OLD + "\n").exit_code == 0
    proofs = Cancelled()
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(cli, "_prove_on", lambda where: proofs)
    got = CliRunner().invoke(cli.main, ["secrets", "--workspace", str(root)],
                             input=f"fireworks,typesafe\n{KEY}\n{KEY}\n")
    assert got.exit_code == 0, got.output
    assert "[x] Fireworks did not verify: cancelled by Ctrl-C\n" in got.output, got.output
    assert "Typesafe API key" in got.output and proofs.calls == ["fireworks", "typesafe"]
    assert "[ok] Typesafe connection verified, key added to secrets store" in got.output
    assert held(root) == {"typesafe": KEY, **({"fireworks": OLD} if connected else {})}
    assert KEY not in got.output


# ---- what a typed key is ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("line", "kept"), [
    ("sk-madeup-ZZ\x1b[ATOP-123456\n", KEY),
    ("sk-madeup-ZZ\x1b[DTOP-123456\n", KEY),
    ("sk-madeup-ZZ\x1bOPTOP-123456\n", KEY),
    ("\x1b[200~sk-madeup-ZZTOP-123456\x1b[201~\n", KEY),
    ("sk-madeup-ZZTOP-123456\t\n", KEY),
    ("  sk-madeup-ZZTOP-123456 \n", KEY),
], ids=["an up arrow", "a left arrow", "an F-key", "a paste", "a trailing tab", "spaces around"])
def test_secrets_set_keeps_a_typed_key_with_another_keys_sequence_left_out_and_stripped_as_a_piped_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, kept: str) -> None:
    root = made(tmp_path)
    assert set_piped(root, "fireworks", OLD + "\n").exit_code == 0
    monkeypatch.setattr(cli, "_asking", lambda: True)
    got = set_piped(root, "fireworks", line)
    assert got.exit_code == 0, got.output
    assert held(root)["fireworks"] == kept and "cancelled" not in got.output


@pytest.mark.parametrize("line", ["\x1b\n", "sk-madeup\x1b\n", "\x1bsk-madeup-ZZTOP\n"],
                         ids=["Escape alone", "Escape after a key", "Escape before a key"])
def test_escape_alone_at_secrets_sets_prompt_cancels_and_keeps_the_old_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str) -> None:
    root = made(tmp_path)
    assert set_piped(root, "fireworks", OLD + "\n").exit_code == 0
    monkeypatch.setattr(cli, "_asking", lambda: True)
    got = set_piped(root, "fireworks", line)
    assert got.exit_code == 1 and "cancelled at the prompt, nothing stored" in got.output, got.output
    assert held(root)["fireworks"] == OLD


def test_a_piped_value_holding_an_escape_is_refused_for_its_control_character_and_names_no_prompt(
        tmp_path: Path) -> None:
    root = made(tmp_path)
    assert set_piped(root, "fireworks", OLD + "\n").exit_code == 0
    got = set_piped(root, "fireworks", "\x1bsk-madeup-ZZTOP-123456\n")
    assert got.exit_code == 1 and "holds a control character" in got.output, got.output
    assert "prompt" not in got.output and held(root)["fireworks"] == OLD


def test_in_the_lobby_an_arrow_inside_a_key_is_left_out_and_the_key_is_proven_and_kept(tmp_path: Path) -> None:
    root = made(tmp_path)
    proofs: list[str] = []

    class Typed:
        def __init__(self) -> None:
            self.asked = ["sk-madeup-ZZ\x1b[ATOP-123456"]

        def say(self, line: str) -> None:
            pass

        def text(self, prompt: str, default: str = "", hidden: bool = False, source: str = "") -> str | None:
            return self.asked.pop(0) if self.asked else None

    session = cli._key_session(root, Typed())
    session.prove = lambda row, value: proofs.append(value)
    said = session.open(session.read()[0])
    assert said.endswith("Fireworks connection verified, key added to secrets store"), said
    assert proofs == [KEY] and held(root) == {"fireworks": KEY}


# ---- names and exit codes -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("args", "given"), [
    (["set", "bad name"], KEY), (["set", "1fireworks"], KEY), (["set", "fïreworks"], KEY), (["set", "a" * 129], KEY),
    (["set", "--", "-fireworks"], KEY), (["set", "fireworks\n"], KEY), (["unset", "bad name"], ""),
    (["unset", "fireworks=x"], ""), (["set", "fireworks"], ""),
], ids=["a space", "a digit first", "a letter outside ASCII", "129 bytes", "a dash first", "a newline last",
        "unset a space", "unset name=value", "nothing piped"])
def test_a_name_that_is_none_or_no_value_piped_is_a_usage_error_with_exit_2(
        tmp_path: Path, args: list[str], given: str) -> None:
    root = made(tmp_path)
    got = CliRunner().invoke(cli.main, ["secrets", args[0], "--workspace", str(root), *args[1:]], input=given)
    assert got.exit_code == 2, got.output
    assert held(root) == {} and KEY not in got.output


@pytest.mark.parametrize("args", [["secrets", "list"], ["secrets", "set", "fireworks"], ["secrets"],
                                  ["secrets", "unset", "fireworks"]])
def test_a_file_named_as_the_workspace_is_refused_in_one_line_with_exit_1(
        tmp_path: Path, args: list[str]) -> None:
    afile = tmp_path / "afile"
    afile.write_text("not a folder\n")
    got = CliRunner().invoke(cli.main, [*args, "--workspace", str(afile)], input=KEY + "\n")
    assert got.exit_code == 1, got.output
    assert got.output == f"Error: --workspace {afile.resolve()}: a file, not a folder\n"


@pytest.mark.parametrize("words", ["cerebro secrets nope ", "cerebro secrets -- -V "])
def test_tab_completion_after_a_word_that_is_no_secrets_command_prints_no_traceback(words: str) -> None:
    env = {"_CEREBRO_COMPLETE": "zsh_complete", "COMP_WORDS": words, "COMP_CWORD": str(len(words.split()))}
    got = CliRunner().invoke(cli.main, [], env=env, prog_name="cerebro")
    assert "Traceback" not in got.output and (got.exception is None or isinstance(got.exception, SystemExit)), (
        got.output, got.exception)
    assert "nope" not in got.output and "-V" not in got.output  # the group's commands may be offered, nothing else


# ---- on a terminal the test holds --------------------------------------------------------------------------------

OWN_TERMINAL = "import fcntl, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"


def on_pty(code: str, env: dict[str, str], keys: list[tuple[bytes, bytes]], seconds: float = 30) -> tuple[str, int]:
    """What a child running `code` prints on an 80 by 24 terminal, and its exit code. Each of `keys` is typed once
    what it printed after the place the key before it was typed holds its marker. The test holds the leader side
    open, and so the terminal, until it has read every byte."""
    leader, follower = pty.openpty()
    fcntl.ioctl(follower, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    child = subprocess.Popen([sys.executable, "-c", OWN_TERMINAL + code], stdin=follower, stdout=follower,
                             stderr=follower, env={"PATH": os.environ.get("PATH", ""), **env}, start_new_session=True)
    os.close(follower)
    got, script, seen = b"", list(keys), 0
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            ready, _, _ = select.select([leader], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(leader, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                got += chunk
                if b"\x1b]11;?" in chunk:
                    os.write(leader, b"\x1b]11;rgb:0000/0000/0000\x1b\\")
                if b"\x1b[6n" in chunk:
                    os.write(leader, b"\x1b[1;1R")
                while script and (found := got.find(script[0][0], seen)) >= 0:
                    marker, typed = script.pop(0)
                    seen = found + len(marker)
                    if marker:  # a prompt is drawn before it reads; a key typed in parts goes on at once
                        time.sleep(0.2)
                    os.write(leader, typed)
            elif child.poll() is not None:
                break
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(leader)
    return got.decode("utf-8", "replace"), child.returncode


def env_of(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
            "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "COLORTERM": "truecolor"}


SIGNALLED = """
import os, signal
import cerebro.cli as cli
from cerebro.secrets import FileSecrets
POINT, ARGS = {point!r}, {args!r}
def ctrl_c():
    os.kill(os.getpid(), signal.SIGINT)
cli._prove_on = lambda root: lambda row, value: None
real = FileSecrets.set
def set_(self, name, value):
    if POINT == "keeping":
        ctrl_c()
    real(self, name, value)
    if POINT == "kept":
        ctrl_c()
FileSecrets.set = set_
import cerebro.keys as keys
opened = keys.KeySession.open
def open_(self, *args, **kwargs):
    line = opened(self, *args, **kwargs)
    if POINT == "said" and line:
        ctrl_c()  # the key's line is said: the next prompt has not begun
    return line
keys.KeySession.open = open_
try:
    cli.main(ARGS, prog_name="cerebro")
except SystemExit as stop:
    print("EXITED", stop.code, flush=True)
"""


# Ctrl-C while the key is proven is test_secrets_lobby.py's case. While it is kept, plain and drawn lines share one
# hold, so each end of the hold is at one of them; once its line is said, each goes on its own way.
@pytest.mark.parametrize(("point", "plain"), [("keeping", True), ("kept", False), ("said", True), ("said", False)],
                         ids=["plain-keeping", "drawn-kept", "plain-said", "drawn-said"])
def test_a_ctrl_c_once_the_provider_answered_ends_one_of_two_ways_each_said(
        tmp_path: Path, point: str, plain: bool) -> None:
    """Once the key is being kept it is too late to stop it: the key is kept and its line says so. Never `Aborted!`,
    never a reason that is not the Ctrl-C."""
    root = made(tmp_path)
    args = ["secrets", "--workspace", str(root), *(["--plain"] if plain else [])]
    first: list[tuple[bytes, bytes]] = [(b"Set which?", b"fireworks,typesafe\r")] if plain else [(b" move", b"\r")]
    after = [(b"Typesafe API key", b"\r")] if plain else [(b"key added to secrets store", b""), (b" move", b"\x1b")]
    text, _ = on_pty(SIGNALLED.format(point=point, args=args), env_of(tmp_path),
                     [*first, (b"Fireworks API key", f"{KEY}\r".encode()), *after])
    assert "Aborted" not in text and "rollback" not in text and "Traceback" not in text, text[-900:]
    if point == "said":  # Ctrl-C between one prompt and the next ends the command, saying what stands
        assert held(root) == {"fireworks": KEY}
        assert "1 of 2 model keys set" in text and "EXITED 130" in text, text[-900:]
    else:
        assert held(root) == {"fireworks": KEY}
        assert "Fireworks connection verified, key added to secrets store" in text, text[-900:]
        assert "EXITED 0" in text and ("Typesafe API key" in text if plain else True), text[-900:]


INTERRUPTED_LEDGER = """
import os, signal, sqlite3
import cerebro.cli as cli
from cerebro.app import Cerebro
ARGS = {args!r}
class Commit:
    def __init__(self, db):
        self.db = db
    def execute(self, sql, *rest):
        got = self.db.execute(sql, *rest)
        if sql == "commit":
            os.kill(os.getpid(), signal.SIGINT)  # lands after the commit, inside the store's transaction
        return got
    def __getattr__(self, name):
        return getattr(self.db, name)
def prove(row, value):
    with Cerebro({root!r}) as workspace:
        workspace.store.db = Commit(workspace.store.db)
        with workspace.store._tx():
            pass
cli._prove_on = lambda root: prove
try:
    cli.main(ARGS, prog_name="cerebro")
except SystemExit as stop:
    print("EXITED", stop.code, flush=True)
"""


def test_a_ctrl_c_another_error_took_the_place_of_in_the_stores_transaction_still_reads_as_cancelled(
        tmp_path: Path) -> None:
    root = made(tmp_path)
    args = ["secrets", "--workspace", str(root), "--plain"]
    text, _ = on_pty(INTERRUPTED_LEDGER.format(args=args, root=str(root)), env_of(tmp_path),
                     [(b"Set which?", b"fireworks\r"), (b"Fireworks API key", f"{KEY}\r".encode())])
    assert "Fireworks did not verify: cancelled by Ctrl-C" in text and "rollback" not in text, text[-900:]
    assert "EXITED 0" in text and held(root) == {}


LONG = """
import cerebro.cli as cli
print("LIMIT", cli._typed_limit(), flush=True)
try:
    cli.main({args!r}, prog_name="cerebro")
except SystemExit as stop:
    print("EXITED", stop.code, flush=True)
"""


def typed_limit() -> int:
    """The line limit of a terminal like the test's, on this system: Linux 4,095 bytes, macOS 1,023."""
    leader, follower = pty.openpty()
    try:
        if sys.platform.startswith("linux"):
            return 4095
        return int(os.fpathconf(follower, "PC_MAX_CANON")) - 1
    finally:
        os.close(leader)
        os.close(follower)


@pytest.mark.parametrize("short", [1, 0], ids=["one short of the limit", "at the limit"])
def test_a_key_that_fills_the_terminals_line_is_refused_with_its_limit_and_one_short_of_it_is_kept(
        tmp_path: Path, short: int) -> None:
    """A key that fills the line is refused; the limit is the terminal's, which cuts a longer line on Linux and holds it back on macOS."""
    root = made(tmp_path)
    assert set_piped(root, "fireworks", OLD + "\n").exit_code == 0
    limit = typed_limit()
    key = ("sk-madeup-" + "ZZTOP0123456789" * 400)[:limit - short]
    keys = [(b"Value of fireworks", key[i:i + 200].encode()) for i in range(0, len(key), 200)]
    keys = [keys[0], *((b"", part) for _, part in keys[1:]), (b"", b"\r")]
    text, _ = on_pty(LONG.format(args=["secrets", "set", "fireworks", "--workspace", str(root)]), env_of(tmp_path),
                     keys)
    assert f"LIMIT {limit}" in text, text[:300]
    if short:
        assert "EXITED 0" in text and held(root)["fireworks"] == key, text[-400:]
    else:
        assert f"fills the prompt's {limit} characters and may be cut" in text and "EXITED 1" in text, text[-400:]
        assert held(root)["fireworks"] == OLD


def test_a_proof_error_without_an_interrupt_behind_it_is_said_as_its_own_reason(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = made(tmp_path)

    def prove(row: Any, value: str) -> None:
        raise CerebroError("Fireworks answered 401: the key was not accepted")

    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(cli, "_prove_on", lambda where: prove)
    got = CliRunner().invoke(cli.main, ["secrets", "--workspace", str(root)], input=f"fireworks\n{KEY}\n")
    assert "[x] Fireworks did not verify: Fireworks answered 401: the key was not accepted" in got.output, got.output


def test_a_ctrl_c_between_prompts_in_inits_key_step_says_what_stands_then_ends_with_130(tmp_path: Path) -> None:
    import click

    from cerebro import guided

    root = made(tmp_path)

    class Interrupted:
        def __init__(self) -> None:
            self.said: list[str] = []

        def menu(self, menu: Any, cursor: int = 0) -> int | None:
            raise KeyboardInterrupt  # between the last view and the next menu, before the menu reads its keys

        def say(self, line: str) -> None:
            self.said.append(line)

    screen = Interrupted()
    walk = guided.Walk(root, screen, cli.open_workspace, lobby=lambda: None, first_run=lambda: None, home=Path(),
                       command="cerebro", prove=lambda row, value: None)
    with pytest.raises(click.exceptions.Exit) as ended:
        walk.keys()
    assert ended.value.exit_code == 130
    assert "keys      0 of 2 model keys set" in screen.said, screen.said
