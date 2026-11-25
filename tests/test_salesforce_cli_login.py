"""The login the Salesforce CLI holds, read through a fake `sf` on the PATH: a current CLI that shows a notice in place
of the token in `org display`, an older one that shows the token there and does not know `show-access-token`, one
with no login, one that hangs, one that prints a megabyte, and one whose token the stand-in org refuses. No real CLI
or org is reached, and every token is made up."""

from __future__ import annotations

import time
from pathlib import Path

import fake_sf
import httpx
import pytest
from click.testing import CliRunner
from test_connector_salesforce import FakeOrg, seed

from cerebro.connectors import salesforce
from cerebro.connectors.connecting import KeyRefused, KeyUnproven
from cerebro.connectors.salesforce import CliTrouble, NotLoggedIn, SalesforceConnector, run_sf, sf_login
from cerebro.source import SourceFile, Window


class Org(FakeOrg):
    """The stand-in org, refusing a token on userinfo as a real org was seen to: 403 Missing_OAuth_Token."""

    def __init__(self) -> None:
        super().__init__(seed())
        self.sent: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request.headers.get("Authorization", ""))
        if request.url.path == "/services/oauth2/userinfo" and \
                request.headers.get("Authorization") != "Bearer " + fake_sf.TOKEN:
            return httpx.Response(403, text="Missing_OAuth_Token")
        return super().__call__(request)


def login(org: str = "work") -> tuple[salesforce.SfLogin, list[str]]:
    masked: list[str] = []
    return sf_login(run_sf, org, masked.append), masked


def source(org: str = "work") -> SourceFile:
    return SourceFile(name="salesforce", kind="salesforce", window=Window(), settings={"org": org} if org else {})


class CheckedWorkspace:
    """What connect's check reads of a workspace: how sf runs, the mask, and the HTTP client to the stand-in org."""

    def __init__(self, org: Org) -> None:
        self.sf_run = run_sf
        self.masked: list[str] = []
        self.mask = type("Mask", (), {"add": lambda _, *values: self.masked.extend(values)})()
        self.http = httpx.Client(transport=httpx.MockTransport(org))


def test_a_current_cli_hands_the_token_over_through_show_access_token_and_never_the_notice(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = fake_sf.install(monkeypatch, tmp_path)
    got, masked = login()
    assert (got.token, got.instance, got.org, got.username) == (fake_sf.TOKEN, fake_sf.INSTANCE, "work", fake_sf.USER)
    assert masked == [fake_sf.TOKEN] and fake_sf.TOKEN not in repr(got)
    assert fake_sf.calls(cli) == ["org display --target-org work --json",
                                  "org auth show-access-token --target-org work --json"]  # --json: no prompt waits


def test_an_older_cli_without_show_access_token_hands_over_the_token_org_display_shows(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, "older")
    got, masked = login()
    assert got.token == fake_sf.TOKEN and masked == [fake_sf.TOKEN]


def test_a_cli_too_old_to_hand_over_a_token_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, "older", display={"accessToken": "hidden value"})
    with pytest.raises(CliTrouble, match="too old to hand over the access token.*sf update") as trouble:
        login()
    assert trouble.value.why == "the CLI is too old"


def test_a_reply_with_no_token_says_the_cli_may_be_newer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, token_result={"token": "moved"})
    with pytest.raises(CliTrouble, match="may be newer than cerebro: run sf org auth show-access-token --target-org "
                                         "work"):
        login()


def test_a_cli_with_no_login_names_the_org_and_the_command_that_logs_it_in(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, "nologin")
    with pytest.raises(NotLoggedIn, match="no login for work: run sf org login web --alias work, then try again"):
        login()
    with pytest.raises(CliTrouble, match="logged in to no org: run sf org login web --alias work"):
        login("")


def test_the_auth_url_a_verbose_reply_carries_is_masked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, display={"sfdxAuthUrl": "force://PlatformCLI::5Aep8madeup@acme.example"})
    _, masked = login()
    assert masked == ["force://PlatformCLI::5Aep8madeup@acme.example", fake_sf.TOKEN]


