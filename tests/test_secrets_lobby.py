"""`cerebro secrets` alone shows the model keys: the keys lobby init's walk shows at a terminal
that draws, a table and a line of names at one that does not, and off a terminal the table and exit 2. `secrets set`,
`list` and `unset` print their recorded output, byte for byte. A key is proven with one small call before it is
kept: in every test that call is a stand-in, and the app's own proof is refused under a daily cap for a model with no
price before any call. A key is never shown: each test types a stand-in value and searches for it in everything
printed. Every store is the file store under a scratch folder, never the keychain."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from secrets_cases import CASES, STAND_IN, record
from test_output import on_a_pty

from cerebro import cli, keys
from cerebro.app import Cerebro
from cerebro.errors import CerebroError
from cerebro.models import DEFAULTS, ModelSpec

RECORDED = Path(__file__).parent / "data" / "secrets"
ESC = b"\x1b"


@pytest.fixture(autouse=True)
def scratch_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file store under the test's own folder, and no key in the environment."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    for name in ("FIREWORKS_API_KEY", "TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


class Proofs:
    """A key's proof call, standing in: it keeps the provider, the job and the model it was asked on, and fails with
    `why`, quoting the key as a careless provider's error might, when `why` is set. It never sees a provider."""

    def __init__(self, why: str = "") -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.why = why

    def __call__(self, row: Any, value: str) -> None:
        job, spec = row.jobs[0]
        self.calls.append((row.provider, job, spec.model))
        if self.why:
            raise CerebroError(f"{self.why} (the key was {value})")


def made(tmp_path: Path) -> Path:
    root = (tmp_path / "wiki").resolve()
    got = CliRunner().invoke(cli.main, ["init", str(root), "--timezone", "UTC", "--daily-usd", "5"])
    assert got.exit_code == 0, got.output
    return root


def kept(root: Path) -> list[str]:
    with Cerebro(root) as workspace:
        assert workspace.secrets is not None
        return workspace.secrets.names()


# ---- secrets set, list and unset, byte for byte ---------------------------------------------------------------------


def test_secrets_set_list_and_unset_print_the_recorded_output_byte_for_byte(tmp_path: Path) -> None:
    got = record(tmp_path)
    assert list(got) == [name for name, _, _ in CASES]
    for name, text in got.items():
        assert text == (RECORDED / f"{name}.txt").read_text(), name
    assert all(STAND_IN not in text for text in got.values())


# ---- the rows ---------------------------------------------------------------------------------------------------------


def test_one_row_a_provider_in_job_order_a_provider_serving_two_jobs_on_one_row() -> None:
    rows = keys.key_rows(DEFAULTS, ["fireworks"], {"TYPESAFE_API_KEY": "set"})
    assert [(r.provider, r.job_words(), r.state) for r in rows] == [
        ("fireworks", "extraction and compilation", "connected"),
        ("typesafe", "extraction and decision", "in the environment")]  # the facts layer decides with it to extract
    assert rows[0].jobs[0] == ("extraction", DEFAULTS["extraction"])
    models = {**DEFAULTS, "compilation": ModelSpec("anthropic", "claude-sonnet-5-5", "", "compilation")}
    rows = keys.key_rows(models, [], {"TYPESAFE_API_KEY": "  "})
    assert [(r.title, r.state, r.job_words()) for r in rows] == [
        ("Fireworks", "not added", "extraction"), ("Typesafe", "not added", "extraction and decision"),
        ("Anthropic", "not added", "compilation")]
    assert keys.summary(rows) == ("0 of 3 model keys set", "cerebro secrets set fireworks")


# ---- off a terminal and outside a workspace ---------------------------------------------------------------------------


