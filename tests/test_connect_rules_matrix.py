"""The two rules of `cerebro connect`, over the cells of a matrix: no login is kept unproven and none is lost, and
no source file is left naming a login the store does not hold, nor a login kept with no file. Each kind with a
login is run against a stand-in that takes the login, refuses it, fails, or does not answer; from a first connect,
over a kept login that works, and over one the provider refuses; with no flag at a terminal, `--replace`, `--stdin`
and `--from-env`; and with nothing failing, the secrets store refusing the write, and the source file failing to be
written. Every kind meets every answer from every state; the flags are spread so each meets every value of the other
three. Every provider and the browser step are stand-ins; every value is made up."""

from __future__ import annotations

import imaplib
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from test_connect_lobby import lay_out_workspace
from test_connect_walks import at_a_plain_terminal, signing_in

from cerebro import cli
from cerebro import secrets as secrets_module
from cerebro.connect import need, new_source
from cerebro.connectors.mcp_source_client import McpError, ToolRefused

KINDS = ("github", "gong", "slack", "gmail", "calendar", "linear", "granola")
BROWSER = ("linear", "granola")
NEW = {"github": "ghp_MADEUPnew0001", "gong": "madeup-new-access:madeup-new-secret",
       "slack": "xoxp-MADEUP-new-0001", "gmail": "pat@quillmoor.example:madeup new app pass",
       "calendar": "https://cal.example/new-madeup-feed.ics", "linear": "lin_api_MADEUPnew0001",
       "granola": "madeup-granola-new-0001"}
KEPT = {"github": "ghp_MADEUPkept0001", "gong": "madeup-kept-access:madeup-kept-secret",
        "slack": "xoxp-MADEUP-kept-0001", "gmail": "pat@quillmoor.example:madeup kept app pass",
        "calendar": "https://cal.example/kept-madeup-feed.ics"}
SIGNED_IN = "tok-test-signed-in"
KEPT_LOGIN = ('{"server": "https://mcp.example/mcp", "token_endpoint": "https://mcp.example/token", "client_id": '
              '"client-test", "access_token": "tok-test-kept"}')


class Provider:
    """Every provider at once: a login in `taken` works; `answer` is how the provider answers: as it should,
    `refused` for every login, `500` for every request, or `timeout` for none."""

    def __init__(self, answer: str, taken: set[str]):
        self.answer, self.taken = answer, taken

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.answer == "timeout":
            raise httpx.ReadTimeout("stand-in", request=request)
        if self.answer == "500":
            return httpx.Response(500, text="stand-in")
        host = request.url.host
        if host == "api.github.com":
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            return httpx.Response(200, json={"login": "someone"}) if token in self.taken else \
                httpx.Response(401, json={"message": "Bad credentials"})
        if host == "api.gong.io":
            import base64

            pair = base64.b64decode(request.headers.get("Authorization", "Basic ").removeprefix("Basic ")).decode()
            if pair not in self.taken:
                return httpx.Response(401, json={"errors": ["stand-in"]})
            return httpx.Response(200, json={"workspaces": [], "records": {"totalRecords": 0}, "calls": [],
                                             "users": []})
        if host == "slack.com":
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if token not in self.taken:
                return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
            scopes = "channels:read,channels:history,groups:read,groups:history,users:read,users:read.email"
            return httpx.Response(200, headers={"X-OAuth-Scopes": scopes},
                                  json={"ok": True, "user_id": "U0MADEUP", "team": "Team", "url": "https://t.example/"})
        if host == "cal.example":
            if str(request.url) in self.taken:
                return httpx.Response(200, text="BEGIN:VCALENDAR\nEND:VCALENDAR\n")
            return httpx.Response(404, text="no feed")
        return httpx.Response(404)


