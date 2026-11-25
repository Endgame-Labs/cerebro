"""The app builds each source's connector with what it needs: its login from the workspace's secrets store, the HTTP
client or session opener, who is on our side, and the workspace's clock. Every value read from the store is masked in
each failure line, log row and transcript the app gives or writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from gmail_standin import Mail, StandIn
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from suites.connector import every_page
from test_connector_github import StandIn as GithubStandIn
from test_connector_granola import StandIn as GranolaStandIn
from test_connector_linear import RecordedLinear
from test_connector_slack import StandIn as SlackStandIn

from cerebro import Cerebro
from cerebro.agent import Agent
from cerebro.app import Masked
from cerebro.article import Identity
from cerebro.connector import Connector, Listing, Ours, Record
from cerebro.connectors.gong import GongKey, GongKeyMissing
from cerebro.connectors.granola import READ_TOOLS as GRANOLA_TOOLS
from cerebro.connectors.granola import NotLoggedIn as GranolaNotLoggedIn
from cerebro.connectors.granola import Reply as GranolaReply
from cerebro.connectors.linear import READ_TOOLS as LINEAR_TOOLS
from cerebro.connectors.linear import LinearConnector
from cerebro.connectors.mcp_source_client import McpClient, McpError, Reach, ToolNotOffered, ToolRefused, reach
from cerebro.connectors.salesforce import NotLoggedIn
from cerebro.evidence import Evidence
from cerebro.fakes import FakeDecider, FakeExtraction, FakeSecrets
from cerebro.run import AgentRun, Entry, Result, Run
from cerebro.secrets import FileSecrets, SecretsError
from cerebro.source import SourceFile

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
SECRET = "sk-test-0123456789abcdef"
SLACK_TOKEN = "xoxp-fixture-0000-token"  # the token the test Slack workspace takes
FEED = ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Fixture//Test//EN\nBEGIN:VEVENT\nUID:qbr@fixture.example\n"
        "DTSTART:20261118T170000Z\nDTEND:20261118T180000Z\nSUMMARY:Acme QBR\n"
        "ATTENDEE;CN=Wile Coyote:mailto:wile@acme.example\nATTENDEE;CN=Casey:mailto:casey@eu.vandelay.example\n"
        "END:VEVENT\nEND:VCALENDAR\n")
WEEKLY = "---\nheartbeat: [change]\n---\n\n# Instructions\n\nSum up the week.\n\n# Output\n\n## Summary\n"


def make_workspace(root: Path, sources: dict[str, str] | None = None, settings: str = "") -> Path:
    root.mkdir(exist_ok=True)
    (root / "settings.yaml").write_text(settings or "timezone: UTC\n")
    (root / "sources").mkdir()
    for name, text in (sources or {}).items():
        (root / "sources" / f"{name}.yaml").write_text(text)
    return root


def open_workspace(root: Path, **options: Any) -> Cerebro:
    return Cerebro(root, extraction=FakeExtraction(), clock=lambda: NOW, **options)


def test_a_workspace_with_no_id_loads_and_a_source_that_needs_a_login_says_in_one_line_what_to_add(
        tmp_path: Path) -> None:
    with open_workspace(make_workspace(tmp_path, {"team": "kind: calendar\n"})) as workspace:
        assert workspace.secrets is None and list(workspace.connectors) == ["team"]
        line = workspace.run_once().failed["sources/team"]
    assert "\n" not in line
    assert line.endswith("settings.yaml has no id, so no secrets store is open: add id: to settings.yaml, then "
                         "run cerebro connect team")


def test_a_workspace_with_an_id_opens_its_own_secrets_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    with open_workspace(make_workspace(tmp_path / "workspace", settings="id: alder-wiki\n")) as workspace:
        assert isinstance(workspace.secrets, FileSecrets)
        assert str(workspace.secrets.where()).endswith("alder-wiki.json")
        said = (f"secrets: CEREBRO_SECRETS_STORE=file asks for it, so this workspace's keys and logins are kept in plain "
                f"text in {workspace.secrets.where()}, which only your user can read")
        assert [n for n in workspace.notices if n.startswith("secrets:")] == [said]


def test_a_calendar_reads_its_feed_at_the_stored_address_with_our_side_and_the_workspaces_clock(tmp_path: Path) -> None:
    root = make_workspace(tmp_path, {"calendar": "kind: calendar\n"})
    (root / "workspace.md").write_text("---\nidentity:\n  name: Vandelay\n  domains: [Vandelay.example]\n---\n")
    (root / "profile.md").write_text("---\nidentity:\n  email: [jane.doe@vandelay.example]\n---\n")
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, text=FEED)

    http = httpx.Client(transport=httpx.MockTransport(answer))
    with open_workspace(root, secrets=FakeSecrets({"calendar": "webcal://cal.example/private-feed.ics"}),
                    http=http) as workspace:
        calendar = workspace.connectors["calendar"]
        assert workspace.ours == Ours(("jane.doe@vandelay.example",), ("vandelay.example",))
        assert calendar.now is workspace.clock
        assert workspace.run_once().listed == {"calendar": 1}
        assert calendar.get("qbr@fixture.example").keys == {"email": ["wile@acme.example"], "domain": ["acme.example"],
                                                            "calendar_event_id": ["qbr@fixture.example#20261118"]}
    assert set(asked) == {"https://cal.example/private-feed.ics"}


def test_a_feed_that_no_longer_answers_says_why_without_its_address(tmp_path: Path) -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    with open_workspace(make_workspace(tmp_path, {"calendar": "kind: calendar\n"}), http=http,
                    secrets=FakeSecrets({"calendar": "https://cal.example/private-feed.ics"})) as workspace:
        line = workspace.run_once().failed["sources/calendar"]
    assert "private-feed" not in line and "(HTTP 404)" in line


def test_gong_sources_read_the_one_stored_key_and_share_one_throttle(tmp_path: Path) -> None:
    root = make_workspace(tmp_path, {"calls": "kind: gong\n", "calls-eu": "kind: gong\nurl: https://eu.api.gong.io\n"})
    with open_workspace(root, secrets=FakeSecrets({"gong": " access-key-1 : gong-secret-1 "})) as workspace:
        calls, eu = workspace.connectors["calls"], workspace.connectors["calls-eu"]
        assert calls.throttle is eu.throttle
        assert calls.http is eu.http is workspace.http and calls.ours is workspace.ours and calls.now is workspace.clock
        assert calls.credentials() == GongKey("access-key-1", "gong-secret-1")
        assert workspace.mask("key access-key-1, secret gong-secret-1") == "key [secret], secret [secret]"
        workspace.secrets.set("gong", "no-colon-here")
        with pytest.raises(SecretsError, match="run cerebro connect calls --replace with the access key and its secret"):
            calls.credentials()
    with open_workspace(root) as workspace, pytest.raises(GongKeyMissing, match="settings.yaml has no id"):
        workspace.connectors["calls"].credentials()


def test_github_reads_with_the_stored_token_through_the_workspaces_client_and_clock(tmp_path: Path) -> None:
    issue = {"number": 2, "title": "Flaky export", "state": "OPEN", "author": "ja--ne", "created": "2026-11-16T00:00:00Z",
             "updated": "2026-11-16T00:00:00Z", "comments": []}
    github = GithubStandIn({"users": {"ja--ne": "Jane Doe"}, "repos": [{"name": "acme/api", "pulls": [], "issues": [issue],
                                                                     "releases": []}]}, token="ghp_synthetic_token")
    http = httpx.Client(transport=httpx.MockTransport(github.handle))
    root = make_workspace(tmp_path, {"code": "kind: github\nrepos: [acme/api]\n"})
    with open_workspace(root, secrets=FakeSecrets({"github": "ghp_synthetic_token"}), http=http) as workspace:
        assert workspace.run_once().listed == {"code": 1}
        assert workspace.connectors["code"].now is workspace.clock
        assert workspace.mask("token ghp_synthetic_token") == "token [secret]"
    assert github.writes == []


def test_gmail_logs_in_with_the_stored_address_and_app_password_and_lists_the_mailbox(tmp_path: Path) -> None:
    box = StandIn()
    box.deliver(Mail(sender="Wile Coyote <wile@acme.example>", subject="Renewal", to="ada@quillmoor.example",
                     date=datetime(2026, 11, 16, 9, 0, tzinfo=UTC), text="Shall we talk pricing?"), msgid=101)
    hosts: list[str] = []
    login = "Ada@Quillmoor.example:abcd efgh ijkl mnop"
    with open_workspace(make_workspace(tmp_path, {"mail": "kind: gmail\n"}), secrets=FakeSecrets({"mail": login})) as workspace:
        workspace.imap_connect = lambda host: hosts.append(host) or box
        assert workspace.run_once().listed == {"mail": 1}
        assert workspace.mask("password abcdefghijklmnop") == "password [secret]"
    assert hosts == ["imap.gmail.com"] and "LOGIN ada@quillmoor.example ***" in box.commands


def test_salesforce_logs_in_through_the_sf_cli_and_reads_the_accounts_the_store_holds_as_admitted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fake_sf

    root = make_workspace(tmp_path, {"crm": "kind: salesforce\norg: work\n"})
    cli = fake_sf.install(monkeypatch, tmp_path / "bin")
    with open_workspace(root) as workspace:
        crm = workspace.connectors["crm"]
        assert crm.ours is workspace.ours  # so the login's own address is never an email's key
        login = crm.login()
        assert (login.token, login.instance, login.org) == (fake_sf.TOKEN, fake_sf.INSTANCE, "work")
        assert fake_sf.calls(cli) == ["org display --target-org work --json",
                                      "org auth show-access-token --target-org work --json"]
        assert fake_sf.TOKEN not in repr(login)
        assert workspace.mask(f"Bearer {fake_sf.TOKEN}") == "Bearer [secret]"
        workspace.store.create(Identity("accounts/acme", "accounts", "Acme", {"salesforce_id": ["001Hs00000ACME01AAA"]}))
        workspace.store.create(Identity("accounts/gone", "accounts", "Gone", {"salesforce_id": ["001Hs00000GONE01AAA"]},
                                    status="archived"))
        workspace.store.create(Identity("people/wile", "people", "Wile", {"salesforce_id": ["003Hs00000WILE01AAA"]}))
        assert crm.accounts() == ["001Hs00000ACME01AAA"]
        fake_sf.write(cli, "nologin")
        with pytest.raises(NotLoggedIn, match="no login for work: run sf org login web --alias work"):
            crm.login()


class Leaky(Connector):
    """A source whose failure carries the login it read, as a careless error message would."""

    name = "leaky"

    def __init__(self) -> None:
        self.login: Any = None

    def list(self, since: str | None = None) -> Listing:
        raise RuntimeError(f"refused the token {self.login()}")


class KeyedRun(AgentRun):
    """A run that reads its model's key, as a real one would, and whose transcript and note carry it."""

    def __init__(self) -> None:
        self.key: Any = None

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Any) -> Run:
        key = self.key()
        return Run(id="run_1", agent=agent.id, target=agent.target, status="done", result=Result("## Summary\n\nQuiet."),
                   transcript=[Entry("call", tool="fetch", args={"headers": [f"Bearer {key}"]}),
                               Entry("tool", f"echoed {key}", tool="fetch")], note=f"used {key}")


