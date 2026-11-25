"""`init`'s lines on a pseudo-terminal when the input ends, as with Ctrl-D: text
typed before the end is kept whole or nothing is written, never a cut value, and nothing more is asked after it, not
by `init`'s own lines and not by the walk's menus. Every name and address is made up, and no key is typed."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from test_output import on_a_pty

TYPED = "rowan@quillmoor.example"


def run_init(tmp_path: Path, args: list[str], keys: tuple[tuple[object, ...], ...]) -> str:
    code = ("import cerebro.cli\n"
            "try:\n"
            f"    cerebro.cli.main(['init', *{args!r}], prog_name='cerebro')\n"
            "except SystemExit as stop:\n"
            "    print('EXITED', stop.code, flush=True)\n")
    env = {"HOME": str(tmp_path / "home"), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file", "TZ": "UTC",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "COLORTERM": "truecolor"}
    (tmp_path / "home").mkdir(exist_ok=True)
    return on_a_pty(code, env, answer=b"\x1b]11;rgb:0000/0000/0000\x1b\\", seconds=20, keys=keys).decode(
        "utf-8", "replace")


def email_of(root: Path) -> str | None:
    found = re.search(r"email: \[([^\]]*)\]", (root / "profile.md").read_text()) if (root / "profile.md").is_file() \
        else None
    return found.group(1) if found else None


def test_an_address_typed_then_ctrl_d_twice_is_kept_whole_and_nothing_more_is_asked(tmp_path: Path) -> None:
    """The text read before the end of input is kept."""
    root = tmp_path / "wiki"
    out = run_init(tmp_path, ["--plain", str(root)], (
        (b"Your name", 1, b"Rowan Vale\r"), (b"work email", 1, TYPED.encode() + b"\x04"),
        (TYPED.encode(), 1, b"\x04")))
    assert "EXITED 0" in out or "EXITED None" in out, out[-600:]
    assert email_of(root) == TYPED, out[-600:]
    assert "quillmoor.example" in (root / "workspace.md").read_text()
    assert "Team or organization" not in out and "Daily budget" not in out, out[-600:]


@pytest.mark.parametrize(("at", "typed"), [("work email", b"ab"), ("Team or organization web domain", b"not a domain"),
                                           ("Daily budget", b"lots"), ("Time zone", b"Mars/Olympus")])
def test_a_value_typed_then_ctrl_d_that_does_not_do_ends_init_with_1_and_writes_nothing(
        tmp_path: Path, at: str, typed: bytes) -> None:
    """The check's error stands when the input ended: init ends, exit 1."""
    root = tmp_path / "wiki"
    keys = [(b"Your name", 1, b"Rowan Vale\r")]
    for before, answer in (("work email", TYPED.encode() + b"\r"), ("Team or organization", b"\r"),
                           ("Team or organization web domain", b"\r"), ("Daily budget", b"\r")):
        if before == at:
            break
        keys.append((before.encode(), 1, answer))
    keys += [(at.encode(), 1, typed + b"\x04"), (typed, 1, b"\x04")]
    out = run_init(tmp_path, ["--plain", str(root)], tuple(keys))
    assert "EXITED 1" in out, out[-600:]
    assert not (root / "settings.yaml").exists()


def test_after_the_input_ends_the_walk_opens_no_menu_and_init_ends(tmp_path: Path) -> None:
    """Nothing is asked once the input ends, and every step of the walk asks only while it can."""
    root = tmp_path / "wiki"
    out = run_init(tmp_path, [str(root)], ((b"New workspace", 1, b"\r"), (b"Your name", 1, b"\x04")))
    assert "EXITED" in out, out[-600:]
    assert (root / "settings.yaml").is_file()
    assert "Each job runs on one model" not in out and "esc done" not in out, out[-800:]


def test_ctrl_d_at_a_keys_view_prompt_is_no_key_and_escape_goes_back_and_the_walk_goes_on(tmp_path: Path) -> None:
    """The key view reads its prompt raw, where Ctrl-D is a control key the prompt does not use: it does nothing, Escape goes back, and the walk goes on. Only init's own
    lines end the asking on the end of input."""
    root = tmp_path / "wiki"
    out = run_init(tmp_path, [str(root)], (
        (b"New workspace", 1, b"\r"), (b"Your name", 1, b"\r"), (b"work email", 1, b"\r"),
        (b"Team or organization", 1, b"\r"), (b"Team or organization web domain", 1, b"\r"), (b"Daily budget", 1, b"\r"),
        (b"Time zone", 1, b"\r"), (b"Choose providers", 1, b"\x1b"), (b"Add keys for", 1, b"\r"),
        (b"Fireworks API key", 1, b"\x04", 1.0), (b"Fireworks API key", 1, b"\x1b", 0.5),
        (b"Add keys for", 2, b"\x1b", 0.5),
        (b"Pick one to connect", 1, b"\x1b"), (b"Claude Desktop", 1, b"\x1b")))
    assert "Pick one to connect" in out and "EXITED 0" in out, out[-800:]


LINES = ("Your name", "work email", "Team or organization", "Team or organization web domain", "Daily budget", "Time zone")


@pytest.mark.parametrize("at", LINES)
def test_ctrl_c_at_each_of_inits_lines_on_a_terminal_ends_init_with_130_and_writes_nothing(
        tmp_path: Path, at: str) -> None:
    root = tmp_path / "wiki"
    keys = []
    for line in LINES:
        keys.append((line.encode(), 1, b"\x03" if line == at else b"\r"))
        if line == at:
            break
    out = run_init(tmp_path, ["--plain", str(root)], tuple(keys))
    assert "EXITED 130" in out, out[-600:]
    assert not root.exists()
