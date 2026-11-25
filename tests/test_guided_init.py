"""The guided init: what it asks and writes at a terminal that draws, the MCP clients' entries it writes against a
scratch home, and init off a terminal, with --plain, NO_COLOR or any option, exactly as its recording holds.

No test resolves the real home folder: `Path.home` raises in every test here, and the walk is given a scratch one.
No test runs claude or any client: Claude Code's add goes through a stand-in runner. No key is real."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from cerebro import cli, guided, mcp_clients, scaffold
from cerebro.connector import Ours, Record
from cerebro.errors import CerebroError
from cerebro.mcp_clients import Client, ClientError, Entry
from cerebro.models import ModelSpec, read_models
from cerebro.project import read_ours

sys.path.insert(0, str(Path(__file__).parent))
from test_output import on_a_pty

GOLDEN = Path(__file__).parent / "data" / "init"
ENTRY = Entry("cerebro-wiki", "/opt/stand-in/bin/cerebro", ("mcp", "--workspace", "/srv/stand-in/wiki"))


@pytest.fixture(autouse=True)
def no_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Path:
        raise AssertionError("a test asked for the real home folder")

    monkeypatch.setattr(Path, "home", classmethod(refuse))
    monkeypatch.setattr(cli, "_client_home", refuse)

    def no_proof(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a test reached the app's own key proof, which calls a provider")

    monkeypatch.setattr("cerebro.app.Cerebro.prove_key", no_proof)


class Proofs:
    """A key's proof call, standing in: it keeps the provider, the job and the model it was asked on, and fails with
    `why` when it is set. It never sees a provider."""

    def __init__(self, why: str = "") -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.why = why

    def __call__(self, row: Any, value: str) -> None:
        job, spec = row.jobs[0]
        self.calls.append((row.provider, job, spec.model))
        if self.why:
            raise CerebroError(f"{self.why} (the key was {value})")


# ---- init off a terminal, byte for byte -----------------------------------------------------------------------------


def said(text: str, root: Path) -> str:
    """Init's output with what changes from run to run in its place: the folder, the command and the repository."""
    text = text.replace(str(root), "<root>").replace(shutil.which("cerebro") or "cerebro", "<cerebro>")
    return re.sub(r"made inside the git repository at [^:]+:", "made inside the git repository at <repository>:",
                  text)


def in_a_repository(tmp_path: Path) -> Path:
    """A workspace folder inside a scratch repository, a folder holding `.git`, so init's notice about a repository
    prints wherever the temp folder lives, as it did when the output was recorded. No git runs."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    return (tmp_path / "repo" / "wiki").resolve()


NOTICE = re.compile(r"^notice    made inside the git repository at <repository>: .*\n", re.MULTILINE)


@pytest.mark.parametrize(("name", "args"), [
    ("budget-only", ["--daily-usd", "20"]), ("budget-only", ["--plain", "--daily-usd", "20"]),
    ("flags", ["--template", "gtm", "--daily-usd", "5", "--timezone", "UTC", "--domain", "quillmoor.example"]),
    ("no-cap", ["--no-cap"])])
def test_off_a_terminal_init_prints_exactly_what_its_recording_holds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, args: list[str]) -> None:
    monkeypatch.setattr("cerebro.clock.local_zone", lambda *a, **k: "Europe/Berlin")
    root = in_a_repository(tmp_path)
    got = CliRunner().invoke(cli.main, ["init", str(root), *args])
    assert f"exit {got.exit_code}\n" + said(got.output, root) == (GOLDEN / f"{name}.txt").read_text()
    assert not (root / "profile.md").exists()


def test_off_a_terminal_and_outside_any_repository_init_prints_the_same_with_no_repository_notice(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.clock.local_zone", lambda *a, **k: "Europe/Berlin")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # the search for a repository stops at the temp folder
    root = (tmp_path / "wiki").resolve()
    got = CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "20"])
    recorded = (GOLDEN / "budget-only.txt").read_text()
    assert NOTICE.search(recorded)
    assert f"exit {got.exit_code}\n" + said(got.output, root) == NOTICE.sub("", recorded)


@pytest.mark.parametrize("variables", [{"NO_COLOR": "1"}, {"TERM": "dumb"}])
def test_at_a_terminal_that_draws_nothing_init_asks_one_question_at_a_time_then_budget_and_zone(
        tmp_path: Path, variables: dict[str, str]) -> None:
    root = in_a_repository(tmp_path)
    code = ("import cerebro.clock\ncerebro.clock.local_zone = lambda *a, **k: 'Europe/Berlin'\n"
            f"from cerebro.cli import main\nmain(['init', {str(root)!r}], prog_name='cerebro')\n")
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", **variables}
    got = on_a_pty(code, env, seconds=30, keys=(
        (b"Your name", 1, b"Rowan Vale\r"), (b"Your work email", 1, b"rowan@quillmoor\r"),
        (b"Your work email", 2, b"rowan@quillmoor.example\r"), (b"Team or organization name", 1, b"\r"),
        (b"Team or organization web domain", 1, b"\r"), (b"Daily budget", 1, b"7\r"), (b"Time zone", 1, b"UTC\r")),
        size=(24, 500))  # wide enough that no line wraps: the recording holds each line whole
    assert b"\x1b" not in got
    text = said(got.decode().replace("\r\n", "\n"), root)
    assert text == (GOLDEN / "plain-terminal.txt").read_text()
    assert read_ours(root) == Ours(("rowan@quillmoor.example",), ("quillmoor.example",))
    assert front(root / "workspace.md")["identity"]["name"] == "Quillmoor"


@pytest.mark.parametrize("args", [["--plain"], ["--daily-usd", "7"]])
def test_on_a_colour_terminal_an_option_or_plain_sends_no_byte_to_the_terminal_and_asks_on_plain_lines(
        tmp_path: Path, args: list[str]) -> None:
    """A terminal that takes colour, with no NO_COLOR: an option, --plain among them, means plain lines, so init
    queries nothing of the terminal and prints exactly what the plain terminal's recording holds."""
    root = in_a_repository(tmp_path)
    code = ("import cerebro.clock\ncerebro.clock.local_zone = lambda *a, **k: 'Europe/Berlin'\n"
            f"from cerebro.cli import main\nmain(['init', {str(root)!r}, *{args!r}], prog_name='cerebro')\n")
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "COLORTERM": "truecolor", "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
    got = on_a_pty(code, env, seconds=30, keys=(
        (b"Your name", 1, b"Rowan Vale\r"), (b"Your work email", 1, b"rowan@quillmoor\r"),
        (b"Your work email", 2, b"rowan@quillmoor.example\r"), (b"Team or organization name", 1, b"\r"),
        (b"Team or organization web domain", 1, b"\r"), *(() if "--daily-usd" in args else ((b"Daily budget", 1, b"7\r"),)),
        (b"Time zone", 1, b"UTC\r")), size=(24, 500))  # wide enough that no line wraps
    assert b"\x1b" not in got  # no query, no style: not one escape byte
    want = (GOLDEN / "plain-terminal.txt").read_text()
    if "--daily-usd" in args:
        want = want.replace("Daily budget in dollars [50.0]: 7\n", "")
    assert said(got.decode().replace("\r\n", "\n"), root) == want


# ---- the MCP clients' table and their files -------------------------------------------------------------------------


def test_the_clients_table_given_a_scratch_home_names_no_path_outside_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for system in ("darwin", "linux", "win32"):
        listed = mcp_clients.clients(home, system, {})
        assert [c.app for c in listed][-3:] == ["Claude Code", "Codex", "Cursor"]
        assert all(c.path.is_relative_to(home) for c in listed), system
    assert next(c.app for c in mcp_clients.clients(home, "darwin", {})) == "Claude Desktop"
    moved = mcp_clients.clients(home, "linux", {"CLAUDE_CONFIG_DIR": str(home / "claude-config")})
    assert next(c.path for c in moved if c.app == "Claude Code") == home / "claude-config" / ".claude.json"


def desktop(home: Path) -> Client:
    return next(c for c in mcp_clients.clients(home, "darwin", {}) if c.app == "Claude Desktop")


