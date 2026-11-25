"""`cerebro connect`'s lobby and its lists to tick, on stand-ins: no model call, no network and no key.

    uv run python checks/connect_demo.py                  # the lobby, in this terminal
    uv run python checks/connect_demo.py --source github  # connect github, then its list of repositories
    uv run python checks/connect_demo.py --source slack   # connect slack: its manifest, then its channels, grouped
    uv run python checks/connect_demo.py --source calendar  # connect calendar: where it lives, then its address
    uv run python checks/connect_demo.py --plain          # what `connect` prints with no source off a terminal
    uv run python checks/connect_demo.py --save <folder>  # save the frames, light and dark and plain

It makes a workspace in a temp folder, and keeps its config and secrets there too, with a github source already
connected and listing one repository, and a slack source not yet connected. Every request goes to a stand-in that
answers GitHub's and Slack's read calls with made-up repositories and channels; nothing reaches a network. Any token
typed at the prompt is taken, once a Slack one has a user token's form (xoxp-stand-in), and the channels are
grouped by a stand-in model that always answers alike. A calendar address is answered by a stand-in feed. The frames under tests/data/tui/ are `--save`'s, drawn on an 80-column
terminal: the lobby and a list, and what Slack, Calendar and Linear say and ask before their first question.
"""

from __future__ import annotations

import argparse
import atexit
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import click
import httpx

from cerebro.connect_screen import CONNECTED, NO_LOGIN, NOT_ADDED, NOT_CONNECTED, Place, lobby_menu, picking
from cerebro.connector import Choice, Offer
from cerebro.output import Printer

WIDTH = 80

# Made-up repositories, the most recently pushed first, as GitHub's /user/repos lists them.
REPOS = [
    {"full_name": "quillmoor/handbook", "pushed_at": "2026-11-17T10:00:00Z", "private": True,
     "description": "How the team works"},
    {"full_name": "quillmoor/site", "pushed_at": "2026-11-12T08:30:00Z", "description": "The public site"},
    {"full_name": "quillmoor/planner", "pushed_at": "2026-10-30T16:00:00Z", "private": True},
    {"full_name": "quillmoor/old-notes", "pushed_at": "2025-02-01T09:00:00Z", "archived": True},
    {"full_name": "quillmoor-lab/sketches", "pushed_at": None},
]
# Made-up channels the user belongs to.
CHANNELS = [
    {"id": "C01", "name": "proj-alpha", "topic": "Alpha, week by week", "members": 9},
    {"id": "C02", "name": "proj-beta", "topic": "Beta launch room", "members": 7},
    {"id": "C03", "name": "proj-gamma", "members": 4},
    {"id": "C04", "name": "planning", "purpose": "What we work on next", "members": 21},
    {"id": "C05", "name": "with-larkspur", "shared": True, "topic": "Shared with Larkspur", "members": 12},
    {"id": "C06", "name": "random", "members": 40},
    {"id": "C07", "name": "lunch", "members": 18},
]
# Made-up people of the Slack workspace: the one shared channel has a member from another organization.
USERS = [{"id": "U01", "name": "stand-in", "real_name": "Stand In", "profile": {"email": "stand-in@quillmoor.example"}},
         {"id": "U02", "name": "rowan", "real_name": "Rowan", "profile": {"email": "rowan@larkspur.example"}}]
GROUPING = {"groups": [
    {"label": "channel name patterns", "rows": [{"pattern": "proj-*", "checked": True}]},
    {"label": "shared with other organizations", "rows": [{"channel": "with-larkspur", "checked": True}]},
    {"label": "across many things", "rows": [{"channel": "planning", "checked": False}]},
]}


