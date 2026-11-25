"""`cerebro connect` at a terminal: the lobby of a workspace's sources, and the list a person ticks for one.

Every connector here talks to a stand-in: GitHub to recorded pages under tests/data/connect, Linear to the recorded
MCP server of its own tests, Slack to the stand-in Web API of its own tests, and the workspace's model to a scripted
reply. No key, no login and no network.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import httpx
import pytest
import yaml
from click.testing import CliRunner
from test_connector_linear import RecordedLinear, linear_source
from test_connector_slack import StandIn, make, slack_source
from test_output import DATA, on_a_pty

from cerebro import cli
from cerebro.app import Cerebro
from cerebro.budget import BudgetSpent, spent_by_runs
from cerebro.connect import ConnectError, add_source, answers, need, places, write_source
from cerebro.connect_screen import (
    ATTENTION,
    CONNECTED,
    NO_LOGIN,
    NOT_ADDED,
    NOT_CONNECTED,
    Place,
    chosen,
    lobby_menu,
    pick,
    picking,
    written_line,
)
from cerebro.connector import Ask, Choice, Connector, ListKept, Offer, with_list
from cerebro.connectors.github import GithubConnector
from cerebro.connectors.linear import LinearConnector
from cerebro.errors import CerebroError
from cerebro.fakes import FakeRewriter
from cerebro.models import TokenCount
from cerebro.output import Printer, strip_escapes
from cerebro.source import SourceFile, Window, read_source

CHECKS = Path(__file__).resolve().parents[1] / "checks"
sys.path.insert(0, str(CHECKS))
import connect_demo

RECORDED = Path(__file__).parent / "data" / "connect"
NOW = datetime(2026, 11, 19, 9, tzinfo=UTC)
BAD = "bad\x1b[2J\x07\u202ename"  # a screen clear, a bell and a right-to-left override


def printer(dark: bool = True, plain: bool = False) -> Printer:
    if plain:
        return Printer(io.StringIO(), io.StringIO(), plain=True, width=lambda: 80)
    return Printer(io.StringIO(), io.StringIO(), terminal=lambda stream: True, dark=dark, width=lambda: 80,
                   getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))


# ---- what each connector offers -------------------------------------------------------------------------------------


class RecordedGithub:
    """GitHub's REST API as a stand-in serves it: two recorded pages of /user/repos, joined by a Link."""

    def __init__(self) -> None:
        self.asked: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        if request.url.host == "api.gong.io":  # Gong's list of calls, which takes any key but one named refused
            import base64

            key = base64.b64decode(request.headers.get("Authorization", "Basic ").removeprefix("Basic ")).decode()
            if request.method != "GET" or request.url.path not in ("/v2/workspaces", "/v2/calls") or "refused" in key:
                return httpx.Response(401, json={"errors": ["unauthorized"]})
            return httpx.Response(200, json={"records": {"totalRecords": 0}, "calls": [], "workspaces": []})
        if request.headers.get("Authorization") != "Bearer stand-in":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "someone"})
        if request.url.path == "/user/repos" and "page" not in request.url.params:
            following = f"{request.url.scheme}://{request.url.host}/user/repos?page=2&sort=pushed&direction=desc&per_page=100"
            return httpx.Response(200, json=json.loads((RECORDED / "github-user-repos-1.json").read_text()),
                                  headers={"link": f'<{following}>; rel="next"'})
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=json.loads((RECORDED / "github-user-repos-2.json").read_text()))
        return httpx.Response(404, json={"message": "Not Found"})


def github(repos: list[str] | None = None, server: RecordedGithub | None = None) -> GithubConnector:
    source = SourceFile(name="code", kind="github", window=Window(), extract="facts",
                        settings={"repos": repos or []})
    client = httpx.Client(transport=httpx.MockTransport((server or RecordedGithub()).handle))
    return GithubConnector(source, client=client, token=lambda: "stand-in", base="https://github.test",
                           now=lambda: NOW)


def test_github_offers_the_repositories_the_token_reads_most_recently_pushed_first_page_by_page() -> None:
    server = RecordedGithub()
    offer = github(["quillmoor/api", "someone/gone"], server).offer()
    assert (offer.key, offer.one, offer.many, offer.current) == ("repos", "repository", "repositories",
                                                                 ("quillmoor/api", "someone/gone"))
    assert [(c.value, c.detail) for c in offer.choices] == [
        ("quillmoor/api", "private, pushed 2026-10-30: The API"),
        ("quillmoor/old", "archived, pushed 2025-01-02"),
        ("quillmoor-lab/web", "pushed 2026-09-01"),
        ("quillmoor-lab/drafts", "never pushed")]
    assert offer.note == ()
    first = server.asked[0].url.params
    assert (first["sort"], first["direction"], first["per_page"]) == ("pushed", "desc", "100")
    assert {r.method for r in server.asked} == {"GET"}