def test_every_value_read_from_the_store_is_masked_in_failure_lines_log_rows_and_transcripts(tmp_path: Path) -> None:
    root = make_workspace(tmp_path)
    (root / "agents" / "reports").mkdir(parents=True)
    (root / "agents" / "reports" / "weekly.md").write_text(WEEKLY)
    leaky, keyed = Leaky(), KeyedRun()
    with open_workspace(root, secrets=FakeSecrets({"gong": SECRET, "fireworks": "fw-" + SECRET}), connectors=[leaky],
                    agent_run=keyed, decider=FakeDecider()) as workspace:
        leaky.login = workspace.login(SourceFile(name="leaky", kind="gong"))
        keyed.key = workspace.model_key(workspace.models["compilation"])
        result = workspace.run_once()
        assert result.failed["sources/leaky"] == "RuntimeError: refused the token [secret]"
        [run] = result.runs
        assert run.note.startswith("used [secret]; ") and SECRET not in run.note
        assert [(e.text, e.args) for e in run.transcript] == [("", {"headers": ["Bearer [secret]"]}),
                                                             ("echoed [secret]", {})]
        assert workspace.store.append("mcp-requests", {"args": {"q": SECRET}}).data == {"args": {"q": "[secret]"}}
        from cerebro.curate import Curated
        from cerebro.records import TellResult

        assert workspace.masked(TellResult("r_1", notes=[f"saw {SECRET}"])).notes == ["saw [secret]"]
        from cerebro.app import Masked
        from cerebro.fakes import FakeConnector

        pasted = Record(id="i1", source="issues", revision="1", body=f"token {SECRET}", fields={"note": SECRET})
        record = Masked(FakeConnector("issues", [pasted]), lambda: workspace.mask).get("i1")
        assert record.body == "token [secret]" and record.fields == {"note": "[secret]"}
        assert workspace.masked(Curated(notes=[SECRET], run=run)).notes == ["[secret]"]


