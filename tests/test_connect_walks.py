"""`cerebro connect <source>`: each source says what to make and where before its first
question, in its connector's own words; Slack prints its app manifest; Calendar asks where the calendar lives; Gmail
offers your address from profile.md; Linear asks which way in and a new Granola source signs in through the browser;
a Gong key is proven before it is kept; `connect local` adds its file; an empty first answer, or Escape at a choice,
ends done; and the plain walk and the lobby end with a last line that names a true next step.

Every provider is a stand-in: Gong's list of calls behind a mock transport, Linear's MCP server and its authorization
server in this process with a fake browser, and a sign-in that is never run where the browser step is replaced. No
key, no login and no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from test_connect import FileSecrets, never, open_workspace, signing, store  # noqa: F401 - fixtures
from test_connect_lobby import RecordedGithub, in_the_lobby, lay_out_workspace, printer
from test_mcp_oauth import Browser, loopback_only  # noqa: F401 - a fixture

from cerebro import cli
from cerebro.connect import ConnectBack, ConnectLater, LoginRefused, Tell, connect, connecting, new_source
from cerebro.connect_screen import CONNECTED, NOT_ADDED, NOT_CONNECTED, explained
from cerebro.connectors.connecting import Way
from cerebro.connectors.linear import LinearConnector
from cerebro.connectors.mcp_oauth import Login
from cerebro.output import strip_escapes

# The Slack app manifest connect prints.
EXPECTED_MANIFEST = """display_information:
  name: Cerebro
oauth_config:
  scopes:
    user:
      - channels:read
      - channels:history
      - groups:read
      - groups:history
      - users:read
      - users:read.email
settings:
  token_rotation_enabled: false
