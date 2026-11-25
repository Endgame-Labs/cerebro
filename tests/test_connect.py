"""Connecting a source: the login its kind needs, got from a flag, a person or the browser, checked with one read-only
call and kept in the workspace's secrets store. Every provider is a stand-in: GitHub and Slack behind a mock transport,
Linear's MCP server in this process behind a bearer check, its authorization server on loopback, and a fake browser.
The store is the file store under the test's own folder, and every token is synthetic."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from gmail_standin import StandIn as MailStandIn
from mcp.server.mcpserver import MCPServer
from suites.connector import every_page
from test_connector_linear import RecordedLinear
from test_connector_mcp_source import Served
from test_connector_slack import WORLD as SLACK_WORLD
from test_connector_slack import StandIn as SlackStandIn
from test_mcp_oauth import (
    AuthStandIn,
    Browser,
    Clock,
    loopback_only,  # noqa: F401 - a fixture
)

from cerebro import Cerebro
from cerebro.connect import ConnectError, LoginRefused, NotATerminal, connect, need
from cerebro.connectors.granola import GranolaConnector
from cerebro.connectors.linear import READ_TOOLS as LINEAR_TOOLS
from cerebro.connectors.linear import LinearConnector
from cerebro.connectors.mcp_oauth import LiveToken, Login
from cerebro.connectors.mcp_source_client import McpClient, Reach
from cerebro.fakes import FakeExtraction
from cerebro.secrets import FileSecrets, SecretNotFound
from cerebro.source import read_source

GITHUB_TOKEN = "tok-test-github-0001"


def make_workspace(root: Path, sources: dict[str, str]) -> Path:
    root.mkdir()
    (root / "settings.yaml").write_text("id: workspace-test\ntimezone: UTC\n")
    (root / "sources").mkdir()
    for name, text in sources.items():
        (root / "sources" / f"{name}.yaml").write_text(text)
    return root


@pytest.fixture
def store(tmp_path: Path) -> FileSecrets:
    return FileSecrets(tmp_path / "secrets", "workspace-test")


def open_workspace(tmp_path: Path, store: FileSecrets, sources: dict[str, str], **options: Any) -> Cerebro:
    return Cerebro(make_workspace(tmp_path / "workspace", sources), extraction=FakeExtraction(), clock=options.pop("clock",
                   Clock()), secrets=store, **options)


def never(label: str, hidden: bool) -> str:
    raise AssertionError(f"connect asked {label!r} when it had what it needed")


# -- a token


def github(request: httpx.Request) -> httpx.Response:
    if request.headers.get("authorization") != f"Bearer {GITHUB_TOKEN}":
        return httpx.Response(401, json={"message": "Bad credentials"})
    if request.method == "GET" and request.url.path == "/user":
        return httpx.Response(200, json={"login": "ada-test"})
    return httpx.Response(404, json={"message": "Not Found"})


def test_a_github_token_given_through_a_flag_is_checked_and_kept_and_reads_back_from_the_store(
        tmp_path: Path, store: FileSecrets) -> None:
    http = httpx.Client(transport=httpx.MockTransport(github))
    with open_workspace(tmp_path, store, {"code": "kind: github\nrepos: [alder/api]\n"}, http=http) as workspace:
        done = connect(workspace, "code", given=f"  {GITHUB_TOKEN}\n", ask=never)
        assert workspace.mask(f"token {GITHUB_TOKEN}") == "token [secret]"
    assert (done.name, done.where, done.way, done.checked, done.kept, done.who) == (
        "github", store.where(), "token", True, False, "@ada-test")
    assert store.get("github") == GITHUB_TOKEN and GITHUB_TOKEN not in repr(done)


def test_a_token_the_provider_refuses_keeps_nothing_and_says_so_without_the_token(
        tmp_path: Path, store: FileSecrets) -> None:
    http = httpx.Client(transport=httpx.MockTransport(github))
    refusal = "code: github refused the token, so nothing was kept"
    with (open_workspace(tmp_path, store, {"code": "kind: github\n"}, http=http) as workspace,
          pytest.raises(LoginRefused, match=refusal) as refused):
        connect(workspace, "code", given="tok-test-wrong-0002")
    assert "tok-test-wrong-0002" not in str(refused.value) and store.names() == []


def test_a_slack_token_typed_at_the_prompt_is_checked_kept_and_kept_again_when_it_still_works(
        tmp_path: Path, store: FileSecrets) -> None:
    slack = SlackStandIn()
    asked: list[tuple[str, bool]] = []

    def ask(label: str, hidden: bool) -> str:
        asked.append((label, hidden))
        return SLACK_WORLD["token"]

    with open_workspace(tmp_path, store, {"chat": "kind: slack\nchannels: [alder-deal]\n"}, http=slack.client()) as workspace:
        first = connect(workspace, "chat", ask=ask)
        again = connect(workspace, "chat")  # off a terminal: the kept token works, so nothing needs asking
    assert asked == [("Slack token", True)] and store.get("chat") == SLACK_WORLD["token"]
    assert (first.name, first.checked, first.kept, again.kept, again.checked) == ("chat", True, False, True, True)
    assert [method for method, _ in slack.calls] == ["auth.test", "auth.test"]


def test_off_a_terminal_connect_asks_nothing_and_names_the_flags_and_the_secrets_call(
        tmp_path: Path, store: FileSecrets) -> None:
    with open_workspace(tmp_path, store, {"code": "kind: github\n", "work": "kind: linear\nauth: oauth\n"}) as workspace:
        with pytest.raises(NotATerminal) as refused:
            connect(workspace, "code")
        assert str(refused.value) == ("connect code: input is not a terminal, so nothing is asked: pass the github "
                                      "token with --stdin or --from-env NAME, or keep it with cerebro secrets set "
                                      "github")
        with pytest.raises(NotATerminal, match="signing in through the browser needs a person at a terminal"):
            connect(workspace, "work", open_browser=Browser())
    assert store.names() == []


def test_gmail_asks_for_the_address_and_the_app_password_and_logs_in_once_to_check_them(
        tmp_path: Path, store: FileSecrets) -> None:
    box = MailStandIn()
    answers = iter(["Ada@Quillmoor.example", "abcd efgh ijkl mnop"])
    with open_workspace(tmp_path, store, {"mail": "kind: gmail\n"}) as workspace:
        workspace.imap_connect = lambda host: box
        done = connect(workspace, "mail", ask=lambda label, hidden: next(answers))
    assert store.get("mail") == "Ada@Quillmoor.example:abcd efgh ijkl mnop" and done.who == "ada@quillmoor.example"
    assert box.commands == ["LOGIN ada@quillmoor.example ***", "LOGOUT"]


@pytest.mark.parametrize(("text", "line"), [
    ("kind: files\nselect: [{ glob: 'notes/*.md' }]\n", ("a files source keeps no login, so there is nothing to "
     "connect")),
    ("kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\n", ("a database source's login is its "
     "connection string, which connect does not ask for: keep it with cerebro secrets set warehouse-dsn")),
    ("kind: salesforce\norg: work\n", ("salesforce logs in through the salesforce cli, and cerebro keeps nothing for "
     "it: run sf org login web --alias work")),
    ("kind: linear\nurl: https://mcp.elsewhere.example/mcp\n", ("url mcp.elsewhere.example: it is not linear's server, "
     "so the linear login is not sent there")),
])
def test_a_source_with_no_login_to_keep_or_one_that_points_elsewhere_is_refused_with_one_line(
        tmp_path: Path, store: FileSecrets, text: str, line: str) -> None:
    with open_workspace(tmp_path, store, {"work": text}) as workspace, pytest.raises(ConnectError) as refused:
        connect(workspace, "work", given="tok-test-0001", ask=never)
    assert line in str(refused.value) and "\n" not in str(refused.value) and store.names() == []


def test_the_file_says_whether_a_token_or_a_browser_sign_in_is_needed(tmp_path: Path) -> None:
    files = {"g": "kind: granola\n", "o": "kind: granola\nauth: oauth\n",
             "m": "kind: mcp\nurl: https://wiki.example/mcp\nauth: oauth\nbinding: contract\n",
             "t": "kind: mcp\nurl: https://wiki.example/mcp\nauth: { keychain: t-token }\nbinding: contract\n",
             "c": "kind: calendar\n", "p": "kind: gong\n", "e": "kind: gmail\n", "s": "kind: salesforce\n"}
    root = make_workspace(tmp_path / "workspace", files)
    found = {name: need(read_source(root / "sources" / f"{name}.yaml")) for name in files}
    assert {name: (n.way, n.name) for name, n in found.items()} == {
        "g": ("token", "granola"), "o": ("browser", "granola"), "m": ("browser", "m-login"), "t": ("token", "t-token"),
        "c": ("address", "calendar_c"), "p": ("pair", "gong"), "e": ("pair", "e"), "s": ("cli", "")}
    assert found["o"].server == GranolaConnector.mcp_server and found["m"].server == "https://wiki.example/mcp"
    assert found["t"].server == "https://wiki.example/mcp" and found["e"].parts == ("Gmail address", "App password")
    assert "sf org login web --alias work" in found["s"].what
    (root / "sources" / "x.yaml").write_text("kind: linear\nauth: magic\n")
    with pytest.raises(ConnectError, match="auth magic: write auth: oauth to sign in through the browser, or auth: "
                                           r"\{ keychain: linear \} to keep a token"):
        need(read_source(root / "sources" / "x.yaml"))


def test_a_pair_login_is_typed_unseen_in_both_halves_unless_its_connector_says_one_may_be_seen() -> None:
    from cerebro.connect import _read
    from cerebro.connectors.connecting import Need
    from cerebro.source import SourceFile

    asked: list[tuple[str, bool]] = []
    for shown in ((False, False), (True, False)):
        _read(SourceFile(name="x", kind="parts"), Need("pair", "x", "the parts key", parts=("Key", "Secret"),
                                                       shown=shown), None, lambda label, hidden: asked.append(
            (label, hidden)) or "typed")
    assert asked == [("Key", True), ("Secret", True), ("Key", False), ("Secret", True)]


@pytest.mark.parametrize(("text", "answers", "line"), [
    ("kind: calendar\n", ["http://cal.example/feed.ics"], "the calendar's address is not https"),
    ("kind: gmail\n", ["ada@quillmoor.example", ""], "nothing was given, so nothing was kept"),
    ("kind: github\n", ['{"access_token": "tok-test-0001"}'], "reads as a browser login, so it is not kept as a token"),
    ("kind: github\n", ["   "], ("nothing was given, so nothing was kept: run cerebro connect work when you have the "
                                 "github token")),
])
def test_what_a_person_gives_must_read_as_the_login_its_kind_needs_or_nothing_is_kept(
        tmp_path: Path, store: FileSecrets, text: str, answers: list[str], line: str) -> None:
    given = iter(answers)
    with open_workspace(tmp_path, store, {"work": text}) as workspace, pytest.raises(ConnectError) as refused:
        connect(workspace, "work", ask=lambda label, hidden: next(given))
    assert line in str(refused.value) and store.names() == []
    (tmp_path / "again").mkdir()
    with open_workspace(tmp_path / "again", store, {"work": "kind: gmail\n"}) as workspace, pytest.raises(ConnectError,
                                                                                                    match="no two parts"):
        connect(workspace, "work", given="ada@quillmoor.example")


def test_a_kept_token_the_provider_now_refuses_is_replaced_by_the_new_one_and_masked(
        tmp_path: Path, store: FileSecrets) -> None:
    store.set("github", "tok-test-revoked-0009")
    said: list[str] = []
    http = httpx.Client(transport=httpx.MockTransport(github))
    with open_workspace(tmp_path, store, {"code": "kind: github\n"}, http=http) as workspace:
        done = connect(workspace, "code", given=GITHUB_TOKEN, say=said.append)
        assert workspace.mask("old tok-test-revoked-0009") == "old [secret]"
    assert not done.kept and store.get("github") == GITHUB_TOKEN
    assert said == ["The github token kept as github no longer works, so a new one is asked for."]


def test_a_refused_gmail_login_keeps_nothing(tmp_path: Path, store: FileSecrets) -> None:
    box = MailStandIn()

    def refuse(user: str, password: str) -> None:
        raise box.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    box.login = refuse  # type: ignore[method-assign]
    with open_workspace(tmp_path, store, {"mail": "kind: gmail\n"}) as workspace:
        workspace.imap_connect = lambda host: box
        with pytest.raises(LoginRefused, match="gmail refused the login for ada@quillmoor.example") as refused:
            connect(workspace, "mail", given="ada@quillmoor.example:abcd efgh ijkl mnop")
    assert "abcdefghijklmnop" not in str(refused.value) and store.names() == []
    assert str(refused.value).endswith(", so nothing was kept")


# -- a browser sign-in, and Linear read with it


def linear_server(recorded: RecordedLinear) -> MCPServer:
    """Linear's read tools over MCP, each taking the arguments its recorded schema lists and answering from the
    recorded replies."""
    server = MCPServer("linear")
    for tool in LINEAR_TOOLS:
        def answer(_tool: str = tool, **arguments: Any) -> Any:
            return recorded(_tool, {k: v for k, v in arguments.items() if v is not None})

        answer.__signature__ = inspect.Signature([  # type: ignore[attr-defined]
            inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Any)
            for key in recorded.schemas[tool]["properties"]])
        server.add_tool(answer, name=tool, structured_output=False)
    return server


class Guarded(Served):
    """The MCP server in this process, taking a request only with a bearer token the authorization server still
    takes, and keeping each bearer it was sent."""

    def __init__(self, server: MCPServer, live: set[str]):
        super().__init__(server)
        self.live, self.bearers = live, []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        given = request.headers.get("authorization", "")
        self.bearers.append(given)
        if given.removeprefix("Bearer ") not in self.live:
            await request.aread()
            return httpx2.Response(401, json={"error": "invalid_token"})
        return await super().handle_async_request(request)


@pytest.fixture
def signing(loopback_only: None) -> Iterator[tuple[AuthStandIn, Guarded, RecordedLinear]]:  # noqa: F811
    standin = AuthStandIn()
    recorded = RecordedLinear()
    guarded = Guarded(linear_server(recorded), standin.live)
    yield standin, guarded, recorded
    guarded.stop()
    standin.stop()


def test_linear_signs_in_through_the_browser_and_lists_through_the_mcp_client_after_its_token_expires(
        tmp_path: Path, store: FileSecrets, signing: tuple[AuthStandIn, Guarded, RecordedLinear],
        monkeypatch: pytest.MonkeyPatch) -> None:
    standin, guarded, recorded = signing
    monkeypatch.setattr(LinearConnector, "mcp_server", standin.server)
    clock, browser, said = Clock(), Browser(), []
    http = httpx.Client(timeout=5.0)
    text = "kind: linear\nauth: oauth\nteams: [ENG]\nwindow: { behind: all }\n"
    with open_workspace(tmp_path, store, {"work": text}, http=http, clock=clock) as workspace:
        workspace.mcp_open = lambda where: McpClient(replace(where, transport=guarded))
        done = connect(workspace, "work", ask=never, open_browser=browser, say=said.append)
        assert (done.name, done.way, done.where, done.checked, done.kept) == ("linear", "browser", store.where(), True,
                                                                              False)
        first = Login.from_json(store.get("linear"), "linear")
        assert first.access_token == "tok-test-0003" and recorded.asked("list_teams") == [{"limit": 1}]
        assert workspace.mask("Bearer tok-test-0003") == "Bearer [secret]" and len(browser.opened) == 1

        again = connect(workspace, "work", ask=never, open_browser=browser)  # the kept login still works
        assert again.kept and len(browser.opened) == 1

        clock.at += timedelta(hours=2)
        standin.live.discard(first.access_token)  # the server no longer takes the expired token
        token = LiveToken(store, "linear", standin.server, http=http, now=clock, source="work")
        where = Reach("work", url=standin.server, credentials=token, transport=guarded)
        with McpClient(where) as client:
            listed = every_page(LinearConnector(read_source(workspace.root / "sources" / "work.yaml"),
                                                client.reader(*LINEAR_TOOLS), now=clock))
    assert listed.records and len(browser.opened) == 1  # renewed with no person
    renewed = Login.from_json(store.get("linear"), "linear")
    assert renewed.access_token == "tok-test-0004" and renewed.expiry == clock.at + timedelta(hours=1)
    assert guarded.bearers[-1] == "Bearer tok-test-0004" and "Bearer tok-test-0003" not in guarded.bearers[-3:]
    assert not [line for line in said if "tok-test" in line or "refresh-test" in line]


def test_a_linear_token_is_checked_through_its_mcp_server_and_a_refused_one_keeps_nothing(
        tmp_path: Path, store: FileSecrets, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LinearConnector, "mcp_server", "http://127.0.0.1:9/mcp")  # served in this process, never dialed
    recorded = RecordedLinear()
    guarded = Guarded(linear_server(recorded), {"tok-test-linear-0001"})
    try:
        with open_workspace(tmp_path, store, {"work": "kind: linear\nauth: { keychain: linear }\n"}) as workspace:
            workspace.mcp_open = lambda where: McpClient(replace(where, transport=guarded))
            # The MCP SDK gives a 401 as an error of its own; the client notes the 401 and says the login was
            # refused, so connect keeps nothing and says why.
            with pytest.raises(LoginRefused, match="work: the server refused the login, so nothing was kept"):
                connect(workspace, "work", given="tok-test-linear-wrong")
            assert store.names() == []
            done = connect(workspace, "work", given="tok-test-linear-0001")
    finally:
        guarded.stop()
    assert (done.name, done.way, done.checked) == ("linear", "token", True) and store.get("linear") == (
        "tok-test-linear-0001")
    assert recorded.asked("list_teams") == [{"limit": 1}] and guarded.bearers[-1] == "Bearer tok-test-linear-0001"


def test_a_browser_login_whose_server_will_not_read_with_it_is_not_kept(
        tmp_path: Path, store: FileSecrets, signing: tuple[AuthStandIn, Guarded, RecordedLinear],
        monkeypatch: pytest.MonkeyPatch) -> None:
    standin, guarded, _ = signing
    monkeypatch.setattr(LinearConnector, "mcp_server", standin.server)
    with open_workspace(tmp_path, store, {"work": "kind: linear\nauth: oauth\n"}, http=httpx.Client(timeout=5.0)) as workspace:
        workspace.mcp_open = lambda where: McpClient(replace(where, transport=guarded))
        guarded.live.clear()
        standin.live = set()  # every token it hands out, renewed or not, is refused by the MCP server
        with pytest.raises(ConnectError, match="so nothing was kept") as refused:
            connect(workspace, "work", ask=never, open_browser=Browser())
    assert "tok-test" not in str(refused.value) and "refresh-test" not in str(refused.value)
    with pytest.raises(SecretNotFound):
        store.get("linear")


def test_a_token_is_not_taken_for_a_file_that_signs_in_through_the_browser(tmp_path: Path, store: FileSecrets) -> None:
    refusal = (r"says auth: oauth, which signs in through the browser, so a token is not taken: write auth: \{ "
               r"keychain: granola \}")
    with (open_workspace(tmp_path, store, {"work": "kind: granola\nauth: oauth\n"}) as workspace,
          pytest.raises(ConnectError, match=refusal)):
        connect(workspace, "work", given="tok-test-0001")
    with pytest.raises(SecretNotFound):
        store.get("granola")
