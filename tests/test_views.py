"""The terminal's views, one test per view: the prompt of a view a person types or pastes
into, with its legend under it, the keys it answers and where each lands; each lobby's `Done` row and its legend;
and connect's views, each headed by its source's title in the lobby, with the legend under its prompt and Escape
going back. The frames are drawn by the same code a terminal runs, at 80 columns."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from cerebro.checklist import (
    FIELD_BACK,
    FIELD_CANCEL,
    FIELD_TAKEN,
    Field,
    FieldAsk,
    Menu,
    hard_wrap,
    wrap_head,
)
from cerebro.connect_screen import CONNECTED, NO_LOGIN, NOT_ADDED, Place, lobby_menu, ways_menu
from cerebro.output import Printer, strip_escapes

sys.path.insert(0, str(Path(__file__).parent))

WIDTH = 80


def styled() -> Printer:
    import io

    return Printer(io.StringIO(), io.StringIO(), terminal=lambda stream: True, dark=True, width=lambda: WIDTH,
                   getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))


def plain(lines: list[str]) -> list[str]:
    return [strip_escapes(line) for line in lines]


# ---- the prompt a person types or pastes into ------------------------------------------------------------------------


def test_a_views_prompt_has_a_blank_line_and_its_legend_under_it_the_keys_accented_and_the_words_muted() -> None:
    p = styled()
    lines = Field(FieldAsk("Gong access key", hidden=True)).render(WIDTH, 24, True, p.st)
    assert plain(lines) == ["Gong access key: ", "", "⏎ submit   esc back"]
    legend = lines[-1]
    assert p.st.paint("key", "⏎") in legend and p.st.paint("key", "esc") in legend
    assert p.st.paint("muted", " submit") in legend and p.st.paint("muted", " back") in legend


def test_a_default_shows_with_where_it_comes_from_and_a_yes_or_no_prompt_shows_its_default() -> None:
    assert plain(Field(FieldAsk("Email address", "ada@quillmoor.example", "from profile.md", hidden=False))
                 .render(WIDTH, 24))[0] == "Email address [ada@quillmoor.example, from profile.md]: "
    assert plain(Field(FieldAsk("Confirm the Salesforce CLI is authenticated?", yes_no=True, default_yes=True))
                 .render(WIDTH, 24)) == ["Confirm the Salesforce CLI is authenticated? [Y/n]: ", "",
                                          "⏏ submit   esc back".replace("⏏", "⏎")]
    assert Field(FieldAsk("Install and run?", yes_no=True, esc="skip")).render(WIDTH, 24)[-1] == "⏎ submit   esc skip"


def test_the_keys_of_a_prompt_are_typing_backspace_ctrl_u_enter_escape_and_ctrl_c() -> None:
    field = Field(FieldAsk("GitHub token", hidden=True))
    assert not field.type("ab\x01c")  # a control key is no text
    field.erase()
    assert field.typed == "ab" and field.shown() == ""  # a hidden answer shows nothing as it is typed
    field.kill()
    assert field.typed == "" and not field.enter()  # with no default an empty Enter does nothing
    assert field.state is None
    field.type("j k a ")  # the list keys are text here
    assert field.enter() and field.state == FIELD_TAKEN and field.answer == "j k a"
    assert Field(FieldAsk("Email", "me@x.example")).enter()  # Enter on an empty line takes the default
    back = Field(FieldAsk("Secret address"))
    back.type("half")
    assert back.back() and back.state == FIELD_BACK and back.render(WIDTH, 24) == []  # nothing is left of it
    cancelled = Field(FieldAsk("Secret address"))
    assert cancelled.cancel() and cancelled.state == FIELD_CANCEL


def test_a_yes_or_no_prompt_answers_at_y_or_n_and_enter_takes_its_default() -> None:
    yes = Field(FieldAsk("Confirm?", yes_no=True, default_yes=True))
    assert not yes.type("x") and yes.type("n") and yes.answer == "n"
    assert Field(FieldAsk("Confirm?", yes_no=True, default_yes=True)).enter()
    no = Field(FieldAsk("Install and run?", yes_no=True))
    assert no.enter() and no.answer == "n"


def test_once_answered_the_prompts_line_stays_with_the_answer_unless_hidden_and_the_legend_goes() -> None:
    shown = Field(FieldAsk("Email address", hidden=False))
    shown.type("ada@quillmoor.example")
    shown.enter()
    assert plain(shown.render(WIDTH, 24)) == ["Email address: ada@quillmoor.example"]
    hidden = Field(FieldAsk("App password", hidden=True))
    hidden.type("stand-in")
    hidden.enter()
    assert plain(hidden.render(WIDTH, 24)) == ["App password: "]


def test_a_long_prompt_wraps_a_column_short_of_the_width() -> None:
    rows = hard_wrap("x" * 100, WIDTH - 1)
    assert [len(r) for r in rows] == [79, 21]
    assert wrap_head("Pick one to connect. Come back any time with cerebro connect, or in the sources/ folder of "
                     "the workspace.", WIDTH) == [
        "Pick one to connect. Come back any time with cerebro connect, or in the sources/",
        "folder of the workspace."]


# ---- connect's lobby and its views -----------------------------------------------------------------------------------


LOBBY = [Place("local", NO_LOGIN, title="Local files"), Place("gong", NOT_ADDED, title="Gong"),
         Place("github", CONNECTED, title="GitHub")]


def test_the_connect_lobby_has_its_titles_three_states_a_done_row_set_apart_and_its_legend() -> None:
    p = styled()
    lines = plain(Menu(lobby_menu(LOBBY, p)).render(WIDTH, 30, True, p.st))
    assert lines == [
        "Sources",
        "Pick one to connect. Come back any time with cerebro connect, or in the sources/",
        "folder of the workspace.",
        "",
        "> Local files   ✓ connected",
        "  Gong          ✗ not connected",
        "  GitHub        ✓ connected",
        "",
        "  Done",
        "",
        "↑↓ move   ⏎ select   esc done"]


def test_the_lobbys_done_row_is_reached_past_the_last_row_and_ends_it_as_escape_does() -> None:
    from cerebro.checklist import Key

    menu = Menu(lobby_menu(LOBBY, Printer(plain=True)))
    for _ in range(5):
        menu.press(Key.DOWN)
    assert menu.on_end() and menu.press(Key.ENTER) == (True, False)
    assert Menu(lobby_menu(LOBBY, Printer(plain=True))).press(Key.ESC) == (True, False)


def test_a_choice_of_where_to_look_is_headed_by_its_title_and_a_way_in_by_its_lines_above_it() -> None:
    from cerebro.connectors.calendar import CONNECTING as CALENDAR
    from cerebro.connectors.linear import CONNECTING as LINEAR

    calendar = plain(Menu(ways_menu(CALENDAR.title, CALENDAR.choices)).render(WIDTH, 30))
    assert calendar[:3] == ["Calendar", "", "> Google Calendar"]
    assert calendar[-1] == "↑↓ move   ⏎ select   esc back" and "Done" not in calendar
    linear = plain(Menu(ways_menu(LINEAR.title, LINEAR.choices, LINEAR.says)).render(WIDTH, 30))
    # no paragraph above the ways, and one blank line under the heading
    assert LINEAR.says == () and LINEAR.plain  # a plain line still says what each way does
    assert linear[:3] == ["Linear", "", "> Sign in through the browser      a login kept in the secrets store"]
    assert linear[-1] == "↑↓ move   ⏎ select   esc back"


def test_a_way_in_by_key_says_its_lines_with_no_blank_line_before_its_prompt() -> None:
    from cerebro.connectors.linear import CONNECTING as LINEAR

    assert LINEAR.choices[1].says[-1].startswith("Or store one first with")


# ---- on a pseudo-terminal: the keys, where each lands ----------------------------------------------------------------


def on_the_demo(args: list[str], keys: tuple[tuple[object, ...], ...]) -> str:
    from test_connect_lobby import on_the_demo as run

    return run(args, keys)


def test_in_the_lobby_a_view_is_headed_by_its_title_with_its_legend_and_escape_goes_back_to_the_lobby() -> None:
    drawn = on_the_demo([], ((b"select", 1, b"jj\r"), (b"submit", 2, b"\x1b"), (b"Pick one to connect.", 2, b"\x1b", 1.0)))
    view = drawn[drawn.index("Install a custom app"):]
    assert drawn[:drawn.index("Install a custom app")].rstrip().endswith("Slack")  # its title heads it
    assert "OAuth token: " in view and "⏎ submit   esc back" in view
    assert view.index("⏎ submit   esc back") < view.index("Pick one to connect.")  # Escape: back to the lobby
    assert "Slack connection verified" not in drawn  # nothing was kept or checked


def test_connect_source_shows_its_prompt_with_the_legend_and_escape_ends_it_done_with_nothing_kept() -> None:
    drawn = on_the_demo(["--source", "slack"], ((b"submit", 2, b"\x1b"),))
    assert "OAuth token: " in drawn and "⏎ submit   esc back" in drawn
    assert "✓ Done." in drawn and "connection verified" not in drawn


# ---- Salesforce's view: a login the Salesforce CLI holds ------------------------------------------------------------


class StandInSf:
    """The Salesforce CLI, standing in, as a current one answers: `sf config get target-org` names `alias`, `sf org
    display` shows the notice in place of the token, and `sf org auth show-access-token` hands over a made-up token,
    or none of it when logged out. No real CLI runs."""

    def __init__(self, logged_in: bool = True, alias: str = "work") -> None:
        self.logged_in, self.alias = logged_in, alias
        self.ran: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: object) -> object:
        import json
        import subprocess

        from fake_sf import REDACTED

        self.ran.append(args)
        said: dict[str, object] = {"status": 1, "name": "NamedOrgNotFoundError", "message": "No authorization"}
        if args[1:3] == ["config", "get"]:
            said = {"status": 0, "result": [{"name": "target-org", "value": self.alias}]}
        elif self.logged_in and args[1:3] == ["org", "display"]:
            said = {"status": 0, "result": {"accessToken": REDACTED, "alias": self.alias,
                                            "instanceUrl": "https://quillmoor.my.salesforce.example",
                                            "username": "ada@quillmoor.example", "connectedStatus": "Connected"}}
        elif self.logged_in and args[1:4] == ["org", "auth", "show-access-token"]:
            said = {"status": 0, "result": {"accessToken": "00Dstandin!madeup"}, "warnings": ["sensitive"]}
        return subprocess.CompletedProcess(args, 0 if said["status"] == 0 else 1, json.dumps(said), "")


def salesforce_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sf: StandInSf, status: int = 200) -> Path:
    import httpx
    from click.testing import CliRunner

    from cerebro import cli
    from cerebro.app import Cerebro

    def org(request: httpx.Request) -> httpx.Response:  # the versions answer anyone, as a real org's do
        if request.url.path == "/services/data/":
            return httpx.Response(200, json=[{"version": "62.0", "url": "/services/data/v62.0"}])
        if status != 200 or request.headers.get("Authorization") != "Bearer 00Dstandin!madeup":
            return httpx.Response(403 if status == 200 else status, text="Missing_OAuth_Token")
        return httpx.Response(200, json={"user_id": "005000000000001AAA"})

    client = httpx.Client(transport=httpx.MockTransport(org))

    def open_workspace(root: Path | None) -> Cerebro:
        workspace = Cerebro(root, http=client)
        workspace.sf_run = sf
        return workspace

    monkeypatch.setattr(cli, "open_workspace", open_workspace)
    root = tmp_path / "team"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    monkeypatch.setattr("shutil.which", lambda name: f"/stand-in/bin/{name}")
    return root


def connect_salesforce(root: Path, monkeypatch: pytest.MonkeyPatch, answer: str | None) -> tuple[list[str], str]:
    import contextlib
    import io

    from cerebro import checklist, cli

    asked: list[FieldAsk] = []

    def field(ask: FieldAsk, p: Printer) -> str:
        asked.append(ask)
        if answer is None:
            raise checklist.FieldBack("back")
        return answer

    monkeypatch.setattr(checklist, "field", field)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        lines = cli._connect_by_cli(root, "salesforce", styled(), cli._cli_owner(root, "salesforce"))
    assert [a.prompt for a in asked] == ["Confirm the Salesforce CLI is authenticated?"]
    assert asked[0].yes_no and asked[0].default_yes
    return lines, strip_escapes(err.getvalue())


def test_salesforce_has_its_view_and_a_yes_reads_the_clis_login_checks_the_org_and_adds_its_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sf = StandInSf()
    root = salesforce_workspace(tmp_path, monkeypatch, sf)
    lines, said = connect_salesforce(root, monkeypatch, "y")
    first = ("This tool requires the Salesforce CLI. Install it from https://developer.salesforce.com/tools/"
             "salesforcecli, then log in from another terminal session:")
    rows = [line.rstrip() for line in said.splitlines()]
    tint = rows.index("  sf org login web")
    assert " ".join(rows[:tint - 1]) == first and rows[tint - 1] == "" and rows[tint + 1] == ""  # wrapped whole
    connected = ("Connected to Salesforce as ada@quillmoor.example, in the org work, on the Salesforce CLI's login. "
                 "No token is kept: the CLI holds it.")
    assert lines == [connected, "Added sources/salesforce.yaml."]
    assert (root / "sources" / "salesforce.yaml").read_text() == "kind: salesforce\norg: work\n"
    assert sf.ran == [["sf", "config", "get", "target-org", "--json"],
                      ["sf", "org", "display", "--target-org", "work", "--json"],
                      ["sf", "org", "auth", "show-access-token", "--target-org", "work", "--json"]]
    assert "00Dstandin" not in said  # the token the CLI hands over is never shown or kept


@pytest.mark.parametrize("answer", ["n", None])
def test_no_or_escape_at_salesforces_view_goes_back_with_nothing_run_or_written(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str | None) -> None:
    from cerebro.connect import ConnectBack

    sf = StandInSf()
    root = salesforce_workspace(tmp_path, monkeypatch, sf)
    with pytest.raises(ConnectBack):
        connect_salesforce(root, monkeypatch, answer)
    assert sf.ran == [] and not (root / "sources" / "salesforce.yaml").exists()


def test_salesforce_with_no_login_or_an_org_that_refuses_it_writes_nothing_and_says_why(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connect import ConnectError

    root = salesforce_workspace(tmp_path, monkeypatch, StandInSf(logged_in=False))
    with pytest.raises(ConnectError, match="has no login for work: run sf org login web --alias work"):
        connect_salesforce(root, monkeypatch, "y")
    refused = salesforce_workspace(tmp_path / "other", monkeypatch, StandInSf(), status=401)
    with pytest.raises(ConnectError, match=r"the org refused ada@quillmoor.example's login for the org work \(status "
                                           r"401 Missing_OAuth_Token\): run sf org login web --alias work"):
        connect_salesforce(refused, monkeypatch, "y")
    assert not (root / "sources" / "salesforce.yaml").exists()
    assert not (refused / "sources" / "salesforce.yaml").exists()


# ---- init's views ---------------------------------------------------------------------------------------------------


def specs() -> dict:
    from cerebro.models import ModelSpec

    return {"extraction": ModelSpec("fireworks", "glm-5p3-flash", "", "extraction"),
            "decision": ModelSpec("typesafe", "jev-1.13.0", "", "decision"),
            "compilation": ModelSpec("fireworks", "glm-5p3", "", "compilation")}


def test_init_model_selection_is_a_lobby_with_its_done_row_and_legend() -> None:
    from cerebro import guided

    lines = plain(Menu(guided.models_menu(specs())).render(100, 30))
    assert lines == [
        "Model selection", "Choose providers and models for the following processes.", "",
        "> Extraction    [ok] fireworks glm-5p3-flash   extracts facts from source records",
        "  Decision      [ok] typesafe jev-1.13.0       classifies, decides, and evaluates",
        "  Compilation   [ok] fireworks glm-5p3         compiles facts into articles",
        "", "  Done", "", "↑↓ move   ⏎ select   esc done"]


def test_init_provider_and_model_pickers_have_a_done_row_that_goes_back_with_no_change() -> None:
    from cerebro import guided
    from cerebro.checklist import Key

    menu, providers = guided.providers_menu("compilation")
    lines = plain(Menu(menu).render(100, 30))
    assert lines[:3] == ["Compilation model provider", "The compilation model compiles facts into articles.", ""]
    assert [line.strip("> ") for line in lines[3:3 + len(providers)]] == providers
    assert lines[-4:] == ["", "  Done", "", "↑↓ move   ⏎ select   esc done"]
    picker = Menu(menu)
    for _ in providers:
        picker.press(Key.DOWN)
    assert picker.press(Key.ENTER) == (True, True) and picker.cursor == len(providers)  # Done: apart from Escape
    models, _ = guided.model_menu("fireworks", "compilation")
    drawn = plain(Menu(models).render(100, 60))
    assert drawn[0] == "Compilation model" and "Done" in drawn[-3]
    row = next(line for line in drawn if line.strip("> ").startswith("glm-5p3 "))
    assert row.endswith("$1.40 in, $4.40 out per million tokens")


def test_init_model_keys_is_a_lobby_its_intro_one_line_with_its_done_row_and_legend() -> None:
    from cerebro import keys

    rows = [keys.KeyRow("fireworks", (("extraction", None), ("compilation", None)), keys.NOT_ADDED)]
    lines = plain(Menu(keys.menu(rows, keys.head())).render(100, 30))
    assert lines[:4] == [
        "Model keys",
        "Add keys for the following model providers to continue. Model selection can also be configured by",
        "editing settings.yaml in your workspace and running cerebro secrets to add the required provider",
        "credentials."]
    assert lines[-4:] == ["", "  Done", "", "↑↓ move   ⏎ select   esc done"]


def test_init_mcp_clients_is_a_lobby_with_a_clients_view_and_its_steps() -> None:
    from cerebro import guided

    lobby = plain(Menu(guided.clients_menu([("Claude Desktop", "not installed", ""),
                                            ("Claude Code", "installed", "")])).render(100, 30))
    assert lobby == ["MCP clients", "", "> Claude Desktop   [x]  not installed", "  Claude Code      [ok] installed",
                     "", "  Done", "", "↑↓ move   ⏎ select   esc done"]
    view = plain(Menu(guided.client_menu("Cursor", [guided.WRITE, guided.SHOW])).render(100, 30))
    assert view == ["Cursor", "Add the MCP client for this workspace to Cursor.", "", "> Install for me",
                    "  Let me do it", "", "↑↓ move   ⏎ select   esc back"]
    steps = plain(guided.manual_lines("Claude Code", [("Run this command in a terminal", "claude mcp add x")],
                                      styled()))
    assert [line.rstrip() for line in steps] == ["Claude Code", "Add this workspace's MCP server to Claude Code.", "",
                                                 "1. Run this command in a terminal", "  claude mcp add x"]


class Drawn:
    """A drawn walk's screen, standing in: its lines kept, its answers given in order."""

    drawn = True

    def __init__(self, answers: list[object]) -> None:
        import io

        from cerebro.output import Printer as P

        self.out = io.StringIO()
        self.printer = P(self.out, io.StringIO(), terminal=lambda stream: True, dark=True, width=lambda: 100,
                         getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))
        self.answers, self.asked, self.erased, self.rows = list(answers), [], 0, 0

    def say(self, line: str) -> None:
        self.out.write(line + "\n")

    def styled(self, line: str, indent: str = "") -> None:
        from cerebro.guided import TerminalScreen

        TerminalScreen.styled(self, line, indent)  # type: ignore[arg-type]

    def _count(self, line: str) -> None:
        pass

    def echo(self, line: str) -> None:
        self.out.write(line + "\n")

    def text(self, prompt: str, default: str = "", hidden: bool = False, source: str = "") -> object:
        self.asked.append(prompt)
        return self.answers.pop(0)

    def erase(self) -> None:
        self.erased += 1

    def summary(self, mark: object, line: str, hint: str = "") -> None:
        from cerebro.guided import TerminalScreen

        TerminalScreen.summary(self, mark, line, hint)  # type: ignore[arg-type]