def stand_in(request: httpx.Request) -> httpx.Response:
    """GitHub's and Slack's read calls, answered from the made-up lists above, whatever the token."""
    host, path = request.url.host, request.url.path
    if host == "api.github.com" and path == "/user":
        return httpx.Response(200, json={"login": "stand-in"})
    if host == "api.github.com" and path == "/user/repos":
        return httpx.Response(200, json=REPOS)
    if host == "slack.com" and path == "/api/auth.test":
        return httpx.Response(200, json={"ok": True, "user_id": "U01", "user": "stand-in", "team": "Quillmoor",
                                         "url": "https://quillmoor.slack.example/"})
    if host == "slack.com" and path == "/api/conversations.list":
        rows = [{"id": c["id"], "name": c["name"], "is_member": True, "is_ext_shared": bool(c.get("shared")),
                 "num_members": c["members"], "topic": {"value": c.get("topic", "")},
                 "purpose": {"value": c.get("purpose", "")}} for c in CHANNELS]
        return httpx.Response(200, json={"ok": True, "channels": rows, "response_metadata": {"next_cursor": ""}})
    if host == "slack.com" and path == "/api/users.list":
        return httpx.Response(200, json={"ok": True, "members": USERS, "response_metadata": {"next_cursor": ""}})
    if host == "slack.com" and path == "/api/conversations.members":
        return httpx.Response(200, json={"ok": True, "members": [u["id"] for u in USERS],
                                         "response_metadata": {"next_cursor": ""}})
    if host == "calendar.example":
        return httpx.Response(200, text="BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")
    return httpx.Response(404, json={"ok": False, "error": "not_in_the_stand_in", "message": "Not Found"})


class StandInModel:
    """The workspace's model, standing in: it groups the channels the same way every time and spends nothing."""

    def rewrite(self, system: str, request: str) -> tuple[str, Any]:
        from cerebro.models import TokenCount

        return json.dumps(GROUPING), TokenCount(0, 0)


def made_up_offer(current: tuple[str, ...] = ("quillmoor/handbook", "quillmoor/retired")) -> Offer:
    """The repositories above as GitHub's connector offers them, with a file that lists one of them and one the
    login cannot see."""
    rows = []
    for repo in REPOS:
        pushed = (repo.get("pushed_at") or "")[:10]
        detail = ("archived, " if repo.get("archived") else "") + ("private, " if repo.get("private") else "") \
            + (f"pushed {pushed}" if pushed else "never pushed")
        if repo.get("description"):
            detail += f": {repo['description']}"
        rows.append(Choice(repo["full_name"], repo["full_name"], detail))
    return Offer("repos", "repository", "repositories", tuple(rows), current)


def made_up_places() -> list[Place]:
    return [Place("github", CONNECTED, title="GitHub"), Place("local", NO_LOGIN, title="Local files"),
            Place("slack", NOT_CONNECTED, title="Slack"), Place("linear", NOT_ADDED, title="Linear")]


def frames(dark: bool | None) -> str:
    """The lobby and GitHub's list, as an 80-column terminal draws them, or as plain text when `dark` is None."""
    from cerebro.checklist import Menu, Model, write_plain
    from cerebro.output import strip_escapes

    if dark is None:
        p = Printer(io.StringIO(), io.StringIO(), plain=True, width=lambda: WIDTH)
        menu = Menu(lobby_menu(made_up_places(), p, ["✓ GitHub connected"])).render(WIDTH, 30)
        listed = write_plain(picking(made_up_offer(), "github").checklist)
        return "\n".join(strip_escapes(line) for line in menu) + "\n\n-- the list --\n\n" + listed
    p = Printer(io.StringIO(), io.StringIO(), terminal=lambda stream: True, dark=dark, width=lambda: WIDTH,
                getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))
    menu = Menu(lobby_menu(made_up_places(), p, ["✓ GitHub connected"])).render(WIDTH, 30, p.st)
    listed = Model(picking(made_up_offer(), "github").checklist).render(WIDTH, 40, True, p.st)
    return "\n".join(menu) + "\n\n-- the list --\n\n" + "\n".join(listed) + "\n"