def test_github_offers_at_most_its_cap_and_says_more_are_there(monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.connectors.github as module

    monkeypatch.setattr(module, "PICK_REPOS", 3)
    offer = github().offer()
    assert [c.value for c in offer.choices] == ["quillmoor/api", "quillmoor/old", "quillmoor-lab/web"]
    assert "more than the 3 repositories" in offer.note[0]


def test_linear_offers_the_teams_the_login_sees_by_key_each_with_its_projects_and_calls_only_list_tools() -> None:
    server = RecordedLinear()
    calls: list[str] = []

    def call(tool: str, arguments: dict[str, Any]) -> Any:
        calls.append(tool)
        return getattr(server, tool)(arguments)

    offer = LinearConnector(linear_source(["T1"]), call).offer()
    assert (offer.key, offer.current) == ("teams", ("T1",))
    assert [c.value for c in offer.choices] == [t["key"] for t in server.teams]
    assert all(c.detail.startswith("projects: ") or c.detail == "no projects" for c in offer.choices)
    assert set(calls) == {"list_teams", "list_projects"}


GROUPED = {"groups": [
    {"label": "channel name patterns", "rows": [{"pattern": "quasar-*", "checked": True}]},
    {"label": "shared with other organizations",
     "rows": [{"channel": "quillmoor-northwind", "checked": True}, {"channel": "not-a-channel", "checked": True},
              {"channel": "general", "pattern": "gen*", "checked": True}]},
]}


def asking(reply: Any, asked: list[tuple[str, str]] | None = None, room: int = 48_000) -> Ask:
    """A stand-in for the workspace's model: each question kept, and `reply` given back."""
    def call(prompt: str, request: str) -> str:
        if asked is not None:
            asked.append((prompt, request))
        return reply(request) if callable(reply) else reply

    return Ask(call, lambda prompt: room)


def test_slack_groups_the_channels_you_belong_to_with_one_question_and_shows_the_rest_unticked() -> None:
    asked: list[tuple[str, str]] = []
    offer = make(StandIn(), slack_source([])).offer(asking("```json\n" + json.dumps(GROUPED) + "\n```", asked))
    assert [p for p, _ in asked] == ["slack-channel-grouping"]
    sent = json.loads(asked[0][1])
    assert {c["name"] for c in sent["channels"]} >= {"quasar-lunch-club", "general"}
    assert all(set(c) <= {"name", "topic", "purpose", "shared", "outside_domains", "members"}
               for c in sent["channels"])
    rows = [(c.value, c.group, c.checked) for c in offer.choices]
    assert rows[:2] == [("quasar-*", "channel name patterns", True),
                        ("quillmoor-northwind", "shared with other organizations", True)]
    assert all(group == "the rest" and not checked for _, group, checked in rows[2:])
    assert "quasar-lunch-club" not in [v for v, _, _ in rows]  # the pattern takes it in
    assert "not-a-channel" not in [v for v, _, _ in rows] and offer.note == ()
    assert offer.listed == 7  # the channels read, which the rows group


def test_slack_sends_each_shared_channels_outside_domains_and_shows_them() -> None:
    asked: list[tuple[str, str]] = []
    slack = StandIn()
    offer = make(slack, slack_source([])).offer(asking(json.dumps(GROUPED), asked))
    sent = {c["name"]: c for c in json.loads(asked[0][1])["channels"]}
    assert sent["quillmoor-northwind"]["outside_domains"] == ["northwind.example", "quillmoor.example"]
    assert "outside_domains" not in sent["general"]
    assert {params.get("channel") for m, params in slack.calls if m == "conversations.members"} == {
        c["id"] for c in slack.channels if c.get("connect") and c.get("member")}
    assert any(c.label == "#quillmoor-northwind  (northwind.example, quillmoor.example)" for c in offer.choices)


def test_the_grouping_question_fits_the_room_beside_the_prompt() -> None:
    from cerebro.connectors.slack import _grouping_request, _Offered

    many = [_Offered(f"C{i:05}", f"channel-{i:05}", "t" * 200, "", False, i) for i in range(3000)]
    request, sent = _grouping_request(many, 20_000)
    assert len(request.encode()) <= 20_000 and '"topic"' not in request
    assert '": ' not in request and '", "' not in request  # compact JSON, as its size is measured
    bigger = len(json.dumps({"channels": [{"name": f"channel-{i:05}", "shared": False, "members": i}
                                          for i in sorted(range(3000), reverse=True)[:sent + 1]],
                             "left_out": 3000 - sent - 1}, separators=(",", ":")).encode())
    assert bigger > 20_000  # the most that fit, not half of them: one more would not
    assert json.loads(request)["left_out"] == 3000 - sent
    few = [_Offered("C1", "one", "é" * 100, "", False, 1)]
    assert len(json.loads(_grouping_request(few, 20_000)[0])["channels"][0]["topic"].encode()) <= 80
    with pytest.raises(ValueError, match="do not fit"):
        _grouping_request(few, 10)


def test_a_reply_that_suggests_no_channel_shows_every_channel_under_the_rest_and_says_nothing_failed() -> None:
    for reply in ('{"groups": []}', json.dumps({"groups": [{"label": "x", "rows": []}]})):
        offer = make(StandIn(), slack_source([])).offer(asking(reply))
        assert offer.note == ()
        assert {c.group for c in offer.choices} == {"the rest"} and not any(c.checked for c in offer.choices)


def test_a_channel_row_that_holds_a_pattern_is_left_out() -> None:
    reply = json.dumps({"groups": [{"label": "x", "rows": [{"channel": "quasar-*", "checked": True},
                                                          {"channel": "general", "checked": True}]}]})
    offer = make(StandIn(), slack_source([])).offer(asking(reply))
    assert [c.value for c in offer.choices if c.group == "x"] == ["general"]


def test_slack_shows_every_channel_ungrouped_with_a_line_that_says_why_when_no_model_groups_them() -> None:
    plain = make(StandIn(), slack_source(["quasar-*", "C02NWSHARED"])).offer(None)
    assert plain.note == ("Every channel shows ungrouped: no model was asked to group them.",)
    assert plain.choices[0] == Choice("quasar-*", "#quasar-*", "1 channel now, and those made later")
    assert "C02NWSHARED" in [c.value for c in plain.choices]  # the file's own entry, so the row shows it ticked
    for reply in ("not json", json.dumps({"groups": [{"label": "x", "rows": [{"channel": "nowhere"}]}]})):
        broken = make(StandIn(), slack_source([])).offer(asking(reply))
        assert broken.note[0].startswith("The model gave no grouping, so every channel shows ungrouped: ")
        assert {c.group for c in broken.choices} == {""}


def test_with_no_key_for_the_model_slack_says_how_to_set_one() -> None:
    from cerebro.app import MissingModelKey

    def no_key(prompt: str, request: str) -> str:
        raise MissingModelKey("the extraction model, fireworks m, has no key: run cerebro secrets set fireworks")

    offer = make(StandIn(), slack_source([])).offer(Ask(no_key, lambda prompt: 48_000))
    assert offer.note[0].startswith("No channel suggestions yet: the extraction model")
    assert offer.note[1] == "Then run cerebro connect slack again to group the channels."


def test_a_channel_the_reply_names_twice_or_a_pattern_that_is_no_pattern_shows_once_as_a_channel() -> None:
    general = next(c["id"] for c in StandIn().channels if c["name"] == "general")
    reply = json.dumps({"groups": [{"label": "x", "rows": [{"channel": "general", "checked": True},
                                                          {"channel": general, "checked": False},
                                                          {"pattern": "shared-ops", "checked": True}]}]})
    offer = make(StandIn(), slack_source([])).offer(asking(reply))
    grouped = [(c.value, c.checked) for c in offer.choices if c.group == "x"]
    assert grouped == [("general", True), ("shared-ops", True)]


def test_a_file_entry_a_pattern_takes_in_keeps_its_own_row_and_says_no_false_thing() -> None:
    reply = json.dumps({"groups": [{"label": "x", "rows": [{"pattern": "quasar-*", "checked": True}]}]})
    offer = make(StandIn(), slack_source(["quasar-lunch-club"])).offer(asking(reply))
    first = offer.choices[0]
    assert (first.value, first.group) == ("quasar-lunch-club", "in sources/slack.yaml")
    rows = picking(offer, "slack").checklist.rows
    assert not any("not among what the login can see" in r.detail for r in rows)


def test_a_connector_that_does_not_answer_leaves_its_file_to_a_person() -> None:
    assert answers("github") and answers("linear") and answers("slack")
    assert not answers("gmail") and not answers("files") and not answers("no-such-kind")
    from cerebro.connectors import CONNECTORS

    for kind, built in CONNECTORS.items():  # a kind that picks a list answers what there is to choose, and back
        assert bool(built.picks) == (built.offer is not Connector.offer), kind


# ---- the file a pick writes ----------------------------------------------------------------------------------------


def test_a_list_is_written_into_a_file_with_every_comment_and_every_other_line_as_it_was() -> None:
    text = ("kind: github\n# what it reads\nrepos:  # only what the team owns\n"
            "  # legal asked to keep this one until March\n  - a/b  # the main one\n  - c/d\n\n"
            "  # bots: leave on exclude\nbots: exclude\n")
    written = with_list(text, "repos", ["c/d", "*z", "#c"])
    assert written == ("kind: github\n# what it reads\nrepos:  # only what the team owns\n"
                       "  # legal asked to keep this one until March\n  # the main one\n  - c/d\n"
                       "  - \"*z\"\n  - \"#c\"\n\n  # bots: leave on exclude\nbots: exclude\n")
    assert yaml.safe_load(written)["repos"] == ["c/d", "*z", "#c"]
    same = with_list(text, "repos", ["a/b", "c/d"])
    assert same == text  # Enter with nothing changed leaves the file as it was, byte for byte
    assert with_list("kind: github\nrepos: [a/b]  # do not add forks\n", "repos", ["a/b", "c/d"]) == (
        "kind: github\nrepos: [a/b, c/d]  # do not add forks\n")
    assert with_list("kind: linear\nteams: [ENG]\n", "teams", []) == "kind: linear\nteams: []\n"
    assert with_list("kind: slack\n\n", "channels", ["a", "x,y"]) == 'kind: slack\n\nchannels: [a, "x,y"]\n'
    assert with_list("kind: github\nrepos:  # none yet\nbots: exclude\n", "repos", ["a/b"]) == (
        "kind: github\nrepos: [a/b]  # none yet\nbots: exclude\n")
    assert with_list("kind: github\r\nrepos:\r\n  - a/b\r\n", "repos", ["a/b", "x/y"]) == (
        "kind: github\r\nrepos:\r\n  - a/b\r\n  - x/y\r\n")
    assert with_list('kind: github\n"repos":\n  - a/b\n', "repos", ["a/b", "c/d"]) == (
        'kind: github\n"repos":\n  - a/b\n  - c/d\n')


def test_a_list_whose_form_cannot_be_kept_is_refused_in_one_line_and_nothing_is_written() -> None:
    for text, why in (("kind: github\nrepos: &mine [a/b]\nalso: *mine\n", "another key leans"),
                      ("kind: github\nrepos: [a/b,\n  # c/d\n  e/f]\n", "comments among its values"),
                      ("kind: github\nrepos: nope\n", "is not a list"),
                      ("kind: github\nrepos: [a]\nrepos: [b]\n", "given twice")):
        with pytest.raises(ListKept, match=why):
            with_list(text, "repos", ["c/d"])


def test_a_file_is_written_whole_or_not_at_all_and_never_over_one_that_changed_or_exists(tmp_path: Path) -> None:
    path = tmp_path / "sources" / "github.yaml"
    add_source(tmp_path, "github")
    assert path.read_text() == "kind: github\n"
    with pytest.raises(ConnectError, match="changed while you picked"):
        add_source(tmp_path, "github")
    with pytest.raises(ConnectError, match="changed while you picked"):
        write_source(path, "kind: other\n", "kind: github\nrepos: []\n")
    with pytest.raises(ConnectError, match="would not read with that list"):
        write_source(path, "kind: github\n", "kind: github\nrepos: [a]\nrepos: [b]\n")
    path.chmod(0o644)
    write_source(path, "kind: github\n", "kind: github\nrepos: []\n")
    assert path.read_text() == "kind: github\nrepos: []\n"
    assert path.stat().st_mode & 0o777 == 0o644  # the file keeps its mode
    assert sorted(p.name for p in path.parent.iterdir()) == ["github.yaml"]  # no partial file left


def test_a_link_to_a_file_in_the_workspace_is_written_through_and_one_that_leaves_it_is_refused(
        tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    (root / "sources").mkdir(parents=True)
    shared = root / "shared.yaml"
    shared.write_text("kind: github\nrepos: [a/b]\n")
    link = root / "sources" / "github.yaml"
    link.symlink_to(shared)
    write_source(link, shared.read_text(), "kind: github\nrepos: [a/b, c/d]\n")
    assert link.is_symlink() and shared.read_text() == "kind: github\nrepos: [a/b, c/d]\n"
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text("kind: github\nrepos: [a/b]\n")
    link.unlink()
    link.symlink_to(outside)
    with pytest.raises(ConnectError, match="outside the workspace"):
        write_source(link, outside.read_text(), "kind: github\nrepos: []\n")
    assert outside.read_text() == "kind: github\nrepos: [a/b]\n" and link.is_symlink()


# ---- the picker -------------------------------------------------------------------------------------------------------


OFFER = Offer("repos", "repository", "repositories",
              (Choice("a/one", "a/one", "pushed 2026-11-01", checked=True), Choice("a/two", "a/two"),
               Choice("b/three", "b/three", group="later")),
              current=("a/two", "gone/old"))


def test_the_files_values_start_ticked_and_one_no_row_shows_gets_a_ticked_row_first() -> None:
    picked = picking(OFFER, "code")
    rows = picked.checklist.rows
    assert [(r.label, r.checked) for r in rows] == [("gone/old", True), ("a/one", False), ("a/two", True),
                                                    ("b/three", False)]
    assert rows[0].detail == "in sources/code.yaml, and not among what the login can see"
    assert [g.header for g in picked.checklist.groups] == ["in sources/code.yaml", "", "later"]
    assert picked.checklist.head == ["Repositories to read", "into sources/code.yaml"]


def test_a_suggested_tick_holds_only_while_the_file_lists_nothing() -> None:
    from dataclasses import replace

    assert [r.checked for r in picking(replace(OFFER, current=()), "code").checklist.rows] == [True, False, False]


def test_enter_writes_the_files_own_first_in_its_order_then_the_others_in_the_lists_each_once() -> None:
    picked = picking(OFFER, "code")
    assert chosen(OFFER, picked, [True, True, True, True]) == ["a/two", "gone/old", "a/one", "b/three"]
    assert chosen(OFFER, picked, [False, False, False, False]) == []
    assert written_line(OFFER, "code", []) == "sources/code.yaml lists no repository."
    assert written_line(OFFER, "code", ["a/one"]) == "sources/code.yaml lists a/one."
    assert written_line(OFFER, "code", None) == "Nothing written: sources/code.yaml is as it was."


def test_without_raw_keys_the_list_prints_plain_and_only_accept_writes() -> None:
    p = printer(plain=True)
    assert pick(OFFER, "code", p, answer=lambda line: "q") is None
    assert pick(OFFER, "code", p, answer=None) is None
    assert pick(OFFER, "code", p, answer=lambda line: "a") == ["a/two", "gone/old"]
    out = p.out.getvalue()
    assert "Accept writes the checked rows as the repos list. Edit the file to pick others." in out
    assert "\x1b" not in out


def test_every_word_from_a_source_or_a_model_is_drawn_clean() -> None:
    offer = Offer("repos", "repository", "repositories", (Choice(BAD, BAD, BAD, BAD),), current=(BAD + "2",),
                  note=(BAD,))
    lines = connect_demo_lines(offer, [Place(BAD, ATTENTION, BAD)])
    assert "\x1b[2J" not in lines and "\x07" not in lines and "\u202e" not in lines
    assert "bad" in lines
    assert "\x1b[2J" not in written_line(offer, "code", [BAD]) and "\x07" not in written_line(offer, "code", [BAD])


def connect_demo_lines(offer: Offer, rows: list[Place]) -> str:
    from cerebro.checklist import Menu, Model

    p = printer()
    menu = Menu(lobby_menu(rows, p)).render(80, 30, True, p.st)
    listed = Model(picking(offer, "code").checklist).render(80, 40, True, p.st)
    return "\n".join([*menu, *listed])


# ---- the lobby ----------------------------------------------------------------------------------------------------


def lay_out_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: RecordedGithub | None = None) -> Path:
    """A blank workspace with a github source, its token kept, and a slack source with none; its requests go to the
    recorded GitHub, and its model is a stand-in."""
    client = httpx.Client(transport=httpx.MockTransport((server or RecordedGithub()).handle))  # every host

    def open_workspace(root: Path | None) -> Cerebro:
        workspace = Cerebro(root, http=client, clock=lambda: NOW)
        workspace.asker = FakeRewriter(lambda request: json.dumps({"groups": []}))
        return workspace

    monkeypatch.setattr(cli, "open_workspace", open_workspace)
    root = tmp_path / "team"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    (root / "sources" / "code.yaml").write_text("kind: github\n# what it reads\nrepos:\n  - quillmoor/api\n")
    (root / "sources" / "chat.yaml").write_text("kind: slack\n")
    (root / "sources" / "broken.yaml").write_text("kind: github\nrepos: nope\n")
    with open_workspace(root) as workspace:
        assert workspace.secrets is not None
        workspace.secrets.set(need(read_source(root / "sources" / "code.yaml")).name, "stand-in")
    return root


def test_the_lobby_lists_each_source_with_its_state_then_each_kind_it_could_add(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        rows = places(workspace)
    states = {p.name: p.state for p in rows}
    assert (states["code"], states["chat"], states["broken"]) == (CONNECTED, NOT_CONNECTED, ATTENTION)
    assert states["local"] == NO_LOGIN
    assert "repos" in next(p.reason for p in rows if p.name == "broken")
    added = [p.name for p in rows if p.state == NOT_ADDED]
    assert added == sorted(added) and "github" not in added and "slack" not in added and "linear" in added
    assert [p.name for p in rows if p.state != NOT_ADDED] == sorted(p.name for p in rows if p.state != NOT_ADDED)


def test_off_a_terminal_with_plain_or_no_color_connect_with_no_source_prints_its_plain_lines() -> None:
    golden = (RECORDED / "no-source.txt").read_text()
    for args in (["connect"], ["connect", "--plain"]):
        got = CliRunner().invoke(cli.main, args)
        assert (got.exit_code, got.output) == (2, golden), args
    flag = CliRunner().invoke(cli.main, ["connect", "--replace"])  # a flag about one source, with none named
    assert flag.exit_code == 2 and "Missing argument 'SOURCE'" in flag.output
    code = ("import sys\nfrom cerebro.cli import main\nmain(sys.argv[1:], prog_name='main')\n")
    for variables, args in (({"NO_COLOR": "1"}, ["connect"]), ({}, ["connect", "--plain"])):
        drawn = on_a_pty(code.replace("sys.argv[1:]", repr(args)), {"TERM": "xterm-256color", **variables})
        assert b"\x1b" not in drawn.replace(b"\x1b[6n", b"")
        assert drawn.decode().replace("\r\n", "\n") == golden, (variables, args)


def test_off_a_terminal_connect_source_prints_its_lines_then_goes_how_to_pick_and_shows_no_list(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    before = (root / "sources" / "code.yaml").read_text()
    got = CliRunner().invoke(cli.main, ["connect", "code", "--workspace", str(root)])
    assert got.exit_code == 0, got.output
    lines = got.output.splitlines()
    assert lines[0] == "[ok] GitHub connection verified"
    assert lines[1].startswith("Connected to GitHub as @someone. The token is kept under github in ")
    assert lines[2:] == [("Pick the repositories to read: run cerebro connect code in a terminal, or list them "
                          "under repos: in sources/code.yaml.")]
    assert (root / "sources" / "code.yaml").read_text() == before
    (root / "sources" / "code.yaml").write_text("kind: github\nrepos: []\n")
    empty = CliRunner().invoke(cli.main, ["connect", "code", "--workspace", str(root)])
    assert empty.output.splitlines()[-1] == ("check     no repository is picked yet: run cerebro connect code in a "
                                             "terminal, or list them under repos: in sources/code.yaml")


def test_off_a_terminal_connect_with_no_source_prints_the_sources_then_its_usage_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)])
    assert got.exit_code == 2 and got.stderr.splitlines()[-1] == "[x] Failed. Run: cerebro connect <name>"
    assert got.stderr.startswith("Error: connect needs a source's name, one of broken, chat, code, local, ")
    table = ["", *got.stdout.splitlines()]
    assert table[0] == "" and table[1].split() == ["broken", "needs", "attention", "repos:", "write", "a", "list",
                                                    "of", "owner/name,", "as", "repos:", "[octo-org/api]", "Run:",
                                                    "cerebro", "connect", "broken"]
    assert next(line for line in table if line.startswith("chat")).split()[:3] == ["chat", "not", "connected"]
    assert next(line for line in table if line.startswith("code")).split()[:2] == ["code", "connected"]