"""


class Told(Tell):
    """A person at a terminal, scripted: what each source said, and the way picked at a choice, or None."""

    def __init__(self, drawn: bool = False, pick: int | None = 0):
        self.drawn, self.pick, self.said, self.chosen = drawn, pick, [], []

    def explain(self, lines: Any, block: str = "", after: Any = ()) -> None:
        self.said += [*lines, *([block] if block else []), *after]

    def choose(self, title: str, lines: Any, ways: Any) -> int | None:
        self.said += list(lines)
        self.chosen.append((title, [w.title for w in ways]))
        return self.pick


def answering(*answers: str) -> tuple[list[tuple[str, bool]], Any]:
    asked: list[tuple[str, bool]] = []
    given = iter(answers)

    def ask(label: str, hidden: bool) -> str:
        asked.append((label, hidden))
        return next(given, "")

    return asked, ask


def at_a_plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli, "_asking", lambda: True)


# ---- what each source says before it asks, and an empty first answer


@pytest.mark.parametrize(("args", "typed", "says", "asks", "later"), [
    (["gong"], "\n", "Gong keys are made by a Gong tech admin. Send yours this:",
     "Gong access key, or Enter to connect later",
     "Gong is not connected yet. Run cerebro connect gong when you have the key and secret."),
    (["code", "--replace"], "\n", ("Make a fine-grained personal access token at "
     "https://github.com/settings/personal-access-tokens/new"), "GitHub token, or Enter to connect later",
     "GitHub is not connected yet. Run cerebro connect code when you have a token."),
    (["gmail"], "\n", "Create an app password at myaccount.google.com/apppasswords. 2-Step Verification must be on.",
     "Gmail address", ("Gmail is not connected yet. Run cerebro connect gmail when you have the address and an app "
     "password.")),
    (["calendar"], "\n", ("In Google Calendar, open Settings, pick the calendar, then Integrate calendar, and copy the "
     "secret address in iCal format."), "Secret address",
     "The calendar is not connected yet. Run cerebro connect calendar when you have its secret address."),
    (["chat"], "\n", ("connect chat reads Slack with your own user token, from an app you create in your workspace "
     "from this manifest:"), "Slack user token (xoxp-...), or Enter to connect later",
     "Slack is not connected yet. Run cerebro connect chat when you have the token."),
    (["linear"], "k\n\n", "Make a personal API key in Linear under Settings > ", "Linear API key, or Enter to connect later",
     "Linear is not connected yet. Run cerebro connect linear when you have a key."),
])
def test_each_source_says_what_to_make_and_where_before_its_first_question_and_an_empty_answer_ends_done(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], typed: str, says: str, asks: str,
        later: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", *args, "--workspace", str(root)], input=typed)
    assert got.exit_code == 0, got.output
    assert says in got.output and asks in got.output and got.output.index(says) < got.output.index(asks)
    said, last = got.output.splitlines()[-2:]
    assert said == later and last.startswith("[!] Done with warnings. Check: ")
    assert last.removeprefix("[!] Done with warnings. Check: ").casefold() == \
        later.replace(". Run ", ": run ").removesuffix(".").casefold()
    assert not (root / "sources" / f"{args[0]}.yaml").exists() or args[0] in ("code", "chat")


def test_what_each_source_says_lives_in_its_own_connector_and_connects_shared_code_names_none() -> None:
    import inspect
    import re

    import cerebro.connect as engine
    import cerebro.connect_screen as screen
    from cerebro.connectors import CONNECTORS

    said = {kind for kind in CONNECTORS if connecting(kind) is not None}
    assert said == {"calendar", "github", "gmail", "gong", "granola", "linear", "slack"}
    added = [engine.Tell, engine.ConnectBack, engine.ConnectLater, engine.connecting, engine._tell, engine._later,
             engine._with_auth, engine._takes_auth, engine._asked, engine.set_auth, engine.starter, engine.count,
             engine.add_source, engine._closing, cli._Tell, cli._start, cli._retry, cli._failed_line,
             cli._WaitsToRun, cli._NoSource, cli._table, cli._connect, screen.explained, screen.accented,
             screen.ways_menu, screen.plain_ways, screen.way_of]
    code = [inspect.getsource(part) for part in added]
    code.append(inspect.getsource(engine._Check.__call__).split("assert isinstance(value, str)")[1])  # past the check
    code.append(engine.__doc__.split("At a terminal, each source says")[1].split("\n\n")[0])
    names = re.compile(r"\b(" + "|".join([*CONNECTORS, "local"]) + r")\b", re.IGNORECASE)
    found = [names.search(text).group(0) for text in code if names.search(text)]
    assert found == []


# ---- the app manifest


def test_connect_slack_prints_its_manifest_and_its_three_steps_then_asks_for_the_token(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connectors.slack import MANIFEST

    assert MANIFEST == EXPECTED_MANIFEST
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "chat", "--workspace", str(root)], input="\n")
    out = got.output
    assert "\n" + EXPECTED_MANIFEST in out  # at the margin
    steps = [out.index(f"\n{n}. ") for n in (1, 2, 3)]
    assert out.index("display_information:") < steps[0] < steps[1] < steps[2] < out.index("Slack user token (xoxp-")
    told = Told(drawn=True)
    (tmp_path / "drawn").mkdir()
    with open_workspace(tmp_path / "drawn", FileSecrets(tmp_path / "drawn-secrets", "workspace-test"),
                    {"chat": "kind: slack\n"}) as workspace, pytest.raises(ConnectLater):
        connect(workspace, "chat", ask=answering("")[1], tell=told)
    assert told.said[0].startswith("Install a custom app on your Slack workspace using the app manifest below")
    assert EXPECTED_MANIFEST in told.said and told.said[-2].endswith("anytime with `cerebro connect chat`.")
    p = printer(dark=False)
    drawn = explained(p, told.said[:2], EXPECTED_MANIFEST, told.said[3:])
    assert strip_escapes("\n".join(drawn)).count("users:read.email") == 1 and drawn[0] != strip_escapes(drawn[0])


# ---- where a feed lives, and Escape at that choice


def test_calendar_asks_which_provider_first_then_says_where_that_one_keeps_the_secret_address(
        tmp_path: Path, store: FileSecrets) -> None:  # noqa: F811
    told = Told(drawn=True, pick=1)
    asked, ask = answering("")
    with open_workspace(tmp_path, store, {"calendar": "kind: calendar\n"}) as workspace, pytest.raises(ConnectLater) as later:
        connect(workspace, "calendar", ask=ask, tell=told)
    assert told.chosen == [("Calendar", ["Google Calendar", "Outlook Calendar (web-based)",
                                         "Other calendar (ICS link)"])]
    assert told.said[0] == ("Open `Settings > Calendar > Shared calendars`, publish the calendar, then copy its "
                            "`ICS link`.")
    assert asked == [("ICS link", True)] and str(later.value).startswith("The calendar is not connected yet.")
    back = Told(drawn=True, pick=None)
    (tmp_path / "back").mkdir()
    with open_workspace(tmp_path / "back", FileSecrets(tmp_path / "back-secrets", "workspace-test"),
                    {"calendar": "kind: calendar\n"}) as workspace, pytest.raises(ConnectBack):
        connect(workspace, "calendar", ask=never, tell=back)


def test_escape_at_calendars_first_choice_ends_connect_done_with_nothing_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import checklist

    root = lay_out_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(cli, "_drawing", lambda plain: printer())
    monkeypatch.setattr(checklist, "choose", lambda menu, p, cursor=0: checklist.MENU_DONE)
    got = CliRunner().invoke(cli.main, ["connect", "calendar", "--workspace", str(root)])
    assert got.exit_code == 0, got.output
    assert strip_escapes(got.output).splitlines()[-1] == "✓ Done."
    assert not (root / "sources" / "calendar.yaml").exists()


# ---- an address offered from profile.md


def test_gmail_offers_the_address_in_profile_md_and_an_empty_answer_takes_it(
        tmp_path: Path, store: FileSecrets) -> None:  # noqa: F811
    from gmail_standin import StandIn as MailStandIn

    box = MailStandIn()
    asked, ask = answering("", "abcd efgh ijkl mnop")
    workspace = open_workspace(tmp_path, store, {"mail": "kind: gmail\n"})
    (workspace.root / "profile.md").write_text("---\nidentity:\n  email: [Ana@Quillmoor.example]\n---\n")
    workspace.close()
    from cerebro import Cerebro
    from cerebro.fakes import FakeExtraction

    with Cerebro(workspace.root, extraction=FakeExtraction(), secrets=store) as workspace:
        workspace.imap_connect = lambda host: box
        done = connect(workspace, "mail", ask=ask, tell=Told(drawn=False))
        assert asked == [("Gmail address [ana@quillmoor.example, from profile.md]", False),
                         ("Gmail app password", True)]
        assert done.who == "ana@quillmoor.example"
        asked, ask = answering("", "")
        with pytest.raises(ConnectLater, match="Gmail is not connected yet"):
            connect(workspace, "mail", ask=ask, tell=Told(drawn=True), replace=True)
        assert asked[0] == ("Email address [ana@quillmoor.example, from profile.md]", False)


# ---- the ways in


def test_linear_asks_which_way_in_and_the_browser_way_says_so_in_what_it_returns(
        tmp_path: Path, store: FileSecrets, signing: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    from test_connect import McpClient, replace

    standin, guarded, _ = signing
    monkeypatch.setattr(LinearConnector, "mcp_server", standin.server)
    told = Told(drawn=False, pick=0)
    with open_workspace(tmp_path, store, {"linear": "kind: linear\nteams: [ENG]\n"},
                    http=httpx.Client(timeout=5.0)) as workspace:
        workspace.mcp_open = lambda where: McpClient(replace(where, transport=guarded))
        done = connect(workspace, "linear", ask=never, open_browser=Browser(), tell=told)
    assert told.chosen == [("Linear", ["Sign in through the browser", "Paste an API key"])]
    assert told.said[0].startswith("Sign in through the browser: Opens your browser")
    assert (done.way, done.auth, done.checked) == ("browser", "oauth", True)
    asked, ask = answering("")
    (tmp_path / "key").mkdir()
    (tmp_path / "file").mkdir()
    with open_workspace(tmp_path / "key", FileSecrets(tmp_path / "key-secrets", "workspace-test"),
                    {"linear": "kind: linear\n"}) as workspace, pytest.raises(ConnectLater, match="when you have a key"):
        connect(workspace, "linear", ask=ask, tell=Told(pick=1))
    assert asked == [("Linear API key, or Enter to connect later", True)]
    with open_workspace(tmp_path / "file", FileSecrets(tmp_path / "file-secrets", "workspace-test"),
                    {"linear": "kind: linear\nauth: api_key\n"}) as workspace, pytest.raises(ConnectLater):
        connect(workspace, "linear", ask=answering("")[1], tell=(named := Told(pick=0)))
    assert named.chosen == []  # a file that names its way in is asked none


class SignedIn:
    """The browser step, standing in: it opens nothing and gives a made-up login."""

    def __init__(self) -> None:
        self.servers: list[str] = []

    def __call__(self, server: str, **_: Any) -> Login:
        self.servers.append(server)
        return Login(server, f"{server}/token", "client-test", "tok-test-signed-in", expiry=None)


class FakeSession:
    """The workspace's MCP session opener, standing in: no client to any host is made, and each tool called answers
    with nothing."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.called: list[str] = []

    def __call__(self, where: Any) -> FakeSession:
        self.opened.append(where.url)
        return self

    def reader(self, *tools: str) -> Any:
        return lambda tool, arguments: self.called.append(tool) or {}

    def close(self) -> None:
        pass


def signing_in(monkeypatch: pytest.MonkeyPatch) -> tuple[SignedIn, FakeSession]:
    """Stand in for the browser step and for the MCP session the check opens, and fail the test on a browser."""
    import webbrowser

    import cerebro.connect as connecting_module

    signed, session = SignedIn(), FakeSession()
    monkeypatch.setattr(connecting_module, "authorize", signed)
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("a browser was opened"))
    monkeypatch.setattr(cli, "_pick", lambda *a, **k: "picked")  # what the file lists is not asked here
    opened = cli.open_workspace

    def open_workspace(root: Path | None) -> Any:
        workspace = opened(root)
        workspace.mcp_open = session
        return workspace

    monkeypatch.setattr(cli, "open_workspace", open_workspace)
    return signed, session