def test_a_cli_that_hangs_is_stopped_at_its_timeout_with_what_it_left_running(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, "hang")
    monkeypatch.setattr(salesforce, "SF_TIMEOUT", 1)
    began = time.monotonic()
    with pytest.raises(CliTrouble, match="did not finish in 1 seconds") as trouble:
        login()
    assert time.monotonic() - began < 15 and not trouble.value.answered  # the shell's sleep held its output too


def test_a_cli_that_prints_a_megabyte_is_a_reply_that_does_not_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, "megabyte")
    with pytest.raises(CliTrouble) as trouble:
        login()
    assert len(str(trouble.value)) < 400 and "xxxx" not in str(trouble.value)


def test_with_no_org_named_the_cli_default_else_its_one_org_else_a_line_naming_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path / "a", orgs=("work", "sales"), default="sales")
    assert login("")[0].org == "sales"
    fake_sf.install(monkeypatch, tmp_path / "b", orgs=("work",))
    assert login("")[0].org == "work"
    fake_sf.install(monkeypatch, tmp_path / "c", orgs=("work", "sales"))
    with pytest.raises(CliTrouble, match=r"logged in to 2 orgs \(work, sales\) and has no default: write org: <alias> in "
                                         r"sources/salesforce.yaml"):
        login("")


def test_connect_proves_the_login_by_asking_who_it_is_and_names_the_org_it_asked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = fake_sf.install(monkeypatch, tmp_path)
    org = Org()
    workspace = CheckedWorkspace(org)
    closing, text = SalesforceConnector.check_cli(source(""), workspace)
    assert "as ada@quillmoor.example, in the org work," in closing and text == "kind: salesforce\norg: work\n"
    assert fake_sf.calls(cli)[:2] == ["config get target-org --json", "org list --json"]
    assert [r.url.path for r in org.requests] == ["/services/data/", "/services/oauth2/userinfo"]
    assert workspace.masked == [fake_sf.TOKEN]


def test_connect_refuses_a_token_the_org_refuses_and_says_what_to_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, "refused")
    with pytest.raises(KeyRefused, match="the org refused ada@quillmoor.example's login for the org work \\(status 403 "
                                         "Missing_OAuth_Token\\): run sf org login web --alias work") as refused:
        SalesforceConnector.check_cli(source(), CheckedWorkspace(Org()))
    assert refused.value.why == "the org refused the login" and fake_sf.REFUSED not in str(refused.value)


@pytest.mark.parametrize(("mode", "error", "why"), [("nologin", KeyRefused, "the CLI has no login"),
                                                    ("megabyte", KeyRefused, "the CLI has no login")])
def test_connect_with_no_login_writes_nothing_and_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
                                                          error: type, why: str) -> None:
    fake_sf.install(monkeypatch, tmp_path, mode)
    with pytest.raises(error) as refused:
        SalesforceConnector.check_cli(source(), CheckedWorkspace(Org()))
    assert refused.value.why == why


def test_connect_with_a_cli_that_hangs_cannot_tell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, "hang")
    monkeypatch.setattr(salesforce, "SF_TIMEOUT", 1)
    with pytest.raises(KeyUnproven) as unproven:
        SalesforceConnector.check_cli(source(), CheckedWorkspace(Org()))
    assert unproven.value.why == "the CLI did not answer"


def test_plain_connect_salesforce_checks_the_login_and_adds_the_file_with_no_view(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import cli
    from cerebro.app import Cerebro

    fake_sf.install(monkeypatch, tmp_path / "bin", default="work")
    client = httpx.Client(transport=httpx.MockTransport(Org()))
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, http=client))
    root = tmp_path / "team"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    monkeypatch.setenv("NO_COLOR", "1")
    done = CliRunner().invoke(cli.main, ["connect", "salesforce", "--workspace", str(root)])
    assert done.exit_code == 0, done.output
    assert "Salesforce connection verified" in done.output and "Added sources/salesforce.yaml." in done.output
    assert (root / "sources" / "salesforce.yaml").read_text() == "kind: salesforce\norg: work\n"
    assert fake_sf.TOKEN not in done.output