def test_at_a_terminal_that_draws_nothing_connect_asks_which_and_a_list_prints_plain_to_accept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli, "_asking", lambda: True)
    golden = (RECORDED / "no-source.txt").read_text()
    none = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)], input="\n")
    assert golden not in none.output and "Connect which? Names, with commas" in none.output
    assert none.output.splitlines()[-2] == "Nothing picked, nothing connected." and none.exit_code == 0
    picked = CliRunner().invoke(cli.main, ["connect", "code", "--workspace", str(root)], input="x\nj\na\n")
    assert picked.exit_code == 0, picked.output
    assert "[a]ccept  [q]uit  > " in picked.output and "\x1b" not in picked.output
    assert picked.output.rstrip().endswith("sources/code.yaml lists quillmoor/api.")
    walked = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)], input="code\nq\n")
    assert "Nothing written: sources/code.yaml is as it was." in walked.output and walked.exit_code == 0
    assert walked.output.rstrip().splitlines()[-1].startswith("[!] Done with warnings. Check: broken needs attention")


def test_choosing_nothing_at_connect_with_no_source_exits_0_and_waiting_off_a_terminal_stays_2(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    off = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)])
    assert off.exit_code == 2  # off a terminal, the answer it waits for is a usage error
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli, "_asking", lambda: True)
    for given in ("", "\n", " , \n"):  # input that ends, an empty answer, and commas around nothing
        got = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)], input=given)
        assert "Connect which? Names, with commas" in got.output
        assert got.exit_code == 0, (given, got.output)
    import cerebro.connect_screen as screen

    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(cli, "_drawing", lambda plain: printer())
    monkeypatch.setattr(screen, "lobby", lambda *a, **k: None)  # Escape in the lobby
    escaped = CliRunner().invoke(cli.main, ["connect", "--workspace", str(root)])
    assert escaped.exit_code == 0, escaped.output


