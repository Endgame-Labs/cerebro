"""`cerebro forget`, `cerebro secrets list` and `cerebro secrets unset`, on the file store and a stand-in keychain. Each
test has its own config folder and home, so none reaches the person's keychain or config."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

import cerebro.cli
import cerebro.secrets
import cerebro.trust
from cerebro.cli import main
from cerebro.secrets import claims, open_secrets, secrets_dir

VALUES = {"fireworks": "fw-test-0123456789", "typesafe": "ts-test-9876543210", "gong": "gong-test-5555555555"}


class MemoryKeyring(KeyringBackend):
    """A keychain in a dict, so no test reaches the real one."""

    priority = 5

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise PasswordDeleteError(username)
        del self.items[(service, username)]


@pytest.fixture(autouse=True)
def own_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CEREBRO_SECRETS_STORE", raising=False)


@pytest.fixture(params=["file", "keychain"])
def keychain(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring | None:
    """The stand-in keychain, or None where the file store is used."""
    if request.param == "file":
        return None
    backend = MemoryKeyring()
    monkeypatch.setattr(cerebro.secrets, "_default_backend", lambda: backend)
    return backend


def run(*args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(main, list(args), input=input)


def make(folder: Path, *names: str) -> str:
    """A workspace made with init, with each named secret kept, and its id."""
    assert run("init", str(folder), "--daily-usd", "1").exit_code == 0
    for name in names:
        kept = run("secrets", "set", name, "--workspace", str(folder), input=VALUES[name] + "\n")
        assert kept.exit_code == 0, kept.output
    return cerebro.trust.settings_id(folder)


def kept(workspace_id: str, keychain: MemoryKeyring | None) -> list[str]:
    """The names held for an id, read as the store keeps them."""
    if keychain is None:
        path = secrets_dir() / f"{workspace_id}.json"
        return sorted(json.loads(path.read_text())) if path.exists() else []
    return sorted(user for service, user in keychain.items if service == f"cerebro:{workspace_id}")


def recorded(folder: Path) -> bool:
    return any(entry["path"] == str(folder.resolve()) for entry in cerebro.trust.records())


def at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cerebro.cli, "_asking", lambda: True)


def no_value_in(*outputs: str) -> None:
    for output in outputs:
        for value in VALUES.values():
            assert value not in output


# -- a workspace that is there


def test_forget_removes_a_workspaces_secrets_claim_and_trust_and_leaves_its_folder_and_another_workspace_alone(
        tmp_path: Path, keychain: MemoryKeyring | None) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first_id, second_id = make(first, "fireworks", "typesafe"), make(second, "gong")
    if keychain is not None:
        assert kept(first_id, keychain) == [".names", "fireworks", "typesafe"]
    unrelated = ("someone-else", "token")
    if keychain is not None:
        keychain.items[unrelated] = "not cerebro's"
    files = sorted(p.relative_to(first) for p in first.rglob("*"))
    done = run("forget", str(first), "--yes")
    assert done.exit_code == 0, done.output
    lines = done.output.splitlines()
    assert lines[0] == f"folder   {first.resolve()}, id {first_id}"
    assert lines[1].startswith("secrets  fireworks, typesafe in ")
    assert lines[-1] == ("left     an MCP client entry you added from the line init printed: run claude mcp remove "
                         "--scope user cerebro-first for Claude Code, or delete cerebro-first under mcpServers in "
                         "Claude Desktop's or Cursor's config")
    assert f"left     the folder {first.resolve()} and its files stay as they were" in lines
    assert kept(first_id, keychain) == [] and first_id not in claims() and not recorded(first)
    assert sorted(p.relative_to(first) for p in first.rglob("*")) == files
    assert kept(second_id, keychain) == ([".names", "gong"] if keychain is not None else ["gong"])
    assert claims()[second_id] == str(second.resolve()) and recorded(second)
    assert run("secrets", "list", "--workspace", str(second)).output.splitlines()[0] == "secret   gong"
    if keychain is not None:
        assert keychain.items[unrelated] == "not cerebro's"
    no_value_in(done.output)


@pytest.mark.parametrize("now", ["file", ""])
def test_forget_finds_and_removes_secrets_in_every_store_whatever_the_store_setting_says_now(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: str) -> None:
    backend = MemoryKeyring()
    monkeypatch.setattr(cerebro.secrets, "_default_backend", lambda: backend)
    root = tmp_path / "b"
    workspace_id = make(root, "fireworks")
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    assert run("secrets", "set", "gong", "--workspace", str(root), input=VALUES["gong"] + "\n").exit_code == 0
    service, path = f"cerebro:{workspace_id}", secrets_dir() / f"{workspace_id}.json"
    assert kept(workspace_id, backend) == [".names", "fireworks"] and kept(workspace_id, None) == ["gong"]
    if not now:
        monkeypatch.delenv("CEREBRO_SECRETS_STORE")
    done = run("forget", str(root), "--yes")
    assert done.exit_code == 0, done.output
    lines = done.output.splitlines()
    assert f"secrets  fireworks in the os keychain (service {service})" in lines
    assert f"secrets  gong in {path}" in lines
    assert f"removed  secrets fireworks from the os keychain (service {service})" in lines
    assert f"removed  secrets gong from {path}" in lines
    assert kept(workspace_id, backend) == [] and kept(workspace_id, None) == []
    no_value_in(done.output)


class BrokenKeyring(MemoryKeyring):
    def get_password(self, service, username):
        raise RuntimeError(VALUES["fireworks"])


def test_forget_says_in_one_line_that_a_keychain_cannot_be_read_and_removes_the_rest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, other = tmp_path / "b", tmp_path / "c"
    workspace_id, _ = make(root, "gong"), make(other, "gong")
    monkeypatch.setattr(cerebro.secrets, "_default_backend", BrokenKeyring)
    done = run("forget", str(root), "--yes")
    assert done.exit_code == 0, done.output
    assert done.output.count("unread   ") == 1 and (
        f"unread   the os keychain (service cerebro:{workspace_id}) could not be read, so it was not looked in"
        in done.output.splitlines())
    assert kept(workspace_id, None) == [] and workspace_id not in claims()
    no_value_in(done.output)
    monkeypatch.setattr(cerebro.secrets, "_default_backend", lambda: (_ for _ in ()).throw(RuntimeError("no dbus")))
    again = run("forget", str(other), "--yes")
    assert again.exit_code == 0 and "unread   the os keychain could not be reached (RuntimeError)" in again.output


def test_forget_asks_first_and_the_answer_no_removes_nothing(tmp_path: Path, keychain: MemoryKeyring | None,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = make(tmp_path / "b", "fireworks")
    at_a_terminal(monkeypatch)
    said = run("forget", str(tmp_path / "b"), input="n\n")
    assert said.exit_code == 0 and "Its secrets cannot be got back. [y/N]: n" in said.output
    assert said.output.splitlines()[-1] == "nothing  removed"
    assert "fireworks" in kept(workspace_id, keychain) and workspace_id in claims() and recorded(tmp_path / "b")
    yes = run("forget", str(tmp_path / "b"), input="y\n")
    assert yes.exit_code == 0 and kept(workspace_id, keychain) == [] and not recorded(tmp_path / "b")


def test_off_a_terminal_without_yes_forget_removes_nothing_and_says_so(tmp_path: Path,
                                                                       keychain: MemoryKeyring | None) -> None:
    workspace_id = make(tmp_path / "b", "fireworks")
    said = run("forget", "--workspace", str(tmp_path / "b"))
    assert said.exit_code == 1
    assert said.output.splitlines()[-1] == ("Error: nothing removed: forget asks before it removes anything, and "
                                            "there is no terminal to ask at: run it at a terminal, or add --yes")
    assert "fireworks" in kept(workspace_id, keychain) and workspace_id in claims() and recorded(tmp_path / "b")


def test_forget_runs_on_the_current_folder_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = make(tmp_path / "b", "fireworks")
    monkeypatch.chdir(tmp_path / "b")
    assert run("forget", "--yes").exit_code == 0
    assert kept(workspace_id, None) == [] and workspace_id not in claims()


def test_a_half_finished_forget_can_be_run_again(tmp_path: Path, keychain: MemoryKeyring | None) -> None:
    workspace_id = make(tmp_path / "b", "fireworks", "typesafe")
    if keychain is not None:  # stopped after one item went, its name still listed
        del keychain.items[(f"cerebro:{workspace_id}", "fireworks")]
    else:  # stopped after the secrets went
        (secrets_dir() / f"{workspace_id}.json").unlink()
    again = run("forget", str(tmp_path / "b"), "--yes")
    assert again.exit_code == 0, again.output
    assert kept(workspace_id, keychain) == [] and workspace_id not in claims() and not recorded(tmp_path / "b")
    third = run("forget", str(tmp_path / "b"), "--yes")
    assert third.exit_code == 0
    assert "nothing  this workspace keeps nothing outside its folder that forget removes" in third.output.splitlines()


@pytest.mark.untrusted
def test_forget_needs_no_trust_and_runs_nothing_of_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "cloned"
    root.mkdir()
    (root / "settings.yaml").write_text("id: cloned-1\nstore:\n  class: not_a_module_anywhere:Store\n")
    (root / "extensions.py").write_text(f"open({str(tmp_path / 'RAN')!r}, 'w')\n")
    open_secrets("cloned-1", root=root).set("gong", VALUES["gong"])
    assert run("secrets", "list", "--workspace", str(root)).exit_code == 1  # untrusted: a list opens the workspace
    done = run("forget", str(root), "--yes")
    assert done.exit_code == 0, done.output
    assert kept("cloned-1", None) == [] and "cloned-1" not in claims()
    assert not (tmp_path / "RAN").exists() and not (root / ".cerebro").exists()


def test_forget_refuses_a_folder_that_is_not_a_workspace(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    said = run("forget", str(tmp_path / "plain"), "--yes")
    assert said.exit_code == 1 and said.output.strip() == (
        f"Error: {(tmp_path / 'plain').resolve()} is not a workspace (no settings.yaml in it), so forget removes "
        "nothing: name the folder that holds settings.yaml")


def test_what_a_workspaces_files_say_is_printed_with_its_control_characters_shown(tmp_path: Path) -> None:
    root = tmp_path / "evil\x1b[2Jworkspace"
    root.mkdir()
    (root / "settings.yaml").write_text("id: evil-1\n")
    open_secrets("evil-1", root=root).set("fireworks", VALUES["fireworks"])
    done = run("forget", str(root), "--yes")
    assert done.exit_code == 0 and "\x1b" not in done.output and "evil\\x1b[2Jworkspace" in done.output


# -- a copy, and a workspace that moved


def test_forgetting_a_copy_keeps_the_secrets_and_claim_of_the_workspace_it_was_copied_from(
        tmp_path: Path, keychain: MemoryKeyring | None) -> None:
    original, copy = tmp_path / "original", tmp_path / "copy"
    workspace_id = make(original, "fireworks")
    shutil.copytree(original, copy)
    done = run("forget", str(copy), "--yes")
    assert done.exit_code == 0, done.output
    assert f"keep     the secrets of id {workspace_id}: {original.resolve()} holds the same id" in done.output
    assert "fireworks" in kept(workspace_id, keychain) and claims()[workspace_id] == str(original.resolve())
    assert recorded(original)


def test_forgetting_a_workspace_keeps_its_secrets_while_a_trusted_folder_still_holds_the_same_id(
        tmp_path: Path, keychain: MemoryKeyring | None) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    workspace_id = make(first, "fireworks")
    shutil.copytree(first, second)
    cerebro.trust.trust(second)
    done = run("forget", str(first), "--yes")
    assert done.exit_code == 0
    assert "fireworks" in kept(workspace_id, keychain) and not recorded(first) and recorded(second)


def test_a_moved_workspace_is_forgotten_at_its_new_folder_with_the_claim_its_old_folder_left(
        tmp_path: Path, keychain: MemoryKeyring | None) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    workspace_id = make(old, "fireworks")
    old.rename(new)
    done = run("forget", str(new), "--yes")
    assert done.exit_code == 0, done.output
    assert kept(workspace_id, keychain) == [] and workspace_id not in claims()


# -- what deleted workspaces left behind


def test_with_no_workspace_here_forget_finds_what_a_deleted_workspace_left_and_removes_it_on_a_yes(
        tmp_path: Path, keychain: MemoryKeyring | None, monkeypatch: pytest.MonkeyPatch) -> None:
    gone, living = tmp_path / "gone", tmp_path / "living"
    gone_id, living_id = make(gone, "fireworks"), make(living, "gong")
    shutil.rmtree(gone)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    listed = run("forget")
    assert listed.exit_code == 0
    unread = [] if keychain else ["  unread   this machine has no os keychain Cerebro can use, so none was looked in"]
    assert listed.output.splitlines()[:5 + len(unread)] == [
        "left     1 workspace no longer at its recorded folder left things behind",
        f"folder   id {gone_id}, recorded at {gone.resolve()}, which is gone",
        f"  secrets  fireworks in {listed.output.split(' in ', 1)[1].splitlines()[0]}", *unread,
        f"  claim    id {gone_id} for {gone.resolve()}, in {secrets_dir() / '.folders.json'}",
        f"  trust    the record of {gone.resolve()}, in {cerebro.trust.trusted_path()}",
    ]
    assert listed.output.splitlines()[-1].startswith("kept     nothing removed: cerebro cannot tell a folder that moved")
    assert "fireworks" in kept(gone_id, keychain) and run("forget", "--yes").output == listed.output
    at_a_terminal(monkeypatch)
    no = run("forget", input="n\n")
    assert "  kept     nothing removed" in no.output and "fireworks" in kept(gone_id, keychain)
    yes = run("forget", input="y\n")
    assert yes.exit_code == 0 and "  removed  secrets fireworks from " in yes.output
    assert kept(gone_id, keychain) == [] and gone_id not in claims() and not recorded(gone)
    assert "gong" in kept(living_id, keychain) and claims()[living_id] == str(living.resolve()) and recorded(living)
    assert run("forget").output == "nothing  no workspace whose folder is gone left anything behind\n"
    no_value_in(listed.output, yes.output)


def test_forget_names_a_deleted_folder_and_finds_what_that_workspace_alone_left(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one_id, two_id = make(one, "fireworks"), make(two, "gong")
    shutil.rmtree(one)
    shutil.rmtree(two)
    at_a_terminal(monkeypatch)
    done = run("forget", str(one), input="y\n")
    assert done.exit_code == 0 and done.output.splitlines()[0].startswith("left     1 workspace")
    assert kept(one_id, None) == [] and kept(two_id, None) == ["gong"]
    again = run("forget", str(one))
    assert again.exit_code == 1 and "nothing is left behind by a workspace at" in again.output


def test_a_moved_workspace_whose_new_folder_is_trusted_keeps_its_secrets_when_its_old_folder_is_cleared(
        tmp_path: Path, keychain: MemoryKeyring | None, monkeypatch: pytest.MonkeyPatch) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    workspace_id = make(old, "fireworks")
    old.rename(new)
    cerebro.trust.trust(new)
    monkeypatch.chdir(tmp_path)
    at_a_terminal(monkeypatch)
    done = run("forget", input="y\n")
    assert f"  keep     the secrets of id {workspace_id}: {new.resolve()} holds the same id" in done.output
    assert "fireworks" in kept(workspace_id, keychain) and not recorded(old) and recorded(new)


def test_secrets_whose_id_no_folder_records_are_offered_with_their_folder_unknown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    open_secrets("orphan-1").set("gong", VALUES["gong"])
    monkeypatch.chdir(tmp_path)
    at_a_terminal(monkeypatch)
    done = run("forget", input="y\n")
    assert "folder   id orphan-1, folder unknown" in done.output and kept("orphan-1", None) == []


# -- secrets list and unset


def test_secrets_list_names_each_secret_and_where_and_unset_removes_one(tmp_path: Path,
                                                                        keychain: MemoryKeyring | None) -> None:
    root = tmp_path / "b"
    workspace_id = make(root, "fireworks", "typesafe")
    listed = run("secrets", "list", "--workspace", str(root))
    where = "the os keychain (service cerebro:" + workspace_id + ")" if keychain else str(secrets_dir() / f"{workspace_id}.json")
    assert listed.output.splitlines() == ["secret   fireworks", "secret   typesafe", f"kept     2 secrets in {where}"]
    assert run("secrets", "unset", "fireworks", "--workspace", str(root)).output == f"unset    fireworks in {where}\n"
    assert run("secrets", "unset", "fireworks", "--workspace", str(root)).output == (
        "unset    fireworks was not set, so nothing was removed\n")
    assert kept(workspace_id, keychain) == ([".names", "typesafe"] if keychain else ["typesafe"])
    bad = run("secrets", "unset", "a=b", "--workspace", str(root))
    assert bad.exit_code == 2 and "a=b" not in bad.output  # a usage error
    no_value_in(listed.output)


def test_secrets_list_names_a_secret_in_a_store_the_setting_does_not_pick_and_unset_removes_it_from_every_store(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryKeyring()
    monkeypatch.setattr(cerebro.secrets, "_default_backend", lambda: backend)
    root = tmp_path / "b"
    workspace_id = make(root, "fireworks")
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    assert run("secrets", "set", "fireworks", "--workspace", str(root), input=VALUES["typesafe"] + "\n").exit_code == 0
    path, service = secrets_dir() / f"{workspace_id}.json", f"cerebro:{workspace_id}"
    listed = run("secrets", "list", "--workspace", str(root))
    assert listed.output.splitlines() == [
        "secret   fireworks", f"kept     1 secret in {path}",
        (f"also     fireworks in the os keychain (service {service}), which this workspace does not read now: "
         "cerebro secrets unset removes a name from every store")]
    unset = run("secrets", "unset", "fireworks", "--workspace", str(root))
    assert unset.output.splitlines() == [f"unset    fireworks in {path}",
                                         f"unset    fireworks in the os keychain (service {service})"]
    assert kept(workspace_id, backend) == [] and kept(workspace_id, None) == []
    no_value_in(listed.output, unset.output)