def test_the_workspace_builds_its_decider_from_the_stored_key_and_says_once_when_it_has_none(tmp_path: Path) -> None:
    from cerebro.models import HttpDecider

    with open_workspace(make_workspace(tmp_path / "keyed"), secrets=FakeSecrets({"typesafe": "ts-" + SECRET})) as workspace:
        assert isinstance(workspace.decider.inner, HttpDecider) and not any("decision" in n for n in workspace.notices)
    with open_workspace(make_workspace(tmp_path / "bare"), secrets=FakeSecrets({})) as workspace:
        assert workspace.decider is None
        [line] = [n for n in workspace.notices if "models.decision" in n]
        assert "TYPESAFE_API_KEY is not set" in line and "run without a model" in line


# -- sources over MCP


class Session:
    """An open MCP session as the app holds one: `reader` gives a call limited to the tools named, which answers
    from `serve` as the MCP client's reader does, plain data, and `ToolRefused` for a reply marked an error."""

    def __init__(self, serve: Any) -> None:
        self.serve, self.readers, self.closed = serve, [], False

    def reader(self, *tools: str) -> Any:
        self.readers.append(tools)

        def call(tool: str, arguments: dict[str, Any] | None = None) -> Any:
            if tool not in tools:
                raise ToolNotOffered(f"{tool} is not a tool this source reads with")
            reply = self.serve(tool, arguments or {})
            if isinstance(reply, GranolaReply):
                if reply.is_error:
                    raise ToolRefused(f"{tool} returned an error", tool, reply.text)
                return reply.text
            return reply

        return call

    def close(self) -> None:
        self.closed = True