def test_a_json_client_keeps_every_other_key_and_server_and_takes_ours_in_place(tmp_path: Path) -> None:
    client = desktop(tmp_path)
    client.path.parent.mkdir(parents=True)
    client.path.write_text(json.dumps({"theme": "dark", "mcpServers": {
        "other": {"command": "other-tool", "args": ["serve"]},
        "cerebro-wiki": {"command": "old", "args": []}}, "after": [1, 2]}))
    client.path.chmod(0o640)
    assert mcp_clients.holds(client, ENTRY)  # by its name, though it starts something else
    with pytest.raises(ClientError, match="holds cerebro-wiki already, starting old, so nothing was written"):
        mcp_clients.write(client, ENTRY)  # never replaced without the person's say
    mcp_clients.write(client, ENTRY, replace=True)
    data = json.loads(client.path.read_text())
    assert list(data) == ["theme", "mcpServers", "after"] and data["after"] == [1, 2]
    assert list(data["mcpServers"]) == ["other", "cerebro-wiki"]
    assert data["mcpServers"]["other"] == {"command": "other-tool", "args": ["serve"]}
    assert mcp_clients.found(client, ENTRY.name) == {"command": ENTRY.command, "args": list(ENTRY.args)}
    assert stat.S_IMODE(client.path.stat().st_mode) == 0o640
    assert sorted(p.name for p in client.path.parent.iterdir()) == [client.path.name]  # no partial file left


def test_a_new_json_file_is_made_and_a_link_written_through(tmp_path: Path) -> None:
    cursor = next(c for c in mcp_clients.clients(tmp_path, "linux", {}) if c.app == "Cursor")
    mcp_clients.write(cursor, ENTRY)
    assert json.loads(cursor.path.read_text()) == {"mcpServers": {"cerebro-wiki": {
        "command": ENTRY.command, "args": list(ENTRY.args)}}}
    assert stat.S_IMODE(cursor.path.stat().st_mode) == 0o600
    real = tmp_path / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text('{"mcpServers": {}}')
    cursor.path.unlink()
    cursor.path.symlink_to(real)
    mcp_clients.write(cursor, ENTRY)
    assert cursor.path.is_symlink() and mcp_clients.holds(cursor, ENTRY)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"mcpServers": [1]}'])
def test_a_json_file_that_does_not_read_is_refused_in_one_line_and_left_as_it_was(tmp_path: Path, text: str) -> None:
    client = desktop(tmp_path)
    client.path.parent.mkdir(parents=True)
    client.path.write_text(text)
    with pytest.raises(ClientError, match="nothing was written"):
        mcp_clients.write(client, ENTRY)
    assert client.path.read_text() == text


def codex(home: Path) -> Client:
    return next(c for c in mcp_clients.clients(home, "darwin", {}) if c.app == "Codex")


def test_codexs_file_keeps_every_other_line_and_comment_and_takes_our_table_in_place(tmp_path: Path) -> None:
    client = codex(tmp_path)
    client.path.parent.mkdir(parents=True)
    before = ('# my codex settings\nmodel = "o5"   # the one I use\n\n[mcp_servers.other]\ncommand = "other-tool"\n\n'
              '[mcp_servers.cerebro-wiki]\ncommand = "old"\nargs = []\n# a note under it\n\n[profiles.work]\n'
              'model = "o5-mini"\n')
    client.path.write_text(before)
    mcp_clients.write(client, ENTRY, replace=True)
    after = client.path.read_text()
    args = ", ".join(json.dumps(a) for a in ENTRY.args)
    assert after == before.replace('command = "old"\nargs = []', f'command = "{ENTRY.command}"\nargs = [{args}]')
    data = tomllib.loads(after)
    assert data["mcp_servers"]["cerebro-wiki"] == {"command": ENTRY.command, "args": list(ENTRY.args)}
    assert data["mcp_servers"]["other"] == {"command": "other-tool"} and data["profiles"]["work"]["model"] == "o5-mini"
    mcp_clients.write(client, ENTRY)
    assert client.path.read_text() == after  # written again, the same


def test_codexs_file_in_a_form_init_does_not_edit_or_that_does_not_read_is_refused(tmp_path: Path) -> None:
    client = codex(tmp_path)
    client.path.parent.mkdir(parents=True)
    for text, why in (('mcp_servers = { cerebro-wiki = { command = "x" } }\n', "form init does not edit"),
                      ("[mcp_servers\n", "does not read as TOML")):
        client.path.write_text(text)
        with pytest.raises(ClientError, match=why):
            mcp_clients.write(client, ENTRY, replace=True)
        assert client.path.read_text() == text


def test_codexs_file_keeps_the_persons_own_keys_subtables_and_the_comment_above_the_next_table(tmp_path: Path) -> None:
    """Keys the person added to Cerebro's own table, its env subtable, and a comment right above the next table all
    stay."""
    client = codex(tmp_path)
    client.path.parent.mkdir(parents=True)
    before = ('model = "o5"\n\n[mcp_servers.cerebro-wiki]\ncommand = "old"\nargs = []\nstartup_timeout_sec = 30\n\n'
              '[mcp_servers.cerebro-wiki.env]\nHTTPS_PROXY = "http://proxy.stand-in:3128"\n\n'
              '# --- my other servers: keep this note ---\n[mcp_servers.other]\ncommand = "other-tool"\n')
    client.path.write_text(before)
    mcp_clients.write(client, ENTRY, replace=True)
    after = client.path.read_text()
    lost = [line for line in before.splitlines() if line not in after.splitlines()]
    assert lost == ['command = "old"', "args = []"]
    held = tomllib.loads(after)["mcp_servers"]["cerebro-wiki"]
    assert held["startup_timeout_sec"] == 30 and held["env"] == {"HTTPS_PROXY": "http://proxy.stand-in:3128"}


def test_a_line_inside_a_string_that_looks_like_a_table_is_no_table_and_the_file_still_reads(tmp_path: Path) -> None:
    client = codex(tmp_path)
    client.path.parent.mkdir(parents=True)
    before = ('[mcp_servers.cerebro-wiki]\ncommand = "old"\nargs = [\n  "a",\n]\ndescription = """\nNotes:\n'
              '[see below]\n"""\n\n[mcp_servers.other]\ncommand = "other-tool"\n')
    client.path.write_text(before)
    mcp_clients.write(client, ENTRY, replace=True)
    data = tomllib.loads(client.path.read_text())
    assert data["mcp_servers"]["cerebro-wiki"] == {"command": ENTRY.command, "args": list(ENTRY.args),
                                                   "description": "Notes:\n[see below]\n"}
    assert data["mcp_servers"]["other"] == {"command": "other-tool"}