def views(dark: bool | None) -> str:
    """What Slack, Calendar and Linear say and ask before their first question, as an 80-column terminal draws
    them, or as plain lines when `dark` is None: Slack's how-to and manifest, the choice of where a calendar lives,
    and of Linear's way in."""
    from cerebro.checklist import Field, FieldAsk, Menu
    from cerebro.connect_screen import explained, plain_ways, ways_menu
    from cerebro.connectors.calendar import CONNECTING as CALENDAR
    from cerebro.connectors.linear import CONNECTING as LINEAR
    from cerebro.connectors.slack import CONNECTING as SLACK
    from cerebro.output import strip_escapes

    def named(lines: tuple[str, ...]) -> list[str]:
        return [line.replace("{name}", "slack").replace("{secret}", "linear") for line in lines]

    if dark is None:
        p = Printer(io.StringIO(), io.StringIO(), plain=True, width=lambda: WIDTH)
        out = [*explained(p, named(SLACK.plain), SLACK.block, named(SLACK.plain_after)), SLACK.asks[0].plain + ": ",
               "", "-- calendar --", "", *explained(p, CALENDAR.plain), CALENDAR.asks[0].plain + ": ",
               "", "-- linear --", "", *explained(p, named(LINEAR.plain)), plain_ways(LINEAR.choices)]
        return "\n".join(strip_escapes(line) for line in out) + "\n"
    p = Printer(io.StringIO(), io.StringIO(), terminal=lambda stream: True, dark=dark, width=lambda: WIDTH,
                getenv=lambda name: {"COLORTERM": "truecolor"}.get(name))
    def asked(label: str) -> list[str]:  # a view's prompt, with the legend of its keys under it
        return Field(FieldAsk(label, hidden=True)).render(WIDTH, 20, True, p.st)

    out = [p.bold(SLACK.title), *explained(p, named(SLACK.says), SLACK.block, named(SLACK.after)),
           *asked(SLACK.asks[0].label),
           "", "-- calendar --", "",
           *Menu(ways_menu(CALENDAR.title, CALENDAR.choices)).render(WIDTH, 20, True, p.st),
           "", p.bold(CALENDAR.choices[1].title), *explained(p, CALENDAR.choices[1].says),
           *asked(CALENDAR.choices[1].asks[0].label),
           "", "-- linear --", "", *Menu(ways_menu(LINEAR.title, LINEAR.choices, LINEAR.says)).render(WIDTH, 20, True, p.st)]
    return "\n".join(out) + "\n"


def stand_in_workspace() -> Path:
    """A workspace in a temp folder, its config, trust and secrets under it too, with github connected."""
    from click.testing import CliRunner

    import cerebro.trust
    from cerebro import cli
    from cerebro.app import Cerebro
    from cerebro.connect import need
    from cerebro.source import read_source

    folder = Path(tempfile.mkdtemp(prefix="cerebro-connect-demo-"))
    atexit.register(shutil.rmtree, folder, True)  # the temp workspace goes when the demo ends
    root = folder / "demo"
    # nothing outside the temp folder: its config, trust and secrets go under it, and no keychain is touched
    os.environ["XDG_CONFIG_HOME"] = str(folder / "config")
    os.environ["CEREBRO_SECRETS_STORE"] = "file"
    real_trusted = cerebro.trust.trusted
    cerebro.trust.trusted = lambda path, *args, **kwargs: (Path(path).resolve().is_relative_to(folder.resolve())
                                                           or real_trusted(path, *args, **kwargs))
    made = CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1", "--timezone", "UTC"])
    if made.exit_code != 0:
        raise SystemExit(made.output)
    (root / "sources" / "github.yaml").write_text("kind: github\n# what it reads\nrepos:\n  - quillmoor/handbook\n")
    (root / "sources" / "slack.yaml").write_text("kind: slack\n")
    client = httpx.Client(transport=httpx.MockTransport(stand_in))

    def open_workspace(path: Path | None) -> Cerebro:
        workspace = Cerebro(path, http=client)
        workspace.asker = StandInModel()
        return workspace

    cli.open_workspace = open_workspace
    with open_workspace(root) as workspace:
        assert workspace.secrets is not None
        workspace.secrets.set(need(read_source(root / "sources" / "github.yaml")).name, "stand-in-token")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--save", type=Path)
    args = parser.parse_args(argv)
    if args.save is not None:
        args.save.mkdir(parents=True, exist_ok=True)
        for theme, dark in (("light", False), ("dark", True)):
            (args.save / f"connect-{theme}.ansi").write_text(frames(dark), encoding="utf-8")
            (args.save / f"connect-views-{theme}.ansi").write_text(views(dark), encoding="utf-8")
        (args.save / "connect-plain.txt").write_text(frames(None), encoding="utf-8")
        (args.save / "connect-views-plain.txt").write_text(views(None), encoding="utf-8")
        return 0
    from cerebro import cli

    root = stand_in_workspace()
    print(f"workspace {root}")
    command = ["connect", *([args.source] if args.source else []), "--workspace", str(root),
               *(["--plain"] if args.plain else [])]
    try:
        cli.main(command, standalone_mode=False)
    except SystemExit as stop:
        return int(stop.code or 0)
    except click.ClickException as error:  # a usage error, as `connect` prints it
        error.show()
        return error.exit_code
    if args.source:
        print((root / "sources" / f"{args.source}.yaml").read_text(), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