def kept(root: Path, name: str) -> str:
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        return workspace.secrets.get(name)


@pytest.mark.parametrize(("kind", "typed", "probe"), [("linear", "b\n", "list_teams"),
                                                      ("granola", "", "list_meeting_folders")])
def test_a_new_source_that_signs_in_through_the_browser_gets_a_file_that_says_so(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, typed: str, probe: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signed, session = signing_in(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", kind, "--workspace", str(root)], input=typed)
    assert got.exit_code == 0, got.output
    assert len(signed.servers) == 1 and session.opened == signed.servers and session.called == [probe]
    assert f"[ok] {kind.capitalize()} connection verified" in got.output.splitlines()
    assert Login.from_json(kept(root, kind), kind).access_token == "tok-test-signed-in"
    assert (root / "sources" / f"{kind}.yaml").read_text() == f"kind: {kind}\nauth: oauth\n"
    assert f"added sources/{kind}.yaml with auth: oauth" in got.output and "token" not in got.output.casefold()


def test_a_file_that_names_no_way_in_says_the_one_the_connect_took_with_its_comments_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    _, session = signing_in(monkeypatch)
    (root / "sources" / "work.yaml").write_text("kind: linear\n# the teams it reads\nteams: [ENG]")
    got = CliRunner().invoke(cli.main, ["connect", "work", "--workspace", str(root)], input="browser\n")
    assert got.exit_code == 0, got.output
    assert "[ok] Linear connection verified" in got.output and session.called == ["list_teams"]
    assert Login.from_json(kept(root, "linear"), "linear").access_token == "tok-test-signed-in"
    assert "sources/work.yaml says auth: oauth now." in got.output
    assert (root / "sources" / "work.yaml").read_text() == "kind: linear\n# the teams it reads\nteams: [ENG]\n" \
                                                           "auth: oauth\n"


def test_a_file_that_cannot_take_the_auth_line_is_refused_before_any_sign_in_and_nothing_is_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connect import ConnectError, set_auth

    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signed, session = signing_in(monkeypatch)
    flow = "{kind: linear, teams: [ENG]}\n"
    (root / "sources" / "work.yaml").write_text(flow)
    got = CliRunner().invoke(cli.main, ["connect", "work", "--workspace", str(root)], input="b\n")
    assert got.exit_code == 1
    assert ("sources/work.yaml cannot take an auth: oauth line as it is written, so nothing was asked or kept: write "
            "auth: oauth in it by hand, then run cerebro connect work again") in " ".join(got.output.split())
    assert signed.servers == [] and session.opened == [] and (root / "sources" / "work.yaml").read_text() == flow
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None and "linear" not in workspace.secrets.names()
    with pytest.raises(ConnectError, match="cannot take an auth: oauth line"):
        set_auth(root / "sources" / "work.yaml", "oauth")


@pytest.mark.parametrize(("text", "replace", "typed"), [
    ("kind: linear\nauth: api_key\nteams: [ENG]\n", True, "\n"),
    ("kind: linear\nauth: oauth\nteams: [ENG]\n", False, ""),
    ("kind: granola\nauth: api_key\n", True, "\n"),
])
def test_where_the_terminal_draws_a_file_that_names_its_way_in_is_asked_none_and_never_rewritten(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, replace: bool, typed: str) -> None:
    from cerebro import checklist

    root = lay_out_workspace(tmp_path, monkeypatch)
    kind = text.split()[1]
    (root / "sources" / f"{kind}.yaml").write_text(text)
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        workspace.secrets.set(kind, "lin_api_prior_test")
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(cli, "_drawing", lambda plain: printer())
    menus: list[Any] = []
    monkeypatch.setattr(checklist, "choose", lambda menu, p, cursor=0: menus.append(menu) or 0)
    signed, _ = signing_in(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", kind, *(["--replace"] if replace else []),
                                        "--workspace", str(root)], input=typed)
    assert got.exit_code == 0, got.output
    assert menus == [] and (root / "sources" / f"{kind}.yaml").read_text() == text
    if "oauth" in text:  # the file's own way in: through the browser, checked
        assert len(signed.servers) == 1 and "Connected to Linear through the browser." in got.output
    else:  # its token is asked for, the kept one stays, and no browser runs
        assert signed.servers == [] and kept(root, kind) == "lin_api_prior_test"


# ---- a key proven before it is kept


def test_a_gong_key_is_proven_with_the_workspaces_then_the_calls_and_one_gong_refuses_is_not_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_asking", lambda: True)
    lobby, said = in_the_lobby(root, monkeypatch, ["gong", None], answers=["refused-key", "stand-in-secret"])
    assert "gong did not take the key (status 401), so nothing was kept" in said
    assert lobby.shown[1][0]["gong"][0] != CONNECTED and not (root / "sources" / "gong.yaml").exists()
    server = RecordedGithub()
    with cli.open_workspace(root) as workspace:
        done = connect(workspace, "gong", ask=answering("stand-in-key", "stand-in-secret")[1], source=new_source("gong"),
                       http=httpx.Client(transport=httpx.MockTransport(server.handle)))
    asked = server.asked
    assert done.checked and [(r.method, r.url.path) for r in asked] == [("GET", "/v2/workspaces"), ("GET", "/v2/calls")]
    assert asked[1].url.params["fromDateTime"].startswith("2026-10-20")


def test_a_kept_gong_key_is_proven_again_with_the_same_two_steps(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "sources" / "calls.yaml").write_text("kind: gong\n")
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        workspace.secrets.set("gong", "stand-in-key:stand-in-secret")
    server = RecordedGithub()
    with cli.open_workspace(root) as workspace:
        done = connect(workspace, "calls", ask=never, http=httpx.Client(transport=httpx.MockTransport(server.handle)))
    assert done.kept and done.checked and [r.url.path for r in server.asked] == ["/v2/workspaces", "/v2/calls"]


# ---- the local files source


def test_connect_local_adds_its_file_when_missing_counts_what_it_reads_and_exits_0(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connectors.files import STARTER

    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "sources" / "local.yaml").unlink()
    (root / "local").mkdir(exist_ok=True)
    (root / "local" / "kickoff.md").write_text("# Kickoff\n\nWe met.\n")
    got = CliRunner().invoke(cli.main, ["connect", "local", "--workspace", str(root)])
    assert got.exit_code == 0, got.output
    assert (root / "sources" / "local.yaml").read_text() == STARTER.text
    assert got.output.splitlines()[:2] == [
        "Added sources/local.yaml, which reads the notes, call notes and emails you drop into local/.",
        "[ok] Local files: sources/local.yaml reads 1 file now"]
    again = CliRunner().invoke(cli.main, ["connect", "local", "--workspace", str(root)])
    assert again.exit_code == 0
    assert again.output.splitlines()[0] == "[ok] Local files: sources/local.yaml reads 1 file now"


def test_connect_local_uses_a_files_source_the_workspace_has_and_adds_none(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "sources" / "local.yaml").unlink()
    (root / "sources" / "notes.yaml").write_text("kind: files\nselect:\n  - glob: \"local/**/*.md\"\n")
    (root / "local").mkdir(exist_ok=True)
    (root / "local" / "one.md").write_text("# One\n\nA note.\n")
    (root / "local" / "two.md").write_text("# Two\n\nA note.\n")
    got = CliRunner().invoke(cli.main, ["connect", "local", "--workspace", str(root)])
    assert got.exit_code == 0, got.output
    assert got.output.splitlines()[0] == "[ok] Local files: sources/notes.yaml reads 2 files now"
    assert not (root / "sources" / "local.yaml").exists()


def test_connect_local_refuses_a_local_that_is_a_file_before_writing_anything(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "sources" / "local.yaml").unlink()
    shutil.rmtree(root / "local", ignore_errors=True)
    (root / "local").write_text("not a folder\n")
    got = CliRunner().invoke(cli.main, ["connect", "local", "--workspace", str(root)])
    assert got.exit_code == 1
    assert ("local is a file, not a folder, so nothing was written: move it aside, then run cerebro connect local "
            "again") in got.output
    assert not (root / "sources" / "local.yaml").exists() and "Traceback" not in got.output


@pytest.mark.parametrize(("args", "typed"), [(["gong"], "\n"), (["local"], "")])
def test_with_plain_the_last_line_is_plain_on_a_terminal_that_takes_styles(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], typed: str) -> None:
    from cerebro import output

    root = lay_out_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(output, "is_terminal", lambda stream: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    got = CliRunner().invoke(cli.main, ["connect", *args, "--plain", "--workspace", str(root)], input=typed)
    assert got.exit_code == 0, got.output
    last = got.output.splitlines()[-1]
    assert "\x1b" not in last and last.startswith(("[ok] Done.", "[!] Done with warnings."))


def test_what_a_source_says_and_its_question_are_on_one_stream_stderr(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "gong", "--workspace", str(root)], input="\n")
    assert "Gong keys are made by a Gong tech admin" in got.stderr and "Gong access key, or Enter" in got.stderr
    assert "Gong access key" not in got.stdout and "tech admin" not in got.stdout