@pytest.mark.parametrize("app", ["Codex", "Cursor"])
def test_a_write_that_does_not_read_back_puts_the_old_file_back_and_says_so(tmp_path: Path, app: str,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    client = next(c for c in mcp_clients.clients(tmp_path, "darwin", {}) if c.app == app)
    client.path.parent.mkdir(parents=True)
    before = '[mcp_servers.other]\ncommand = "x"\n' if app == "Codex" else '{"mcpServers": {"other": {}}}'
    client.path.write_text(before)
    monkeypatch.setattr(mcp_clients, "_proven", lambda c, e: False)
    with pytest.raises(ClientError, match="was put back as it was"):
        mcp_clients.write(client, ENTRY)
    assert client.path.read_text() == before


class Claude:
    """A stand-in for claude: `claude mcp add` writes the entry into the scratch .claude.json, as claude would."""

    def __init__(self, path: Path, code: int = 0, elsewhere: str = "") -> None:
        self.path, self.code, self.ran, self.elsewhere = path, code, [], elsewhere

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.ran.append(list(argv))
        if argv[2] == "get":  # what claude lists, as from a config of its own init does not read
            return (0, f"{argv[3]}:\n  Args: mcp --workspace {self.elsewhere}") if self.elsewhere else (1, "not found")
        if self.code:
            return self.code, "stand-in refusal"
        name, rest = argv[5], list(argv[7:])
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        data.setdefault("mcpServers", {})[name] = {"type": "stdio", "command": rest[0], "args": rest[1:]}
        self.path.write_text(json.dumps(data))
        return 0, ""


def test_claude_code_is_added_with_its_own_command_and_proven_by_reading_its_config(tmp_path: Path) -> None:
    client = next(c for c in mcp_clients.clients(tmp_path, "darwin", {}) if c.app == "Claude Code")
    claude = Claude(client.path)
    mcp_clients.write(client, ENTRY, claude)
    assert claude.ran == [["claude", "mcp", "add", "--scope", "user", "cerebro-wiki", "--", ENTRY.command,
                           *ENTRY.args]]
    assert mcp_clients.holds(client, ENTRY)
    mcp_clients.write(client, ENTRY, claude)
    assert len(claude.ran) == 1  # held already: claude refuses a name twice, so it is not asked again
    other = tmp_path / "other"
    refused = next(c for c in mcp_clients.clients(other, "darwin", {}) if c.app == "Claude Code")
    with pytest.raises(ClientError, match="claude mcp add did not add it"):
        mcp_clients.write(refused, ENTRY, Claude(refused.path, code=1))
    with pytest.raises(ClientError, match="not on the PATH"):
        mcp_clients.write(refused, ENTRY, None)
    assert not refused.path.exists()


def test_after_a_failed_add_claude_mcp_get_is_asked_before_it_counts_as_failed(tmp_path: Path) -> None:
    client = next(c for c in mcp_clients.clients(tmp_path, "darwin", {}) if c.app == "Claude Code")
    claude = Claude(client.path, code=1, elsewhere=ENTRY.args[-1])
    mcp_clients.write(client, ENTRY, claude)  # claude holds it where init does not read: the entry is held
    assert [argv[2] for argv in claude.ran] == ["add", "get"]
    with pytest.raises(ClientError, match="claude mcp add did not add it"):
        mcp_clients.write(client, ENTRY, Claude(client.path, code=1, elsewhere="/srv/another/wiki"))


def test_a_name_another_workspace_holds_is_shown_as_such_and_replaced_only_on_the_persons_say(tmp_path: Path) -> None:
    root, home = new(tmp_path), tmp_path / "home"
    client = desktop(home)
    client.path.parent.mkdir(parents=True)
    other = {"command": "/opt/stand-in/bin/cerebro", "args": ["mcp", "--workspace", "/srv/team-a/wiki"]}
    client.path.write_text(json.dumps({"mcpServers": {"cerebro-wiki": other}}))
    w, screen, _ = walk(root, home, [0, None, None])  # Claude Desktop opened, then Escape at its choices, then Done
    w.clients()
    assert screen.menus[0][1][0] == ("Claude Desktop", "not installed", "held for /srv/team-a/wiki")
    assert [title for title, _, _ in screen.menus[1][1]] == ["Replace it", "Let me do it"]  # never "Install for me"
    assert json.loads(client.path.read_text())["mcpServers"]["cerebro-wiki"] == other
    assert screen.said[-1] == "mcp       no client set up for this workspace: cerebro init again opens this step"
    w, screen, _ = walk(root, home, [0, 0, None])  # Replace it, the person's say
    w.clients()
    assert json.loads(client.path.read_text())["mcpServers"]["cerebro-wiki"]["args"][-1] == str(root)


def test_a_client_that_holds_this_workspaces_entry_by_name_is_set_up_and_a_changed_command_is_offered(
        tmp_path: Path) -> None:
    root, home = new(tmp_path), tmp_path / "home"
    client = desktop(home)
    client.path.parent.mkdir(parents=True)
    old = {"command": "/opt/old-venv/bin/cerebro", "args": ["mcp", "--workspace", str(root)]}
    client.path.write_text(json.dumps({"mcpServers": {"cerebro-wiki": old}}))
    w, screen, _ = walk(root, home, [0, 0, None])  # Claude Desktop, Update it, Done
    w.clients()
    assert screen.menus[0][1][0] == ("Claude Desktop", "installed", ("starts /opt/old-venv/bin/cerebro: Enter offers "
                                                                 "this command"))
    assert [title for title, _, _ in screen.menus[1][1]] == ["Update it", "Let me do it"]
    assert json.loads(client.path.read_text())["mcpServers"]["cerebro-wiki"]["command"] == "/opt/stand-in/bin/cerebro"
    assert screen.said[-1] == "mcp       Claude Desktop holds cerebro-wiki"


def test_the_steps_by_hand_spell_the_file_from_home(tmp_path: Path) -> None:
    steps = mcp_clients.manual(desktop(tmp_path), ENTRY, tmp_path)
    assert steps[0][0].startswith("Open ~/Library/Application Support/Claude/claude_desktop_config.json")
    assert json.loads(steps[1][1])["mcpServers"]["cerebro-wiki"]["command"] == ENTRY.command
    assert steps[-1][0] == "Restart Claude Desktop"
    code = mcp_clients.manual(next(c for c in mcp_clients.clients(tmp_path, "linux", {}) if c.app == "Claude Code"),
                              ENTRY, tmp_path)
    assert code == [("Run this command in a terminal", ("claude mcp add --scope user cerebro-wiki -- "
                     "/opt/stand-in/bin/cerebro mcp --workspace /srv/stand-in/wiki"))]


# ---- the files the walk writes -------------------------------------------------------------------------------------


def front(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text().split("---\n")[1])


def test_identity_is_set_with_every_other_line_kept_and_a_form_it_cannot_keep_refused(tmp_path: Path) -> None:
    done = scaffold.init(tmp_path / "wiki", daily_usd=5, timezone="UTC")
    path = done.root / "workspace.md"
    before = path.read_text()
    scaffold.set_identity(path, "domains", ["quillmoor.example"], "your own email domains")
    after = path.read_text()
    assert front(path)["identity"] == {"domains": ["quillmoor.example"]}
    assert after.split("---\n", 2)[2] == before.split("---\n", 2)[2]  # the body as it was
    assert [line for line in before.splitlines() if line not in after.splitlines()] == [
        "  domains: []   # your own email domains: people at one are your side"]
    scaffold.set_identity(path, "domains", ["quillmoor.example", "larkspur.example"])
    assert front(path)["identity"]["domains"] == ["quillmoor.example", "larkspur.example"]
    profile = done.root / "profile.md"
    scaffold.set_identity(profile, "name", "Rowan Vale", body="\nWho you are.\n")
    scaffold.set_identity(profile, "email", ["rowan@quillmoor.example"])
    assert front(profile)["identity"] == {"name": "Rowan Vale", "email": ["rowan@quillmoor.example"]}
    assert profile.read_text().endswith("---\n\nWho you are.\n")
    flat = tmp_path / "flat.md"
    flat.write_text("---\nidentity: { email: [a@quillmoor.example] }\n---\n")
    with pytest.raises(scaffold.ScaffoldError, match="on one line"):
        scaffold.set_identity(flat, "name", "x")
    assert flat.read_text() == "---\nidentity: { email: [a@quillmoor.example] }\n---\n"


def test_a_jobs_model_is_set_on_its_own_line_and_a_form_init_does_not_edit_refused(tmp_path: Path) -> None:
    done = scaffold.init(tmp_path / "wiki", daily_usd=5, timezone="UTC")
    path = done.root / "settings.yaml"
    before = path.read_text()
    scaffold.set_model(done.root, "compilation", ModelSpec("anthropic", "claude-sonnet-5-5", "", "compilation"))
    after = path.read_text()
    assert read_models(yaml.safe_load(after))["compilation"].model == "claude-sonnet-5-5"
    assert [a for a, b in zip(after.splitlines(), before.splitlines(), strict=True) if a != b] == [
        "  compilation: { provider: anthropic, model: claude-sonnet-5-5 }"]
    path.write_text(before.replace("  extraction: { provider: fireworks, model: glm-5p3-flash, effort: low }",
                                   "  extraction:\n    provider: fireworks\n    model: glm-5p3-flash"))
    kept = path.read_text()
    with pytest.raises(scaffold.ScaffoldError, match="form init does not edit"):
        scaffold.set_model(done.root, "extraction", ModelSpec("fireworks", "glm-5p3", "", "extraction"))
    assert path.read_text() == kept


# ---- the walk, scripted ---------------------------------------------------------------------------------------------


class Script:
    """A screen whose answers are given in order: for a menu, the place of a row, or None for Escape or Done; for a
    line, the text, "" for Enter, which takes the default as the terminal's prompt does, or None for input that ends. It keeps every menu's head and rows, and every line said."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers, self.menus, self.said, self.asked, self.read_lines = list(answers), [], [], [], []

    def _next(self) -> Any:
        return self.answers.pop(0) if self.answers else None

    def menu(self, menu: Any, cursor: int = 0) -> int | None:
        self.menus.append((list(menu.head), [(r.title, r.state, r.reason) for r in menu.rows]))
        return self._next()

    def text(self, prompt: str, default: str = "", hidden: bool = False, source: str = "") -> str | None:
        self.asked.append((prompt, default, hidden))
        answer = self._next()
        return default if answer == "" else answer  # Enter takes the default, as the terminal's prompt gives it

    def read(self, lines: Sequence[str]) -> None:
        self.read_lines.append(list(lines))

    def say(self, line: str) -> None:
        self.said.append(line)


def walk(root: Path, home: Path, answers: list[Any], claude: Claude | None = None,
         prove: Proofs | None = None) -> tuple[guided.Walk, Script, list[str]]:
    from cerebro.app import Cerebro

    screen, ran = Script(answers), []
    return guided.Walk(root, screen, Cerebro, lobby=lambda: ran.append("lobby"),
                       first_run=lambda: ran.append("first run"), home=home, command="/opt/stand-in/bin/cerebro",
                       system="darwin", environ={}, run_tool=claude, prove=prove or Proofs()), screen, ran


def new(tmp_path: Path) -> Path:
    return scaffold.init(tmp_path / "wiki", daily_usd=5, timezone="UTC").root


def test_the_walk_run_again_asks_its_questions_proves_each_key_and_ends_with_the_first_run(tmp_path: Path) -> None:
    root, home = new(tmp_path), tmp_path / "home"
    (root / "local").mkdir(exist_ok=True)
    (root / "local" / "notes.md").write_text("A note to read.\n")
    claude_file = home / ".claude.json"
    answers = [
        "Rowan@Quillmoor.example",                         # you: only the email profile.md lacks
        2, 0, 0, None,                                     # models: compilation, the first provider, its first model
        0, "stand-in-fireworks-key", 1, "stand-in-typesafe-key", None,  # keys: each proven, then kept; Done
        0, 0, 1, 1, None, None,                            # clients: Claude Desktop written; Claude Code shown
    ]                                                      # the first run starts with no question
    proofs = Proofs()
    w, screen, ran = walk(root, home, answers, Claude(claude_file), proofs)
    checks, then = w.all()
    assert [prompt for prompt, _, _ in screen.asked] == ["Your work email [Enter to skip]", "Fireworks API key",
                                                         "Typesafe API key"]
    assert front(root / "profile.md")["identity"] == {"email": ["Rowan@quillmoor.example"]}
    assert "you       profile.md has no name: fill it in" in screen.said
    assert "domains   none in workspace.md: people at your own domains count as your side once its identity: " \
           "domains: lists them" in screen.said
    chosen = read_models(yaml.safe_load((root / "settings.yaml").read_text()))["compilation"]
    assert chosen.provider == "fireworks" and chosen.job == "compilation"
    assert proofs.calls == [("fireworks", "extraction", "glm-5p3-flash"), ("typesafe", "extraction", "jev-1.13.0")]
    assert "Make a key in the API keys settings of the Fireworks console and paste it below." in screen.said
    assert "keys      2 of 2 model keys set" in screen.said
    from cerebro.app import Cerebro

    with Cerebro(root) as workspace:
        assert workspace.secrets is not None and workspace.secrets.get("fireworks") == "stand-in-fireworks-key"
    assert not any("stand-in-" in line for line in screen.said)  # never shown
    assert mcp_clients.found(desktop(home), scaffold.mcp_entry(root, "x")["name"]) == {
        "command": "/opt/stand-in/bin/cerebro", "args": ["mcp", "--workspace", str(root)]}
    assert screen.read_lines and screen.read_lines[0][0] == "1. Run this command in a terminal"
    assert ran == ["lobby", "first run"]
    # the stand-in first run writes nothing, as Done after discovery does, so serve --once comes back to it
    assert checks == ["profile.md has no name: fill it in"] and then == "cerebro serve --once"


def test_escape_or_input_that_ends_at_any_step_writes_nothing_and_the_first_run_waits_for_its_keys(
        tmp_path: Path) -> None:
    root, home = new(tmp_path), tmp_path / "home"
    files = {name: (root / name).read_bytes() for name in ("settings.yaml", "workspace.md")}
    w, screen, ran = walk(root, home, [None, None, None, None, None, None])
    checks, then = w.all()
    assert not (root / "profile.md").exists() and not home.exists()
    assert {name: (root / name).read_bytes() for name in files} == files
    assert ran == ["lobby"]  # the lobby is its own; the first run is not offered with no key kept
    assert not any(head[0] == "First run" for head, _ in screen.menus)
    assert screen.said[-1] == ("next      the first run waits for the Fireworks key and the Typesafe key: set them "
                               "with cerebro secrets set, then run cerebro serve --once")
    assert checks[-2:] == ["no Fireworks key yet for extraction and compilation: run cerebro secrets set fireworks",
                           "no Typesafe key yet for extraction and decision: run cerebro secrets set typesafe"]
    assert then == "cerebro secrets set fireworks"
    w, screen, _ = walk(root, home, [None, None, 1, None, 0, 0, None, None])  # a provider picked, its model escaped
    w.models()
    assert (root / "settings.yaml").read_bytes() == files["settings.yaml"]


def test_with_its_keys_kept_and_nothing_to_read_the_first_run_says_so_and_waits(tmp_path: Path) -> None:
    root, home = new(tmp_path), tmp_path / "home"
    w, screen, ran = walk(root, home, [])
    w.environ = {"FIREWORKS_API_KEY": "set", "TYPESAFE_API_KEY": "set"}
    w.run()
    assert ran == [] and not screen.menus
    assert w.checks == [NOTHING]


def test_escape_then_enter_at_a_keys_prompt_goes_back_with_nothing_kept_or_proven_and_an_empty_enter_is_ignored(
        tmp_path: Path) -> None:
    from click.testing import CliRunner as Runner

    with Runner().isolation(input="\x1b\n"):  # Escape, then Enter, at the hidden prompt
        typed = guided.TerminalScreen(None, print).text("Fireworks API key", hidden=True)
    assert typed == "\x1b"
    root, home = new(tmp_path), tmp_path / "home"
    proofs = Proofs()
    w, screen, _ = walk(root, home, [0, typed, 0, "", "", None, None], prove=proofs)
    w.keys()
    assert [prompt for prompt, _, _ in screen.asked] == ["Fireworks API key"] * 4
    assert proofs.calls == []
    from cerebro.app import Cerebro

    with Cerebro(root) as workspace:
        assert workspace.secrets is not None and workspace.secrets.names() == []
    assert screen.menus[-1][1][0][1] == "not added"


def test_a_key_that_does_not_verify_is_not_kept_and_says_why(tmp_path: Path) -> None:
    root, home = new(tmp_path), tmp_path / "home"
    w, screen, _ = walk(root, home, [0, "stand-in-key", None], prove=Proofs("401 unauthorized"))
    w.keys()
    assert screen.menus[-1][0][-1] == "[x] Fireworks did not verify: 401 unauthorized (the key was ****)"
    assert "check     the Fireworks key did not work for extraction and compilation (401 unauthorized (the key " \
           "was ****)): run cerebro secrets set fireworks" in screen.said
    from cerebro.app import Cerebro

    with Cerebro(root) as workspace:
        assert workspace.secrets is not None and workspace.secrets.names() == []
    assert not any("stand-in-key" in line for line in screen.said)


def test_run_again_the_walk_shows_what_is_set_and_asks_nothing_more(tmp_path: Path) -> None:
    root, home = new(tmp_path), tmp_path / "home"
    scaffold.set_identity(root / "profile.md", "name", "Rowan Vale")
    scaffold.set_identity(root / "profile.md", "email", ["rowan@quillmoor.example"])
    scaffold.set_identity(root / "workspace.md", "domains", ["quillmoor.example"])
    w, screen, _ = walk(root, home, [])
    w.you()
    w.domains()
    assert screen.asked == []
    assert screen.said == ["you       Rowan Vale <rowan@quillmoor.example>, in profile.md",
                           "domains   quillmoor.example, in workspace.md"]


def test_run_again_a_wrong_email_is_refused_by_the_flags_own_check_and_asked_again_and_written_whole(
        tmp_path: Path) -> None:
    root, home = new(tmp_path), tmp_path / "home"
    w, screen, _ = walk(root, home, ["ann lee@quillmoor.example", "x: y@quillmoor.example", "ann@quillmoor.example"])
    w.you()
    assert [line for line in screen.said if "want one address" in line] == [
        "          email 'ann lee@quillmoor.example': want one address, such as sam.rivera@quillmoor.example",
        "          email 'x: y@quillmoor.example': want one address, such as sam.rivera@quillmoor.example"]
    assert front(root / "profile.md")["identity"] == {"email": ["ann@quillmoor.example"]}


def test_a_client_file_that_does_not_read_says_why_under_the_menu_and_is_left_as_it_was(tmp_path: Path) -> None:
    root, home = new(tmp_path), tmp_path / "home"
    path = desktop(home).path
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    w, screen, _ = walk(root, home, [0, 0, None])
    w.clients()
    assert path.read_text() == "{broken"
    head = screen.menus[-1][0]
    assert any("does not read as JSON" in line for line in head)


# ---- init at a terminal that draws ---------------------------------------------------------------------------------


@pytest.fixture
def drawing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Script:
    """init as at a terminal that draws, on a scripted screen, its clients under a scratch home, no claude."""
    from cerebro.output import Printer

    monkeypatch.setattr(cli, "_drawing", lambda plain: Printer(plain=True))
    monkeypatch.setattr(cli, "_client_home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli, "_claude", lambda: None)
    monkeypatch.setattr("cerebro.clock.local_zone", lambda *a, **k: "Europe/Berlin")
    script = Script([])
    monkeypatch.setattr(guided, "TerminalScreen", lambda printer, echo: script)
    return script


def test_at_a_terminal_that_draws_init_starts_blank_or_from_a_template_and_escape_writes_nothing(
        tmp_path: Path, drawing: Script, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "wiki"
    got = CliRunner().invoke(cli.main, ["init", str(root)])
    assert got.exit_code == 0 and got.output.strip() == "Nothing written." and not root.exists()
    assert drawing.menus[0][1][0] == ("blank", "no agent", "you write the first one")
    assert ("gtm", "template", "starts with the gtm template's agents") in drawing.menus[0][1]
    monkeypatch.setattr(cli, "_lobby", lambda *a, **k: None)
    drawing.answers = [0, "Ann Lee", "ann@ourco.example", "", "", "5", "UTC", None, None, None]
    made = CliRunner().invoke(cli.main, ["init", str(root)])
    assert made.exit_code == 0, made.output
    assert made.output.splitlines()[0] == f"workspace {root.resolve()}"
    assert (root / "settings.yaml").is_file()
    assert drawing.asked[2:4] == [("Team or organization name", "Ourco", False), ("Team or organization web domain", "ourco.example", False)]
    assert drawing.asked[4:] == [("Daily budget in dollars", "50.0", False), ("Time zone", "Europe/Berlin", False)]
    assert len(drawing.asked) == 6  # nothing is asked twice: the walk after it asks no name, email or domain
    assert read_ours(root.resolve()) == Ours(("ann@ourco.example",), ("ourco.example",))
    assert front(root / "profile.md")["identity"] == {"name": "Ann Lee", "email": ["ann@ourco.example"]}
    assert drawing.said[-1].startswith("next      the first run waits for the Fireworks key")
    assert made.output.rstrip().splitlines()[-1].startswith("[!] Done with warnings. Check: no Fireworks key yet")
    again = CliRunner().invoke(cli.main, ["init", str(root)])
    assert again.exit_code == 0 and again.output.splitlines()[0] == f"workspace {root.resolve()}"


@pytest.mark.untrusted
def test_the_walk_writes_nothing_into_a_workspace_its_owner_has_not_trusted(tmp_path: Path, drawing: Script) -> None:
    root = scaffold.init(tmp_path / "wiki", daily_usd=5, timezone="UTC", environ={}).root
    from cerebro import trust

    trust.untrust(str(root))
    files = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    drawing.answers = ["ann@ourco.example", 2, 0, 0, None]
    got = CliRunner().invoke(cli.main, ["init", str(root)])
    assert got.exit_code == 1 and "is not a workspace you trust" in got.output
    assert drawing.asked == [] and drawing.menus == []
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == files


# ---- the frames, and what off a terminal never loads ------------------------------------------------------------------


def test_each_steps_frames_in_a_light_and_a_dark_terminal_and_plain_are_the_saved_ones_and_fit_their_columns() -> None:
    sys.path.insert(0, str(Path(__file__).parents[1] / "checks"))
    import init_demo

    from cerebro.output import strip_escapes

    data = Path(__file__).parent / "data" / "tui"
    light, dark, plain = init_demo.frames(False), init_demo.frames(True), init_demo.frames(None)
    assert light == (data / "init-light.ansi").read_text() and dark == (data / "init-dark.ansi").read_text()
    assert plain == (data / "init-plain.txt").read_text() and "\x1b" not in plain
    assert light != dark and strip_escapes(light) == strip_escapes(dark)
    assert all(len(line) < init_demo.WIDTH and not line.endswith("…") for line in strip_escapes(dark).splitlines())


def test_init_off_a_terminal_loads_no_style_checklist_textual_or_walk_module(tmp_path: Path) -> None:
    import subprocess

    code = (f"import sys\nfrom cerebro.cli import main\ntry:\n    main(['init', {str(tmp_path / 'wiki')!r}])\n"
            "except SystemExit:\n    pass\nprint('LOADED', sorted(m for m in sys.modules if m in ('cerebro.output', "
            "'cerebro.checklist', 'cerebro.guided', 'cerebro.mcp_clients') or m.split('.')[0] == 'textual'), "
            "file=sys.stderr)\n")
    ran = subprocess.run([sys.executable, "-c", code], check=False, stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, timeout=60, env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config")})
    assert "LOADED []" in ran.stderr, ran.stderr[-2000:]


# ---- the workspace's own side, as init gives it ---------------------------------------------------------------------


def made(tmp_path: Path, *args: str) -> Path:
    root = (tmp_path / "wiki").resolve()
    got = CliRunner().invoke(cli.main, ["init", str(root), *args])
    assert got.exit_code == 0, got.output
    return root


def test_init_with_an_email_reads_back_its_own_side_and_a_colleagues_record_carries_the_mark_not_a_key(
        tmp_path: Path) -> None:
    from cerebro import held

    root = made(tmp_path, "--email", "Ann@OurCo.example", "--name", "Ann Lee")
    ours = read_ours(root)
    assert ours == Ours(("ann@ourco.example",), ("ourco.example",))
    assert front(root / "profile.md")["identity"] == {"name": "Ann Lee", "email": ["Ann@ourco.example"]}
    assert front(root / "workspace.md")["identity"]["name"] == "Ourco"  # a placeholder from the domain, marked as one
    assert "# a placeholder from your email's domain" in (root / "workspace.md").read_text()
    colleague = Record(id="m1", source="mail", revision="1", at="2026-11-18T09:00:00Z",
                       keys={"email": ["bo@ourco.example"]})
    assert held.own_side(colleague.keys, ours) and held.one_of_ours(colleague.keys, ours)
    assert ("domain", "ourco.example") not in held.linked(colleague, ours)  # the mark, never a key
    assert ("domain", "ourco.example") in held.linked(colleague)  # which it would be with no own side
    assert held.parties_outside(colleague, ours) == []


def test_an_address_at_a_public_mail_service_gives_no_domain_and_no_placeholder(tmp_path: Path) -> None:
    root = made(tmp_path, "--email", "ann-example@gmail.com")
    assert read_ours(root) == Ours(("ann-example@gmail.com",), ())
    assert front(root / "workspace.md")["name"] == "wiki" and "placeholder" not in (root / "workspace.md").read_text()
    assert "name" not in front(root / "workspace.md")["identity"]  # no team or organization name to write
    assert scaffold.org_guess("ann@mail.yahoo.com") == ("", "")
    w = Script(["Ann", "ann-example@gmail.com", "", ""])
    assert guided.ask_you(w) is not None and w.asked[2:] == [("Team or organization name [Enter to skip]", "", False),
                                                            ("Team or organization web domain [Enter to skip]", "", False)]


def test_an_org_given_is_written_as_given_and_a_domain_given_wins_over_the_emails(tmp_path: Path) -> None:
    root = made(tmp_path, "--email", "ann@mail.ourco.example", "--org", "Our Co: Research",
                "--domain", "ourco.example")
    assert front(root / "workspace.md")["identity"]["name"] == "Our Co: Research"
    assert "placeholder" not in (root / "workspace.md").read_text()
    assert read_ours(root).domains == ("ourco.example",)


@pytest.mark.parametrize("args", [["--daily-usd", "20"], ["--domain", "ourco.example"], ["--email", "ann@ourco.example"],
                                  ["--template", "gtm", "--email", "ann@ourco.example"]])
def test_a_top_level_domains_key_is_never_written(tmp_path: Path, args: list[str]) -> None:
    root = made(tmp_path, *args)
    data = front(root / "workspace.md")
    assert "domains" not in data and "domains" in data["identity"]


def test_a_flags_answer_that_does_not_do_is_refused_before_anything_is_asked_or_written(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    for args, why in ((["--email", "not an address"], "want one address"), (["--name", "a\nb"], "one line")):
        got = CliRunner().invoke(cli.main, ["init", str(root), *args])
        assert got.exit_code != 0 and why in got.output and not root.exists()


# ---- a plain terminal asks init's questions one at a time ---------------------------------------------------------


def plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: str, *args: str) -> tuple[Any, Path]:
    """init at a plain terminal, stood in: the terminal check says yes, and the answers are typed lines."""
    monkeypatch.setattr("cerebro.clock.local_zone", lambda *a, **k: "Europe/Berlin")
    monkeypatch.setattr(cli, "_asking", lambda: True)
    root = (tmp_path / "wiki").resolve()
    return CliRunner().invoke(cli.main, ["init", str(root), *args], input=answers), root


PROMPTS = ("Your name", "Your work email", "Team or organization name", "Team or organization web domain", "Daily budget", "Time zone")


def test_an_empty_answer_to_each_question_takes_its_default_or_none(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    got, root = plain(tmp_path, monkeypatch, "\n" * 6)
    assert got.exit_code == 0, got.output
    asked = got.output.split("workspace ")[0]
    assert asked.startswith("Your name [Enter to skip]: \nYour work email [Enter to skip]: \n"
                            "Team or organization name [Enter to skip]: \nTeam or organization web domain [Enter to skip]: \n"
                            "Daily budget in dollars [50.0]: \nTime zone [Europe/Berlin]: \n"), asked
    assert not (root / "profile.md").exists() and read_ours(root) == Ours((), ())
    assert front(root / "workspace.md")["name"] == "wiki"


def test_an_address_at_a_public_mail_service_offers_no_guess_and_its_domain_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got, root = plain(tmp_path, monkeypatch, "Ann\nann-example@gmail.com\n\ngmail.com\n\n\n\n")
    assert got.exit_code == 0, got.output
    assert "Team or organization name [Enter to skip]: " in got.output
    assert "domain 'gmail.com' is a public mail service, not one organization's: give your team or organization's web domain, such as " \
           "quillmoor.example" in got.output
    assert got.output.count("Team or organization web domain [Enter to skip]: ") == 2
    assert read_ours(root) == Ours(("ann-example@gmail.com",), ())


def test_a_wrong_email_is_told_why_and_asked_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got, root = plain(tmp_path, monkeypatch, "Ann\nann at ourco\nann@ourco.example\n\n\n\n\n")
    assert got.exit_code == 0, got.output
    assert "email 'ann at ourco': want one address, such as sam.rivera@quillmoor.example" in got.output
    assert got.output.count("Your work email [Enter to skip]: ") == 2
    assert "Team or organization name [Ourco, from your email's domain]: " in got.output
    assert "Team or organization web domain [ourco.example, from your email's domain]: " in got.output
    assert read_ours(root) == Ours(("ann@ourco.example",), ("ourco.example",))
    assert front(root / "workspace.md")["identity"]["name"] == "Ourco" and "placeholder" not in (root / "workspace.md").read_text()


@pytest.mark.parametrize(("flag", "value", "skipped"), [
    ("--name", "Ann Lee", "Your name"), ("--email", "ann@ourco.example", "Your work email"),
    ("--org", "Our Co", "Team or organization name"), ("--domain", "ourco.example", "Team or organization web domain"),
    ("--daily-usd", "5", "Daily budget"), ("--timezone", "UTC", "Time zone")])
def test_a_question_whose_flag_was_given_is_not_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str,
                                                      value: str, skipped: str) -> None:
    got, _ = plain(tmp_path, monkeypatch, "\n" * 5, flag, value)
    assert got.exit_code == 0, got.output
    assert skipped not in got.output.split("workspace ")[0]
    assert all(p in got.output for p in PROMPTS if p != skipped)


def test_with_every_answer_given_as_a_flag_init_asks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got, root = plain(tmp_path, monkeypatch, "", "--name", "Ann Lee", "--email", "ann@ourco.example", "--org",
                      "Our Co", "--domain", "ourco.example", "--daily-usd", "5", "--timezone", "UTC")
    assert got.exit_code == 0, got.output
    assert got.output.startswith("workspace ") and not any(p in got.output for p in PROMPTS)
    assert front(root / "workspace.md")["identity"]["name"] == "Our Co"


# ---- one rule for every path ---------------------------------------------------------------------------------------


DOMAIN_CASES = {"gmail.com": None, "co.uk": None, "1.2.3.4": None, "mail.yahoo.com": None,
              "www.acme.example": "acme.example", "eng.acme.example": "acme.example", "https://acme.example/about": "acme.example"}


@pytest.mark.parametrize(("typed", "want"), list(DOMAIN_CASES.items()))
def test_every_path_reads_a_domain_by_the_web_domain_rule(tmp_path: Path, typed: str, want: str | None) -> None:
    """On the web domain question, on --domain, and on scaffold.init: a public mail service, a suffix alone and an
    IP address refused; a website taken; the registrable domain kept."""
    screen = Script(["Sam", "sam@acme.example", "Acme", typed, "fallback.example"])
    you = guided.ask_you(screen)
    assert you is not None and you.domains == ((want,) if want else ("fallback.example",))
    flagged = CliRunner().invoke(cli.main, ["init", str(tmp_path / "w"), "--domain", typed])
    if want:
        assert flagged.exit_code == 0 and read_ours(tmp_path / "w").domains == (want,)
    else:
        assert flagged.exit_code == 1 and "--domain: domain" in flagged.output and not (tmp_path / "w").exists()


def test_an_address_at_a_public_mail_subdomain_offers_no_guess_and_writes_no_domain(tmp_path: Path) -> None:
    screen = Script(["Ada", "ada@mail.yahoo.com", "", ""])
    you = guided.ask_you(screen)
    assert you is not None and you.domains == () and screen.asked[3] == ("Team or organization web domain [Enter to skip]", "",
                                                                         False)
    got = CliRunner().invoke(cli.main, ["init", str(tmp_path / "w"), "--email", "ada@mail.yahoo.com"])
    assert read_ours(tmp_path / "w").domains == ()
    assert "check     yahoo.com is public mail" not in got.output
    assert "check     mail.yahoo.com is public mail, so workspace.md has no team or organization domain or name: fill them in" \
           in got.output


def test_the_org_is_written_under_identity_and_the_top_name_is_the_folders(
        tmp_path: Path) -> None:
    made = scaffold.init(tmp_path / "wiki", environ={}, email="ann@ourco.example", org="Our Co")
    data = front(made.root / "workspace.md")
    assert data["name"] == "wiki" and data["identity"] == {"name": "Our Co", "domains": ["ourco.example"]}


def test_an_international_domain_is_taken_only_in_its_ascii_form() -> None:
    with pytest.raises(scaffold.ScaffoldError, match="ASCII form"):
        scaffold.check_email("ann@\u212aelvin.example")  # a Kelvin sign, which lower case would make a k
    assert scaffold.check_email("ann@xn--kelvin-9ta.example") == "ann@xn--kelvin-9ta.example"


@pytest.mark.parametrize(("folder", "name"), [("-foo", "cerebro-foo"), ("a--b", "cerebro-a-b"), ("my wiki!", "cerebro-my-wiki")])
def test_an_entrys_name_folds_every_run_of_hyphens(tmp_path: Path, folder: str, name: str) -> None:
    assert scaffold.mcp_entry(tmp_path / folder, "cerebro")["name"] == name


def test_a_budget_that_does_not_do_is_asked_again_as_it_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_asking", lambda: True)
    got = CliRunner().invoke(cli.main, ["init", str(tmp_path / "w")], input="\n\n\n\n0\n7\nUTC\n")
    assert got.exit_code == 0, got.output
    assert got.output.count("Daily budget in dollars [50.0]: ") == 2 and "would stop every model call" in got.output
    flagged = CliRunner().invoke(cli.main, ["init", str(tmp_path / "v"), "--daily-usd", "0"], input="\n" * 6)
    assert flagged.exit_code == 1 and "Your name" not in flagged.output  # refused before any question


def test_a_persons_comment_on_a_model_line_stays_when_its_model_is_picked(tmp_path: Path) -> None:
    root = new(tmp_path)
    path = root / "settings.yaml"
    path.write_text(path.read_text().replace("  compilation: { provider: fireworks, model: glm-5p3, effort: high }",
                                             "  compilation: { provider: fireworks, model: glm-5p3, effort: high }"
                                             "   # legal asked for this one"))
    scaffold.set_model(root, "compilation", ModelSpec("anthropic", "claude-sonnet-5-5", "", "compilation"))
    assert "  compilation: { provider: anthropic, model: claude-sonnet-5-5 }   # legal asked for this one" in \
        path.read_text().splitlines()


# ---- the walk's defaults, refusals and client steps -----------------------------------------------------------------


@pytest.mark.parametrize("typed", ["", "mail.eu.acme.co.example", "eng.acme.co.example"],
                         ids=["default taken", "default typed", "another typed"])
def test_the_web_domain_default_shown_is_the_domain_written(
        tmp_path: Path, drawing: Script, monkeypatch: pytest.MonkeyPatch, typed: str) -> None:
    """An email at a subdomain: the question shows the email's own domain, and taking it writes that domain, as
    init --email writes it. A domain typed is read as a web domain, so a subdomain typed gives its
    registrable domain."""
    monkeypatch.setattr(cli, "_lobby", lambda *a, **k: None)
    root = tmp_path / "wiki"
    drawing.answers = [0, "Ann Lee", "ann@mail.eu.acme.co.example", "", typed, None, None, None]
    made = CliRunner().invoke(cli.main, ["init", str(root)], input="5\nUTC\n")
    assert made.exit_code == 0, made.output
    assert drawing.asked[3] == ("Team or organization web domain", "mail.eu.acme.co.example", False)
    want = (["acme.co.example"] if typed == "eng.acme.co.example"
            else ["mail.eu.acme.co.example"])  # the default
    assert front(root / "workspace.md")["identity"]["domains"] == want
    flagged = scaffold.init(tmp_path / "flagged", environ={}, email="ann@mail.eu.acme.co.example")
    assert front(flagged.root / "workspace.md")["identity"]["domains"] == ["mail.eu.acme.co.example"]


@pytest.mark.parametrize(("typed", "found"), [
    ("Ann\x9b31mLee", "a control character (U+009B)"), ("Ann\x85Lee", "a control character (U+0085)"),
    ("Ann\x7fLee", "a control character (U+007F)"), ("Ann\x1bLee", "a control character (U+001B)"),
    ("Ann\nLee", "a newline"), ("Ann\tLee", "a tab"), ("Ann Lee", "a line separator (U+2028)")])
def test_a_name_or_org_holding_any_control_character_is_refused_on_every_path(
        tmp_path: Path, typed: str, found: str) -> None:
    """Every control character, U+0080 to U+009F among them, and the line and
    paragraph separators, refused with what was found, on the flag and at the question."""
    flagged = CliRunner().invoke(cli.main, ["init", str(tmp_path / "w"), "--name", typed])
    assert flagged.exit_code == 1 and f"holds {found}: use one line of text" in flagged.output
    assert not (tmp_path / "w").exists()
    screen = Script([typed, "Ann Lee", "ann@ourco.example", typed, "Our Co", ""])
    you = guided.ask_you(screen)
    assert you is not None and (you.name, you.org) == ("Ann Lee", "Our Co")
    assert sum(f"holds {found}" in line for line in screen.said) == 2


KEYS = {"FIREWORKS_API_KEY": "stand-in", "TYPESAFE_API_KEY": "stand-in"}


def offered(root: Path, home: Path) -> tuple[bool, list[str], list[str]]:
    """Whether the walk's last step starts the first run, with every key given, and what it said and left to check."""
    w, screen, ran = walk(root, home, [None])
    w.environ = KEYS
    w.run()
    return "first run" in ran, screen.said, w.checks


NOTHING = ("the first run would read nothing yet: local/ holds no Markdown file and no other source is connected: "
           "connect one with cerebro connect, then run cerebro serve --once")


def test_a_source_never_connected_and_an_empty_local_folder_offer_no_first_run(tmp_path: Path) -> None:
    """A source counts when connect calls it connected, not when its file is there, as a
    source file written by hand is before anyone connects it; once its login is kept, it counts."""
    root = scaffold.init(tmp_path / "wiki", template="gtm", daily_usd=5, timezone="UTC", environ={}).root
    (root / "sources" / "code.yaml").write_text("kind: github\nrepos: [quillmoor/api]\n")
    shown, said, checks = offered(root, tmp_path / "home")
    assert not shown and checks == [NOTHING] and said[-1] == "next      " + NOTHING
    from cerebro.app import Cerebro
    from cerebro.connect import need, read_source

    with Cerebro(root, trusted=True) as workspace:
        assert workspace.secrets is not None
        workspace.secrets.set(need(read_source(root / "sources" / "code.yaml")).name, "made-up-login")
    assert offered(root, tmp_path / "home")[0]


def test_a_local_folder_with_no_markdown_file_offers_no_first_run_and_one_with_a_note_does(tmp_path: Path) -> None:
    """local/ counts when it holds a Markdown file at any depth, which the files source reads, so a stray
    .DS_Store counts for nothing."""
    root = new(tmp_path)
    (root / "local").mkdir(exist_ok=True)
    (root / "local" / ".DS_Store").write_bytes(b"\x00\x01")
    (root / "local" / "notes.txt").write_text("not read\n")
    assert offered(root, tmp_path / "home")[:1] == (False,)
    (root / "local" / "team").mkdir()
    (root / "local" / "team" / "kickoff.md").write_text("A note.\n")
    assert offered(root, tmp_path / "home")[0]


def test_once_an_article_is_written_the_first_run_is_not_offered_again(tmp_path: Path) -> None:
    """The first run waits only while no article of a curated collection, or of any
    collection when none is curated, has been written; after that the walk says nothing of it."""
    root = new(tmp_path)
    (root / "local").mkdir(exist_ok=True)
    (root / "local" / "kickoff.md").write_text("A note.\n")
    (root / "articles" / "things").mkdir(parents=True)
    (root / "articles" / "things" / "alder-works.md").write_text("---\ntitle: Alder Works\n---\n\nText.\n")
    shown, said, checks = offered(root, tmp_path / "home")
    assert not shown and checks == [] and not any("first run" in line for line in said)


@pytest.mark.parametrize(("case", "held", "line"), [
    ("an older command", {"command": "/opt/old/bin/cerebro"}, "starts /opt/old/bin/cerebro"),
    ("another workspace", {"command": ENTRY.command, "args": ["mcp", "--workspace", "/srv/other"]},
     "held for /srv/other")])
def test_claude_code_holding_the_name_shows_steps_that_work_and_cerebro_removes_nothing(
        tmp_path: Path, case: str, held: dict[str, Any], line: str) -> None:
    """claude refuses a name it holds, so the steps shown remove its entry and then add this workspace's, for the
    person to run: Cerebro runs neither. The row promises nothing it does not do, and the last line is true."""
    root, home, ran = new(tmp_path), tmp_path / "home", []
    home.mkdir()
    held = {"args": ["mcp", "--workspace", str(root)], **held}  # this workspace's, unless the case says another
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"cerebro-wiki": {"type": "stdio", **held}}}))

    def claude(argv: Sequence[str]) -> tuple[int, str]:
        ran.append(list(argv))
        return 1, "MCP server cerebro-wiki already exists in user config"

    w, screen, _ = walk(root, home, [1, None], claude=claude)
    w.clients()
    row = screen.menus[0][1][1]
    assert row[0] == "Claude Code" and row[2] == f"{line}: Enter shows the steps", row
    assert screen.read_lines[0] == ["1. Remove the entry Claude Code holds under this name",
                                    "   claude mcp remove --scope user cerebro-wiki", "",
                                    "2. Then add this workspace's",
                                    ("   claude mcp add --scope user cerebro-wiki -- /opt/stand-in/bin/cerebro mcp "
                                    f"--workspace {root}"), ""]
    assert ran == [] and json.loads((home / ".claude.json").read_text())["mcpServers"]["cerebro-wiki"]["command"] \
        == held["command"]
    assert "sets one up" not in screen.said[-1]


