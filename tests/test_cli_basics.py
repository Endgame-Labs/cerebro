"""The command line's basics: `-h` and `help` on every command, help on standard output with no arguments, `--plain`
and `--workspace` at any place on a command's line, an unknown command refused with exit 2, a command
outside any workspace that names `cerebro init`, and `cerebro completion <shell>`. Every command is reached by walking
the command tree, never by a list typed here."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cerebro.cli import main


def _commands(group: click.Group = main, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every command under `group`, groups and leaves, as the words that name it."""
    out: list[tuple[str, ...]] = []
    for name, command in sorted(group.commands.items()):
        if command.hidden:
            continue
        out.append((*path, name))
        if isinstance(command, click.Group):
            out += _commands(command, (*path, name))
    return out


def _leaves() -> list[tuple[str, ...]]:
    return [p for p in _commands() if not isinstance(_command(p), click.Group)]


def _command(path: tuple[str, ...]) -> click.Command:
    command: click.Command = main
    for word in path:
        assert isinstance(command, click.Group)
        command = command.commands[word]
    return command


def invoke(*args: str) -> click.testing.Result:
    return CliRunner().invoke(main, list(args), prog_name="cerebro")


# -- -h on every command, as --help

@pytest.mark.parametrize("path", [(), *_commands()], ids=lambda p: " ".join(p) or "cerebro")
def test_dash_h_is_the_help_of_every_command(path: tuple[str, ...]) -> None:
    long, short = invoke(*path, "--help"), invoke(*path, "-h")
    assert long.exit_code == 0 and short.exit_code == 0, short.output
    assert short.stdout == long.stdout


# -- cerebro help [command]

@pytest.mark.parametrize("path", [(), *_commands()], ids=lambda p: " ".join(p) or "cerebro")
def test_help_names_a_command_and_prints_its_help(path: tuple[str, ...]) -> None:
    told = invoke("help", *path)
    assert told.exit_code == 0, told.output
    assert told.stdout == invoke(*path, "--help").stdout


def test_help_of_a_command_that_does_not_exist_is_a_usage_error() -> None:
    told = invoke("help", "nosuch")
    assert told.exit_code == 2 and "No such command 'nosuch'" in told.stderr


# -- no arguments: the help on standard output, exit 0

def test_no_arguments_print_the_help_on_standard_output_and_exit_0() -> None:
    bare = invoke()
    assert bare.exit_code == 0
    assert bare.stdout == invoke("--help").stdout and bare.stderr == ""
    assert bare.stdout.startswith("Usage: cerebro [OPTIONS] [COMMAND] [ARGS]...\n")


# -- --plain and --workspace at any place on a command's line

def _placed(path: tuple[str, ...], given: list[str]) -> list[list[str]]:
    """The command line for `path` with `given` at each place it can go: before the first word, between any two, and
    after the last."""
    return [[*path[:n], *given, *path[n:]] for n in range(len(path) + 1)]


@pytest.mark.parametrize("path", [(), *_commands()], ids=lambda p: " ".join(p) or "cerebro")
def test_every_command_takes_plain_and_workspace_at_every_place_on_its_line(path: tuple[str, ...],
                                                                             tmp_path: Path) -> None:
    for given in (["--plain"], ["--workspace", str(tmp_path)]):
        for line in _placed(path, given):
            told = invoke(*line, "--help")
            assert told.exit_code == 0, (line, told.output)
            assert told.stdout == invoke(*path, "--help").stdout, line
    assert "--plain" in invoke(*path, "--help").stdout and "--workspace" in invoke(*path, "--help").stdout


