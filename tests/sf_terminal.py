"""`cerebro connect salesforce` at a pseudo-terminal, with the fake `sf` of `fake_sf` on the PATH and the stand-in org
of `test_connector_salesforce` in place of a real one. No real CLI or org is reached. Every process started is killed
before it returns."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import fake_sf

from cerebro.output import strip_escapes

TESTS = Path(__file__).parent
CHILD = """import fcntl, termios, sys
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
sys.path.insert(1, {tests!r})
import httpx
from test_salesforce_cli_login import Org
from cerebro import cli
from cerebro.app import Cerebro
client = httpx.Client(transport=httpx.MockTransport(Org()))
cli.open_workspace = lambda root: Cerebro(root, http=client)
sys.argv = ["cerebro", "connect", *{args!r}]
code = 0
try:
    cli.main()
except SystemExit as ended:
    code = ended.code if isinstance(ended.code, int) else (0 if ended.code is None else 1)
open({exit!r}, "w").write(str(code))
sys.exit(code)
"""


class Screen:
    """Just enough of a terminal to read a frame as text: characters, carriage return and line feed, the cursor
    moves, erasing a line or below, and the screen apart (1049) kept as its own grid. Styles are dropped."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.main = [[" "] * cols for _ in range(rows)]
        self.apart: list[list[str]] | None = None
        self.y = self.x = 0

    @property
    def grid(self) -> list[list[str]]:
        return self.apart if self.apart is not None else self.main

    def feed(self, data: str) -> None:
        import re

        at = 0
        for m in re.finditer(r"\x1b\[([?<>=0-9;]*)[ -/]*([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]",
                             data):
            self._text(data[at:m.start()])
            at = m.end()
            if m.group(2):
                self._csi(m.group(1), m.group(2))
        self._text(data[at:])

    def _scroll(self) -> None:
        self.grid.pop(0)
        self.grid.append([" "] * self.cols)
        self.y = self.rows - 1

    def _text(self, text: str) -> None:
        for c in text:
            if c == "\r":
                self.x = 0
            elif c == "\n":
                self.y += 1
                if self.y >= self.rows:
                    self._scroll()
            elif c == "\b":
                self.x = max(0, self.x - 1)
            elif c >= " ":
                if self.x >= self.cols:
                    self.x = 0
                    self.y += 1
                    if self.y >= self.rows:
                        self._scroll()
                self.grid[self.y][self.x] = c
                self.x += 1

    def _csi(self, params: str, final: str) -> None:
        if params in ("?1049", "?1047", "?47"):
            if final == "h":
                self.apart, self._saved = [[" "] * self.cols for _ in range(self.rows)], (self.y, self.x)
            elif final == "l":
                self.apart = None
                self.y, self.x = getattr(self, "_saved", (self.y, self.x))
            return
        if params[:1] in ("?", "<", ">", "="):
            return
        n = [int(p) if p else 0 for p in params.split(";")] if params else [0]
        k = max(n[0], 1)
        if final == "A":
            self.y = max(0, self.y - k)
        elif final == "B":
            self.y = min(self.rows - 1, self.y + k)
        elif final == "C":
            self.x = min(self.cols - 1, self.x + k)
        elif final == "D":
            self.x = max(0, self.x - k)
        elif final == "G":
            self.x = min(self.cols - 1, k - 1)
        elif final == "H":
            self.y, self.x = min(self.rows - 1, max(n[0], 1) - 1), min(self.cols - 1, max(n[1] if len(n) > 1 else 1, 1) - 1)
        elif final == "K":
            row = self.grid[self.y]
            span = range(self.x, self.cols) if n[0] == 0 else range(self.x + 1) if n[0] == 1 else range(self.cols)
            for i in span:
                row[i] = " "
        elif final == "J":
            if n[0] == 0:
                self.grid[self.y][self.x:] = [" "] * (self.cols - self.x)
                for r in range(self.y + 1, self.rows):
                    self.grid[r] = [" "] * self.cols
            elif n[0] in (2, 3):
                for r in range(self.rows):
                    self.grid[r] = [" "] * self.cols

    def text(self) -> str:
        lines = ["".join(row).rstrip() for row in self.grid]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


def frames(raw: bytes, marks: tuple[tuple[bytes, ...], ...], size: tuple[int, int] = (24, 80)) -> list[str]:
    """The screen as text at each mark, then at the end. A mark is a run of texts, each found after the one before,
    and the frame is taken where the last of them ends."""
    screen = Screen(*size)
    out, at = [], 0
    for mark in marks:
        i, end = at, -1
        for part in mark:
            i = raw.find(part, i)
            if i < 0:
                break
            end = i = i + len(part)
        if i < 0:
            continue
        screen.feed(raw[at:end].decode("utf-8", "replace"))
        at = end
        out.append(screen.text())
    screen.feed(raw[at:].decode("utf-8", "replace"))
    out.append(screen.text())
    return out


def lay_out_workspace(folder: Path) -> Path:
    """A new workspace in `folder/team`, made by `cerebro init`."""
    from click.testing import CliRunner

    from cerebro import cli

    root = folder / "team"
    done = CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1"])
    assert done.exit_code == 0, done.output
    return root


def connect(folder: Path, root: Path, args: list[str], keys: tuple[tuple[bytes, int, bytes], ...] = (), *,
            styled: bool = True, seconds: float = 60, size: tuple[int, int] = (24, 80)) -> tuple[str, int | None, bytes]:
    """Run `cerebro connect <args> --workspace root` at a pseudo-terminal of `size`, with the fake sf written in
    `folder/bin` on the PATH; styled, or with NO_COLOR set. Each key is typed once what was printed holds its marker
    the given number of times. Gives what was drawn with its escapes taken out, the exit code (None when it did not
    end in time), and the bytes as drawn."""
    leader, follower = pty.openpty()
    fcntl.ioctl(follower, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
    ended = folder / "exit"
    ended.unlink(missing_ok=True)
    code = CHILD.format(tests=str(TESTS), args=[*args, "--workspace", str(root)], exit=str(ended))
    variables = {"PATH": fake_sf.path_with(folder / "bin"), "TERM": "xterm-256color", "COLORTERM": "truecolor",
                 "HOME": os.environ.get("HOME", str(folder)), "CEREBRO_SECRETS_STORE": "file",
                 "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
                 "PYTHONPATH": os.environ.get("PYTHONPATH", "")}
    variables |= {k: v for k, v in os.environ.items() if k in ("XDG_CONFIG_HOME", "TMPDIR")}  # where trust is kept
    if not styled:
        variables["NO_COLOR"] = "1"
    child = subprocess.Popen([sys.executable, "-c", code], stdin=follower, stdout=follower, stderr=follower,
                             env=variables, start_new_session=True)
    os.close(follower)
    got = b""
    script = list(keys)
    try:
        end = time.monotonic() + seconds
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
                    os.write(leader, b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")
                if b"\x1b[6n" in chunk:
                    os.write(leader, b"\x1b[1;1R")
                while script and got.count(script[0][0]) >= script[0][1]:
                    time.sleep(0.3)  # the prompt reads keys once it is drawn
                    os.write(leader, script.pop(0)[2])
            elif child.poll() is not None:
                break
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.wait()
        os.close(leader)
    text = strip_escapes(got.decode("utf-8", "replace")).replace("\r\n", "\n")
    status = int(ended.read_text()) if ended.exists() else child.returncode
    return text, status, got