def test_an_entry_claude_code_keeps_for_one_project_is_removed_there(tmp_path: Path) -> None:
    """An entry kept for one project is removed in that project's folder."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"projects": {"/srv/proj": {"mcpServers": {
        "cerebro-wiki": {"command": "/opt/old/bin/cerebro", "args": list(ENTRY.args)}}}}}))
    code = next(c for c in mcp_clients.clients(home, "linux", {}) if c.app == "Claude Code")
    steps = mcp_clients.manual(code, ENTRY, home)
    assert steps[0] == ("Remove the entry Claude Code holds under this name for /srv/proj, in that folder",
                        "cd /srv/proj && claude mcp remove --scope local cerebro-wiki")


def strict_json(text: str) -> Any:
    """`text` read as JSON is read by the clients' own parsers: no NaN or Infinity, and no number lost to a double."""
    def refuse(constant: str) -> None:
        raise ValueError(f"{constant} is not JSON")

    return json.loads(text, parse_constant=refuse)


def test_a_json_config_is_written_back_as_strict_json_with_every_value_cerebro_did_not_set_kept_as_written(
        tmp_path: Path) -> None:
    """Numbers a double cannot hold, or holds otherwise, stay as the file had them, so a client's strict parser still
    reads its config; the entry Cerebro sets is the one thing changed."""
    cursor = next(c for c in mcp_clients.clients(tmp_path, "linux", {}) if c.app == mcp_clients.CURSOR)
    cursor.path.parent.mkdir(parents=True)
    cursor.path.write_text('{"limits": {"max": 1e400, "min": -1e400, "big": 1E5, "fine": 1.00000000000000000001,\n'
                           ' "count": 12345678901234567890123, "zero": -0.0, "plain": 3, "name": "1e400"},\n'
                           ' "mcpServers": {"other": {"command": "x", "args": [0.1]}}}\n')
    mcp_clients.write(cursor, ENTRY)
    text = cursor.path.read_text()
    data = strict_json(text)
    for raw in ('"max": 1e400', '"min": -1e400', '"big": 1E5', '"fine": 1.00000000000000000001',
                '"count": 12345678901234567890123', '"zero": -0.0', '"plain": 3', '"name": "1e400"', '0.1'):
        assert raw in text, (raw, text)
    assert data["mcpServers"]["cerebro-wiki"] == {"command": ENTRY.command, "args": list(ENTRY.args)}
    assert data["mcpServers"]["other"]["command"] == "x"