class Lobby:
    """The lobby, scripted: each call keeps the rows, status and cursor it was shown, and opens the next named row,
    or ends on None."""

    def __init__(self, picks: list[str | None]):
        self.picks, self.shown = list(picks), []

    def __call__(self, places: list[Place], printer: Any, cursor: int = 0, status: Any = ()) -> int | None:
        self.shown.append(({p.name: (p.state, p.reason) for p in places}, list(status), cursor))
        want = self.picks.pop(0) if self.picks else None
        return None if want is None else next(i for i, p in enumerate(places) if p.name == want)


def in_the_lobby(root: Path, monkeypatch: pytest.MonkeyPatch, picks: list[str | None],
                 answers: list[str] = ()) -> tuple[Lobby, str]:
    import contextlib

    import cerebro.connect_screen as screen

    lobby = Lobby(picks)
    monkeypatch.setattr(screen, "lobby", lobby)
    said = iter(answers)
    monkeypatch.setattr(click, "prompt", lambda *a, **k: next(said, ""))
    monkeypatch.setattr(screen, "pick", lambda offer, name, p, answer=None: None)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        cli._lobby(root, printer())
    return lobby, err.getvalue()


def test_a_connect_that_fails_in_the_lobby_says_what_to_run_and_its_row_needs_attention_until_one_succeeds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    lobby, said = in_the_lobby(root, monkeypatch, ["chat", "chat", None], answers=[""])
    states, status, _ = lobby.shown[1]
    assert states["chat"][0] == ATTENTION and states["chat"][1].startswith("connect chat: input is not a terminal")
    assert "  Run: cerebro connect chat" in said and "chat: connect chat: input is not a terminal" in said
    assert strip_escapes(status[0]).startswith("✗ chat: ")
    assert lobby.shown[2][0]["chat"][0] == ATTENTION  # it stays so until a connect succeeds