# ---- the closing line


def test_the_plain_walk_ends_with_the_last_line_even_on_an_empty_answer_and_names_what_to_connect_next(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "sources" / "broken.yaml").unlink()
    at_a_plain_terminal(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)], input="\n")
    assert got.exit_code == 0 and "Missing argument" not in got.output and got.output.startswith("chat ")
    assert "Connect which? Names, with commas [Enter to skip]: " in got.output
    assert got.output.splitlines()[-2:] == ["Nothing picked, nothing connected.",
                                            "[ok] Done. Next: cerebro connect chat"]


def test_leaving_the_lobby_with_nothing_connected_names_a_kind_to_add_never_a_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    for name in ("broken", "code", "chat"):
        (root / "sources" / f"{name}.yaml").unlink()
    _, said = in_the_lobby(root, monkeypatch, [None])
    assert strip_escapes(said).strip() == "✓ Done. Next: cerebro connect calendar"


def test_escape_at_a_way_in_from_the_lobby_goes_back_to_it_with_no_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import checklist

    root = lay_out_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_asking", lambda: True)
    menus: list[list[str]] = []
    monkeypatch.setattr(checklist, "choose", lambda menu, p, cursor=0: menus.append([r.title for r in menu.rows])
                        or checklist.MENU_DONE)
    lobby, said = in_the_lobby(root, monkeypatch, ["linear", "calendar", None])
    assert [len(rows) for rows in menus] == [2, 3]  # which way in, then where the calendar lives
    assert lobby.shown[1][1] == [] and lobby.shown[2][1] == [] and "Run:" not in said
    assert lobby.shown[2][0]["linear"][0] == NOT_ADDED and lobby.shown[2][0]["chat"][0] == NOT_CONNECTED


def test_every_line_connect_shows_a_person_says_no_section_brain_or_page() -> None:
    import re

    from cerebro.connectors import CONNECTORS
    from cerebro.connectors.files import STARTER

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    seen = [STARTER.added, STARTER.counted, "Connect which? Names, with commas [Enter to skip]: ",
            "Counting the files in local/", "did not connect.  [r]etry  [c]onnect later  > ",
            "Error: connect needs a source's name: run it in a terminal to be asked, or pass <name>",
            "Verifying Gong connection", "Gong connection verified", "Gong did not verify: no answer",
            "could not be written, so nothing was kept: let it be written, then run cerebro connect again",
            "came to be while you connected, so nothing was kept"]
    for kind in CONNECTORS:
        said = connecting(kind)
        if said is None:
            continue
        seen += [*said.says, said.block, *said.after, *said.plain, *said.plain_after, said.later, said.title]
        seen += [said.connected, said.kept, said.needs, said.later_check]
        seen += [p for q in said.asks for p in (q.label, q.plain, q.offered, q.skip)]
        for way in said.choices:
            assert isinstance(way, Way)
            seen += [way.title, way.reason, way.word, *way.says, *(way.plain or ()), way.later, way.connected,
                     way.kept]
            seen += [p for q in way.asks for p in (q.label, q.plain)]
    found = [line for line in seen if words.search(line)]
    assert found == [] and not [line for line in seen if "\u2014" in line]



def test_the_views_in_a_light_and_a_dark_terminal_and_plain_are_the_saved_ones_and_read_the_same() -> None:
    import sys

    from test_output import DATA

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
    import connect_demo

    light, dark = connect_demo.views(dark=False), connect_demo.views(dark=True)
    assert light == (DATA / "connect-views-light.ansi").read_text()
    assert dark == (DATA / "connect-views-dark.ansi").read_text()
    assert connect_demo.views(None) == (DATA / "connect-views-plain.txt").read_text()
    assert light != dark and "\x1b" not in connect_demo.views(None)
    assert EXPECTED_MANIFEST.splitlines()[1].strip() in strip_escapes(dark)


def test_at_a_terminal_that_draws_calendar_asks_where_it_lives_says_where_the_address_is_and_keeps_it() -> None:
    from test_connect_lobby import on_the_demo

    drawn = on_the_demo(["--source", "calendar"], ((b"Other calendar (ICS link)", 1, b"\r"),
                                                    (b"submit", 2, b"https://calendar.example/feed.ics\r")))
    assert "Google Calendar" in drawn and "Outlook Calendar (web-based)" in drawn
    assert drawn.index("Integrate calendar") < drawn.index("Secret address: ")
    assert "Calendar connection verified" in drawn and "Stored the calendar address as calendar, in " in drawn
    assert drawn.rstrip().endswith("kind: calendar")


# ---- the browser's wait