# Discovery starts with no question, and what to build is chosen after it.
def test_inits_first_run_is_a_ready_block_with_no_question_after_it(tmp_path: Path) -> None:
    from cerebro import guided

    screen = Drawn([])
    walk = guided.Walk(tmp_path, screen, None, lambda: None, lambda: None, tmp_path, "cerebro")  # type: ignore[arg-type]
    walk._ready()
    said = strip_escapes(screen.out.getvalue()).splitlines()
    assert said[:2] == ["", "Ready to fetch from your sources and compile the first articles."]
    assert " ".join(said[2:]) == guided.READY  # wrapped at its spaces
    assert screen.asked == [] and screen.erased == 0  # nothing asked, so nothing to take away


def test_inits_summaries_are_a_mark_and_a_line_then_the_hint_muted_its_command_accented() -> None:
    from cerebro.output import Mark

    screen = Drawn([])
    screen.summary(Mark.FAILED, "No model keys set", "Manage provider credentials with cerebro secrets set <provider>.")
    out = screen.out.getvalue()
    assert strip_escapes(out).splitlines() == ["", "✗ No model keys set",
                                               "  Manage provider credentials with cerebro secrets set <provider>."]
    assert screen.printer.st.paint("key", "cerebro secrets set <provider>") in out


def test_init_at_a_terminal_takes_its_questions_off_the_screen_draws_each_step_apart_and_ends_in_its_summaries(
        tmp_path: Path) -> None:
    """The whole walk on a pseudo-terminal, every step passed over with Escape: each question is asked on its line,
    then taken off the screen where its summary takes its place; each step's lobby draws on the alternate screen and
    is gone once it ends; the main screen ends with the summaries alone."""
    import os

    from test_output import on_a_pty

    code = (f"import cerebro.cli\ncerebro.cli.main(['init', {str(tmp_path / 'wiki')!r}], prog_name='cerebro')\n")
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file", "COLORTERM": "truecolor",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "TZ": "UTC"}
    step = 0.5  # long enough for a step to be reading: what is typed before it is dropped
    got = on_a_pty(code, env, answer=b"\x1b]11;rgb:0000/0000/0000\x1b\\", seconds=40, keys=(
        (b" select", 2, b"\r", step), (b"Your name", 1, b"Ada Lovelace\r", step),
        (b"Your work email", 1, b"ada@quillmoor.example\r", step), (b"Team or organization name", 1, b"\r", step),
        (b"Team or organization web domain", 1, b"\r", step), (b"Daily budget", 1, b"\r", step), (b"Time zone", 1, b"\r", step),
        (b"Choose providers", 1, b"\x1b", step), (b"Add keys for", 1, b"\x1b", step),
        (b"Pick one to connect", 1, b"\x1b", step), (b"MCP clients", 1, b"\x1b", step)))
    text = got.decode("utf-8", "replace")
    asked, created = text.rfind("Time zone"), text.find("Created new workspace in")
    assert 0 < asked < created, text[-2000:]
    assert re.search(r"\x1b\[\d+A\r\x1b\[J", text[asked:created])  # the questions go, as the summary replaces them
    for title in ("Choose providers", "Add keys for", "Pick one to connect", "MCP clients"):
        at = text.find(title)
        assert text.rfind("\x1b[?1049h", 0, at) > text.rfind("\x1b[?1049l", 0, at), title  # drawn apart
    end = strip_escapes(re.sub(r"\x1b\[\?1049h.*?\x1b\[\?1049l", "", text[created:], flags=re.DOTALL))  # main screen
    for line in ("Model selection", "✗ No model keys set", "✓ Connected 1 source: Local files",
                 "✗ No MCP clients installed", "Done with warnings."):
        assert line in end, end
    scraps = ("Secret address", "workspace ", "mcp ", "models ", "keys ", "Your name", "Sources")
    assert not [line for line in end.splitlines() if line.startswith(scraps)], end  # no scraps of a step or a view