def test_off_a_terminal_secrets_alone_prints_the_table_asks_nothing_and_exits_2(tmp_path: Path) -> None:
    root = made(tmp_path)
    got = CliRunner().invoke(cli.main, ["secrets", "--workspace", str(root)], input=STAND_IN + "\n")
    assert got.exit_code == 2
    assert got.output == (
        "Fireworks  [x] not added  extraction and compilation\n"
        "Typesafe   [x] not added  extraction and decision\n"
        "Error: secrets alone asks for the model keys at a terminal: set one with cerebro secrets set <provider>, for "
        "fireworks, typesafe\n")
    assert kept(root) == []


def test_outside_a_workspace_secrets_alone_prints_its_help_and_ends_there(tmp_path: Path) -> None:
    got = CliRunner().invoke(cli.main, ["secrets", "--workspace", str(tmp_path)])
    assert got.exit_code == 0 and got.output.startswith("Usage: ") and "Commands:" in got.output
    assert "Done." not in got.output


# ---- at a terminal that draws nothing ---------------------------------------------------------------------------------


def plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: str, proofs: Proofs) -> tuple[Any, Path]:
    root = made(tmp_path)
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(cli, "_prove_on", lambda where: proofs)
    return CliRunner().invoke(cli.main, ["secrets", "--workspace", str(root)], input=answers), root


def test_at_a_plain_terminal_it_asks_for_names_then_each_key_hidden_proves_it_and_keeps_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proofs = Proofs()
    got, root = plain(tmp_path, monkeypatch, f"nope\nFireworks\n{STAND_IN}\n", proofs)
    assert got.exit_code == 0, got.output
    assert got.output.startswith("Fireworks  [x] not added  extraction and compilation\n"
                                 "Typesafe   [x] not added  extraction and decision\n"
                                 "Set which? Names, with commas [Enter to skip]: nope\n"
                                 "          nope is not one of fireworks, typesafe\n"
                                 "Set which? Names, with commas [Enter to skip]: Fireworks\n"), got.output
    assert ("\nFireworks\nMake a key in the API keys settings of the Fireworks console and paste it below.\n"
            "Pasted contents are not shown and are stored in a file only your user can read.\n"
            "Fireworks API key [Enter to skip]: \n"
            "[ok] Fireworks connection verified, key added to secrets store\n"
            "1 of 2 model keys set\n"
            "[!] Done with warnings. Check: no Typesafe key yet for extraction and decision: run cerebro secrets set "
            "typesafe\n") in got.output
    assert proofs.calls == [("fireworks", "extraction", "glm-5p3-flash")]
    assert kept(root) == ["fireworks"] and STAND_IN not in got.output