def test_the_browser_sign_in_waits_ten_minutes_and_the_lines_say_so(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.connect as connecting_module

    waits: list[float] = []

    def signed(server: str, **kwargs: Any) -> Login:
        waits.append(kwargs["wait"])
        return Login(server, f"{server}/token", "client-test", "tok-test-signed-in", expiry=None)

    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    monkeypatch.setattr(connecting_module, "authorize", signed)
    got = CliRunner().invoke(cli.main, ["connect", "granola", "--workspace", str(root)])
    assert got.exit_code == 0, got.output
    assert waits == [600.0]
    for kind in ("linear", "granola"):
        said = connecting(kind)
        assert said is not None
        lines = [line for way in said.choices for line in way.says if "minutes" in line]
        lines += [line for line in said.says if "minutes" in line]
        assert lines and all("ten minutes" in line for line in lines), lines


# ---- Ctrl-C ends the command


def ctrl_c_at(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """Answer each prompt from `answers` until they run out, then press Ctrl-C there, as click reads it: an Abort
    raised while a KeyboardInterrupt is handled. Gives the prompts asked."""
    import click

    asked: list[str] = []
    given = iter(answers)

    def prompt(text: str, *args: Any, **kwargs: Any) -> str:
        asked.append(text)
        answer = next(given, None)
        if answer is not None:
            return answer
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            raise click.Abort() from None

    monkeypatch.setattr(click, "prompt", prompt)
    return asked


@pytest.mark.parametrize(("args", "answers", "last"), [
    (["linear"], [], "[b]rowser  [k]ey  > "),
    (["linear"], ["k"], "Linear API key, or Enter to connect later"),
    (["gong"], [], "Gong access key, or Enter to connect later"),
    (["gong"], ["stand-in-key"], "Gong access key secret"),
    (["code", "--replace"], [], "GitHub token, or Enter to connect later"),
    (["gmail"], [], "Gmail address [Enter to skip]"),
    ([], [], "Connect which? Names, with commas [Enter to skip]: "),
    ([], ["linear, gong"], "[b]rowser  [k]ey  > "),
    (["code"], [], "[a]ccept  [q]uit  > "),
])
def test_ctrl_c_at_any_plain_prompt_ends_connect_with_nothing_kept_and_exits_130(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], answers: list[str], last: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    before = {p.name: p.read_bytes() for p in (root / "sources").iterdir()}
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        held = {name: workspace.secrets.get(name) for name in workspace.secrets.names()}
    asked = ctrl_c_at(monkeypatch, answers)
    got = CliRunner().invoke(cli.main, ["connect", *args, "--workspace", str(root)])
    assert got.exit_code == 130, got.output
    assert asked[-1] == last and "Done" not in got.output and "Aborted!" not in got.output
    assert "Gong access key" not in "".join(asked[:-1]) or args == ["gong"]  # the walk went on to no other source
    assert {p.name: p.read_bytes() for p in (root / "sources").iterdir()} == before
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        assert {name: workspace.secrets.get(name) for name in workspace.secrets.names()} == held


def test_input_that_ends_at_a_hidden_question_ends_connect_as_ctrl_c_does_and_at_the_way_in_connects_later(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "gong", "--workspace", str(root)], input="")
    assert got.exit_code == 130 and "Aborted!" not in got.output and "Done" not in got.output
    assert not (root / "sources" / "gong.yaml").exists()
    later = CliRunner().invoke(cli.main, ["connect", "linear", "--workspace", str(root)], input="")
    assert later.exit_code == 0 and later.output.splitlines()[-1].startswith("[!] Done with warnings.")


# ---- Gong's two-step proof, and a kept key that no longer works


class GongStandIn:
    """Gong's API, standing in: each path answers the status it is given, and the requests are kept."""

    def __init__(self, workspaces: int = 200, calls: int = 200) -> None:
        self.status = {"/v2/workspaces": workspaces, "/v2/calls": calls}
        self.asked: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url.path)
        status = self.status.get(request.url.path, 404)
        body = {"workspaces": []} if request.url.path == "/v2/workspaces" else {"records": {"totalRecords": 0}}
        return httpx.Response(status, json=body if status == 200 else {"errors": ["stand-in"]})


@pytest.mark.parametrize(("workspaces", "calls", "asked", "outcome"), [
    (200, 200, ["/v2/workspaces", "/v2/calls"], "proven"),
    (200, 404, ["/v2/workspaces", "/v2/calls"], "proven"),  # Gong's 404 for a list with no call in it
    (404, 200, ["/v2/workspaces"], "unproven"),  # a wrong address, as a url that doubles /v2 gives
    (401, 200, ["/v2/workspaces"], "refused"),
    (403, 200, ["/v2/workspaces"], "refused"),
    (500, 200, ["/v2/workspaces"], "unproven"),
    (200, 401, ["/v2/workspaces", "/v2/calls"], "refused"),
    (200, 400, ["/v2/workspaces", "/v2/calls"], "refused"),
    (200, 429, ["/v2/workspaces", "/v2/calls"], "unproven"),  # busy, or failing, is never a refusal of the key
    (200, 500, ["/v2/workspaces", "/v2/calls"], "unproven"),
])
def test_a_gong_key_is_proven_on_the_workspaces_then_the_calls_and_only_a_proof_keeps_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspaces: int, calls: int, asked: list[str],
        outcome: str) -> None:
    from cerebro.connect import ConnectError

    root = lay_out_workspace(tmp_path, monkeypatch)
    gong = GongStandIn(workspaces, calls)
    with cli.open_workspace(root) as workspace:
        http = httpx.Client(transport=httpx.MockTransport(gong.handle))
        if outcome == "proven":
            assert connect(workspace, "gong", given="stand-in-key:stand-in-secret", source=new_source("gong"),
                           http=http).checked
        else:
            with pytest.raises(LoginRefused if outcome == "refused" else ConnectError) as failed:
                connect(workspace, "gong", given="stand-in-key:stand-in-secret", source=new_source("gong"), http=http)
            assert isinstance(failed.value, LoginRefused) == (outcome == "refused")
            assert "nothing was kept" in str(failed.value)
    assert gong.asked == asked
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        assert ("gong" in workspace.secrets.names()) == (outcome == "proven")


@pytest.mark.parametrize(("kept", "file", "server"), [
    ("stand-in-key:refused-secret", "kind: gong\n", GongStandIn(workspaces=401)),
    ("onlyonehalf", "kind: gong\n", GongStandIn()),
    ("refused-token", "kind: github\n", None),
])
def test_a_kept_key_the_provider_refuses_fails_naming_replace_and_asks_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kept: str, file: str, server: GongStandIn | None) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch, server)  # type: ignore[arg-type] - a stand-in with the same handle
    kind = file.split()[1]
    (root / "sources" / "kept.yaml").write_text(file)
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        workspace.secrets.set(kind, kept)
    at_a_plain_terminal(monkeypatch)
    asked = ctrl_c_at(monkeypatch, [])
    got = CliRunner().invoke(cli.main, ["connect", "kept", "--workspace", str(root)])
    assert got.exit_code == 1, got.output
    assert asked == [] and "cerebro connect kept --replace" in got.output and kept not in got.output
    assert kept_value(root, kind) == kept


def kept_value(root: Path, name: str) -> str:
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        return workspace.secrets.get(name)


# ---- a Slack token's kind and scopes