def test_a_json_config_holding_what_strict_json_refuses_is_left_as_it_is(tmp_path: Path) -> None:
    """A file holding NaN or Infinity is not JSON to the clients' parsers, so it is refused, as any other file that
    does not read, and nothing is written."""
    cursor = next(c for c in mcp_clients.clients(tmp_path, "linux", {}) if c.app == mcp_clients.CURSOR)
    cursor.path.parent.mkdir(parents=True)
    cursor.path.write_text('{"limit": Infinity}\n')
    with pytest.raises(mcp_clients.ClientError, match="does not read as JSON"):
        mcp_clients.write(cursor, ENTRY)
    assert cursor.path.read_text() == '{"limit": Infinity}\n'


# ---- init off a terminal with no answer: exit 2, nothing written -------------------------------------------------------


@pytest.mark.parametrize("args", [[], ["--plain"], ["--workspace", "{root}"]], ids=["no flag", "plain", "workspace"])
def test_off_a_terminal_init_given_no_answer_is_refused_with_exit_2_and_writes_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    """Off a terminal init asks nothing, so a new workspace needs an answer for each question
    it would ask, and with none it names them, exits 2 and writes nothing, not even the folder."""
    monkeypatch.setattr("cerebro.clock.local_zone", lambda *a, **k: "Europe/Berlin")
    root = tmp_path / "wiki"
    words = [w.replace("{root}", str(root)) for w in args]
    got = CliRunner().invoke(cli.main, ["init", *([] if "--workspace" in words else [str(root)]), *words])
    assert got.exit_code == 2, got.output
    assert got.output.splitlines()[-1].startswith(
        "Error: init needs an answer for each question it would ask: run it in a terminal to be asked, or pass each "
        "of --daily-usd <dollars> or --no-cap, and --timezone <zone>, as cerebro init ")
    assert got.output.rstrip().endswith("--timezone Europe/Berlin")
    assert not root.exists() and not (tmp_path / "config").exists()