def test_a_key_that_does_not_verify_is_not_kept_its_reason_is_said_without_the_key_and_left_to_check(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got, root = plain(tmp_path, monkeypatch, f"typesafe\n{STAND_IN}\n", Proofs("401 unauthorized"))
    assert got.exit_code == 0, got.output
    assert "[x] Typesafe did not verify: 401 unauthorized (the key was ****)\n" in got.output
    assert "0 of 2 model keys set" in got.output
    assert got.output.rstrip().endswith("Check: no Fireworks key yet for extraction and compilation: run cerebro "
                                        "secrets set fireworks (and 1 more above)")
    assert kept(root) == [] and STAND_IN not in got.output


def test_naming_nothing_or_typing_no_key_keeps_nothing_and_says_what_to_set(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proofs = Proofs()
    got, root = plain(tmp_path, monkeypatch, "\n", proofs)
    assert got.exit_code == 0 and "0 of 2 model keys set" in got.output and proofs.calls == []
    assert got.output.rstrip().endswith("Check: no Fireworks key yet for extraction and compilation: run cerebro "
                                        "secrets set fireworks (and 1 more above)")
    got = CliRunner().invoke(cli.main, ["secrets", "--workspace", str(root)], input="fireworks\n\n")
    assert got.exit_code == 0 and proofs.calls == [] and kept(root) == []


# ---- the lobby, as init's walk shows it --------------------------------------------------------------------------------


class Script:
    """The lobby's screen, standing in: each menu answers the next of `answers`, a line the next text, and what is
    said is kept."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers, self.said, self.menus, self.asked = list(answers), [], [], []

    def menu(self, menu: Any, cursor: int = 0) -> int | None:
        self.menus.append((list(menu.head), [(r.title, r.state, r.reason) for r in menu.rows], cursor))
        return self.answers.pop(0)

    def text(self, prompt: str, default: str = "", hidden: bool = False, source: str = "") -> str | None:
        self.asked.append((prompt, hidden))
        return self.answers.pop(0)

    def say(self, line: str) -> None:
        self.said.append(line)


def test_the_lobby_opens_a_provider_proves_its_key_keeps_it_and_comes_back_with_the_row_read_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, proofs = made(tmp_path), Proofs()
    monkeypatch.setattr(cli, "_prove_on", lambda where: proofs)
    screen = Script([1, STAND_IN, 1, "", "\x1b", None])  # Typesafe's key typed; opened again, Enter on nothing is
    # ignored and asked again, then Escape and Enter leave it; Done
    session = cli._key_session(root, screen)
    session.lobby(keys.head())
    first, second, third = screen.menus
    assert first[1] == [("Fireworks", "not added", "extraction and compilation"),
                        ("Typesafe", "not added", "extraction and decision")]
    assert second[0][-1] == "[ok] Typesafe connection verified, key added to secrets store"
    assert second[1][1] == ("Typesafe", "connected", "extraction and decision") and second[2] == 1
    assert third[0][-1] == second[0][-1]  # a view left with nothing kept keeps the line
    assert screen.asked == [("Typesafe API key", True)] * 3
    assert proofs.calls == [("typesafe", "extraction", "jev-1.13.0")]
    assert kept(root) == ["typesafe"] and not any(STAND_IN in line for line in screen.said)
    assert session.checks() == [("no Fireworks key yet for extraction and compilation: run cerebro secrets set "
                                "fireworks")]


def test_at_a_terminal_that_draws_secrets_alone_shows_the_lobby_and_ends_with_how_many_keys_are_set(
        tmp_path: Path) -> None:
    root = made(tmp_path)
    code = ("import cerebro.cli\n"
            "cerebro.cli._prove_on = lambda root: (lambda row, value: None)  # no provider is reached\n"
            f"cerebro.cli.main(['secrets', '--workspace', {str(root)!r}], prog_name='cerebro')\n")
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "COLORTERM": "truecolor"}
    got = on_a_pty(code, env, answer=b"\x1b]11;rgb:0000/0000/0000\x1b\\", seconds=30, keys=(
        (b" move", 2, b"\r"), (b"Fireworks API key", 1, STAND_IN.encode() + b"\r"),
        (b"connection verified", 2, ESC)))  # the legend's words are painted apart, so " move" marks a drawn menu
    text = got.decode("utf-8", "replace")
    assert b"\x1b[" in got and "Model keys" in text
    assert "Fireworks connection verified, key added to secrets store" in text
    assert "1 of 2 model keys set" in text and "secrets set typesafe" in text
    assert STAND_IN not in text and kept(root) == ["fireworks"]


def test_with_plain_on_a_colour_terminal_secrets_alone_sends_no_byte_but_its_plain_lines(tmp_path: Path) -> None:
    """A terminal that takes colour, with no NO_COLOR: --plain means plain lines, so nothing queries the terminal."""
    root = made(tmp_path)
    code = ("import cerebro.cli\n"
            "cerebro.cli._prove_on = lambda root: (lambda row, value: None)  # no provider is reached\n"
            f"cerebro.cli.main(['secrets', '--plain', '--workspace', {str(root)!r}], prog_name='cerebro')\n")
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "COLORTERM": "truecolor"}
    got = on_a_pty(code, env, seconds=30, keys=(
        (b"Set which?", 1, b"fireworks\r"), (b"Fireworks API key", 1, STAND_IN.encode() + b"\r")))
    text = got.decode("utf-8", "replace")
    assert b"\x1b" not in got  # no query, no style: not one escape byte
    assert "[ok] Fireworks connection verified, key added to secrets store" in text
    assert STAND_IN not in text and kept(root) == ["fireworks"]


# The proof call itself, its spend and the unpriced refusal, is tested in test_prove_key.py.


def test_the_lobbys_frames_in_a_light_and_a_dark_terminal_and_plain_are_the_saved_ones_and_fit_80_columns() -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parents[1] / "checks"))
    import secrets_demo

    from cerebro.output import strip_escapes

    data = Path(__file__).parent / "data" / "tui"
    light, dark, plain = secrets_demo.frames(False), secrets_demo.frames(True), secrets_demo.frames(None)
    assert light == (data / "secrets-light.ansi").read_text() and dark == (data / "secrets-dark.ansi").read_text()
    assert plain == (data / "secrets-plain.txt").read_text() and "\x1b" not in plain
    assert light != dark and strip_escapes(light) == strip_escapes(dark)
    assert all(len(line) <= 80 and not line.endswith("…") for line in strip_escapes(dark).splitlines())


@pytest.mark.untrusted
def test_on_a_workspace_its_owner_has_not_trusted_secrets_alone_is_refused_as_secrets_list_is(tmp_path: Path) -> None:
    from cerebro import trust

    root = made(tmp_path)
    trust.untrust(str(root))
    alone = CliRunner().invoke(cli.main, ["secrets", "--workspace", str(root)], input="")
    listed = CliRunner().invoke(cli.main, ["secrets", "list", "--workspace", str(root)])
    assert alone.exit_code == listed.exit_code == 1
    assert "is not a workspace you trust" in alone.output and "Fireworks" not in alone.output


def test_j_moves_in_the_lobby_and_a_key_typed_after_keeps_every_j_and_k(tmp_path: Path) -> None:
    """j moves the lobby's cursor as the down arrow does, and once a provider is open the prompt is text: a typed j
    or k is part of the key, never a move."""
    root = made(tmp_path)
    typed = "stand-in-jkkj-key"
    code = ("import cerebro.cli\n"
            "cerebro.cli._prove_on = lambda root: (lambda row, value: None)  # no provider is reached\n"
            f"cerebro.cli.main(['secrets', '--workspace', {str(root)!r}], prog_name='cerebro')\n")
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "COLORTERM": "truecolor"}
    got = on_a_pty(code, env, answer=b"\x1b]11;rgb:0000/0000/0000\x1b\\", seconds=30, keys=(
        (b" move", 2, b"j"), (b" move", 3, b"\r"), (b"Typesafe API key", 1, typed.encode() + b"\r"),
        (b"connection verified", 2, ESC)))
    assert typed not in got.decode("utf-8", "replace") and kept(root) == ["typesafe"]
    with Cerebro(root) as workspace:
        assert workspace.secrets is not None and workspace.secrets.get("typesafe") == typed


# ---- --workspace on the group, Ctrl-C, values never echoed, a provider's name in any case ----------------


def made_at(tmp_path: Path, name: str) -> Path:
    root = (tmp_path / name).resolve()
    got = CliRunner().invoke(cli.main, ["init", str(root), "--timezone", "UTC", "--no-cap"])
    assert got.exit_code == 0, got.output
    return root


@pytest.mark.parametrize("args", [
    ["secrets", "--workspace", "{a}", "{cmd}"],
    ["secrets", "{cmd}", "--workspace", "{a}"],
    ["secrets", "--workspace", "{b}", "{cmd}", "--workspace", "{a}"]],
    ids=["named on the group", "named on the command", "the command's wins"])
def test_the_workspace_named_on_the_group_or_the_command_is_the_one_set_list_and_unset_act_on(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    """Run from inside workspace B, each subcommand acts on the workspace --workspace names, wherever it is given;
    named twice, the command's own wins, as the last flag does."""
    a, b = made_at(tmp_path, "wiki-a"), made_at(tmp_path, "wiki-b")
    monkeypatch.chdir(b)

    def run(cmd: list[str], given: str | None = None) -> Any:
        words = [w.replace("{a}", str(a)).replace("{b}", str(b)) for w in args]
        at = words.index("{cmd}")
        got = CliRunner().invoke(cli.main, [*words[:at], *cmd, *words[at + 1:]], input=given)
        assert got.exit_code == 0, got.output
        return got

    run(["set", "fireworks"], "made-up-value-a\n")
    assert kept(a) == ["fireworks"] and kept(b) == []
    assert "secret   fireworks" in run(["list"]).output
    CliRunner().invoke(cli.main, ["secrets", "set", "typesafe", "--workspace", str(b)], input="made-up-value-b\n")
    run(["unset", "fireworks"])
    assert kept(a) == [] and kept(b) == ["typesafe"]


def interrupting(monkeypatch: pytest.MonkeyPatch, at: int) -> None:
    """Ctrl-C at the `at`th line Click asks, as the terminal sends it: a KeyboardInterrupt where the line is read."""
    import click.termui

    asked = [0]
    real = click.termui._readline_prompt

    def prompt(read: Any, text: str, err: bool) -> str:
        asked[0] += 1
        if asked[0] == at:
            raise KeyboardInterrupt
        return real(read, text, err)

    monkeypatch.setattr(click.termui, "_readline_prompt", prompt)


@pytest.mark.parametrize(("at", "answers"), [(1, ""), (2, "fireworks,typesafe\n"), (3, "nope\nfireworks\n")],
                         ids=["at Set which", "at the first key", "after a name refused"])
def test_ctrl_c_at_a_plain_prompt_ends_secrets_with_130_and_keeps_nothing_typed_there(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at: int, answers: str) -> None:
    """Ctrl-C at the line of names or at a key ends the command, exit 130, with no summary and
    no next key asked; nothing typed at that prompt is kept."""
    proofs = Proofs()
    interrupting(monkeypatch, at)
    got, root = plain(tmp_path, monkeypatch, answers + "made-up-typesafe-key\n", proofs)
    assert got.exit_code == 130, got.output
    assert "model keys set" not in got.output and "Typesafe API key" not in got.output
    assert kept(root) == [] and proofs.calls == []


VALUE = "zq-madeup-7Kd93LpQ2xvW"


def runs_of(value: str, text: str, length: int = 6) -> list[str]:
    return [value[i:i + length] for i in range(len(value) - length + 1) if value[i:i + length] in text]


@pytest.mark.parametrize("args", [
    ["secrets", "set", f"fireworks={VALUE}"],
    ["secrets", "set", f"fireworks {VALUE}"],
    ["secrets", "set", "fireworks", VALUE],
    ["secrets", "set", f"{VALUE}!"],
    ["secrets", "unset", f"fireworks={VALUE}"],
    ["secrets", "unset", f"fireworks {VALUE}"],
    ["secrets", "unset", "fireworks", VALUE],
    ["secrets", "list", VALUE],
    ["secrets", "sett", "fireworks", VALUE],
    ["secrets", f"fireworks={VALUE}"],
    ["secrets", VALUE],
    ["secrets", "set", "fireworks", "--", f"-{VALUE}"],
    ["secrets", "unset", "fireworks", "--", f"--{VALUE}"],
    ["secrets", "unset", "fireworks", "--", f"--sk={VALUE}"],
    ["secrets", "list", "--", f"-{VALUE}"],
    ["secrets", "--", f"-{VALUE}"],
    ["secrets", "set", "fireworks", f"--{VALUE}"],
    ["secrets", "set", f"--{VALUE}=x", "fireworks"],
    ["secrets", "list", f"-{VALUE}"],
    ["secrets", f"--{VALUE}"],
    ["secrets", f"--plain={VALUE}"],
    ["secrets", "set", "fireworks", "--workspace"]],
    ids=["set name=value", "set a name and a value in one word", "set a value as a second word", "set a bad name",
         "unset name=value", "unset a name and a value in one word", "unset a value as a second word",
         "list a value", "a mistyped command then a value", "name=value as the command", "a value as the command",
         "set a dash value after --", "unset a double dash value after --", "unset a dash value with = after --",
         "list a dash value after --", "a dash value as the command after --", "set an unknown option",
         "set an unknown option with a value", "list an unknown short option", "an unknown option on the group",
         "a value given to a flag", "an option missing its value"])
def test_no_secrets_command_prints_anything_of_a_value_typed_on_its_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    """`secrets set takes the name alone, so what follows = is not shown`: whatever a person may
    have pasted as a value on the command line, no run of six of its characters is printed, and nothing is kept."""
    root = made(tmp_path)
    monkeypatch.chdir(root)
    for given in ("made-up-prompt-answer\n", ""):  # a value piped in, and none
        got = CliRunner().invoke(cli.main, args, input=given)
        assert got.exit_code != 0, got.output
        assert runs_of(VALUE, got.output) == [], got.output
        assert kept(root) == []


def test_a_usage_error_that_holds_no_pasted_word_keeps_its_own_true_reason(tmp_path: Path) -> None:
    """A usage error says `takes one name` only where a word was typed that the command does not take: a file given
    to --workspace after the command is refused with its own true reason, exit 1."""
    root = made(tmp_path)
    notes = tmp_path / "notes.txt"
    notes.write_text("made-up\n")
    for args in (["set", "fireworks"], ["unset", "fireworks"], ["list"]):
        got = CliRunner().invoke(cli.main, ["secrets", *args, "--workspace", str(notes)], input="made-up\n")
        assert got.exit_code == 1, got.output
        assert "takes" not in got.output and "a file, not a folder" in got.output, got.output
    assert kept(root) == []


def test_set_name_equals_value_is_a_usage_error_with_exit_2(tmp_path: Path) -> None:
    """secrets set gives a usage error there, exit 2."""
    root = made(tmp_path)
    got = CliRunner().invoke(cli.main, ["secrets", "set", f"fireworks={VALUE}", "--workspace", str(root)])
    assert got.exit_code == 2 and "secrets set takes the name alone" in got.output, got.output
    assert runs_of(VALUE, got.output) == []


def test_secrets_given_a_folder_that_is_not_there_says_so_and_exits_1(tmp_path: Path) -> None:
    got = CliRunner().invoke(cli.main, ["secrets", "--workspace", str(tmp_path / "nowhere")])
    assert got.exit_code == 1 and "no such folder" in got.output and "Usage:" not in got.output, got.output


@pytest.mark.parametrize("typed", ["\x1b\n", "made-\x1bup\n", "made-\x01up\n"],
                         ids=["Escape then Enter", "Escape inside a key", "a control character in a key"])
def test_secrets_set_never_keeps_a_key_that_is_an_escape_or_holds_a_control_character(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed: str) -> None:
    """At secrets set's hidden prompt, Escape then Enter is no key: it is cancelled with nothing
    stored, and a good key kept before stays."""
    root = made(tmp_path)
    good = CliRunner().invoke(cli.main, ["secrets", "set", "fireworks", "--workspace", str(root)],
                              input="made-up-good-key\n")
    assert good.exit_code == 0, good.output
    monkeypatch.setattr(cli, "_asking", lambda: True)
    got = CliRunner().invoke(cli.main, ["secrets", "set", "fireworks", "--workspace", str(root)], input=typed)
    assert got.exit_code == 1 and "nothing stored" in got.output and "kept" not in got.output, got.output
    with Cerebro(root) as workspace:
        assert workspace.secrets is not None and workspace.secrets.get("fireworks") == "made-up-good-key"


def test_the_lobbys_key_prompt_never_keeps_a_key_that_holds_a_control_character(tmp_path: Path) -> None:
    root = made(tmp_path)
    proofs = Proofs()

    class Typed:
        def __init__(self) -> None:
            self.asked = ["made-\x01up"]

        def say(self, line: str) -> None:
            pass

        def text(self, prompt: str, default: str = "", hidden: bool = False, source: str = "") -> str | None:
            return self.asked.pop(0) if self.asked else None

    session = cli._key_session(root, Typed())
    session.prove = proofs
    row = session.read()[0]
    said = session.open(row)
    assert "control character" in said and "made" not in said and proofs.calls == [] and kept(root) == []


SLOW = ("import time, cerebro.cli\n"
        "def prove(row, value):\n"
        "    print('PROVING', flush=True)\n"
        "    time.sleep(20)\n"
        "cerebro.cli._prove_on = lambda root: prove\n"
        "try:\n"
        "    cerebro.cli.main(ARGS, prog_name='cerebro')\n"
        "except SystemExit as stop:\n"
        "    print('EXITED', stop.code, flush=True)\n")


@pytest.mark.parametrize("plain", [False, True], ids=["drawn", "plain"])
def test_ctrl_c_while_a_key_is_proven_ends_the_lobby_and_keeps_nothing(tmp_path: Path, plain: bool) -> None:
    """Ctrl-C during the proof exits 130 in the drawn lobby; on plain lines
    the proof fails as cancelled, with its own line, the next key is asked, and the command ends with its summary,
    exit 0."""
    root = made(tmp_path)
    args = ["secrets", "--workspace", str(root), *(["--plain"] if plain else [])]
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "COLORTERM": "truecolor"}
    keys_ = (((b"Set which?", 1, b"fireworks,typesafe\r"),) if plain else ((b" move", 2, b"\r"),)) + (
        (b"Fireworks API key", 1, b"made-up-slow\r"), (b"PROVING", 1, b"\x03"), (b"Typesafe API key", 1, b"\r"))
    got = on_a_pty(f"ARGS = {args!r}\n" + SLOW, env, answer=b"\x1b]11;rgb:0000/0000/0000\x1b\\", seconds=30,
                   keys=keys_)
    text = got.decode("utf-8", "replace")
    assert ("EXITED 0" if plain else "EXITED 130") in text, text[-800:]
    assert "Aborted" not in text and kept(root) == []
    if plain:
        assert "Fireworks did not verify: cancelled by Ctrl-C" in text and "model keys set" in text, text[-800:]
        assert "Typesafe API key" in text, text[-800:]
    else:
        assert "model keys set" not in text


def test_a_providers_name_in_any_case_is_kept_under_its_id(tmp_path: Path) -> None:
    """`secrets set Fireworks` keeps the key under fireworks, where the jobs and the lobby look for it; any other name is kept as typed."""
    root = made(tmp_path)
    for name in ("Fireworks", "TYPESAFE", "LINEAR_API_KEY"):
        got = CliRunner().invoke(cli.main, ["secrets", "set", name, "--workspace", str(root)],
                                 input="made-up-value\n")
        assert got.exit_code == 0, got.output
    assert kept(root) == ["LINEAR_API_KEY", "fireworks", "typesafe"]
    assert keys.summary(keys.key_rows(DEFAULTS, kept(root), {}))[0] == "2 of 2 model keys set"


# ---- a provider's view: styled, and gone once it is answered ---------------------------------------------------------


ALT_ON, ALT_OFF = b"\x1b[?1049h", b"\x1b[?1049l"


def test_a_providers_view_is_styled_drawn_apart_and_gone_once_its_key_is_given(tmp_path: Path) -> None:
    """Each provider's view is drawn on a cleared
    alternate screen, its name a heading and its two lines muted with the keys page in the accent, and the
    main screen comes back once its key is given, so the lobby redraws alone and no view piles up above it."""
    root = made(tmp_path)
    code = ("import cerebro.cli\n"
            "cerebro.cli._prove_on = lambda root: (lambda row, value: None)  # no provider is reached\n"
            f"cerebro.cli.main(['secrets', '--workspace', {str(root)!r}], prog_name='cerebro')\n")
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "COLORTERM": "truecolor"}
    got = on_a_pty(code, env, answer=b"\x1b]11;rgb:0000/0000/0000\x1b\\", seconds=30, keys=(
        (b" move", 2, b"\r"), (b"Fireworks API key", 1, b"made-up-one\r", 0.5),
        (b"connection verified", 2, b"\x1b[B\r", 0.5), (b"Typesafe API key", 1, b"made-up-two\r", 0.5),
        (b"connection verified", 4, ESC, 0.5)))
    views = [at for at in range(len(got)) if got.startswith(b"paste it below", at)]
    assert len(views) == 2, got[-800:]
    on, off = got.find(ALT_ON), got.rfind(ALT_OFF)
    for at in views:  # the lobby and each view on the alternate screen, each view from a cleared screen
        assert on < got.rfind(b" move", 0, at) < at < off, got[max(0, at - 300):at + 50]
        assert b"\x1b[2J" in got[got.rfind(b" move", 0, at):at]  # cleared before it is drawn
        assert b" submit" in got[at:] and b" back" in got[at:]  # its prompt's legend
    view = got[got.rfind(b"\x1b[2J", 0, views[0]):views[0]]
    assert b"\x1b[1m" in view or b";1m" in view or b"\x1b[" in view.split(b"Fireworks")[0][-12:]  # styled heading
    assert b"API keys settings" in view and view.split(b"API keys settings")[0].endswith(b"m")  # in the accent
    assert got.rfind(ALT_ON) < got.rfind(ALT_OFF)  # the main screen is back at the end
    assert kept(root) == ["fireworks", "typesafe"]


