"""A provider's view in the keys lobby leaves the terminal as it found it by every way out: a key given, the end of
input, Ctrl-C, SIGTERM, SIGHUP and SIGQUIT. Each case runs `cerebro secrets`
on a pseudo-terminal the test keeps open, so the terminal's modes are read after the command ends. The key's proof is
a stand-in that reaches no provider, and every key typed is made up.

The modes are read on the pseudo-terminal's leader side, before and after. The command is its terminal's session leader,
and once it ends darwin revokes the follower, so a read there fails with ENOTTY, and a follower opened again starts from
the default modes, which would hide a fault. The leader shares the follower's modes on darwin and on Linux, and keeps
them after the command ends: `test_the_leader_side_reads_the_modes_a_command_left_on_its_terminal` shows it on the
platform that runs the suite, with a command that leaves them cleared."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro import cli

ALT = re.compile(rb"\x1b\[\?1049([hl])")
MODES = termios.ECHO | termios.ICANON | termios.ISIG
OWN_TERMINAL = "import fcntl, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
# A hidden reader that clears ECHO and never puts it back, in place of getpass, which restores the modes itself on
# every way out: with it, only the prompt's own restore can give the terminal back, so the cases show that restore works.
# A view reads its keys itself and never calls getpass, so the readers are a matter for the plain prompts alone.
LEAKS = """import getpass, sys, termios
def _leaks(prompt="Password: ", stream=None):
    modes = termios.tcgetattr(0)
    modes[3] &= ~termios.ECHO
    termios.tcsetattr(0, termios.TCSANOW, modes)
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\\n")
getpass.getpass = _leaks
"""
READERS = {"getpass": "", "a reader that leaves echo off": LEAKS}
PROVES = {
    "fine": "lambda row, value: None",
    "slow": "lambda row, value: (print('PROVING', flush=True), __import__('time').sleep(2))[0]",
}


def made(tmp_path: Path) -> Path:
    root = (tmp_path / "wiki").resolve()
    got = CliRunner().invoke(cli.main, ["init", str(root), "--timezone", "UTC", "--daily-usd", "5"])
    assert got.exit_code == 0, got.output
    return root


class Run:
    """`cerebro secrets` on a pseudo-terminal: what it printed, how it ended, and the terminal's modes after."""

    def __init__(self, tmp_path: Path, prove: str = "fine", reader: str = "getpass", args: tuple[str, ...] = ("secrets",),
                 before: str = "") -> None:
        root = made(tmp_path)
        self.root = root
        code = (OWN_TERMINAL + READERS[reader] + before + "import cerebro.cli\n"
                f"cerebro.cli._prove_on = lambda root: {PROVES[prove]}\n"
                f"cerebro.cli.main([*{list(args)!r}, '--workspace', {str(root)!r}], prog_name='cerebro')\n")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "TERM": "xterm-256color",
               "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
               "CEREBRO_SECRETS_STORE": "file", "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
               "COLORTERM": "truecolor"}
        self.leader, self.follower = pty.openpty()
        fcntl.ioctl(self.follower, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.before = modes(self.leader)
        self.child = subprocess.Popen([sys.executable, "-c", code], stdin=self.follower, stdout=self.follower,
                                      stderr=self.follower, env=env, start_new_session=True)
        self.got = b""

    def read_until(self, marker: bytes, count: int = 1, seconds: float = 20) -> None:
        end = time.monotonic() + seconds
        while self.got.count(marker) < count and time.monotonic() < end:
            if not self.read(0.05):
                break
        assert self.got.count(marker) >= count, self.got[-600:]
        time.sleep(0.3)  # the prompt is drawn before the line is read: let it start reading
        self.read(0.05)

    def read(self, wait: float) -> bool:
        ready, _, _ = select.select([self.leader], [], [], wait)
        if not ready:
            return self.child.poll() is None
        try:
            chunk = os.read(self.leader, 65536)
        except OSError:
            return False
        self.got += chunk
        if b"\x1b]11;?" in chunk:
            os.write(self.leader, b"\x1b]11;rgb:0000/0000/0000\x1b\\")
        if b"\x1b[6n" in chunk:
            os.write(self.leader, b"\x1b[1;1R")
        return bool(chunk)

    def type(self, keys: bytes) -> None:
        os.write(self.leader, keys)

    def end(self, seconds: float = 15) -> int | None:
        stop = time.monotonic() + seconds
        while self.child.poll() is None and time.monotonic() < stop:
            self.read(0.05)
        if self.child.poll() is None:
            self.child.kill()
            self.child.wait()
            return None
        while self.read(0.1):
            pass
        return self.child.returncode

    def on_main_screen(self) -> bool:
        toggles = ALT.findall(self.got)
        return bool(toggles) and toggles[-1] == b"l"

    def modes_back(self) -> bool:
        return modes(self.leader) == self.before

    def cursor_shown(self) -> bool:
        return self.got.rfind(b"\x1b[?25l") <= self.got.rfind(b"\x1b[?25h")

    def close(self) -> None:
        os.close(self.leader)
        os.close(self.follower)


def modes(leader: int) -> int:
    """ECHO, ICANON and ISIG of a pseudo-terminal, read on its leader side: the terminal's own, not what a child says."""
    return termios.tcgetattr(leader)[3] & MODES


LEAVES = OWN_TERMINAL + """import sys, termios
modes = termios.tcgetattr(0)
cleared = list(modes)
cleared[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
termios.tcsetattr(0, termios.TCSANOW, cleared)
if sys.argv[1] == "restores":
    termios.tcsetattr(0, termios.TCSANOW, modes)
print("ENDING", flush=True)
"""


@pytest.mark.parametrize("way", ["restores", "leaves them cleared"])
def test_the_leader_side_reads_the_modes_a_command_left_on_its_terminal(way: str) -> None:
    """The way the cases below read the modes tells the truth where the suite runs: a session leader that clears ECHO,
    ICANON and ISIG and ends reads as having left them cleared, and one that puts them back reads as the modes before."""
    leader, follower = pty.openpty()
    try:
        before = modes(leader)
        assert before == MODES  # a new pseudo-terminal echoes, reads lines and sends signals
        child = subprocess.Popen([sys.executable, "-c", LEAVES, way.split()[0]], stdin=follower, stdout=follower,
                                 stderr=follower, start_new_session=True)
        got = b""
        while b"ENDING" not in got:
            ready, _, _ = select.select([leader], [], [], 10)
            assert ready, got
            got += os.read(leader, 1024)
        assert child.wait(10) == 0
        assert modes(leader) == (before if way == "restores" else 0)
    finally:
        os.close(leader)
        os.close(follower)


def open_view(run: Run) -> None:
    run.read_until(b" move", 2)
    run.type(b"\r")
    run.read_until(b"Fireworks API key")


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    for name in ("FIREWORKS_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


# A key given is the Ctrl-\ case below, SIGTERM the lobby's menu's and the proof's, whose way out an exception in
# the proof takes too, and SIGQUIT has its own case.
@pytest.mark.parametrize(("way", "code"), [("the end of input", 0), ("ctrl-c", 130), ("sighup", 129)])
def test_every_way_out_of_a_view_leaves_the_main_screen_and_the_terminals_modes_back(
        scratch: Path, way: str, code: int) -> None:
    run = Run(scratch)
    try:
        open_view(run)
        if way == "the end of input":  # Ctrl-D is no key at the view prompt, which reads raw; Escape goes back
            run.type(b"\x04")
            time.sleep(0.5)
            assert run.child.poll() is None  # the prompt still waits
            run.type(b"\x1b")
            run.read_until(b" move", 3)
            run.type(b"\x1b")
        elif way == "ctrl-c":
            run.type(b"made-up\x03")
        else:
            os.killpg(run.child.pid, signal.SIGHUP)
        assert run.end() == code, run.got[-600:]
        assert run.on_main_screen(), run.got[-600:]
        assert run.modes_back()
        if way == "ctrl-c":  # it ends the command: no lobby is drawn again and no count of keys is said
            assert b"model keys set" not in run.got, run.got[-600:]
    finally:
        run.close()


def test_ctrl_backslash_in_a_view_is_a_key_that_stops_nothing(scratch: Path) -> None:
    """The view reads the key in raw mode, so Ctrl-\\ is a key there, as Ctrl-Z is: the view stays, and a key typed
    around it is kept whole."""
    run = Run(scratch)
    try:
        open_view(run)
        run.type(b"made-up-\x1ckey-five")
        time.sleep(0.5)
        run.type(b"\r")
        run.read_until(b"connection verified")
        run.read_until(b" move", 3)
        run.type(b"\x1b")
        assert run.end() == 0, run.got[-600:]
        assert run.on_main_screen() and run.modes_back()
    finally:
        run.close()
    from cerebro.app import Cerebro

    with Cerebro(run.root) as workspace:
        assert workspace.secrets is not None and workspace.secrets.get("fireworks") == "made-up-key-five"


def test_sigquit_in_a_view_gives_the_main_screen_and_the_modes_back_then_ends_with_131(
        scratch: Path) -> None:
    run = Run(scratch)
    try:
        open_view(run)
        run.type(b"made-up-par")
        os.killpg(run.child.pid, signal.SIGQUIT)
        assert run.end() == 131, run.got[-600:]
        assert run.on_main_screen() and run.modes_back()
    finally:
        run.close()


def test_sigterm_at_the_lobbys_menu_gives_the_terminal_back_then_ends_the_command(scratch: Path) -> None:
    """The menu takes a signal by the handler a view's prompt has, whose SIGHUP and SIGQUIT are the view's cases."""
    run = Run(scratch)
    try:
        run.read_until(b" move", 2)
        os.killpg(run.child.pid, signal.SIGTERM)
        assert run.end() == 130, run.got[-600:]
        assert run.modes_back() and run.cursor_shown(), run.got[-300:]
    finally:
        run.close()


BREAKS = """import cerebro.checklist
def _breaks(self, key):
    raise RuntimeError("a made-up fault")
cerebro.checklist.Menu.press = _breaks
"""


def test_an_error_inside_the_lobbys_menu_ends_with_one_line_and_exit_1(scratch: Path) -> None:
    run = Run(scratch, before=BREAKS)
    try:
        run.read_until(b" move", 2)
        run.type(b"j")
        assert run.end() == 1, run.got[-600:]
        assert run.modes_back() and run.cursor_shown()
        text = run.got.decode("utf-8", "replace")
        assert "Traceback" not in text and "the list ended on an error: RuntimeError: a made-up fault" in text, (
            text[-600:])
        assert "model keys set" not in text
    finally:
        run.close()


# At a terminal that draws this line is a view's prompt, which takes SIGTERM and SIGHUP as a key's view does above.
@pytest.mark.parametrize(("way", "code"), [("sigquit", 131), ("ctrl-backslash", 0)])
def test_connects_hidden_line_gives_the_terminals_modes_back_by_every_way_out(
        scratch: Path, way: str, code: int) -> None:
    run = Run(scratch, args=("connect", "calendar"), reader="a reader that leaves echo off")
    try:
        run.read_until(b"Google Calendar")  # where the calendar lives is asked first
        run.type(b"\r")
        run.read_until(b"Secret address")
        if way == "ctrl-backslash":
            run.type(b"\x1c")
            time.sleep(0.5)
            assert run.child.poll() is None  # the line is still read
            run.type(b"\x03")
            code = run.end() or 0
            assert code not in (-signal.SIGQUIT,), code
        else:
            os.killpg(run.child.pid, signal.SIGQUIT)
            assert run.end() == code, run.got[-600:]
        assert run.modes_back()
    finally:
        run.close()


def test_sigterm_while_a_key_is_proven_leaves_the_main_screen_and_the_terminals_modes_back(
        scratch: Path) -> None:
    run = Run(scratch, prove="slow")
    try:
        open_view(run)
        run.type(b"made-up-key-three\r")
        run.read_until(b"PROVING")
        os.killpg(run.child.pid, signal.SIGTERM)
        assert run.end() == 130, run.got[-600:]
        assert run.on_main_screen() and run.modes_back()
    finally:
        run.close()


TYPED = (b"sk-standin-QWXZ", b"7319PLMK")  # a made-up key, typed in two halves around a way out
# Each prompt's own last words, which no line before it holds: the view's line above names the console's API keys.
PROMPTS = {"secrets --plain": (("secrets", "--plain"), b"Fireworks API key [Enter to skip]: "),
           "secrets set": (("secrets", "set", "fireworks"), b"Value of fireworks: ")}
# Each way out and its exit, as the hidden line is read raw: Ctrl-\ and Ctrl-Z are keys that stop nothing, and Ctrl-C
# or Ctrl-D cancels, which goes on to the next key on plain lines and ends `set` with 1.
# SIGTERM, SIGHUP and SIGQUIT end with 130, 129 and 131, as at a list and at `connect`'s hidden lines.
HIDDEN_WAYS = {"ctrl-backslash": (0, 0), "ctrl-z": (0, 0), "ctrl-c": (130, 1), "the end of input": (0, 1),
               "sigterm": (130, 130), "sighup": (129, 129), "sigquit": (131, 131)}


# Ctrl-C and Ctrl-D end each prompt its own way, so each is at both prompts. Ctrl-\, Ctrl-Z and each signal take the
# terminal held while a hidden line is read, one road at every prompt and the only hold on these, so each is at one,
# and a key typed around Ctrl-\ or Ctrl-Z is a key given. The reader that leaves echo off needs the hold's own
# restore, and getpass is the reader at a prompt's own Ctrl-C and Ctrl-D.
ECHO_OFF = "a reader that leaves echo off"
HIDDEN_CELLS = [("secrets --plain", "ctrl-backslash", ECHO_OFF), ("secrets set", "ctrl-z", ECHO_OFF),
                ("secrets --plain", "ctrl-c", ECHO_OFF), ("secrets set", "ctrl-c", "getpass"),
                ("secrets --plain", "the end of input", "getpass"), ("secrets set", "the end of input", ECHO_OFF),
                ("secrets --plain", "sigterm", ECHO_OFF), ("secrets set", "sighup", ECHO_OFF),
                ("secrets set", "sigquit", "getpass")]


@pytest.mark.parametrize(("prompt", "way", "reader"), HIDDEN_CELLS, ids=["-".join(cell) for cell in HIDDEN_CELLS])
def test_a_plain_hidden_key_prompt_gives_the_terminal_back_and_shows_no_key_by_every_way_out(
        scratch: Path, prompt: str, way: str, reader: str) -> None:
    """The terminal's modes, read on the leader side once the command has gone, are as before by every way out; no
    part of the key typed is on the screen or in a file but the store, and the store holds the whole key or none."""
    args, ready = PROMPTS[prompt]
    run = Run(scratch, args=args, reader=reader)
    whole = termios.tcgetattr(run.leader)
    try:
        if prompt == "secrets --plain":
            run.read_until(b"Set which?")
            run.type(b"fireworks\r")
        run.read_until(ready)
        if way in ("ctrl-backslash", "ctrl-z"):
            run.type(TYPED[0] + (b"\x1c" if way == "ctrl-backslash" else b"\x1a"))
            time.sleep(0.5)
            assert run.child.poll() is None  # the line is still read
            run.type(TYPED[1] + b"\r")
        elif way in ("ctrl-c", "the end of input"):
            run.type(b"\x03" if way == "ctrl-c" else b"\x04")
        else:
            run.type(TYPED[0])
            time.sleep(0.3)
            os.killpg(run.child.pid, {"sigterm": signal.SIGTERM, "sighup": signal.SIGHUP}.get(way, signal.SIGQUIT))
        code = HIDDEN_WAYS[way][prompt == "secrets set"]
        assert run.end() == code, run.got[-600:]
        assert termios.tcgetattr(run.leader) == whole
    finally:
        run.close()
    from cerebro.app import Cerebro

    kept = b"".join(TYPED).decode() if way in ("ctrl-backslash", "ctrl-z") else None
    with Cerebro(run.root) as workspace:
        assert workspace.secrets is not None
        assert (workspace.secrets.get("fireworks") if "fireworks" in workspace.secrets.names() else None) == kept
        store = Path(workspace.secrets.where())
    assert not any(half in run.got for half in TYPED), run.got[-600:]
    for path in scratch.rglob("*"):
        if path.is_file() and path != store:
            assert not any(half in path.read_bytes() for half in TYPED), path