class SlackScopes:
    """Slack's auth.test, standing in: it takes any token and lists the scopes it is given in X-OAuth-Scopes."""

    def __init__(self, scopes: str | None) -> None:
        self.scopes = scopes
        self.asked: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url.path)
        headers = {"X-OAuth-Scopes": self.scopes} if self.scopes is not None else {}
        return httpx.Response(200, headers=headers, json={"ok": True, "url": "https://quillmoor.slack.example/",
                                                          "user": "someone", "team": "Quillmoor"})


ALL_SCOPES = "channels:read,channels:history,groups:read,groups:history,users:read,users:read.email"


@pytest.mark.parametrize(("token", "scopes", "refused", "asked"), [
    ("xoxb-made-up-bot-0001", ALL_SCOPES, "that is a bot token, which reads only the channels its bot is in", []),
    ("made-up-0002", ALL_SCOPES, "that is not a Slack user token", []),
    ("xoxp-made up-0003", ALL_SCOPES, "the token holds a space or a control character", []),
    ("xoxp-made-up-0004", "channels:read,users:read", ("the token lacks the scopes channels:history, groups:read, "
                                                       "groups:history, users:read.email"), ["/api/auth.test"]),
    ("xoxp-made-up-0005", ALL_SCOPES, "", ["/api/auth.test"]),
    ("xoxp-made-up-0006", None, "", ["/api/auth.test"]),  # Slack listed no scopes, so none is missing
])
def test_a_slack_token_is_read_by_its_form_before_anything_is_sent_and_one_short_of_a_scope_is_not_kept(
        tmp_path: Path, store: FileSecrets, token: str, scopes: str | None, refused: str,  # noqa: F811
        asked: list[str]) -> None:
    slack = SlackScopes(scopes)
    with open_workspace(tmp_path, store, {"chat": "kind: slack\n"},
                    http=httpx.Client(transport=httpx.MockTransport(slack.handle))) as workspace:
        if refused:
            with pytest.raises(LoginRefused, match=refused) as failed:
                connect(workspace, "chat", ask=answering(token)[1])
            assert token not in str(failed.value) and "nothing was kept" in str(failed.value)
            assert store.names() == []
        else:
            assert connect(workspace, "chat", ask=answering(token)[1]).checked and store.get("chat") == token
    assert slack.asked == asked


def test_a_kept_slack_bot_token_is_not_sent_and_a_new_token_is_asked_for(
        tmp_path: Path, store: FileSecrets) -> None:  # noqa: F811
    slack = SlackScopes(ALL_SCOPES)
    store.set("chat", "xoxb-made-up-bot-0007")
    said: list[str] = []
    asked, ask = answering("xoxp-made-up-0008")
    with open_workspace(tmp_path, store, {"chat": "kind: slack\n"},
                    http=httpx.Client(transport=httpx.MockTransport(slack.handle))) as workspace:
        connect(workspace, "chat", ask=ask, say=said.append)
    assert slack.asked == ["/api/auth.test"] and store.get("chat") == "xoxp-made-up-0008" and len(asked) == 1


# ---- a login is kept only once its file is written


@pytest.mark.parametrize(("name", "text", "typed"), [
    ("work", "kind: linear\nteams: [ENG]\n", "b\n"),  # a file that gains auth: oauth
    ("granola", None, ""),  # a new file, through the browser
    ("linear", None, "k\nlin_api_made_up_0009\n"),  # a new file, with a key
    ("gong", None, ""),  # a new file, for a key kept already
])
def test_a_source_file_that_cannot_be_written_keeps_no_login_and_says_so_in_one_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, text: str | None, typed: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    path = root / "sources" / f"{name}.yaml"
    if text is not None:
        path.write_text(text)
    (root / "sources" / f".{name}.yaml.partial").mkdir()  # the write's partial file cannot be made
    if name == "gong":
        with cli.open_workspace(root) as workspace:
            assert workspace.secrets is not None
            workspace.secrets.set("gong", "stand-in-key:stand-in-secret")
    got = CliRunner().invoke(cli.main, ["connect", name, "--workspace", str(root)], input=typed)
    assert got.exit_code == 1 and isinstance(got.exception, SystemExit), got.output
    assert "Traceback" not in got.output and "checked" not in got.output and "Connected" not in got.output
    assert "verified" not in got.output and "[ok]" not in got.output
    assert f"sources/{name}.yaml could not be written" in got.output and "nothing was kept" in got.output
    assert (path.read_text() if path.exists() else None) == text
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None and not {"linear", "granola"} & set(workspace.secrets.names())
        assert workspace.secrets.names().count("gong") == (name == "gong")  # a key kept before stays as it was


def test_a_file_that_names_auth_by_the_time_the_sign_in_ends_keeps_no_login_and_says_nothing_was_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.connect as connecting_module

    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    path = root / "sources" / "work.yaml"
    path.write_text("kind: linear\n")

    def edited_meanwhile(server: str, **_: Any) -> Login:
        path.write_text("kind: linear\nauth: oauth\n")
        return Login(server, f"{server}/token", "client-test", "tok-test-signed-in", expiry=None)

    monkeypatch.setattr(connecting_module, "authorize", edited_meanwhile)
    got = CliRunner().invoke(cli.main, ["connect", "work", "--workspace", str(root)], input="b\n")
    assert got.exit_code == 1 and "nothing was kept" in got.output and "asked" not in got.output.split("Error:")[-1]
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None and "linear" not in workspace.secrets.names()


def test_a_source_file_that_links_out_of_the_workspace_is_refused_before_the_sign_in(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signed, _ = signing_in(monkeypatch)
    outside = tmp_path / "outside.yaml"
    outside.write_text("kind: linear\n")
    (root / "sources" / "work.yaml").symlink_to(outside)
    got = CliRunner().invoke(cli.main, ["connect", "work", "--workspace", str(root)], input="b\n")
    assert got.exit_code == 1 and signed.servers == [] and "cannot take an auth: oauth line" in got.output
    assert outside.read_text() == "kind: linear\n"


# ---- the check as a step, and the line that closes a connect, in each provider's words


@pytest.mark.parametrize(("args", "typed", "server", "lines"), [
    (["gong", "--stdin"], "stand-in-key:stand-in-secret\n", None,
     ["[ok] Gong connection verified", "Connected to Gong. The key is kept under gong in "]),
    (["gong", "--stdin"], "stand-in-key:refused-secret\n", None,
     ["[x] Gong did not verify: the key was refused"]),
    (["gong", "--stdin"], "stand-in-key:stand-in-secret\n", GongStandIn(workspaces=404),
     ["[x] Gong did not verify: no answer"]),
    (["gong", "--stdin"], "stand-in-key:stand-in-secret\n", GongStandIn(calls=500),
     ["[x] Gong did not verify: gong answered an error"]),
    (["gong", "--stdin"], "stand-in-key:stand-in-secret\n", GongStandIn(calls=400),
     ["[x] Gong did not verify: the key listed no calls"]),
    (["code"], "", None,
     ["[ok] GitHub connection verified", "Connected to GitHub as @someone. The token is kept under github in "]),
    (["code", "--replace", "--stdin"], "made-up-refused\n", None,
     ["[x] GitHub did not verify: the token was refused"]),
])
def test_each_connect_shows_its_check_as_a_verifying_step_and_closes_in_its_providers_words(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], typed: str, server: Any,
        lines: list[str]) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch, server)
    got = CliRunner().invoke(cli.main, ["connect", *args, "--workspace", str(root)], input=typed)
    out = got.output.splitlines()
    for line in lines:
        assert any(said.startswith(line) for said in out), got.output
    assert out.index(next(s for s in out if s.startswith(lines[0]))) < len(out)
    assert "checked" not in got.output