def test_linear_reads_its_readonly_server_with_the_stored_token_through_a_reader_of_its_own_tools(
        tmp_path: Path) -> None:
    opened: list[Reach] = []
    session = Session(RecordedLinear())
    root = make_workspace(tmp_path, {"work": "kind: linear\nteams: [ENG]\nwindow: { behind: all }\n"})
    with open_workspace(root, secrets=FakeSecrets({"linear": "lin_api_synthetic_token"})) as workspace:
        workspace.mcp_open = lambda where: opened.append(where) or session
        assert workspace.run_once().listed["work"] > 0
        assert [(where.url, where.credentials()) for where in opened] == [(LinearConnector.mcp_server,
                                                                           "lin_api_synthetic_token")]
        assert workspace.mask("Bearer lin_api_synthetic_token") == "Bearer [secret]"
        assert workspace.connectors["work"].now is workspace.clock
    assert set(session.readers) == {LINEAR_TOOLS} and session.closed  # the app closes the session it opened


def test_granola_reads_its_server_through_a_reader_and_an_expired_login_says_what_to_do(tmp_path: Path) -> None:
    standin = GranolaStandIn()
    session = Session(standin)
    root = make_workspace(tmp_path, {"granola": "kind: granola\nauth: { keychain: granola }\n"})
    with open_workspace(root, secrets=FakeSecrets({"granola": "grn_synthetic_token"})) as workspace:
        workspace.mcp_open = lambda where: session
        assert workspace.run_once().listed["granola"] > 0 and session.readers[0] == GRANOLA_TOOLS
        standin.replies["list_meetings"] = GranolaReply("Access token expired", is_error=True)
        with pytest.raises(GranolaNotLoggedIn, match="the granola login is gone or expired"):
            workspace.connectors["granola"].list()