@pytest.mark.parametrize(("args", "marker"), [(["connect"], b" select"), (["connect", "gong"], b"submit")])
def test_a_view_whose_terminal_goes_away_ends_at_once_and_does_not_spin(
        tmp_path: Path, args: list[str], marker: bytes) -> None:
    """A screen whose terminal is gone, as when its window closes or an SSH session drops, ends with 129, as on
    SIGHUP, within two seconds, where Textual's reader would read the dead terminal again and again for good."""
    import fcntl
    import os
    import pty
    import select
    import struct
    import subprocess
    import termios
    import time

    from click.testing import CliRunner

    from cerebro import cli

    root = tmp_path / "wiki"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "TERM": "xterm-256color",
           "COLORTERM": "truecolor", "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring", "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    try:
        assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1", "--timezone", "UTC"]).exit_code == 0
    finally:
        del os.environ["XDG_CONFIG_HOME"]
    leader, follower = pty.openpty()
    fcntl.ioctl(follower, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    code = f"import cerebro.cli\ncerebro.cli.main({[*args, '--workspace', str(root)]!r}, prog_name='cerebro')\n"
    child = subprocess.Popen([sys.executable, "-c", code], stdin=follower, stdout=follower, stderr=follower, env=env,
                             start_new_session=True)
    os.close(follower)
    closed = False
    try:
        got, end = b"", time.monotonic() + 30
        while marker not in got and time.monotonic() < end:
            if select.select([leader], [], [], 0.1)[0]:
                try:
                    chunk = os.read(leader, 65536)
                except OSError:
                    break
                got += chunk
                if b"\x1b]11;?" in chunk:
                    os.write(leader, b"\x1b]11;rgb:0000/0000/0000\x1b\\")
                if b"\x1b[6n" in chunk:
                    os.write(leader, b"\x1b[1;1R")
        assert marker in got, got[-500:]
        time.sleep(0.5)
        os.close(leader)  # the terminal goes away
        closed = True
        gone = time.monotonic() + 2
        while child.poll() is None and time.monotonic() < gone:
            time.sleep(0.05)
        assert child.poll() == 129, "the view kept running once its terminal was gone"
    finally:
        if not closed:
            os.close(leader)
        if child.poll() is None:
            child.kill()
        child.wait()


# ---- the walk's lines, unused keys, signals, tall views and the Salesforce file --------------------------------------


def test_a_styled_line_the_walk_draws_goes_through_the_printer_never_the_echo_that_shows_escapes() -> None:
    import io

    from cerebro.guided import TerminalScreen
    from cerebro.shown import shown

    echoed = io.StringIO()
    screen = TerminalScreen(styled(), lambda line: echoed.write(shown(line, None) + "\n"))
    screen.styled(screen.printer.bold("Model selection"))  # type: ignore[union-attr]
    out = screen.printer.out.getvalue()  # type: ignore[union-attr]
    assert "\x1b[1m" in out and "\\x1b" not in out and echoed.getvalue() == ""


def test_every_key_a_views_prompt_does_not_use_does_nothing_and_crashes_nothing() -> None:
    """Arrows, Home, End, Tab, Page Up and Down, F-keys and Alt chords at Slack's prompt, then Escape: the prompt
    stays, nothing fails, and Escape ends `connect slack` done: the view ignores every key it does not use."""
    keys = b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F\t\x1b[5~\x1b[6~\x1bOP\x1b[15~"
    drawn = on_the_demo(["--source", "slack"], ((b"submit", 2, keys, 0.5), (b"submit", 3, b"\x1bx", 0.5),
                                                (b"submit", 4, b"\x1b", 0.5)))
    assert "Error" not in drawn and "AttributeError" not in drawn, drawn[-800:]
    assert "✓ Done." in drawn


def test_a_signal_while_the_screen_is_apart_ends_the_command_with_the_main_screen_back() -> None:
    import os
    import signal

    from cerebro.connect_screen import Apart

    p = styled()
    p.styled_err = True
    apart = Apart(p)
    kept = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as ended:
        try:
            apart.take()
            os.kill(os.getpid(), signal.SIGTERM)
            import time

            time.sleep(1)
        finally:
            apart.leave()
    assert ended.value.code == 130
    assert p.err.getvalue().endswith("\x1b[?1049l")  # type: ignore[attr-defined]
    assert signal.getsignal(signal.SIGTERM) == kept


def test_a_view_taller_than_the_screen_apart_is_drawn_on_the_main_screen_to_scroll_and_copy(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import os

    from cerebro import cli
    from cerebro.connect_screen import Apart
    from cerebro.connectors.slack import CONNECTING as SLACK

    monkeypatch.setattr("shutil.get_terminal_size", lambda *a, **k: os.terminal_size((80, 24)))
    p = styled()
    p.styled_err = True
    apart = Apart(p)
    apart.take()
    tell = cli._Tell(p, apart, "Slack")
    tell.explain(SLACK.says, SLACK.block, SLACK.after)
    assert not apart.on and p.err.getvalue().endswith("\x1b[?1049l")  # type: ignore[attr-defined]
    said = strip_escapes(capsys.readouterr().err).splitlines()
    assert said[:2] == ["", "Slack"] and "  display_information:" in [line.rstrip() for line in said]  # then all of it
    apart.take()
    short = cli._Tell(p, apart, "Gong")
    from cerebro.connectors.gong import CONNECTING as GONG

    short.explain(GONG.says)
    assert apart.on  # a view that fits stays apart
    apart.leave()


def test_the_engine_names_no_salesforce_idea_in_connect() -> None:
    src = (Path(__file__).parents[1] / "src" / "cerebro" / "cli.py").read_text()
    assert not re.search(r"sf_run|\bsf org\b|\[\"org\"\]|\{org\}|\"cli\" in str", src)


@pytest.mark.parametrize("alias", ["work\nopportunities: {closed_since: all}", "0777", "work: x"])
def test_the_salesforce_file_is_written_by_the_yaml_writer_so_no_alias_forges_a_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str) -> None:
    import yaml

    from cerebro.connect import new_source
    from cerebro.connectors.salesforce import SalesforceConnector

    root = salesforce_workspace(tmp_path, monkeypatch, StandInSf(alias=alias))
    from cerebro import cli

    with cli.open_workspace(root) as workspace:
        _, text = SalesforceConnector.check_cli(new_source("salesforce"), workspace)
    assert yaml.safe_load(text) == {"kind": "salesforce", "org": alias}


def test_a_step_whose_terminal_goes_away_ends_the_command_at_once_with_no_hangup_signal(tmp_path: Path) -> None:
    import fcntl
    import os
    import pty
    import struct
    import subprocess
    import termios
    import time

    code = ("import time\nfrom cerebro.output import Printer\np = Printer()\nstep = p.step('Verifying Gong connection')"
            "\ntime.sleep(60)\n")
    leader, follower = pty.openpty()
    fcntl.ioctl(follower, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    env = {"PATH": os.environ.get("PATH", ""), "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    child = subprocess.Popen([sys.executable, "-c", code], stdin=follower, stdout=follower, stderr=follower, env=env,
                             start_new_session=True)  # no controlling terminal: no SIGHUP comes when it goes
    os.close(follower)
    try:
        deadline = time.monotonic() + 20
        got = b""
        while b"Verifying" not in got and time.monotonic() < deadline:
            import select

            if select.select([leader], [], [], 0.1)[0]:
                got += os.read(leader, 65536)
        assert b"Verifying" in got
        os.close(leader)
        leader = -1
        gone = time.monotonic() + 2
        while child.poll() is None and time.monotonic() < gone:
            time.sleep(0.05)
        assert child.poll() == 129
    finally:
        if leader != -1:
            os.close(leader)
        if child.poll() is None:
            child.kill()
        child.wait()


def test_nothing_typed_while_a_step_runs_is_drawn_and_the_echo_comes_back_when_it_ends(tmp_path: Path) -> None:
    """A key pasted again while the first is checked never shows: the terminal's echo is off while a live step runs,
    and back once the step ends."""
    import fcntl
    import os
    import pty
    import select
    import struct
    import subprocess
    import termios
    import time

    code = ("import os, sys, termios, time\nfrom cerebro.output import Printer\n"
            "def seen(at):\n    s = termios.tcgetattr(0)[3] & termios.ECHO\n    sys.stdout.write(f'{at}:{bool(s)}\\n')\n"
            "    sys.stdout.flush()\n"
            "p = Printer()\nseen('before')\nstep = p.step('Verifying Gong connection')\nseen('during')\n"
            "time.sleep(1.5)\nstep.done('Gong connection verified')\nseen('after')\n")
    leader, follower = pty.openpty()
    fcntl.ioctl(follower, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    own = "import fcntl, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"  # the terminal is its own, in front
    env = {"PATH": os.environ.get("PATH", ""), "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    child = subprocess.Popen([sys.executable, "-c", own + code], stdin=follower, stdout=follower, stderr=follower,
                             env=env, start_new_session=True)
    os.close(follower)
    got = b""
    try:
        end = time.monotonic() + 20
        typed = False
        while time.monotonic() < end and child.poll() is None or select.select([leader], [], [], 0.1)[0]:
            if select.select([leader], [], [], 0.1)[0]:
                try:
                    got += os.read(leader, 65536)
                except OSError:
                    break
            if b"during:" in got and not typed:
                os.write(leader, b"pasted-again-madeup")
                typed = True
    finally:
        os.close(leader)
        if child.poll() is None:
            child.kill()
        child.wait()
    text = got.decode("utf-8", "replace")
    assert "before:True" in text and "during:False" in text and "after:True" in text, text
    assert "pasted-again" not in text  # never drawn


# ---- wrapping, and init's own lines ---------------------------------------------------------------------------------


def test_a_paragraph_wraps_at_its_spaces_its_styles_kept_and_a_block_to_copy_is_never_rewrapped() -> None:
    from cerebro.connect_screen import explained
    from cerebro.connectors.gong import CONNECTING as GONG
    from cerebro.connectors.slack import CONNECTING as SLACK
    from cerebro.output import fold

    p = styled()
    rows = explained(p, GONG.says)
    assert len(rows) > 1 and all(len(strip_escapes(r)) <= WIDTH for r in rows)
    words = " ".join(strip_escapes(r) for r in rows).split()
    assert words == " ".join(GONG.says).replace("`", "").split()  # every word whole
    assert all(r.startswith("\x1b[") for r in rows if r)  # each row muted on its own
    slack = [strip_escapes(r).rstrip() for r in explained(p, SLACK.says, SLACK.block, SLACK.after)]
    assert "  display_information:" in slack and "        - users:read.email" in slack
    assert fold("x" * 30, 20) == ["x" * 30] and fold("a b", 0) == ["a b"]


def test_inits_own_paragraphs_wrap_at_their_spaces(tmp_path: Path) -> None:
    from cerebro import guided

    screen = Drawn([""])
    walk = guided.Walk(tmp_path, screen, None, lambda: None, lambda: None, tmp_path, "cerebro")  # type: ignore[arg-type]
    walk._ready()
    rows = [r for r in strip_escapes(screen.out.getvalue()).splitlines() if r]
    assert all(len(r) <= 100 for r in rows) and " ".join(rows).split() == (
        "Ready to fetch from your sources and compile the first articles. " + guided.READY).split()


def test_init_on_a_workspace_off_a_terminal_shows_what_is_set_and_asks_nothing(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from cerebro import cli

    root = tmp_path / "wiki"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1", "--timezone", "UTC"]).exit_code == 0
    again = CliRunner().invoke(cli.main, ["init", str(root)])
    assert again.exit_code == 0 and "already a workspace" not in again.output, again.output
    assert f"workspace {root}" in again.output and "models    " in again.output and "mcp       " in again.output


def test_the_model_screen_says_extraction_needs_the_facts_layers_model_when_no_row_names_it() -> None:
    from cerebro import guided, keys
    from cerebro.models import ModelSpec

    lines = plain(Menu(guided.models_menu(specs())).render(120, 30))
    at = next(i for i, line in enumerate(lines) if "Extraction" in line)
    assert "Decision" in lines[at + 1] and sum("typesafe jev-1.13.0" in line for line in lines) == 1
    other = {**specs(), "decision": ModelSpec("fireworks", "rune", "", "decision")}
    lines = plain(Menu(guided.models_menu(other)).render(120, 30))
    at = next(i for i, line in enumerate(lines) if "Extraction" in line)
    assert lines[at + 1].strip() == "and typesafe jev-1.13.0, which decides on the facts it finds"
    assert "fireworks rune" in lines[at + 2]
    rows = keys.key_rows(specs(), [], {})
    assert next(r for r in rows if r.provider == "typesafe").job_words() == "extraction and decision"


def test_a_rows_note_wraps_under_its_state_and_is_never_cut() -> None:
    note = ("Left out 0. By line: deals you own 0 found, no source answers it; deals your reports own 0 found, no "
            "source answers it")
    from cerebro.checklist import MenuList as ML
    from cerebro.checklist import MenuRow as MR

    lines = plain(Menu(ML([MR("opportunities", "proposed nothing", below=(note,))], ["Proposed articles"],
                          end="Approve")).render(80, 30))
    assert not any(line.endswith("…") for line in lines)
    assert " ".join(line.strip() for line in lines[3:5]).startswith(note[:60])
    assert "source answers it" in lines[4]


def test_inits_lines_with_flags_at_a_terminal_wrap_at_their_spaces_under_their_words_commands_whole(
        tmp_path: Path) -> None:
    import os

    from test_output import on_a_pty

    root = tmp_path / "w"
    code = (f"from cerebro.cli import main\nmain(['init', {str(root)!r}, '--daily-usd', '7', '--timezone', 'UTC', "
            "'--name', 'Ada Quill', '--email', 'ada@quillmoor.example', '--org', 'Quillmoor', "
            "'--domain', 'quillmoor.example', "
            "'--template', 'gtm'], prog_name='cerebro')\n")
    env = {"HOME": str(tmp_path), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
           "XDG_CONFIG_HOME": str(tmp_path / "config"), "CEREBRO_SECRETS_STORE": "file", "NO_COLOR": "1",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
    rows = on_a_pty(code, env, seconds=30).decode().replace("\r\n", "\n").splitlines()
    keyed = [i for i, row in enumerate(rows) if row.startswith("key       ")]
    assert keyed, rows
    for i in keyed:  # the key line wraps under its words, at a space, and its command stays on one row
        assert len(rows[i]) < 80 and rows[i + 1].startswith(" " * 10) and rows[i + 1][10] != " "
        assert any("cerebro secrets set " in row for row in rows[i:i + 4])
    words = " ".join(row.strip() for row in rows[keyed[0]:keyed[0] + 3]).split()
    assert "FIREWORKS_API_KEY" in words and "store" in words  # every word whole


# ---- an answered list -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "left"), [([True, False], False), (None, True)])
def test_an_approved_list_goes_from_the_screen_and_a_cancelled_one_stays(
        monkeypatch: pytest.MonkeyPatch, answer: object, left: bool) -> None:
    """The approval drops its list, so what it approved comes next; Escape leaves the list where it stood, with its
    line under it."""
    from cerebro import checklist
    from cerebro.checklist import Checklist, Group, Row

    runs: list[dict[str, object]] = []

    def run(self: object, **kwargs: object) -> object:
        runs.append(kwargs)
        return answer

    monkeypatch.setattr(checklist, "_raw_keys", lambda printer: None)
    monkeypatch.setattr(checklist._InlineApp, "run", run)
    p = styled()
    lst = Checklist([Row("Brightwater Logistics kickoff"), Row("Kestrel Foods kickoff")], [Group("in your sources", [0, 1])],
                    ["Working set proposal"], verb="Approve all")
    assert checklist.ask(lst, p, gone_once_taken=True) == answer
    assert runs == [{"inline": True}]  # drawn so it clears itself as it ends, never left as its last frame
    drawn = strip_escapes(p.err.getvalue())  # type: ignore[attr-defined]
    assert ("Working set proposal" in drawn and "Brightwater Logistics kickoff" in drawn) is left


# ---- the connect pickers --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("head", "verb", "n"), [("Linear teams to read", "Write", 11),
                                                 ("GitHub repositories to read", "Write", 40),
                                                 ("Slack channels to import", "Import", 12)])
def test_a_picker_with_more_rows_than_a_group_starts_open_with_shows_every_row(
        head: str, verb: str, n: int) -> None:
    """Connect's pickers never fold a group with no header, so every row shows, with the title and the keys, at
    any height."""
    from cerebro.checklist import Model
    from cerebro.connect_screen import picking
    from cerebro.connector import Choice, Offer

    offer = Offer("teams", "team", "teams", tuple(Choice(f"T{i}", f"Team {i} (T{i})", "no projects")
                                                  for i in range(n)), (), head=head, verb=verb)
    for height in (30, 18, 12):
        lines = plain(Model(picking(offer, "linear").checklist).render(100, height))
        assert lines[0] == head and lines[1] == "into sources/linear.yaml"
        assert "Team 0 (T0)" in "\n".join(lines), lines  # rows show, however short the screen
        assert lines[-1] == f"↑↓ move   space toggle   a all or none   ⏎ {verb} selected   esc Cancel and quit"


def test_a_headerless_group_never_starts_closed_and_the_keys_go_on_two_lines_only_when_one_does_not_fit() -> None:
    from cerebro.checklist import Checklist, Group, Model, Row

    lst = Checklist([Row(f"row {i}") for i in range(15)], [Group("", list(range(15)))], ["Things"], verb="Write")
    wide = plain(Model(lst).render(120, 40))
    assert "row 14" in "\n".join(wide) and wide[-1].startswith("↑↓ move") and "⏎ Write all" in wide[-1]
    narrow = plain(Model(lst).render(50, 40))
    assert narrow[-2].startswith("↑↓ move") and narrow[-1].startswith("⏎ Write all")  # two lines, where it must


# Each source a prompt heads, by its own title: a source's prompt with lines alone (Gong's, Gmail's) or with a block and
# lines after it (Slack's); a choice by two roads, with the source's lines above it or not.
@pytest.mark.parametrize(("kind", "title", "first"), [
    ("gong", "Gong", "Provide a Gong access key"), ("slack", "Slack", "Install a custom app"),
    ("gmail", "Gmail", "Provide an app password"), ("calendar", "Calendar", "Google Calendar"),
    ("linear", "Linear", "Sign in through the browser")])
def test_connect_source_alone_shows_the_view_every_road_shows_headed_with_its_footer(
        kind: str, title: str, first: str) -> None:
    """`connect <source>` alone heads its view with the source's title, as the lobby's and init's do; its prompt or choice has its footer, and Escape goes back."""
    marker = b"submit" if kind in ("gong", "slack", "gmail") else b"select"
    drawn = on_the_demo(["--source", kind], ((marker, 2, b"\x1b", 0.5),))
    at = drawn.index(first)
    above = [line.strip() for line in drawn[:at].splitlines() if line.strip() not in ("", ">")]
    assert above[-1] == title, drawn[max(0, at - 200):at + 50]  # the title is the line right above the view
    assert ("esc back" in drawn) and ("✓ Done." in drawn or "not connected yet" in drawn), drawn[-400:]
@pytest.mark.parametrize(("make", "marker", "want"), [
    ("choose(MenuList([MenuRow('one'), MenuRow('two')], ['Sources'], end='Done', open='select'), p)", b"select", "0"),
    ("field(FieldAsk('Gong access key'), p)", b"submit", "None"),
    ("ask(Checklist([Row('a'), Row('b')], [Group('things', [0, 1])], ['A list']), p)", b"Approve", "[True, True]")])
def test_a_view_turns_on_no_focus_or_mouse_reporting_and_a_focus_report_is_no_key(
        make: str, marker: bytes, want: str) -> None:
    """The views turn on neither, and VS Code's terminal, told to report them, sends a sequence at each focus change
    and mouse move. A focus report fed in anyway changes nothing: Enter then answers the view as it would have."""
    import os
    import re

    from test_output import on_a_pty

    code = ("from cerebro.output import Printer\nfrom cerebro.checklist import *\np = Printer()\n"
            f"try:\n    got = {make}\nexcept FieldBack:\n    got = 'back'\nprint('GOT', got, flush=True)\n")
    env = {"PATH": os.environ.get("PATH", ""), "TERM": "xterm-256color", "COLORTERM": "truecolor",
           "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    keys = ((marker, 1, b"\x1b[O", 0.5), (marker, 1, b"\x1b[I", 0.5), (marker, 1, b"x" if "field" in make else b"", 0.3),
            (marker, 1, b"\r", 0.5))
    got = on_a_pty(code, env, seconds=20, keys=keys)
    assert not re.search(rb"\x1b\[\?(?:1000|1002|1003|1004|1006|1015)h", got)
    if "field" in make:
        want = "x"
    assert f"GOT {want}".encode() in got, got[-300:]
