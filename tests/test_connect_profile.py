"""`cerebro connect` adds who the login is to `profile.md` once the login is kept:
`github_login`, `gong_id` found by your address among Gong's users, `slack_id` beside any other, and the Gmail address
under `email`. Only that line is written; every comment stays. A workspace with no `profile.md` gets none made, and a
lookup that fails never fails the connect. Every provider is a stand-in behind a mock transport; every value is made
up."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from test_connect_lobby import RecordedGithub, lay_out_workspace

from cerebro import cli
from cerebro.connect import ConnectError, _set_profile
from cerebro.connectors.slack import SlackConnector
from cerebro.source import source_of

PROFILE = """---
# who you are
identity:
  name: Sam Rivera  # as colleagues write it
  aliases: []
  email:
    - sam@quillmoor.example
role: ""
---

Say who you are and how you like to be briefed.
"""


class GongUsers(RecordedGithub):
    """GitHub as recorded, and Gong whose key works and whose users come in two pages."""

    def __init__(self, users: int | None = None) -> None:
        super().__init__()
        self.users = users

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.gong.io" and request.url.path == "/v2/users":
            self.asked.append(request)
            if self.users is not None:
                return httpx.Response(self.users, json={"errors": ["stand-in"]})
            if "cursor" not in request.url.params:
                return httpx.Response(200, json={"records": {"cursor": "page-2"}, "users": [
                    {"id": "4400000000000000009", "emailAddress": "someone@quillmoor.example", "active": True}]})
            return httpx.Response(200, json={"records": {}, "users": [
                {"id": "4400000000000000001", "emailAddress": "s.rivera@quillmoor.example",
                 "emailAliases": ["SAM@quillmoor.example"], "active": True}]})
        return super().handle(request)


def connect(root: Path, *args: str, typed: str = "") -> Any:
    return CliRunner().invoke(cli.main, ["connect", *args, "--workspace", str(root)], input=typed)


def test_github_sets_its_login_with_every_other_line_and_comment_kept(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "profile.md").write_text(PROFILE)
    got = connect(root, "code")
    assert got.exit_code == 0, got.output
    assert (root / "profile.md").read_text() == PROFILE.replace(
        "    - sam@quillmoor.example\n", '    - sam@quillmoor.example\n  github_login: "someone"\n')
    assert "profile.md" not in got.output  # GitHub's connect sets it saying nothing


def test_gong_finds_your_user_by_your_address_and_adds_its_id(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch, GongUsers())
    (root / "profile.md").write_text(PROFILE)
    got = connect(root, "gong", "--stdin", typed="madeup-access-AAAA:madeup-secret-BBBB\n")
    assert got.exit_code == 0, got.output
    assert "Found your Gong user by sam@quillmoor.example and added it to profile.md as gong_id." in got.output
    assert 'gong_id: "4400000000000000001"' in (root / "profile.md").read_text()
    assert "# as colleagues write it" in (root / "profile.md").read_text()
    again = connect(root, "gong")
    assert "Your Gong user, found by sam@quillmoor.example, is in profile.md as gong_id already." in again.output


def test_gong_says_what_it_could_not_do_and_still_connects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch, GongUsers(users=500))
    got = connect(root, "gong", "--stdin", typed="madeup-access-AAAA:madeup-secret-BBBB\n")
    assert got.exit_code == 0 and "profile.md has no email to find your Gong user by" in got.output
    (root / "profile.md").write_text(PROFILE)
    failed = connect(root, "gong")
    assert failed.exit_code == 0, failed.output
    assert ("warning: could not look up your Gong user by sam@quillmoor.example, so profile.md gets no gong_id: "
            "gong answered status 500 to the list of users. Run cerebro connect gong again to add it.") in failed.output
    assert (root / "profile.md").read_text() == PROFILE and "madeup" not in failed.output


def test_gmail_adds_its_address_beside_yours_and_no_file_is_made(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    opened = cli.open_workspace

    class Session:
        def login(self, address: str, password: str) -> None:
            pass

        def logout(self) -> None:
            pass

    def open_workspace(r: Path | None) -> Any:
        workspace = opened(r)
        workspace.imap_connect = lambda host: Session()
        return workspace

    monkeypatch.setattr(cli, "open_workspace", open_workspace)
    none = connect(root, "gmail", "--stdin", typed="Pat@quillmoor.example:madeup app pass word\n")
    assert none.exit_code == 0 and "There is no profile.md to add your Gmail address to." in none.output
    assert not (root / "profile.md").exists()
    (root / "profile.md").write_text(PROFILE)
    added = connect(root, "gmail", "--stdin", typed="Pat@quillmoor.example:madeup app pass word\n")
    assert "Added your Gmail address to profile.md." in added.output
    assert '    - sam@quillmoor.example\n    - "pat@quillmoor.example"\n' in (root / "profile.md").read_text()
    again = connect(root, "gmail", "--stdin", typed="pat@QUILLMOOR.example:madeup app pass word\n")
    assert "Your Gmail address is in profile.md already." in again.output


def test_slack_finds_your_user_id_with_auth_test() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth.test")
        return httpx.Response(200, json={"ok": True, "user_id": "U0MADEUP1", "team": "Quillmoor"})

    found = SlackConnector.find_you(source_of("chat", "kind: slack\n"), "xoxp-madeup",
                                    http=httpx.Client(transport=httpx.MockTransport(handle)),
                                    now=lambda: datetime(2026, 11, 19, tzinfo=UTC))
    assert found == ("U0MADEUP1", "U0MADEUP1")


@pytest.mark.parametrize(("before", "key", "value", "joins", "after"), [
    ("---\nidentity:\n  email: []\n---\nbody\n", "slack_id", "U1", True,
     '---\nidentity:\n  email: []\n  slack_id: ["U1"]\n---\nbody\n'),
    ("---\nidentity:\n  slack_id: [U1]  # the first\n---\n", "slack_id", "U2", True,
     '---\nidentity:\n  slack_id: ["U1", "U2"]  # the first\n---\n'),
    ("---\nrole: x\n---\n", "gong_id", "44", False, '---\nrole: x\nidentity:\n  gong_id: "44"\n---\n'),
    ("---\r\nidentity:\r\n    gong_id: '1'\r\n---\r\n", "gong_id", "44", False,
     '---\r\nidentity:\r\n    gong_id: "44"\r\n---\r\n'),
])
def test_one_line_of_profile_md_is_written(tmp_path: Path, before: str, key: str, value: str, joins: bool,
                                           after: str) -> None:
    path = tmp_path / "profile.md"
    path.write_bytes(before.encode())
    path.chmod(0o640)
    assert _set_profile(path, key, value, joins)
    assert path.read_bytes() == after.encode() and path.stat().st_mode & 0o777 == 0o640
    assert not _set_profile(path, key, value, joins)


def test_only_the_value_changes_and_every_comment_stays(tmp_path: Path) -> None:
    path = tmp_path / "profile.md"
    path.write_text("---\nidentity:\n  slack_id:\n    - U0OLD1\n    # second workspace\n    - U0OLD2\n    # below\n"
                    "  github_login: oldname  # my own\n---\n")
    assert _set_profile(path, "slack_id", "U0NEW", True) and _set_profile(path, "github_login", "newname", False)
    assert path.read_text() == ('---\nidentity:\n  slack_id:\n    - U0OLD1\n    # second workspace\n    - U0OLD2\n'
                                '    - "U0NEW"\n    # below\n  github_login: "newname"  # my own\n---\n')


def test_a_linked_profile_is_written_through_its_link(tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    target = tmp_path / "shared" / "me.md"
    target.write_text("---\nidentity:\n  email: []\n---\n")
    (tmp_path / "profile.md").symlink_to(target)
    assert _set_profile(tmp_path / "profile.md", "gong_id", "44", False)
    assert (tmp_path / "profile.md").is_symlink() and 'gong_id: "44"' in target.read_text()


@pytest.mark.parametrize("text", ["---\nidentity:\n  slack_id: [U0OLD1, 2024-01-01]\n---\n",
                                  '---\nidentity:\n  "slack_id": [U0OLD1]\n---\n',
                                  "---\nidentity:\n  slack_id: {a: 1}\n---\n"])
def test_a_value_that_is_not_text_or_a_key_written_otherwise_is_left_by_hand(tmp_path: Path, text: str) -> None:
    path = tmp_path / "profile.md"
    path.write_text(text)
    with pytest.raises(ConnectError, match="by hand"):
        _set_profile(path, "slack_id", "U0NEW", True)
    assert path.read_text() == text


def test_a_profile_that_cannot_take_the_line_in_place_is_left_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "profile.md"
    path.write_text("---\nidentity: {email: [sam@quillmoor.example]}\n---\n")
    with pytest.raises(ConnectError, match="write gong_id: \"44\" under identity: by hand"):
        _set_profile(path, "gong_id", "44", False)
    assert path.read_text() == "---\nidentity: {email: [sam@quillmoor.example]}\n---\n"
    with pytest.raises(FileNotFoundError):
        _set_profile(tmp_path / "none.md", "gong_id", "44", False)


def test_a_profile_that_fails_after_the_login_is_kept_is_a_warning_not_a_failed_connect(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import secrets as secrets_module

    root = lay_out_workspace(tmp_path, monkeypatch, GongUsers())
    (root / "profile.md").write_text(PROFILE)
    real_read, real_bytes, real_set = Path.read_text, Path.read_bytes, secrets_module.FileSecrets.set

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "profile.md":
            raise PermissionError(13, "Permission denied (stand-in)", str(self))
        return real_read(self, *args, **kwargs)

    def no_bytes(self: Path) -> bytes:
        if self.name == "profile.md":
            raise PermissionError(13, "Permission denied (stand-in)", str(self))
        return real_bytes(self)

    def set_then_lock(self: Any, name: str, value: str) -> None:
        real_set(self, name, value)
        monkeypatch.setattr(Path, "read_text", unreadable)
        monkeypatch.setattr(Path, "read_bytes", no_bytes)

    monkeypatch.setattr(secrets_module.FileSecrets, "set", set_then_lock)
    got = connect(root, "gong", "--stdin", typed="madeup-access-AAAA:madeup-secret-BBBB\n")
    monkeypatch.setattr(Path, "read_text", real_read)
    monkeypatch.setattr(Path, "read_bytes", real_bytes)
    assert got.exit_code == 0, got.output
    assert "nothing was kept" not in got.output and "warning: profile.md does not read" in got.output
    assert (root / "sources" / "gong.yaml").exists()