def cell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, answer: str, state: str, flag: str,
         failure: str) -> tuple[int, str | None, str | None, str | None, bytes | None, bytes | None, str]:
    """Run one cell; give the exit, the secret before and after, the new value, and the file before and after."""
    new = NEW.get(kind, "")
    taken = {new} if answer == "good" else set()
    if state == "kept-good" and answer in ("good", "refused"):
        taken.add(KEPT.get(kind, ""))
    provider = Provider(answer if answer != "good" else "ok", taken)
    from cerebro.connectors.github import GithubConnector
    from cerebro.connectors.slack import SlackConnector

    for waits in (GithubConnector, SlackConnector):  # a 5xx is asked again after a wait, which no test sits out
        monkeypatch.setattr(waits.__init__, "__kwdefaults__", {**waits.__init__.__kwdefaults__,
                                                                "sleep": lambda seconds: None})
    root = lay_out_workspace(tmp_path, monkeypatch, provider)  # type: ignore[arg-type]
    for name in ("code", "chat", "broken"):
        (root / "sources" / f"{name}.yaml").unlink()
    at_a_plain_terminal(monkeypatch)
    signing_in(monkeypatch)
    opened = cli.open_workspace

    class Mail:
        def __init__(self, host: str):
            if answer in ("timeout", "500"):
                raise OSError("stand-in: no answer")

        def login(self, address: str, password: str) -> None:
            if f"{address}:{password}" not in {v.replace(" ", "") for v in taken} | taken:
                raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

        def logout(self) -> None:
            pass

    class Session:
        """The MCP session a check opens: it reads with the login it was handed, as the provider answers."""

        def __init__(self, where: Any):
            self.token = where.credentials()

        def reader(self, *tools: str) -> Any:
            def call(tool: str, arguments: Any) -> Any:
                if answer in ("500", "timeout"):
                    raise McpError("stand-in: no answer")
                kept = self.token == "tok-test-kept"
                if answer == "refused" and not kept or kept and state == "kept-refused":
                    raise ToolRefused("stand-in", tool, "invalid_token")
                return {}
            return call

        def close(self) -> None:
            pass

    def open_workspace(r: Path | None) -> Any:
        workspace = opened(r)
        workspace.imap_connect = Mail
        workspace.mcp_open = Session
        return workspace

    monkeypatch.setattr(cli, "open_workspace", open_workspace)
    secret = need(new_source(kind)).name
    path = root / "sources" / f"{kind}.yaml"
    if state != "first":
        path.write_text(f"kind: {kind}\n" + ("auth: oauth\n" if kind in BROWSER else ""))
        with cli.open_workspace(root) as workspace:
            workspace.secrets.set(secret, KEPT_LOGIN if kind in BROWSER else KEPT[kind])
    with cli.open_workspace(root) as workspace:
        before = workspace.secrets.get(secret) if secret in workspace.secrets.names() else None
    file_before = path.read_bytes() if path.exists() else None
    if failure == "store":
        def refuse(self: Any, name: str, value: str) -> None:
            raise secrets_module.SecretsError("the os keychain refused the write (stand-in)")
        monkeypatch.setattr(secrets_module.FileSecrets, "set", refuse)
    if failure == "file":
        import cerebro.connect as connect_module

        def full(*_: Any, **__: Any) -> None:
            raise OSError(28, "No space left on device (stand-in)")
        monkeypatch.setattr(connect_module, "write_source", full)
    if kind in ("gmail", "gong"):
        typed = new.replace(":", "\n", 1) + "\n"
    elif kind == "linear" and state == "first":
        typed = "b\n"
    else:
        typed = new + "\n"
    args = ["connect", kind, "--workspace", str(root)]
    if flag == "--replace":
        args.append("--replace")
    elif flag == "--stdin":
        args.append("--stdin")
        typed = new + "\n"
    elif flag == "--from-env":
        monkeypatch.setenv("MADEUP_LOGIN", new)
        args += ["--from-env", "MADEUP_LOGIN"]
    got = CliRunner().invoke(cli.main, args, input=typed)
    monkeypatch.undo()  # the store is read with nothing failing
    with opened(root) as workspace:
        after = workspace.secrets.get(secret) if secret in workspace.secrets.names() else None
    file_after = path.read_bytes() if path.exists() else None
    return got.exit_code, before, after, new, file_before, file_after, got.output


ANSWERS = ("good", "refused", "500", "timeout")
STATES = ("first", "kept-good", "kept-refused")
FLAGS = ("none", "--replace", "--stdin", "--from-env")


def cells() -> list[tuple[str, str, str, str]]:
    """Every kind against every answer from every state, each with a flag, so every flag also meets every kind, every
    answer and every state: 84 of the 336 cells. How a login is given reaches the provider's proof and the keep by
    one road, so a flag adds no case of its own to the other three."""
    need = {("kas", k, a, s) for k in KINDS for a in ANSWERS for s in STATES}
    need |= {(f"f{at}", f, x) for f in FLAGS for at, xs in enumerate((KINDS, ANSWERS, STATES)) for x in xs}
    chosen: list[tuple[str, str, str, str]] = []
    for kind in KINDS:
        for answer in ANSWERS:
            for state in STATES:
                flag = max(FLAGS, key=lambda f: (sum(r in need for r in (("f0", f, kind), ("f1", f, answer),
                                                                           ("f2", f, state))),
                                                 -sum(c[3] == f for c in chosen)))
                need -= {("kas", kind, answer, state), ("f0", flag, kind), ("f1", flag, answer), ("f2", flag, state)}
                chosen.append((kind, answer, state, flag))
    assert not need, need
    return chosen


@pytest.mark.parametrize(("kind", "answer", "state", "flag"), cells(), ids="-".join)
def test_no_login_is_kept_unproven_or_lost_and_no_file_names_one_the_store_lacks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, answer: str, state: str, flag: str) -> None:
    broken: list[str] = []
    works = answer == "good" or (answer, state) == ("refused", "kept-good")  # only a login that works is written
    for failure in ("none", "store", "file") if works else ("none",):
        folder = tmp_path / failure
        folder.mkdir()
        with pytest.MonkeyPatch.context() as each:
            code, before, after, new, file_before, file_after, said = cell(folder, each, kind, answer, state, flag,
                                                                           failure)
        where = f"{kind} {answer} {state} {flag} {failure}: exit {code}"
        signed = after is not None and SIGNED_IN in after
        if after != before and not (answer == "good" and (after == new or signed)):
            broken.append(f"{where}: a login was kept unproven")
        if before is not None and state == "kept-good" and after is None:
            broken.append(f"{where}: a working kept login was lost")
        if file_after != file_before and (after is None or after == before):
            broken.append(f"{where}: the file changed for a login the store does not hold")
        if after is not None and before is None and file_after is None:
            broken.append(f"{where}: a login was kept with no file")
        if code == 0 and after is None:
            broken.append(f"{where}: exit 0 with no login kept")
        if "Traceback" in said:
            broken.append(f"{where}: a traceback")
    assert not broken, broken