def test_off_a_terminal_init_given_an_answer_writes_the_workspace(tmp_path: Path) -> None:
    got = CliRunner().invoke(cli.main, ["init", str(tmp_path / "wiki"), "--timezone", "UTC"])
    assert got.exit_code == 0, got.output
    assert (tmp_path / "wiki" / "settings.yaml").is_file()


# ---- the ends of input at init's own lines -----------------------------------------------------------------------


def plain_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr("cerebro.clock.local_zone", lambda *a, **k: "Europe/Berlin")


@pytest.mark.parametrize(("given", "name", "email"), [
    ("", "", ""), ("Ann Lee\n", "Ann Lee", ""), ("Ann Lee\nann@quillmoor.example\n", "Ann Lee", "ann@quillmoor.example"),
    ("Ann Lee\nann@quillmoor.example\nQuillmoor\n\n7\n", "Ann Lee", "ann@quillmoor.example")],
    ids=["at the name", "at the email", "at the org", "at the zone"])
def test_input_that_ends_at_inits_plain_lines_asks_nothing_more_and_lays_the_workspace_down_with_what_it_has(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str, name: str, email: str) -> None:
    """Once the input ends, as with Ctrl-D, it asks nothing more, and init lays the
    workspace down with what it has, each question unanswered taking its default, and exits 0."""
    plain_lines(monkeypatch)
    root = tmp_path / "wiki"
    got = CliRunner().invoke(cli.main, ["init", str(root), "--template", "gtm"], input=given)
    assert got.exit_code == 0, got.output
    assert "Aborted" not in got.output and (root / "settings.yaml").is_file()
    settings = (root / "settings.yaml").read_text()
    assert "timezone: Europe/Berlin" in settings
    assert f"daily_usd: {'7' if '7' in given else '50'}" in settings
    identity = front(root / "profile.md")["identity"] if (root / "profile.md").exists() else {}
    assert (identity.get("name", ""), identity.get("email", [])) == (name, [email] if email else [])