def test_leaving_the_lobby_names_what_needs_attention_or_what_to_connect_next(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    _, said = in_the_lobby(root, monkeypatch, [None])
    assert strip_escapes(said).strip().startswith("! Done with warnings. Check: broken needs attention: ")
    (root / "sources" / "broken.yaml").unlink()
    (root / "sources" / "code.yaml").write_text("kind: github\nrepos: [quillmoor/api]\n")
    _, said = in_the_lobby(root, monkeypatch, [None])
    assert "Next:" in said and "cerebro connect chat" in said


def test_a_kind_not_added_gets_its_file_only_once_its_connect_succeeds_and_the_cursor_stays_on_its_row(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    target = root / "sources" / "linear.yaml"
    lobby, _ = in_the_lobby(root, monkeypatch, ["linear", None], answers=[""])
    assert not target.exists()  # the connect failed: no file is written, so none is left
    assert lobby.shown[1][0]["linear"][0] == ATTENTION  # a failed row is marked, until a connect succeeds
    rows = list(lobby.shown[1][0])
    assert lobby.shown[1][2] == rows.index("linear")


def test_a_list_left_empty_reads_needs_attention_in_the_lobby(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "sources" / "code.yaml").write_text("kind: github\nrepos: []\n")
    with cli.open_workspace(root) as workspace:
        row = next(p for p in places(workspace) if p.name == "code")
    assert row.state == ATTENTION
    assert row.reason == "sources/code.yaml lists no repositories yet: run cerebro connect code in a terminal to " \
                         "pick them"


def test_a_workspace_that_does_not_open_says_why_in_the_lobby(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(cli, "_drawing", lambda plain: printer())
    import cerebro.connect_screen as screen

    monkeypatch.setattr(screen, "lobby", lambda *a, **k: None)
    got = CliRunner().invoke(cli.main, ["connect", "--workspace", str(empty)])
    assert got.exit_code == 1 and "is not a workspace" in got.output


def test_an_empty_first_answer_connects_it_later_with_no_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_asking", lambda: True)
    lobby, said = in_the_lobby(root, monkeypatch, ["chat", "gong", None], answers=["", ""])
    assert lobby.shown[1][1] == [] and lobby.shown[2][1] == []
    assert lobby.shown[2][0]["chat"][0] == NOT_CONNECTED and lobby.shown[2][0]["gong"][0] == NOT_ADDED
    assert "Run:" not in said and not (root / "sources" / "gong.yaml").exists()


def test_the_lobby_adds_any_kind_with_a_login_once_its_connect_succeeds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    from cerebro.connect import logs_in

    monkeypatch.setattr(cli, "_asking", lambda: True)
    assert logs_in("gong") and logs_in("gmail") and logs_in("calendar") and not logs_in("files")
    lobby, _ = in_the_lobby(root, monkeypatch, ["gong", None], answers=["stand-in-key", "stand-in-secret"])
    assert (root / "sources" / "gong.yaml").read_text() == "kind: gong\n"
    assert strip_escapes(lobby.shown[1][1][0]) == "✓ Gong connected" and lobby.shown[1][1][1] == "added sources/gong.yaml"
    assert lobby.shown[1][0]["gong"][0] == CONNECTED
    (root / "sources" / "local.yaml").write_text("kind: files\n")
    lobby, said = in_the_lobby(root, monkeypatch, ["local", None])
    assert lobby.shown[1][1] == ["local: a files source keeps no login, so there is nothing to connect"]
    assert "database" not in lobby.shown[0][0] and "mcp" not in lobby.shown[0][0]  # no row with no view
    assert "Run:" not in said and lobby.shown[1][0]["local"][0] == NO_LOGIN
    by_name = CliRunner().invoke(cli.main, ["connect", "database", "--workspace", str(root)])
    assert "write it with its kind" in by_name.output  # asked by name, it still says how to write the file


def test_connect_with_a_kind_and_no_file_adds_its_file_once_the_connect_succeeds_so_the_advice_works(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    _, said = in_the_lobby(root, monkeypatch, ["calendar", None])
    assert "  Run: cerebro connect calendar" in said
    refused = CliRunner().invoke(cli.main, ["connect", "calendar", "--workspace", str(root), "--stdin"],
                                 input="http://feed.example/cal\n")
    assert refused.exit_code == 1 and "is not https" in refused.output
    assert not (root / "sources" / "calendar.yaml").exists()
    got = CliRunner().invoke(cli.main, ["connect", "gong", "--workspace", str(root), "--stdin"], input="key:secret\n")
    assert got.exit_code == 0, got.output
    assert got.output.splitlines()[-1] == "added sources/gong.yaml"
    assert (root / "sources" / "gong.yaml").read_text() == "kind: gong\n"
    unknown = CliRunner().invoke(cli.main, ["connect", "nowhere", "--workspace", str(root), "--stdin"], input="x\n")
    assert "is not one of the workspace's sources or kinds of source" in unknown.output


def test_a_failed_connect_names_the_command_its_failure_names() -> None:
    import contextlib

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        cli._failed("chat", "the workspace is locked: run cerebro unlock, then connect again", printer(plain=True))
        cli._failed("code", "the token was refused", printer(plain=True))
    assert err.getvalue().splitlines()[1] == "  Run: cerebro unlock"
    assert err.getvalue().splitlines()[3] == "  Run: cerebro connect code"


def test_a_reason_that_names_its_command_gets_no_second_one_in_the_table(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "sources" / "code.yaml").write_text("kind: github\nrepos: []\n")
    rows = cli._places(root)
    line = next(line for line in cli._table(rows) if line.startswith("code"))
    assert line.count("cerebro connect code") == 1


def test_a_file_deleted_while_its_list_is_read_is_a_change_and_one_with_crlf_keeps_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path_of: list[Path] = []

    class Deleting(RecordedGithub):
        def handle(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/user/repos" and path_of:
                path_of[0].unlink(missing_ok=True)
            return super().handle(request)

    root = lay_out_workspace(tmp_path, monkeypatch, Deleting())
    path = root / "sources" / "code.yaml"
    path_of.append(path)
    from cerebro import checklist

    monkeypatch.setattr(checklist, "ask", lambda lst, p: [True for _ in lst.rows])
    with pytest.raises(ConnectError, match="changed while its list was read"):
        cli._pick(root, "code", printer())
    path_of.clear()
    path.write_bytes(b"kind: github\r\n# what it reads\r\nrepos:\r\n  - quillmoor/api\r\n")
    cli._pick(root, "code", printer())
    written = path.read_bytes()
    assert written.count(b"\r\n") == written.count(b"\n") and written.count(b"  - ") > 1


def test_an_edit_made_while_the_list_is_read_is_never_written_over(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path_of: list[Path] = []

    class Editing(RecordedGithub):
        def handle(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/user/repos" and path_of:
                path_of[0].write_text(path_of[0].read_text() + "  - someone/private-fork\n")
            return super().handle(request)

    root = lay_out_workspace(tmp_path, monkeypatch, Editing())
    path = root / "sources" / "code.yaml"
    path_of.append(path)
    from cerebro import checklist

    monkeypatch.setattr(checklist, "ask", lambda lst, p: [r.checked for r in lst.rows])
    with pytest.raises(ConnectError, match="changed while its list was read"):
        cli._pick(root, "code", printer())
    assert "someone/private-fork" in path.read_text()


def test_a_source_files_name_reaches_the_screen_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    name = "gh\x1b]0;owned\x07\x1b[2J"
    (root / "sources" / f"{name}.yaml").write_text("kind: github\nrepos: [quillmoor/api]\n")
    from cerebro import checklist

    def no_raw(*a: Any, **k: Any) -> Any:
        raise checklist.NoRawKeys("stand-in")

    monkeypatch.setattr(checklist, "ask", no_raw)
    monkeypatch.setattr(click, "prompt", lambda *a, **k: "q")
    p = printer()
    line = cli._pick(root, name, p)
    assert "\x07" not in line and "\x1b]" not in line and "\x1b[2J" not in line
    assert "\x1b]0;" not in p.out.getvalue() and "\x07" not in p.out.getvalue()


def on_the_demo(args: list[str], keys: tuple[tuple[object, ...], ...]) -> str:
    """What the stand-in demo prints at an 80-column pseudo-terminal that takes styles, with keys typed."""
    code = f"import sys; sys.path.insert(0, {str(CHECKS)!r}); import connect_demo; sys.exit(connect_demo.main({args!r}))"
    variables = {name: os.environ[name] for name in ("HOME", "TMPDIR") if name in os.environ}
    variables |= {"TERM": "xterm-256color", "COLORTERM": "truecolor", "CEREBRO_SECRETS_STORE": "file",
                  "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
    drawn = on_a_pty(code, variables, answer=b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\", seconds=40, keys=keys)
    return strip_escapes(drawn.decode("utf-8", "replace")).replace("\r\n", "\n")


def test_at_a_terminal_connect_source_shows_the_list_and_enter_writes_the_ticks_into_the_file() -> None:
    drawn = on_the_demo(["--source", "github"], ((b"Write selected", 1, b"j"), (b"Write selected", 2, b" "),
                                                 (b"Write selected", 3, b"\r")))
    assert "Listed 5 repositories" in drawn
    assert "sources/github.yaml lists quillmoor/handbook, quillmoor/site." in drawn
    assert drawn.rstrip().endswith("kind: github\n# what it reads\nrepos:\n  - quillmoor/handbook\n  - quillmoor/site")


def test_at_a_terminal_escape_writes_nothing() -> None:
    drawn = on_the_demo(["--source", "github"], ((b"Write selected", 2, b"\x1b"),))
    assert "Nothing written: sources/github.yaml is as it was." in drawn
    assert drawn.rstrip().endswith("kind: github\n# what it reads\nrepos:\n  - quillmoor/handbook")


def test_the_lobby_opens_a_source_connects_it_shows_its_grouped_list_and_comes_back_with_how_it_went() -> None:
    drawn = on_the_demo([], ((b"select", 1, b"jj\r"), (b"submit", 2, b"xoxp-stand-in\r"), (b"Import", 1, b"\r"),
                             (b"lists proj", 1, b"\x1b")))
    assert "Sources" in drawn and "GitHub" in drawn and "not connected" in drawn and "not added" not in drawn
    assert "Slack connection verified" in drawn and "Connected to Slack at quillmoor.slack.example." in drawn
    assert "channel name patterns" in drawn and "the rest" in drawn
    assert "Slack connected" in drawn and "sources/slack.yaml lists proj-*, with-larkspur." in drawn


# ---- the workspace's model, held to its cap ---------------------------------------------------------------------------


def test_a_connectors_question_to_the_model_is_held_to_the_cap_and_its_spend_recorded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        model = FakeRewriter(lambda request: "{}")
        workspace.asker = model
        assert workspace.ask_model("slack-channel-grouping", json.dumps({"channels": []})) == "{}"
        assert workspace.ledger.spent(NOW, "extraction") > 0
        assert spent_by_runs(workspace.store, ["slack-channel-grouping"]) > 0  # recorded under the prompt
        workspace.ledger.record("extraction", NOW, workspace.models["extraction"], TokenCount(50_000_000, 0), "spent")
        with pytest.raises(BudgetSpent):
            workspace.ask_model("slack-channel-grouping", json.dumps({"channels": []}))
        assert len(model.asked) == 1


def test_what_a_connector_offers_reaches_the_screen_with_every_kept_secret_masked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        offer = workspace.connectors["code"].offer(workspace.ask_model)
        assert offer is not None
        workspace.mask.add("The API")  # a value read as a secret, which turns up in a repository's description
        masked = workspace.connectors["code"].offer(workspace.ask_model)
    assert "The API" in offer.choices[0].detail
    assert masked is not None and "The API" not in masked.choices[0].detail


# ---- frames, imports and words ---------------------------------------------------------------------------------------


def test_the_frames_in_a_light_and_a_dark_terminal_and_plain_are_the_saved_ones_and_read_the_same() -> None:
    light, dark = connect_demo.frames(dark=False), connect_demo.frames(dark=True)
    assert light == (DATA / "connect-light.ansi").read_text()
    assert dark == (DATA / "connect-dark.ansi").read_text()
    assert connect_demo.frames(None) == (DATA / "connect-plain.txt").read_text()
    assert light != dark and strip_escapes(light) == strip_escapes(dark)
    assert "\x1b" not in connect_demo.frames(None)
    assert all(len(line) <= 80 for line in strip_escapes(dark).splitlines())  # a head wraps at the width


def test_status_mcp_and_connect_off_a_terminal_load_no_style_checklist_or_textual_module(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    code = "import sys\nimport cerebro.connect\nfrom cerebro.cli import main\n"
    for args in (["status"], ["mcp"], ["connect"], ["connect", "chat"]):  # one process: a module loaded stays loaded
        code += f"try:\n    main({[*args, '--workspace', str(root)]!r})\nexcept SystemExit:\n    pass\n"
    code += ("print('LOADED', sorted(m for m in sys.modules if m in ('cerebro.output', 'cerebro.checklist', "
             "'cerebro.progress', 'cerebro.first_run_list') or m.split('.')[0] == 'textual'), file=sys.stderr)\n")
    ran = subprocess.run([sys.executable, "-c", code], check=False, stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, timeout=60, env={**os.environ})
    assert "LOADED []" in ran.stderr, ran.stderr[-2000:]


def test_a_source_the_workspace_refused_says_why_where_its_list_would_be(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    line = cli._pick(root, "broken", printer())
    assert line.startswith("sources/broken.yaml repos: write a list of owner/name")
    assert cli._pick(root, "local", printer()) == ""  # no list to pick: nothing said of writing it by hand


def test_a_question_too_big_for_one_call_or_with_no_key_is_refused_before_anything_is_sent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.app import MissingModelKey

    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        model = FakeRewriter(lambda request: "{}")
        workspace.asker = model
        room = workspace.asking().room("slack-channel-grouping")
        assert 0 < room < 48_000
        with pytest.raises(CerebroError, match="do not fit in one call"):
            workspace.ask_model("slack-channel-grouping", "x" * (room + 1))
        assert model.asked == []
        workspace.asker = None
        monkeypatch.delenv(workspace.models["extraction"].variable, raising=False)
        with pytest.raises(MissingModelKey, match="has no key"):
            workspace.ask_model("slack-channel-grouping", "{}")


def test_a_failed_grouping_call_names_the_prompt_not_another_layers_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import pydantic_ai.direct

    from cerebro.models import DEFAULTS
    from cerebro.run import PydanticRewriter, RewriteFailed

    def refused(*a: Any, **k: Any) -> Any:
        raise RuntimeError("the stand-in provider refused")

    monkeypatch.setattr(pydantic_ai.direct, "model_request_sync", refused)
    rewriter = PydanticRewriter.from_spec(DEFAULTS["extraction"], key=lambda provider: "stand-in",
                                          what="slack-channel-grouping")
    with pytest.raises(RewriteFailed, match="^the slack-channel-grouping call failed: RuntimeError"):
        rewriter.rewrite("system", "{}")


def test_the_live_grouping_check_passes_on_the_labels_own_answer_and_fails_a_wrong_one_with_no_model_or_slack(
        capsys: pytest.CaptureFixture[str]) -> None:
    import os

    import live_channel_grouping

    before = os.environ.get("CEREBRO_SECRETS_STORE")
    assert live_channel_grouping.main(["--fake"]) == 0
    assert os.environ.get("CEREBRO_SECRETS_STORE") == before  # the check puts the process's values back
    out = capsys.readouterr().out
    assert "read 20 of 20 (100%), 0 wrong ticks, 0 cases ungrouped, patterns given 4 of 4" in out
    assert out.rstrip().endswith("PASS")
    cases = json.loads(live_channel_grouping.CASES.read_text())["cases"]
    quiet = next(case for case in cases if case["id"] == "nothing-to-read")
    right = live_channel_grouping.score(quiet, live_channel_grouping.offer_for(quiet, asking('{"groups": []}')))
    assert right["grouped"] and right["ticked"] == [] and right["wrong"] == []  # suggesting nothing is right here
    wrong = live_channel_grouping.score(quiet, live_channel_grouping.offer_for(quiet, asking(json.dumps(
        {"groups": [{"label": "x", "rows": [{"channel": quiet["skip"][0], "checked": True}]}]}))))
    assert wrong["wrong"] == [quiet["skip"][0]]
    flat = live_channel_grouping.score(quiet, live_channel_grouping.offer_for(quiet, asking("not json")))
    assert not flat["grouped"]
    domains = {d for case in cases for c in case["channels"] for d in c.get("outside_domains", [])}
    assert domains and all(d.endswith(".example") for d in domains)
    for case in cases:
        names = {c["name"] for c in case["channels"]}
        assert set(case["read"]) <= names and set(case["skip"]) <= names and not set(case["read"]) & set(case["skip"])


@pytest.mark.parametrize("kind", ["gong", "gmail", "granola", "calendar"])
def test_a_source_with_no_list_to_pick_ends_with_the_check_to_run_next(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Connect lists nothing to pick for these: the pass scopes what they read by the articles, so connect never
    says the file is written by hand, and ends `Done. Next: cerebro sources check <name>`."""
    root = lay_out_workspace(tmp_path, monkeypatch)
    (root / "sources" / f"{kind}.yaml").write_text(f"kind: {kind}\n")
    assert cli._pick(root, kind, printer()) == ""
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr(cli, "_connect_one", lambda *args, **kwargs: "")
    got = CliRunner().invoke(cli.main, ["connect", kind, "--workspace", str(root), "--plain"])
    assert "written by hand" not in got.output
    assert got.stderr.strip().splitlines()[-1].endswith(f"Next: cerebro sources check {kind}"), got.output