def test_workspace_at_any_place_reaches_a_command_under_a_group_and_the_last_given_wins(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import cli

    seen: list[Path | None] = []

    def opened(root: Path | None) -> None:
        seen.append(root)
        raise click.ClickException("stop here")

    monkeypatch.setattr(cli, "open_workspace", opened)
    a, b, c = (str(tmp_path / name) for name in "abc")
    for line in _placed(("agents", "list"), ["--workspace", a]):
        invoke(*line)
    invoke("--workspace", a, "agents", "--workspace", b, "list")
    invoke("agents", "--workspace", b, "list", "--workspace", c)
    invoke("--workspace", a, "agents", "list", "--workspace", c)
    invoke("--workspace", a, "--workspace", b, "agents", "list")
    assert seen == [Path(a)] * 3 + [Path(b), Path(c), Path(c), Path(b)]


def test_plain_at_any_place_reaches_a_group_with_a_plain_of_its_own(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import cli, scaffold

    root = tmp_path / "wiki"
    scaffold.init(root)
    seen: list[bool] = []

    def drawing(plain: bool) -> None:
        seen.append(plain)
        raise click.ClickException("stop here")

    monkeypatch.setattr(cli, "_drawing", drawing)
    monkeypatch.setattr(cli, "_asking", lambda: True)
    for line in [*_placed(("secrets",), ["--plain"]), ["secrets"]]:
        invoke(*line, "--workspace", str(root))
    assert seen == [True, True, False]


def test_plain_before_the_command_reaches_a_command_with_a_plain_of_its_own(monkeypatch: pytest.MonkeyPatch,
                                                                            tmp_path: Path) -> None:
    from cerebro import cli

    seen: list[bool] = []

    def drawing(plain: bool) -> None:
        seen.append(plain)
        raise click.ClickException("stop here")

    monkeypatch.setattr(cli, "_drawing", drawing)
    (tmp_path / "settings.yaml").write_text("")  # connect refuses a folder that is no workspace before it draws
    invoke("--plain", "connect", "--workspace", str(tmp_path))
    invoke("connect", "--plain", "--workspace", str(tmp_path))
    invoke("connect", "--workspace", str(tmp_path))
    assert seen == [True, True, False]


@pytest.mark.parametrize("given", [["--plain"], ["--workspace", "{sub}"]], ids=["plain", "workspace"])
def test_a_flag_before_init_is_the_flag_after_it_at_a_terminal_that_draws(tmp_path: Path, given: list[str],
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """At a terminal that draws, init walks only with no option given: --plain or --workspace before its name is an
    option given, so it asks on plain lines, in the folder named, as with the flag after its name."""
    from cerebro import cli
    from cerebro.output import Printer

    walked: list[Path | None] = []
    monkeypatch.setattr(cli, "_drawing", lambda plain: None if plain else Printer(plain=True))
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(cli, "_guided_init", lambda folder, printer: walked.append(folder))
    monkeypatch.chdir(tmp_path)
    told = {}
    for place, folder in (("before", tmp_path / "one" / "w"), ("after", tmp_path / "two" / "w")):
        flags = [word.replace("{sub}", str(folder)) for word in given]
        named = [] if "--workspace" in given else [str(folder)]
        line = [*flags, "init", *named] if place == "before" else ["init", *named, *flags]
        told[place] = invoke(*line)
        assert told[place].exit_code == 0, (line, told[place].output)
    assert walked == []
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("settings.yaml")) == [
        "one/w/settings.yaml", "two/w/settings.yaml"]
    assert told["before"].output.replace(f"{tmp_path}/one", f"{tmp_path}/two") == told["after"].output


@pytest.mark.parametrize("given", [["--plain"], ["--workspace", "{sub}"]], ids=["plain", "workspace"])
def test_a_flag_before_init_on_a_colour_terminal_sends_no_escape_byte_and_makes_the_workspace_where_named(
        tmp_path: Path, given: list[str]) -> None:
    from test_output import on_a_pty

    here, sub = tmp_path / "here", tmp_path / "there" / "sub"
    here.mkdir()
    flags = [word.replace("{sub}", str(sub)) for word in given]
    args = [*flags, "init", *([] if "--workspace" in given else [str(sub)])]
    code = (f"import os\nos.chdir({str(here)!r})\nimport cerebro.clock\n"
            "cerebro.clock.local_zone = lambda *a, **k: 'Europe/Berlin'\n"
            f"from cerebro.cli import main\nmain({args!r}, prog_name='cerebro')\n")
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "COLORTERM": "truecolor", "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
    got = on_a_pty(code, env, seconds=15, keys=tuple((asked, 1, b"\r") for asked in (
        b"Your name", b"Your work email", b"Team or organization name", b"Team or organization web domain", b"Daily budget",
        b"Time zone")))
    assert b"\x1b" not in got, got[-400:]
    assert (sub / "settings.yaml").is_file(), got[-400:]
    assert not (here / "settings.yaml").exists()


# -- an unknown command

def test_an_unknown_command_is_refused_with_exit_2() -> None:
    told = invoke("nosuch")
    assert told.exit_code == 2 and told.stderr.strip().endswith("No such command 'nosuch'.")


# -- a command outside any workspace names cerebro init

def test_a_command_outside_any_workspace_names_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    told = invoke("status")
    assert told.exit_code == 1 and "cerebro init" in told.stderr, told.stderr
    named = invoke("status", "--workspace", str(tmp_path / "elsewhere"))
    assert named.exit_code == 1 and f"cerebro init {tmp_path / 'elsewhere'}" in named.stderr, named.stderr


def test_a_folder_inside_a_workspace_is_never_offered_cerebro_init(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder inside a workspace is never offered `cerebro init`, since an init there would make a second workspace
    inside the first."""
    from cerebro import scaffold

    root = tmp_path / "wiki"
    scaffold.init(root)
    (root / "local" / "deeper").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(root / "local")
    said = [invoke("status"), invoke("agents", "list"), invoke("status", "--workspace", "deeper"),
            invoke("--workspace", str(root / "local" / "gone"), "status")]
    for told in said:
        assert told.exit_code == 1 and "is not a workspace" in told.stderr, told.output
        assert "cerebro init" not in told.stderr, told.stderr
        assert told.stderr.rstrip().endswith("run in the workspace's folder, or name it with --workspace")


def test_trust_given_two_folders_refuses_as_init_and_forget_do(tmp_path: Path) -> None:
    from cerebro import scaffold

    a, b = scaffold.init(tmp_path / "a").root, scaffold.init(tmp_path / "b").root
    for line in (["--workspace", str(a), "trust", str(b)], ["trust", str(b), "--workspace", str(a)]):
        told = invoke(*line)
        assert told.exit_code == 1, told.output
        assert told.stderr == f"Error: trust was given two folders, {b} and --workspace {a}: give one\n"
    for command in ("init", "forget"):
        told = invoke("--workspace", str(a), command, str(b))
        assert told.stderr == f"Error: {command} was given two folders, {b} and --workspace {a}: give one\n"
    same = invoke("--workspace", str(a), "trust", str(a))
    assert same.exit_code == 0 and same.stdout.rstrip().splitlines()[-1] == f"trusted   {a}"


# -- cerebro completion <shell>

@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_prints_the_script_for_a_shell(shell: str) -> None:
    told = invoke("completion", shell)
    assert told.exit_code == 0, told.output
    assert "_CEREBRO_COMPLETE" in told.stdout and "cerebro" in told.stdout


def test_completion_for_a_shell_it_does_not_cover_is_refused_with_exit_2() -> None:
    told = invoke("completion", "powershell")
    assert told.exit_code == 2 and told.stdout == "" and "No such command 'powershell'" in told.stderr


def test_completion_with_no_shell_prints_its_help_and_exits_0() -> None:
    told = invoke("completion")
    assert told.exit_code == 0 and told.stderr == ""
    assert told.stdout == invoke("completion", "--help").stdout
    for shell in ("bash", "zsh", "fish"):
        assert shell in told.stdout


@pytest.mark.parametrize(("shell", "loads"), [
    ("bash", "source <(cerebro completion bash)"), ("zsh", "source <(cerebro completion zsh)"),
    ("fish", "cerebro completion fish | source")])
def test_each_shells_help_says_how_to_load_its_script(shell: str, loads: str) -> None:
    told = invoke("completion", shell, "-h")
    assert told.exit_code == 0 and loads in told.stdout, told.stdout
    assert "Open a new shell to use it." in told.stdout
    assert "--no-descriptions" in told.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_no_descriptions_prints_a_script_whose_completions_carry_no_description(shell: str, tmp_path: Path) -> None:
    told = invoke("completion", shell, "--no-descriptions")
    assert told.exit_code == 0, told.output
    assert "_CEREBRO_COMPLETE" in told.stdout
    instruction = re.search(r"_CEREBRO_COMPLETE=(\w+)", told.stdout)
    assert instruction is not None
    env = {"_CEREBRO_COMPLETE": instruction.group(1), "COMP_WORDS": "cerebro agents ",
           "COMP_CWORD": "2" if shell != "fish" else ""}
    bare = CliRunner().invoke(main, [], prog_name="cerebro", env=env)
    described = CliRunner().invoke(main, [], prog_name="cerebro", env={**env, "_CEREBRO_COMPLETE": f"{shell}_complete"})
    assert bare.exit_code == 0 and "list" in bare.stdout, bare.output
    words = ("agents in this workspace", "Scaffold")
    assert not any(word in bare.stdout for word in words), bare.stdout
    if shell != "bash":  # bash's script shows no description either way
        assert any(word in described.stdout for word in words), described.stdout


def _shell_session(shell: str, road: str, script: str, home: Path) -> str:
    """The rc a new interactive session of `shell` starts from, with `script` loaded by `road`, as the shell's
    `cerebro completion <shell> -h` says: "file" is the for-every-new-session line, "source" the current-session one."""
    path = home / "script"
    path.write_text(script)
    if shell == "zsh":
        loads = (f"fpath=({home / 'zf'} $fpath)\n" if road == "file" else "") + "autoload -U compinit; compinit\n"
        if road == "file":
            (home / "zf").mkdir()
            path.rename(home / "zf" / "_cerebro")
        else:
            loads += f"source {path}\n"
        (home / ".zshrc").write_text("bindkey -e\nPS1='$ '\n" + loads)
        return "zsh"
    if shell == "bash":
        loads = f"source {path}\n"
        if road == "file":  # the bash-completion package reads /etc/bash_completion.d, here a folder of the test's
            (home / "compat").mkdir()
            path.rename(home / "compat" / "cerebro")
            loads = f"BASH_COMPLETION_COMPAT_DIR={home / 'compat'}\n. {BASH_COMPLETION}\n"
        (home / ".bashrc").write_text("PS1='$ '\n" + loads)
        return "bash"
    config = home / ".config" / "fish"
    (config / "completions").mkdir(parents=True)
    loads = f"source {path}\n"
    if road == "file":
        path.rename(config / "completions" / "cerebro.fish")
        loads = ""
    (config / "config.fish").write_text("function fish_prompt; echo -n '$ '; end\nset -g fish_greeting\n" + loads)
    return "fish"


BASH_COMPLETION = "/usr/share/bash-completion/bash_completion"
_ARGV = {"zsh": ["zsh", "-d", "-i"], "bash": ["bash", "--rcfile", ".bashrc", "-i"], "fish": ["fish", "-i"]}


def _three_tabs(shell: str, road: str, script: str, home: Path) -> list[str]:
    """What the line reads after a real Tab on `cerebro sta`, three times in one new session of `shell` on a
    pseudo-terminal, with the completion script loaded by `road`."""
    _shell_session(shell, road, script, home)
    bin_ = home / "bin"
    bin_.mkdir()
    (bin_ / "cerebro").write_text(f"#!/bin/sh\nexec {sys.executable} -c 'import sys; from cerebro.cli import main; "
                                  "main(prog_name=\"cerebro\")' \"$@\"\n")
    (bin_ / "cerebro").chmod(0o755)
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(home), "ZDOTDIR": str(home), "TERM": "xterm",
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"), "LANG": "C.UTF-8"}
    leader, follower = pty.openpty()
    fcntl.ioctl(follower, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 200, 0, 0))
    takes_terminal = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
    argv = [shutil.which(shell) or shell, *_ARGV[shell][1:]]  # the shell `cerebro completion` checks, by its path
    child = subprocess.Popen([sys.executable, "-c", takes_terminal, *argv], stdin=follower, stdout=follower,
                             stderr=follower, env=env, cwd=home, start_new_session=True)
    os.close(follower)
    got = b""

    def read(until: re.Pattern[bytes] | None = None, quiet: float = 0.3, most: float = 5.0) -> None:
        nonlocal got
        end, last = time.monotonic() + most, time.monotonic()
        while time.monotonic() < end:
            if select.select([leader], [], [], 0.05)[0]:
                chunk = os.read(leader, 65536)
                got, last = got + chunk, time.monotonic()
                if b"\x1b[6n" in chunk:  # fish asks where the cursor is, as a terminal answers
                    os.write(leader, b"\x1b[1;1R")
                if b"\x1b[c" in chunk:
                    os.write(leader, b"\x1b[?62;c")
                if until is not None and until.search(got):
                    return
            elif until is None and time.monotonic() - last > quiet:
                return

    lines = []
    try:
        read(quiet=1.5)
        for n in range(3):  # what was typed after Tab waits until the completion ends, as typed-ahead keys do
            os.write(leader, b"cerebro sta\t")
            read(quiet=1.0)
            os.write(leader, f'\x01echo TAB{n}""SAW:\r'.encode())  # Ctrl-A, then echo the line as Tab left it
            said = re.compile(rf"TAB{n}SAW:([^\r\n]*)\r?\n".encode())
            read(until=said)
            found = said.search(got)
            lines.append(found.group(1).decode().strip() if found else f"nothing came back: {got[-300:]!r}")
    finally:
        # the leader is closed before the wait: on darwin a session leader that ends waits for what it wrote to its
        # terminal to be read, so a shell killed with its last prompt unread never ends while the leader stays open
        child.kill()
        os.close(leader)
        child.wait(10)
    return lines


OLD_BASH = "Shell completion is not supported for Bash versions older than 4.4."


def _bash_before_4_4() -> bool:
    """Whether the bash `cerebro completion bash` checks is older than 4.4, whose `complete` has no `-o nosort`."""
    said = subprocess.run([shutil.which("bash") or "bash", "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'],
                          capture_output=True, text=True, check=True).stdout.split()
    return (int(said[0]), int(said[1])) < (4, 4)


# bash's script is one with or without descriptions, so bash is tried once by each road.
TABS = [(shell, road, bare) for shell in ("zsh", "bash", "fish") for road in ("file", "source")
        for bare in ((False,) if shell == "bash" else (False, True))]


@pytest.mark.timeout(30)
@pytest.mark.parametrize(("shell", "road", "bare"), TABS,
                         ids=[f"{s}-{r}-{'no-descriptions' if b else 'descriptions'}" for s, r, b in TABS])
def test_every_tab_completes_in_a_new_session_by_each_road_the_help_gives(
        shell: str, road: str, bare: bool, tmp_path: Path) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed here, so no real {shell} can press Tab")
    if shell == "bash" and road == "file" and not Path(BASH_COMPLETION).is_file():
        pytest.skip("the bash-completion package, which loads /etc/bash_completion.d, is not installed here")
    made = invoke("completion", shell, *(["--no-descriptions"] if bare else []))
    if shell == "bash" and _bash_before_4_4():  # as the help says, the script needs bash 4.4 or later
        assert OLD_BASH in made.stderr, made.stderr  # where it is older, the script comes with this warning
        assert _three_tabs(shell, road, made.stdout, tmp_path) == ["cerebro sta"] * 3  # and Tab completes nothing
        return
    assert OLD_BASH not in made.stderr
    assert _three_tabs(shell, road, made.stdout, tmp_path) == ["cerebro status"] * 3


# -- --plain prints no escape byte on a colour terminal, for every command

CHILD = """
import fcntl, os, sys, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
from pathlib import Path
from cerebro.run import block_model_requests
block_model_requests()
import cerebro.secrets, cerebro.clock, cerebro.app, cerebro.trust
class _NoKeychain:
    priority = 0
cerebro.secrets._default_backend = _NoKeychain
cerebro.clock.local_zone = lambda *a, **k: None
cerebro.app.FactoidExtraction = lambda *a, **k: None
base = Path(sys.argv[1]).resolve()
cerebro.trust.trusted = lambda root, *a, **k: Path(root).resolve().is_relative_to(base)
from cerebro import scaffold
from cerebro.cli import main
for n, line in enumerate(sys.argv[2:]):
    here = base / f"c{n}"
    if line.split()[0] == "init":
        here.mkdir()
    else:
        scaffold.init(here)  # a workspace of its own, trusted, so the command runs past the folder it is given
    os.chdir(here)
    print(f"=== {line}", flush=True)
    try:
        main([*line.split(), "--plain"], prog_name="cerebro")
    except SystemExit:
        pass
    except BaseException as error:
        print(f"raised {type(error).__name__}", flush=True)
print("=== end", flush=True)
"""


def test_plain_prints_no_escape_byte_on_a_colour_terminal_for_any_command(tmp_path: Path) -> None:
    leader, follower = pty.openpty()
    fcntl.ioctl(follower, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    lines = [" ".join(path) for path in _leaves() if path not in (("serve",), ("mcp",))]  # no server is started
    lines += ["serve --once"]
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(tmp_path), *lines], stdin=follower, stdout=follower,
                             stderr=follower, start_new_session=True,
                             env={"PATH": os.environ.get("PATH", ""), "TERM": "xterm-256color", "COLORTERM": "truecolor",
                                  "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")})
    os.close(follower)
    got = b""
    end = time.monotonic() + 60
    try:
        while time.monotonic() < end and b"=== end" not in got:
            ready, _, _ = select.select([leader], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(leader, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                got += chunk
            else:
                os.write(leader, b"\x04")  # a question it asks ends, as at a person's Ctrl-D
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        os.close(leader)
    text = got.decode("utf-8", "replace")
    (tmp_path / "seen.txt").write_text(text)
    assert "=== end" in text, text[-2000:]
    parts = text.split("=== ")[1:]
    assert [part for part in parts if "\x1b" in part] == []
    assert [part for part in parts if "No such option" in part or "raised " in part] == []  # each took --plain


def test_the_lines_a_person_reads_say_no_section_brain_or_page() -> None:
    import re

    from cerebro import cli

    said = [cli._or_init(f"x {cli.NOT_A_WORKSPACE}", Path("/nowhere/x")),
            *(invoke(*path, "--help").stdout for path in [(), ("help",), ("completion",), *(
                ("completion", shell) for shell in ("bash", "zsh", "fish"))]),
            invoke("trust", "/nowhere/a", "--workspace", "/nowhere/b").stderr]
    for line in said:
        assert not re.search(r"\b(sections?|brains?|pages?)\b", line, re.IGNORECASE), line