@pytest.mark.parametrize(("text", "line"), [
    ("kind: linear\nauth: magic\nteams: [ENG]\n", ("sources/work.yaml auth magic: write auth: oauth to sign in "
     "through the browser with cerebro connect work, or auth: { keychain: linear } to keep a token")),
    ("kind: linear\nurl: https://mcp.elsewhere.example/mcp\n", ("url mcp.elsewhere.example: it is not linear's "
     "server, so the linear login is not sent there")),
    ("kind: granola\ncommand: granola-server\n", "a granola source reads granola's own server: delete command"),
    ("kind: mcp\nauth: oauth\ncommand: wiki-server\nbinding: contract\n", ("auth oauth: a browser sign-in "
     "reaches a server over http: add url")),
    ("kind: mcp\nurl: https://collector.example/mcp\nauth: { keychain: github }\nbinding: contract\n",
     "an mcp source reads only a secret named for itself: keep its token with cerebro secrets set work-token"),
    ("kind: mcp\nurl: https://collector.example/mcp\nauth: { keychain: workday }\nbinding: contract\n",
     "auth keychain: workday: an mcp source reads only a secret named for itself"),
])
def test_a_source_that_needs_a_browser_sign_in_or_points_elsewhere_is_refused_with_one_line(
        tmp_path: Path, text: str, line: str) -> None:
    with open_workspace(make_workspace(tmp_path, {"work": text})) as workspace:
        [refused] = workspace.refused
    assert line in refused and "\n" not in refused and "work" not in workspace.connectors