QUIET_SLOW = ("import time, cerebro.cli\n"
              "from cerebro.errors import CerebroError\n"
              "def prove(row, value):\n"
              "    time.sleep(0.6)\n"
              "    if value.startswith('bad'):\n"
              "        raise CerebroError('the provider said no')\n"
              "cerebro.cli._prove_on = lambda root: prove\n")


@pytest.mark.parametrize(("key", "mark", "said"), [
    (b"made-up-good\r", "✓".encode(), b"Fireworks connection verified, key added to secrets store"),
    (b"bad-made-up\r", "✗".encode(), b"Fireworks did not verify: the provider said no")],
    ids=["verified", "refused"])
def test_while_a_key_is_proven_the_view_shows_a_verifying_step_then_its_mark(
        tmp_path: Path, key: bytes, mark: bytes, said: bytes) -> None:
    """`Verifying <provider> connection` with a spinner, two spaces
    in, while the proof runs, then the step ends with a mark for good or bad before the view leaves."""
    root = made(tmp_path)
    code = QUIET_SLOW + f"cerebro.cli.main(['secrets', '--workspace', {str(root)!r}], prog_name='cerebro')\n"
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "COLORTERM": "truecolor"}
    got = on_a_pty(code, env, answer=b"\x1b]11;rgb:0000/0000/0000\x1b\\", seconds=30, keys=(
        (b" move", 2, b"\r"), (b"Fireworks API key", 1, key, 0.5), (b" move", 3, ESC, 0.5)))
    entered = got.find(b"Fireworks API key")
    view = got[entered:got.find(b" move", entered)]  # the view, until the lobby draws again
    assert b"\r\x1b[K  " in view and "⠋".encode() in view, view[-600:]  # a spinner, two spaces in
    assert b"Verifying Fireworks connection" in view, view[-600:]
    resolved = view[view.rfind(b"Verifying Fireworks connection"):]
    assert mark in resolved and said in resolved, resolved
    assert b"made-up" not in got