@pytest.mark.parametrize("at", [1, 5, 6], ids=["at the name", "at the budget", "at the zone"])
def test_ctrl_c_at_inits_plain_lines_ends_init_with_130_and_writes_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at: int) -> None:
    """Ctrl-C at any of init's lines ends it with exit 130, and before the workspace is laid
    down nothing is written."""
    import click.termui

    plain_lines(monkeypatch)
    asked = [0]
    real = click.termui._readline_prompt

    def prompt(read: Any, text: str, err: bool) -> str:
        asked[0] += 1
        if asked[0] == at:
            raise KeyboardInterrupt
        return real(read, text, err)

    monkeypatch.setattr(click.termui, "_readline_prompt", prompt)
    root = tmp_path / "wiki"
    got = CliRunner().invoke(cli.main, ["init", str(root), "--template", "gtm"],
                             input="Ann Lee\nann@quillmoor.example\n\n\n5\nUTC\n")
    assert got.exit_code == 130, got.output
    assert "Aborted" not in got.output and not (root / "settings.yaml").exists()


def test_input_that_ends_in_the_walk_asks_no_line_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a line meets the end of input, ask_you asks no line after it and gives what it has, and init's budget
    and zone are not asked but take their defaults."""
    import click

    screen = guided.TerminalScreen(None, lambda line: None)
    asked = []

    def ended(read: Any, text: str, err: bool) -> str:
        asked.append(text)
        raise EOFError

    monkeypatch.setattr(click.termui, "_readline_prompt", ended)
    assert guided.ask_you(screen, guided.You(name="Ann Lee")) == guided.You("Ann Lee", "", "", ())
    assert screen.ended and cli._budget_and_zone(None, False, None, "Europe/Berlin", screen) == (50, "Europe/Berlin")
    assert asked == ["Your work email [Enter to skip]: "]


def test_init_writes_a_daily_cap_of_50_dollars_unless_told_another(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """$50 is the cap init writes, and the default the budget
    question shows."""
    got = CliRunner().invoke(cli.main, ["init", str(tmp_path / "a"), "--timezone", "UTC"])
    assert got.exit_code == 0, got.output
    assert "daily_usd: 50" in (tmp_path / "a" / "settings.yaml").read_text()
    plain_lines(monkeypatch)
    asked = CliRunner().invoke(cli.main, ["init", str(tmp_path / "b"), "--template", "gtm"], input="\n" * 6)
    assert asked.exit_code == 0, asked.output
    assert "Daily budget in dollars [50.0]: " in asked.output
    assert "daily_usd: 50" in (tmp_path / "b" / "settings.yaml").read_text()


def test_the_walk_names_serve_once_while_the_first_run_waits_and_serve_once_an_article_is_written(
        tmp_path: Path) -> None:
    root = new(tmp_path)
    (root / "local").mkdir(exist_ok=True)
    (root / "local" / "notes.md").write_text("A note to read.\n")

    def writes() -> None:
        (root / "articles" / "notes").mkdir(parents=True, exist_ok=True)
        (root / "articles" / "notes" / "a.md").write_text("---\ntitle: A\n---\n\nText.\n")

    for first_run, then in ((lambda: None, "cerebro serve --once"), (writes, "cerebro serve")):
        w, _, _ = walk(root, tmp_path / "home", [])
        w.environ, w.first_run = KEYS, first_run
        w.run()
        assert w.next == then


def test_no_jobs_model_menu_offers_an_embedding_model() -> None:
    from cerebro.facts import EMBED

    for job in ("extraction", "decision", "compilation"):
        _, specs = guided.model_menu("fireworks", job)
        assert specs and not {s.model for s in specs} & {m.split("/", 1)[1] for m in EMBED.values()}