def test_an_mcp_source_reads_its_server_with_its_token_and_its_variables_from_the_store(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIKI_TOKEN", raising=False)
    server = MCPServer("wiki")
    pages = {"p1": "The launch moved to March."}

    @server.tool(name="cerebro.list", annotations=ToolAnnotations(read_only_hint=True))
    def listing(since: str) -> dict[str, Any]:
        return {"ids": sorted(pages)}

    @server.tool(name="cerebro.get", annotations=ToolAnnotations(read_only_hint=True))
    def get(id: str) -> dict[str, Any]:
        return {"id": id, "revision": "1", "text": pages[id]}

    opened: list[Reach] = []

    def open_session(where: Reach) -> McpClient:
        opened.append(where)
        return McpClient(replace(where, server=server))

    root = make_workspace(tmp_path, {"wiki": "kind: mcp\nurl: https://wiki.example/mcp\nauth: { keychain: wiki-token }\n"
                                         "binding: contract\n",
                                 "notes": "kind: mcp\ncommand: notes-server --stdio\nenv: [WIKI_TOKEN]\n"
                                          "binding: contract\n"})
    secrets = FakeSecrets({"wiki-token": "wiki-synthetic-token", "notes-WIKI_TOKEN": "notes-synthetic-value"})
    with open_workspace(root, secrets=secrets) as workspace:
        workspace.mcp_open = open_session
        assert workspace.run_once().listed == {"notes": 1, "wiki": 1}
        assert workspace.connectors["wiki"].now is workspace.clock
        notes, wiki = opened
        assert wiki.url == "https://wiki.example/mcp" and wiki.credentials() == "wiki-synthetic-token"
        assert workspace.mask("wiki-synthetic-token notes-synthetic-value") == "[secret] [secret]"
    assert notes.command == ["notes-server", "--stdio"] and notes.env["WIKI_TOKEN"] == "notes-synthetic-value"


def test_slack_reads_with_the_stored_token_and_only_the_channels_admitted_articles_bind(tmp_path: Path) -> None:
    slack = SlackStandIn()
    http = httpx.Client(transport=httpx.MockTransport(slack.handle))
    root = make_workspace(tmp_path, {"chat": "kind: slack\nchannels: [acme-corp-deal, quillmoor-northwind]\n"
                                         "window: { behind: all }\n"})
    with open_workspace(root, secrets=FakeSecrets({"chat": SLACK_TOKEN}), http=http) as workspace:
        workspace.store.create(Identity("accounts/acme", "accounts", "Acme", {"slack_channels": ["acme-corp-deal"]}))
        workspace.store.create(Identity("accounts/gone", "accounts", "Gone", {"slack_channels": ["quillmoor-northwind"]},
                                    status="archived"))
        workspace.run_once()
        listed = every_page(workspace.connectors["chat"]).records
        assert {r.keys["slack_channels"][1] for r in listed} == {"acme-corp-deal"}
        assert workspace.connectors["chat"].now is workspace.clock and workspace.connectors["chat"].ours is workspace.ours
        assert workspace.mask(f"token {SLACK_TOKEN}") == "token [secret]"


def test_a_kept_browser_sign_in_is_never_sent_as_a_token_nor_to_another_server(tmp_path: Path) -> None:
    from cerebro.app import TokenIsLogin
    from cerebro.connectors.mcp_oauth import Login
    from cerebro.connectors.mcp_source_client import _headers_class

    login = Login(server="https://mcp.wiki.example/mcp", token_endpoint="https://mcp.wiki.example/token",
                  client_id="c1", access_token="wiki-access-synthetic", refresh_token="wiki-refresh-synthetic")
    wiki = SourceFile(name="wiki", kind="mcp", url="https://wiki.example/mcp", auth="keychain: wiki-token")
    with (open_workspace(make_workspace(tmp_path), secrets=FakeSecrets({"wiki-token": login.to_json()})) as workspace,
          pytest.raises(TokenIsLogin, match="holds a browser sign-in, not a token, so it is not sent")):
        workspace.token(wiki)()

    class Bound:
        server = "https://mcp.wiki.example/mcp"

        def __call__(self) -> str:
            return "wiki-access-synthetic"

    import httpx2
    signer = _headers_class()(Bound())
    with pytest.raises(McpError, match="the browser login for mcp.wiki.example is not sent to elsewhere.example"):
        signer._sign(httpx2.Request("POST", "https://elsewhere.example/mcp"))
    request = httpx2.Request("POST", "https://mcp.wiki.example/mcp")
    signer._sign(request)
    assert request.headers["Authorization"] == "Bearer wiki-access-synthetic"


def test_an_mcp_login_goes_over_https_or_to_this_machine_alone(tmp_path: Path) -> None:
    def source(url: str) -> SourceFile:
        return SourceFile(name="wiki", kind="mcp", url=url, auth="keychain: wiki-token")

    with pytest.raises(McpError, match="a login is sent over https alone, or over http to this machine"):
        reach(source("http://collector.example/mcp"), tmp_path, credentials=lambda: "t")
    for url in ("http://127.0.0.1:8080/mcp", "http://localhost/mcp", "https://wiki.example/mcp"):
        assert reach(source(url), tmp_path, credentials=lambda: "t").url == url


def test_a_calendar_feed_follows_a_redirect_on_https_alone(tmp_path: Path) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cal.example":
            return httpx.Response(302, headers={"location": "https://cdn.example/feed.ics"})
        if request.url.host == "cdn.example":
            return httpx.Response(301, headers={"location": "http://collector.example/feed.ics"})
        raise AssertionError("the address went in clear")

    http = httpx.Client(transport=httpx.MockTransport(answer))
    with open_workspace(make_workspace(tmp_path, {"calendar": "kind: calendar\n"}), http=http,
                    secrets=FakeSecrets({"calendar": "https://cal.example/private-feed.ics"})) as workspace:
        line = workspace.run_once().failed["sources/calendar"]
    assert "the feed redirects off https, so it is not followed" in line


def test_a_secret_pasted_into_a_record_is_masked_before_the_record_leaves_the_app(tmp_path: Path) -> None:
    issue = {"number": 2, "title": "Token in a log", "state": "OPEN", "author": "ja--ne", "body":
             "The export failed with Bearer ghp_synthetic_token in the log.", "created": "2026-11-16T00:00:00Z",
             "updated": "2026-11-16T00:00:00Z", "comments": []}
    github = GithubStandIn({"users": {"ja--ne": "Jane Doe"}, "repos": [{"name": "acme/api", "pulls": [],
                                                                       "issues": [issue], "releases": []}]},
                           token="ghp_synthetic_token")
    http = httpx.Client(transport=httpx.MockTransport(github.handle))
    root = make_workspace(tmp_path, {"code": "kind: github\nrepos: [acme/api]\n"})
    with open_workspace(root, secrets=FakeSecrets({"github": "ghp_synthetic_token"}), http=http) as workspace:
        code = workspace.connectors["code"]
        code.list()
        assert "Bearer [secret] in the log" in code.get("acme/api#2").body


def test_a_source_that_signs_in_through_the_browser_reaches_its_server_with_a_live_token_from_the_store(
        tmp_path: Path) -> None:
    from cerebro.connectors.mcp_oauth import LiveToken

    root = make_workspace(tmp_path, {"team": "kind: linear\nauth: oauth\nteams: [ENG]\n",
                                 "wiki": "kind: mcp\nurl: https://wiki.example/mcp\nauth: oauth\nbinding: contract\n"})
    reached: list[Any] = []

    def refuse(where: Any) -> Any:
        reached.append(where)
        raise RuntimeError("no server in a test")

    with open_workspace(root, secrets=FakeSecrets()) as workspace:
        assert workspace.refused == []
        workspace.mcp_open = refuse
        for name in ("team", "wiki"):
            with pytest.raises(Exception):  # noqa: B017 - the stand-in refuses to open any session
                workspace.connectors[name].list()
    tokens = {where.name: where.credentials for where in reached}
    assert all(isinstance(token, LiveToken) for token in tokens.values()) and len(tokens) == 2
    assert {(t.name, t.server) for t in tokens.values()} == {("linear", "https://mcp.linear.app/mcp/readonly"),
                                                            ("wiki-login", "https://wiki.example/mcp")}


def test_each_source_that_answers_questions_gets_the_workspaces_zone_and_who_is_on_our_side(tmp_path: Path) -> None:
    root = make_workspace(tmp_path, {"work": "kind: linear\nteams: [ENG]\n", "mail": "kind: gmail\n",
                                 "calls": "kind: gong\n", "notes": "kind: granola\nauth: { keychain: granola }\n",
                                 "crm": "kind: salesforce\norg: work\n", "team": "kind: calendar\n",
                                 "code": "kind: github\nrepos: [acme/api]\n",
                                 "chat": "kind: slack\nchannels: [acme-corp-deal]\n"},
                      settings="timezone: Asia/Tokyo\n")
    (root / "workspace.md").write_text("---\nidentity:\n  domains: [quillmoor.example]\n---\n")
    with open_workspace(root, secrets=FakeSecrets({})) as workspace:
        assert sorted(workspace.connectors) == ["calls", "chat", "code", "crm", "mail", "notes", "team", "work"]
        for name, connector in workspace.connectors.items():
            assert connector.questions and isinstance(connector, Masked), name
            if name != "chat":  # a slack row carries no day
                assert connector.inner.zone == workspace.zone, name
            if name != "code":  # github keys a row by its repository alone, never an address
                assert connector.inner.ours == workspace.ours and workspace.ours.domains == ("quillmoor.example",), name


def test_a_script_and_a_sql_line_are_masked_of_every_secret_the_store_holds_read_or_not(tmp_path: Path) -> None:
    import sqlite3

    from cerebro.evidence import Finished, Launch

    root = make_workspace(tmp_path, {"warehouse": "kind: database\nurl: sqlite:data/w.sqlite\n"})
    (root / "data").mkdir()
    db = sqlite3.connect(root / "data" / "w.sqlite")
    db.execute("create table t (note text)")
    db.execute("insert into t values ('pasted ghp_unread_123456 here')")
    db.commit()
    db.close()
    with open_workspace(root, secrets=FakeSecrets({"github": "ghp_unread_123456"})) as workspace:
        workspace.script_start = lambda launch: Finished(0, b"token=ghp_unread_123456\n")
        assert workspace.start_script(Launch(["x"], root, {})).stdout == b"token=[secret]\n"
        assert workspace.connectors["warehouse"].select("select note from t").rows == [["pasted [secret] here"]]


def test_a_kept_record_is_masked_of_every_secret_the_store_holds_before_it_is_kept(tmp_path: Path) -> None:
    from cerebro.caller import Caller
    from cerebro.fakes import notes_app

    with open_workspace(make_workspace(tmp_path), secrets=FakeSecrets({"github": "ghp_unread_123456"}),
                    apps=[notes_app()]) as workspace:
        kept = workspace.handle.keep(Caller("you", "person"), "note", "pasted ghp_unread_123456 here",
                                 {"collection": "notes", "title": "ghp_unread_123456"})
        stored = workspace.store.record(kept.id)
    assert stored.body == "pasted [secret] here" and stored.fields["title"] == "[secret]"