def test_a_kept_login_closes_with_its_providers_line_for_one_kept_already(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "sources" / "calls.yaml").write_text("kind: gong\n")
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        workspace.secrets.set("gong", "stand-in-key:stand-in-secret")
    got = CliRunner().invoke(cli.main, ["connect", "calls", "--workspace", str(root)])
    assert got.exit_code == 0, got.output
    assert got.output.splitlines()[:2] == [
        "[ok] Gong connection verified",
        (f"Gong is connected already, and the key kept under gong in {kept_where(root)} stays. Pass --replace to "
         "replace it.")]


def kept_where(root: Path) -> str:
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        return workspace.secrets.where()


@pytest.mark.parametrize(("kind", "typed", "closing"), [
    ("linear", "b\n", "Connected to Linear through the browser. The login is kept under linear in "),
    ("granola", "", "Connected to Granola. The login is kept under granola in "),
])
def test_a_browser_sign_in_closes_in_its_providers_words(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, typed: str, closing: str) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", kind, "--workspace", str(root)], input=typed)
    assert got.exit_code == 0, got.output
    assert f"[ok] {kind.capitalize()} connection verified" in got.output.splitlines()
    assert any(line.startswith(closing) for line in got.output.splitlines()), got.output


# ---- what a plain line shows, and the accents where the terminal draws


@pytest.mark.parametrize(("kind", "typed", "shown", "unshown"), [
    ("linear", "b\n", ["Sign in through the browser: Opens your browser"], ["Linear signs you in through your browser"]),
    ("granola", "", ["Granola lists participants incompletely"], ["Granola signs you in through your browser"]),
])
def test_on_a_plain_line_the_browser_way_says_its_plain_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, typed: str, shown: list[str],
        unshown: list[str]) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", kind, "--workspace", str(root)], input=typed)
    assert got.exit_code == 0, got.output
    assert all(line in got.output for line in shown) and not any(line in got.output for line in unshown)


def test_linears_plain_lines_put_no_blank_line_before_a_prompt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "linear", "--workspace", str(root)], input="k\n\n")
    lines = got.stderr.splitlines()
    way = next(i for i, line in enumerate(lines) if line.startswith("[b]rowser  [k]ey  > "))
    assert lines[way - 1].startswith("Paste an API key: Reads a personal API key")
    assert lines[way + 1].startswith("Make a personal API key in Linear")
    assert lines[way + 2] == "Or store one first with: cerebro secrets set linear"
    assert lines[way + 3].startswith("Linear API key, or Enter to connect later")


def test_gmails_plain_prompt_with_no_address_offered_says_enter_skips(
        tmp_path: Path, store: FileSecrets) -> None:  # noqa: F811
    asked, ask = answering("")
    with open_workspace(tmp_path, store, {"mail": "kind: gmail\n"}) as workspace, pytest.raises(ConnectLater):
        connect(workspace, "mail", ask=ask, tell=Told(drawn=False))
    assert asked == [("Gmail address [Enter to skip]", False)]


def test_where_the_terminal_draws_the_words_in_backticks_are_accented_and_a_plain_line_shows_them_bare() -> None:
    from cerebro.connectors import CONNECTORS

    drawn, plain = printer(), printer(plain=True)
    drawn._width = lambda: 300  # wide enough that no line wraps, so each accent is read whole
    gong = connecting("gong")
    assert gong is not None
    line = explained(drawn, gong.says)[0]
    assert drawn.accent("Admin Center > Settings > Ecosystem > API") in line and "`" not in line
    assert drawn.accent("Get API Key") in line and strip_escapes(line).startswith("Provide a Gong access key")
    for kind in CONNECTORS:
        said = connecting(kind)
        if said is None:
            continue
        lines = [*said.says, *said.after, *[x for w in said.choices for x in w.says]]
        assert all(text.count("`") % 2 == 0 for text in lines), kind
        assert not any("`" in text or "\x1b" in text for text in explained(plain, lines)), kind
    slack = connecting("slack")
    assert slack is not None
    accents = [drawn.accent(word) for word in ("https://api.slack.com/apps", "Create New App", "From manifest",
                                               "Install App", "Request to Install", "OAuth & Permissions",
                                               "User OAuth Token", "xoxp-")]
    said = "\n".join(explained(drawn, slack.after))
    assert all(a in said for a in accents)


def test_on_a_plain_line_slacks_manifest_is_at_the_margin_so_a_copy_from_its_first_character_reads_as_yaml(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "chat", "--workspace", str(root)], input="\n")
    assert EXPECTED_MANIFEST in got.output
    start = got.output.index("display_information:")
    copied = got.output[start:start + len(EXPECTED_MANIFEST)]
    assert yaml.safe_load(copied) == yaml.safe_load(EXPECTED_MANIFEST)


def test_where_the_terminal_draws_a_choice_is_headed_by_its_title_and_a_place_to_look_by_the_rows(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import checklist

    root = lay_out_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(cli, "_drawing", lambda plain: printer())
    menus: list[Any] = []
    monkeypatch.setattr(checklist, "choose", lambda menu, p, cursor=0: menus.append(menu) or 1)
    got = CliRunner().invoke(cli.main, ["connect", "calendar", "--workspace", str(root)], input="\n")
    said = strip_escapes(got.stderr).splitlines()
    assert menus and menus[0].head == ["Calendar"]  # the title heads the menu, from a clear screen
    row = said.index("Outlook Calendar (web-based)")  # the place to look heads its own screen, its lines under it
    assert said[row + 1].startswith("Open Settings > Calendar > Shared calendars")


# ---- the lines around a connect


def test_at_a_terminal_connect_opens_with_a_header_line_naming_the_workspace(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    monkeypatch.setattr(cli, "_terminal_out", lambda: True)
    got = CliRunner().invoke(cli.main, ["connect", "gong", "--workspace", str(root)], input="\n")
    assert got.stdout.splitlines()[0] == f"cerebro connect  {root.resolve()}"
    monkeypatch.setattr(cli, "_terminal_out", lambda: False)
    off = CliRunner().invoke(cli.main, ["connect", "gong", "--workspace", str(root)], input="\n")
    assert "cerebro connect  " not in off.output


def test_off_a_terminal_connect_with_no_source_ends_with_its_usage_line_and_its_failed_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)])
    assert got.exit_code == 2
    err = got.stderr.splitlines()
    assert err[-1] == "[x] Failed. Run: cerebro connect <name>"
    assert err[-2].startswith("Error: connect needs a source's name, one of broken, chat, code, ")
    assert err[-2].endswith(": run it in a terminal to be asked, or pass <name>") and "Usage:" not in got.stderr
    assert not got.stdout.endswith("\n\n")


def test_off_a_terminal_a_connect_that_needs_a_person_ends_with_its_failed_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "chat", "--workspace", str(root)])
    assert got.exit_code == 2
    assert got.stderr.splitlines()[-1] == "[x] Failed. Run: cerebro connect chat --from-env NAME"


def test_the_plain_table_says_what_each_source_needs_and_no_blank_line_comes_before_the_question(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)], input="\n")
    lines = got.output.splitlines()
    assert next(line for line in lines if line.startswith("chat ")).endswith(
        "an app you create from our manifest, and its user token")
    assert next(line for line in lines if line.startswith("gong ")).endswith(
        "an API key and secret from your Gong tech admin")
    question = next(i for i, line in enumerate(lines) if line.startswith("Connect which?"))
    assert lines[question - 1] != ""


def test_a_connect_that_fails_in_the_plain_walk_asks_to_retry_or_connect_later(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    at_a_plain_terminal(monkeypatch)
    typed = "gong, chat\nrefused-key\nstand-in-secret\nr\nrefused-key\nstand-in-secret\n\n\n"
    got = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)], input=typed)
    assert got.exit_code == 0, got.output
    assert got.output.count("gong did not connect.  [r]etry  [c]onnect later  > ") == 2
    assert got.output.count("Gong access key, or Enter to connect later") == 2
    assert "Slack user token (xoxp-...)" in got.output  # connect later went on to the next source
    assert "Check: gong is not connected yet: run cerebro connect gong" in got.output.splitlines()[-1]


# ---- refusals, the table, and kept logins asked again


class SlackSays:
    """Slack's auth.test, standing in: it answers ok false with the code it is given, or ok with every scope."""

    def __init__(self, code: str = "") -> None:
        self.code = code
        self.asked = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.asked += 1
        if self.code:
            return httpx.Response(200, json={"ok": False, "error": self.code})
        return httpx.Response(200, headers={"X-OAuth-Scopes": ALL_SCOPES},
                              json={"ok": True, "url": "https://quillmoor.slack.example/"})


@pytest.mark.parametrize("code", ["not_allowed_token_type", "missing_scope", "ekm_access_denied"])
def test_any_code_slack_answers_refuses_the_token_and_a_kept_one_is_asked_for_again(
        tmp_path: Path, store: FileSecrets, code: str) -> None:  # noqa: F811
    slack = SlackSays(code)
    with open_workspace(tmp_path, store, {"chat": "kind: slack\n"},
                    http=httpx.Client(transport=httpx.MockTransport(slack.handle))) as workspace:
        with pytest.raises(LoginRefused, match=f"the token did not work with Slack, so nothing was kept: {code}"):
            connect(workspace, "chat", ask=answering("xoxp-made-up-0010")[1])
        assert store.names() == []
        store.set("chat", "xoxp-made-up-0011")
        said: list[str] = []
        asked, ask = answering("xoxp-made-up-0012")
        with pytest.raises(LoginRefused):
            connect(workspace, "chat", ask=ask, say=said.append)
        assert len(asked) == 1 and said == ["The slack token kept as chat no longer works, so a new one is asked for."]
        assert "--name" not in " ".join(said)


def test_a_slack_token_refused_by_its_form_opens_no_verifying_step_and_says_slack_with_its_capital(
        tmp_path: Path, store: FileSecrets) -> None:  # noqa: F811
    slack = SlackSays()
    steps: list[str] = []
    with (open_workspace(tmp_path, store, {"chat": "kind: slack\n"},
                     http=httpx.Client(transport=httpx.MockTransport(slack.handle))) as workspace,
          pytest.raises(LoginRefused, match="that is not a Slack user token")):
        connect(workspace, "chat", ask=answering("made-up-0013")[1], step=lambda label: steps.append(label) or never)
    assert steps == [] and slack.asked == 0


def test_the_table_says_what_github_and_linear_need(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)])
    lines = got.stdout.splitlines()
    assert next(line for line in lines if line.startswith("code ")).endswith("a GitHub token")
    assert next(line for line in lines if line.startswith("linear ")).endswith("a Linear API key, or a browser sign-in")


@pytest.mark.parametrize(("file", "kept", "typed"), [
    ("kind: gmail\n", "kept.person@example.com:abcd efgh ijkl mnop", ""),
    ("kind: calendar\n", "https://calendar.example/kept.ics", ""),
])
def test_a_kept_gmail_login_or_calendar_address_is_not_read_and_connect_asks_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file: str, kept: str, typed: str) -> None:
    from gmail_standin import StandIn as MailStandIn

    class Feed:  # a calendar feed that answers, so a kept address would work
        def handle(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="BEGIN:VCALENDAR\nEND:VCALENDAR\n")

    root = lay_out_workspace(tmp_path, monkeypatch, Feed())  # type: ignore[arg-type] - a stand-in with the same handle
    opened = cli.open_workspace

    def open_workspace(root: Path | None) -> Any:  # a mailbox that takes the kept login
        workspace = opened(root)
        workspace.imap_connect = lambda host: MailStandIn()
        return workspace

    monkeypatch.setattr(cli, "open_workspace", open_workspace)
    from cerebro.connect import need
    from cerebro.source import read_source

    (root / "sources" / "mine.yaml").write_text(file)
    name = need(read_source(root / "sources" / "mine.yaml")).name
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets is not None
        workspace.secrets.set(name, kept)
    off = CliRunner().invoke(cli.main, ["connect", "mine", "--workspace", str(root)])
    assert off.exit_code == 2 and "input is not a terminal" in off.output, off.output
    at_a_plain_terminal(monkeypatch)
    asked = ctrl_c_at(monkeypatch, [""])
    got = CliRunner().invoke(cli.main, ["connect", "mine", "--workspace", str(root)])
    assert got.exit_code == 0 and len(asked) == 1 and "connected already" not in got.output
    assert kept_value(root, name) == kept


@pytest.mark.parametrize(("html_at", "outcome"), [("/v2/workspaces", "unproven"), ("/v2/calls", "unproven")])
def test_a_gong_answer_that_is_not_json_proves_no_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, html_at: str, outcome: str) -> None:
    from cerebro.connect import ConnectError

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == html_at:
            return httpx.Response(200, text="<html>a sign-in page</html>")
        return httpx.Response(200, json={"workspaces": [], "records": {"totalRecords": 0}})

    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        with pytest.raises(ConnectError) as failed:
            connect(workspace, "gong", given="stand-in-key:stand-in-secret", source=new_source("gong"),
                    http=httpx.Client(transport=httpx.MockTransport(handle)))
        assert isinstance(failed.value, LoginRefused) == (outcome == "refused")
        assert workspace.secrets is not None and "gong" not in workspace.secrets.names()
