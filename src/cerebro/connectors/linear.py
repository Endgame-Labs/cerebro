"""Linear as a source, read only, through Linear's own MCP server.

A `kind: linear` source reads the issues, projects and project updates of the teams its file lists under `teams:`,
by key, such as `teams: [ENG]`. Each is one record. An issue's id is its identifier, such as `ENG-12`, a project's
`project:<id>` with the server's own `P-ENG-3` form, and an update's `update:<id>`. A record's revision and time are
when it last changed: for an issue its own date or the date of its newest customer request, whichever is later.
Its url opens it in Linear. Its keys are what can bind it to an article, `linear_projects` with its project's name
and, for an issue, `linear_issues` with its identifier. An issue and an update link to their project's record.

The connector speaks to the server through one function handed in, `call(tool, arguments)`, which gives the tool's
reply as JSON and raises `ToolError` for a reply the server marks as an error. It calls a `list_` or `get_` tool
alone, and refuses any other before it leaves. It never reads a key or opens a connection of its own.

A listing reads the window of every listed team anew: the issues and project updates changed within it, and the
projects of the listed teams, of which those changed within it are records. The cursor carries each record's id and
revision, so a pass over an unchanged source lists nothing, and a record gone at the source or aged out of the window
since is in `removed`. A record is kept only when its own team is listed, as the reply gives it, never the team it
was asked for. `list_issues` cuts a description, so a record's whole text comes from `get_issue`, with its comments
from `list_comments`, its customer requests from `list_customers` and a person's address from `get_user`. Nothing of
a record's text is kept between calls.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import uuid
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from email.utils import format_datetime
from pathlib import Path
from typing import Any, ClassVar

from ..clock import system_clock
from ..connector import (
    NOBODY,
    Ask,
    Choice,
    Connector,
    CursorError,
    Listing,
    Offer,
    Ours,
    QuestionError,
    Record,
    RecordNotFound,
    gone,
    guard,
    holds,
    masked_first,
    one_line,
    outside_domain,
)
from ..errors import CerebroError
from ..source import SourceError, SourceFile
from .connecting import Connecting, Prompt, Way
from .github_render import OWN_LINES, without_comments
from .mcp_source_client import ToolRefused
from .questions import Days, Text, asked

__all__ = ["QUESTIONS", "READ_TOOLS", "Call", "LinearConnector", "LinearError", "ReadOnlyError", "ToolError", "render_issue",
           "render_project", "render_update"]

Call = Callable[[str, dict[str, Any]], Any]

# The tools the connector calls, each one that reads: the app hands it a call that reaches these alone.
READ_TOOLS = ("list_teams", "list_issues", "list_comments", "list_projects", "list_customers", "get_issue",
              "get_project", "get_status_updates", "get_user", "list_issue_labels")
SOURCE_KEYS = ("auth", "url", "extract", "window", "teams", "owner", "freshness", "full_every")
REFUSED_KEYS = {"into": "a linear record names no collection, it distills into facts that cite it: delete into",
                "command": "a linear source reads linear's own server: delete command"}
PAGE_SIZE = 100
TOOL_PAGE = 250  # list tools take 250 a page at most
PICK_TEAMS = 250  # the most teams a person is offered to pick from
PROJECT_PAGE = 50  # list_projects and get_project's customer requests take 50
BODY_CAP = 8000
COMMENT_CAP = 2500
DISCUSSION_CAP = 16000
COMMENTS_KEPT = 30
NEEDS_KEPT = 30
LABELS_KEPT = 10
LINKS_KEPT = 5
PROJECT_PREFIX = "project:"
UPDATE_PREFIX = "update:"
# list_issues refuses attachments in its fields: an issue's links are get_issue's alone.
ISSUE_FIELDS = ["id", "uuid", "title", "priority", "url", "createdAt", "updatedAt", "status", "project", "projectId",
                "team", "teamId"]
PROJECT_FIELDS = ["id", "uuid", "name", "description", "url", "status", "teams", "updatedAt"]
CUT_DESCRIPTION = "… (truncated, use `get_issue` for full description)"
_CURSOR = "linear1."
# How far before the latest time Linear gave a record a listing the runner set for changes reaches back, for a change
# Linear stamped late.
OVERLAP = timedelta(minutes=5)
_IDENTIFIER = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)-([0-9]+)$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_NOT_FOUND = ("not found", "no issue", "does not exist", "could not find")
_BLANK_RUN = re.compile(r"\n{3,}")
_HEALTH = {"onTrack": "on track", "atRisk": "at risk", "offTrack": "off track"}

# The questions a curator may ask the source with `ask`, each with the params it takes. Every one reads the listed
# teams alone. A project's description is a name-like field, on one line; no row carries an issue's description, a
# comment or the words of a customer's request.
QUESTIONS = {
    "projects": "the projects of the listed teams, the latest changed first: name, description on one line, teams, "
                "state and when it last changed; params: none",
    "issues": "the issues of the listed teams changed in the last `days` (30 by default, at most 90), the latest "
              "first: identifier, title, team, state, project and dates; params: project (its name or id), team (a "
              "listed key, such as ENG), days",
    "customers": "the customers with a request on an issue or a project of the listed teams in the last `days` (90 "
                 "by default, at most 90), the most requests first: name, domains, how many requests and when; "
                 "params: days",
    "customer_needs": "the customer requests on an issue or a project of the listed teams in the last `days` (90 by "
                      "default, at most 90), the latest first: the customer, its domains, the issue or the project, "
                      "and when, never what the request says; params: days",
    "labels": "the issue labels of the listed teams: name and team; params: none",
}
ASK_DAYS = {"issues": 30, "customers": 90, "customer_needs": 90}
MAX_ASK_DAYS = 90
TAKES = {"projects": {}, "labels": {},
         "issues": {"project": Text("Onboarding"), "team": Text("ENG"), "days": Days(30, 1, MAX_ASK_DAYS)},
         "customers": {"days": Days(90, 1, MAX_ASK_DAYS)}, "customer_needs": {"days": Days(90, 1, MAX_ASK_DAYS)}}
DESCRIBED = 200  # characters a row's description keeps, on one line, as every text in a fetch reply
MOST = {"projects": 100, "issues": 250, "customers": 250, "customer_needs": 250, "labels": 500}  # rows at most


class LinearError(CerebroError):
    """Linear's MCP server answered with something the connector cannot read, or a tool failed."""


class ReadOnlyError(LinearError):
    """The connector was about to call a tool that is not a `list_` or `get_` one."""


class ToolError(CerebroError):
    """What `call` raises for a reply the server marks as an error: the tool and what the server said."""

    def __init__(self, tool: str, text: str):
        super().__init__(f"the mcp tool {tool} answered with an error: {text}")
        self.tool = tool
        self.text = text


# -- reading a reply
def _text(o: Any, *keys: str) -> str:
    """The first of `keys` that holds text: a string, a number as written, or an object's name."""
    if not isinstance(o, dict):
        return ""
    for key in keys:
        value = o.get(key)
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)):
            return f"{value:g}"
        if isinstance(value, dict) and _text(value, "name"):
            return _text(value, "name")
    return ""


def _obj(o: Any, key: str) -> dict[str, Any]:
    value = o.get(key) if isinstance(o, dict) else None
    return value if isinstance(value, dict) else {}


def _items(o: Any, key: str) -> list[dict[str, Any]]:
    """The list under `key`, a bare string as an object of that name."""
    value = o.get(key) if isinstance(o, dict) else None
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            out.append({"name": item})
    return out


def _when(text: str) -> datetime | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _shape(value: Any) -> str:
    """What a reply was, for an error: never its words."""
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "a bare list"
    if isinstance(value, dict):
        keys = sorted(value)
        return "an object with " + ", ".join(keys[:6] + (["…"] if len(keys) > 6 else []))
    return type(value).__name__


# -- rendering text
def _cut(text: str, n: int) -> str:
    """Text cut to at most `n` bytes, on a character's edge, marked as cut."""
    text = masked_first(text)
    data = text.encode()
    if len(data) <= n:
        return text
    return data[:n - len(" [cut]")].decode(errors="ignore") + " [cut]"


def _clean(text: str) -> str:
    """Text as a reader sees it: no HTML comments, no runs of blank lines."""
    text = without_comments(text.replace("\r\n", "\n"))
    return guard(_BLANK_RUN.sub("\n\n", text).strip(), OWN_LINES)


def _safe_name(name: str) -> str:
    return " ".join(name.translate(str.maketrans("", "", '<>()"')).split())


@dataclass
class _Person:
    name: str = ""
    email: str = ""
    id: str = ""

    def who(self) -> str:
        name = _safe_name(self.name)
        if self.email:
            return f"{name} <{self.email}>" if name else self.email
        return name


def _person(o: dict[str, Any], key: str, id_key: str = "") -> _Person | None:
    value = o.get(key)
    if isinstance(value, str) and value:
        return _Person(value, id=_text(o, id_key) if id_key else "")
    if isinstance(value, dict):
        return _Person(_text(value, "name") or _text(value, "displayName"), _text(value, "email"), _text(value, "id"))
    return None


class _Text:
    """A record's text: headers a reader would see, the body, then the discussion."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def header(self, key: str, value: str) -> None:
        value = " ".join(value.split())
        if value:
            self.parts.append(f"{key}: {value}\n")

    def body(self, text: str) -> None:
        self.parts.append("\n")
        text = _clean(text)
        if text:
            self.parts.append(_cut(text, BODY_CAP) + "\n")

    def discussion(self, parts: list[str]) -> None:
        if parts:
            self.parts.append("\nDiscussion, oldest first:\n\n" + "\n\n".join(parts) + "\n")

    def __str__(self) -> str:
        return "".join(self.parts)


def _date(when: datetime | None) -> str:
    return format_datetime(when) if when else ""


def _day(when: datetime | None) -> str:
    return when.date().isoformat() if when else ""


def _latest(text: str) -> datetime:
    """A time a reply gave, for sorting the latest first: one that does not read sorts last."""
    return _when(text) or datetime.min.replace(tzinfo=UTC)


def _domains(domains: list[str], ours: Ours = NOBODY) -> list[str]:
    """A customer's domains as `domain` keys, written as the part a company registers, as an address's are, and
    never a public mail service's or one of your company's."""
    return sorted({d for d in (outside_domain(f"customer@{raw}", ours) for raw in domains) if d})


@dataclass
class _Need:
    """One customer request: who asked, what they asked, and when."""

    id: str
    customer: str
    domains: list[str]
    says: str
    at: datetime | None
    customer_id: str = ""
    issue: str = ""  # the identifier of the issue it is on, or ""
    archived: bool = False


def _need(o: dict[str, Any], customer: dict[str, Any] | None = None) -> _Need:
    customer = customer if customer is not None else (o.get("customer") if isinstance(o.get("customer"), dict)
                                                      else {"name": _text(o, "customer")})
    return _Need(_text(o, "id"), _text(customer, "name"), [_text(d, "name") for d in _items(customer, "domains")],
                 " ".join((_text(o, "content") or _text(o, "body")).split()), _when(_text(o, "createdAt")))


def _merge_needs(listed: list[_Need], own: list[_Need]) -> list[_Need]:
    """A record's requests, each once: by id, or by who asked and what for one with none. `listed` come first."""
    seen: set[str] = set()
    out = []
    for need in listed + own:
        key = "id:" + need.id if need.id else f"says:{need.customer}\n{need.says}"
        if key not in seen:
            seen.add(key)
            out.append(need)
    return out


def _customers_line(needs: list[_Need]) -> str:
    shown = sorted((n for n in needs if n.customer or n.says),
                   key=lambda n: (n.at or datetime.min.replace(tzinfo=UTC), n.id))
    older = max(len(shown) - NEEDS_KEPT, 0)
    kept = []
    for need in shown[older:]:
        name = need.customer + (f" ({', '.join(need.domains)})" if need.customer and need.domains else "")
        kept.append(name + (": " if name and need.says else "") + _cut(need.says, 300))
    line = "; ".join(kept)
    if older:
        line += f"; [{older} older request{'s' if older > 1 else ''} not in this record]"
    return line


def render_issue(issue: dict[str, Any], team: tuple[str, str], comments: list[dict[str, Any]],
                 needs: list[_Need], people: dict[str, str] | None = None) -> str:
    """An issue's text, as `get_issue` and `list_comments` give it: team, identifier and title, state, project,
    customers, priority, labels, people, links and dates, the description, then the newest comments oldest first."""
    people = people or {}
    text = _Text()
    text.header("Team", f"{team[1]} ({team[0]})")
    text.header("Issue", f"{_text(issue, 'id')} {_text(issue, 'title')}")
    state = _text(issue, "status")
    for key, word in (("completedAt", " on "), ("canceledAt", " on "), ("startedAt", ", started ")):
        if when := _when(_text(issue, key)):
            state += word + _day(when)
            break
    text.header("State", state)
    text.header("Project", _text(issue, "project"))
    text.header("Milestone", _text(issue, "projectMilestone"))
    text.header("Customers", _customers_line(needs))
    priority = _text(issue, "priority")
    if priority != "No priority":
        text.header("Priority", priority)
    estimate = _obj(issue, "estimate").get("value", issue.get("estimate"))
    if isinstance(estimate, (int, float)) and not isinstance(estimate, bool):
        text.header("Estimate", f"{estimate:g} points")
    text.header("Due", _text(issue, "dueDate"))
    text.header("Labels", ", ".join(_text(label, "name") for label in _items(issue, "labels")[:LABELS_KEPT]))
    for key, id_key, label in (("assignee", "assigneeId", "Assignee"), ("createdBy", "createdById", "Creator")):
        if person := _person(issue, key, id_key):
            person.email = person.email or people.get(person.id, "")
            text.header(label, person.who())
    links = [" ".join(filter(None, (_text(a, "title"), _text(a, "url"))))
             for a in _items(issue, "attachments")[:LINKS_KEPT]]
    text.header("Links", "; ".join(links))
    text.header("Opened", _date(_when(_text(issue, "createdAt"))))
    text.header("Date", _date(_when(_text(issue, "updatedAt"))))
    text.body(_text(issue, "description").removesuffix(CUT_DESCRIPTION))
    text.discussion(_discussion(comments, people))
    return str(text)


def _discussion(comments: list[dict[str, Any]], people: dict[str, str]) -> list[str]:
    """The newest comments, oldest first, with a line for those left out."""
    timed = sorted(comments, key=lambda c: _when(_text(c, "createdAt")) or datetime.min.replace(tzinfo=UTC))
    older = max(len(timed) - COMMENTS_KEPT, 0)
    timed = timed[older:]
    parts: list[str] = []
    used = 0
    for i in range(len(timed) - 1, -1, -1):
        comment = timed[i]
        body = _clean(_text(comment, "body"))
        if not body:
            continue
        author = _person(comment, "author") or _Person()
        author.email = author.email or people.get(author.id, "")
        when = _when(_text(comment, "createdAt"))
        head = f"[{when.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC') if when else ''}, {author.who()}]"
        part = head + "\n" + _cut(body, COMMENT_CAP)
        if used + len(part) > DISCUSSION_CAP:
            parts.append(f"[{i + 1} earlier comments left out]")
            break
        used += len(part)
        parts.append(part)
    parts.reverse()
    if older:
        parts.insert(0, f"[{older} older comment{'s' if older > 1 else ''} not in this record]")
    return parts


def _teams_line(project: dict[str, Any]) -> str:
    return ", ".join(f"{_text(t, 'name')} ({_text(t, 'key')})" for t in _items(project, "teams") if _text(t, "key"))


def render_project(project: dict[str, Any], needs: list[_Need], people: dict[str, str] | None = None) -> str:
    """A project's text, as `get_project` gives it: status, health, progress, summary, customers, teams, lead,
    members, dates and milestones, then its overview."""
    people = people or {}
    text = _Text()
    text.header("Project", _text(project, "name"))
    state = _text(project, "status")
    if when := _when(_text(project, "completedAt")) or _when(_text(project, "canceledAt")):
        state += " on " + _day(when)
    text.header("State", state)
    health = _text(project, "health")
    text.header("Health", _HEALTH.get(health, health))
    progress = project.get("progress")
    if isinstance(progress, (int, float)) and not isinstance(progress, bool):
        text.header("Progress", f"{progress * 100:.0f}%")
    summary = _text(project, "summary") or _text(project, "description")
    text.header("Summary", summary)
    text.header("Customers", _customers_line(needs))
    text.header("Teams", _teams_line(project))
    if lead := _person(project, "lead"):
        lead.email = lead.email or people.get(lead.id, "")
        text.header("Lead", lead.who())
    text.header("Members", ", ".join(_Person(_text(m, "name") or _text(m, "displayName"), _text(m, "email")).who()
                                     for m in _items(project, "members")))
    priority = _text(project, "priority")
    if priority != "No priority":
        text.header("Priority", priority)
    text.header("Start", _text(project, "startDate"))
    text.header("Target", _text(project, "targetDate"))
    text.header("Milestones", "; ".join(" ".join(filter(None, (_text(m, "name"), _text(m, "targetDate"))))
                                        for m in _items(project, "milestones")))
    text.header("Date", _date(_when(_text(project, "updatedAt"))))
    content = _text(project, "content") or (_text(project, "description") if _text(project, "summary") else "")
    text.body(content)
    return str(text)


def render_update(update: dict[str, Any], project: str, people: dict[str, str] | None = None) -> str:
    """A project update's text: its project, who wrote it, its health and its body."""
    people = people or {}
    text = _Text()
    text.header("Project", project)
    user = _person(update, "user") or _Person()
    user.email = user.email or people.get(user.id, "")
    text.header("Update", "project update by " + user.who())
    health = _text(update, "health")
    text.header("Health", _HEALTH.get(health, health))
    text.header("Date", _date(_when(_text(update, "createdAt"))))
    text.body(_text(update, "body"))
    return str(text)


# -- one pass
@dataclass
class _Project:
    """A project of a listed team, from `list_projects`: its record's id, its uuid, name and teams."""

    id: str
    uuid: str
    name: str
    teams: list[str]
    row: dict[str, Any]


@dataclass
class _Pass:
    """What a pass reads once: the teams, the projects, the customer requests and people's addresses. It lives
    for one pass, and the gets after it, so nothing is read again within them."""

    teams: list[dict[str, Any]] | None = None
    projects: list[_Project] | None = None
    needs: tuple[dict[str, list[_Need]], dict[str, list[_Need]]] | None = None
    people: dict[str, str] = field(default_factory=dict)
    dropped: set[str] = field(default_factory=set)


# What connect says and asks: which way in, then what that way says. The browser waits as long as this package's
# sign-in does.
CONNECTING = Connecting(
    # Drawn, the choice says nothing above its rows: the rows explain themselves, and the heading has one blank
    # line under it. A plain line still says what each way does.
    says=(),
    plain=(("Sign in through the browser: Opens your browser to sign in to Linear's MCP server, waits up to ten "
           "minutes, and keeps the login in the secrets store as one item."),
           ("Paste an API key: Reads a personal API key of yours without echo, checks it with Linear, and keeps it "
           "in the secrets store as one item.")),
    title="Linear",
    needs="a Linear API key, or a browser sign-in",
    choices=(
        Way("Sign in through the browser", "a login kept in the secrets store", "browser", "oauth",
               (("Linear signs you in through your browser: connect opens it next and waits up to ten minutes for "
                "you to sign in."),),
               connected="Connected to Linear through the browser. The login is kept under {secret} in {where}.",
               plain=(),
               kept=("Linear is connected already, and the browser login kept under {secret} in {where} stays. Pass "
                     "--replace to replace it.")),
        Way("Paste an API key", "a personal API key with Read access", "key", "",
               (("Make a personal API key in Linear under Settings > Account > Security & access, with Read access "
                "only."),
                "Or store one first with: cerebro secrets set {secret}"),
               (Prompt("Linear API key", "Linear API key, or Enter to connect later"),),
               "Linear is not connected yet. Run cerebro connect {name} when you have a key.",
               "Linear is not connected yet: run cerebro connect {name} when you have a key",
               connected="Connected to Linear with an API key. The key is kept under {secret} in {where}.",
               plain=(("Make a personal API key in Linear under Settings > Account > Security & access, with Read "
                       "access only."), "Or store one first with: cerebro secrets set {secret}"),
               kept=("Linear is connected already, and the API key kept under {secret} in {where} stays. Pass "
                     "--replace to replace it."))),
    later="Linear is not connected yet. Run cerebro connect {name} to sign in through the browser or paste a key.",
    later_check="Linear is not connected yet: run cerebro connect {name}",
    asks_again=False)


class LinearConnector(Connector):
    """A `kind: linear` source. `call` speaks to Linear's MCP server, `now` is the clock the window is read by."""

    connecting = CONNECTING
    mcp_server: ClassVar[str] = "https://mcp.linear.app/mcp/readonly"
    mcp_probe = ("list_teams", {"limit": 1}, READ_TOOLS)
    IDENTITY_KEYS: ClassVar[Mapping[str, int]] = {"linear_projects": 0, "linear_issues": 0}

    @classmethod
    def login_name(cls, source: SourceFile) -> str | None:
        """Every source of the kind keeps one login, under the kind's name."""
        return "linear"

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> LinearConnector:
        return cls(source, app.mcp_session(source, cls.mcp_server, READ_TOOLS), now=app.clock, zone=app.zone,
                   ours=app.ours)

    def __init__(self, source: SourceFile, call: Call, *, page_size: int = PAGE_SIZE,
                 now: Callable[[], datetime] | None = None, zone: tzinfo = UTC, ours: Ours = NOBODY):
        if source.kind != "linear":
            raise SourceError(f"sources/{source.name}.yaml is kind {source.kind}: a linear connector reads kind "
                              f"linear alone")
        teams = source.settings.get("teams") or []
        if not isinstance(teams, list) or not all(isinstance(t, str) and t.strip() for t in teams):
            raise SourceError(f"sources/{source.name}.yaml teams: write a list of team keys, as teams: [ENG]")
        self.name = source.name
        self.source = source
        self.teams = [t.strip() for t in teams]
        self._call_tool = call
        self.page_size = max(1, page_size)
        self.now = now or system_clock
        self.zone = zone  # the workspace's zone, in which a question's days are counted
        self.ours = ours  # your addresses and your company's domains, never a row's key
        self.behind = source.behind()
        self.notices: list[str] = []
        self._held_run: tuple[str, _Pass] | None = None
        config = json.dumps([self.behind.total_seconds() if self.behind else None, source.extract, "",  # "": into
                             sorted(t.upper() for t in self.teams)])
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]

    # -- talking to the server
    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if not tool.startswith(("list_", "get_")):
            raise ReadOnlyError(f"{self.name}: {tool} is not a tool that reads: the connector calls list_ and get_ "
                                f"tools alone")
        try:
            return self._call_tool(tool, dict(arguments))
        except (ToolError, ToolRefused) as error:  # its own stand-in's error, or the mcp client's
            if any(words in error.text.lower() for words in _NOT_FOUND):
                raise RecordNotFound(f"{self.name}: {tool} found no such record: list the source for the ids it "
                                     f"holds") from None
            raise LinearError(f"{self.name}: the mcp tool {tool} answered with an error: run the check again, and "
                              f"if it fails again, check that the login can read the teams under teams:") from None

    def _page(self, tool: str, arguments: dict[str, Any], key: str) -> tuple[list[dict[str, Any]], str]:
        """One page of a list tool, and the tool's cursor for the next, or "" when none follows."""
        reply = self._call(tool, arguments)
        if not isinstance(reply, dict) or not isinstance(reply.get(key), list):
            raise LinearError(f"{self.name}: {tool} answered no list the connector reads: {_shape(reply)}, "
                              f"where an object with {key} was expected: check the server is linear's own")
        cursor = reply.get("cursor")
        more = reply.get("hasNextPage") is True and isinstance(cursor, str) and cursor
        return list(_items(reply, key)), cursor if more else ""

    def _pages(self, tool: str, arguments: dict[str, Any], key: str) -> Iterator[dict[str, Any]]:
        """Every item of a list tool, page by page, until a page says no more or a cursor comes back."""
        seen: set[str] = set()
        while True:
            reply = self._call(tool, arguments)
            if not isinstance(reply, dict) or not isinstance(reply.get(key), list):
                raise LinearError(f"{self.name}: {tool} answered no list the connector reads: {_shape(reply)}, "
                                  f"where an object with {key} was expected: check the server is linear's own")
            yield from _items(reply, key)
            cursor = reply.get("cursor")
            if reply.get("hasNextPage") is not True or not isinstance(cursor, str) or not cursor or cursor in seen:
                return
            seen.add(cursor)
            arguments = {**arguments, "cursor": cursor}

    picks = ("teams", "team", "teams")

    def offer(self, ask: Ask | None = None) -> Offer:
        """The teams the login can see, up to `PICK_TEAMS`, each by its key, with the names of the projects it can
        see in that team as its detail, so a row shows what a team brings in: what `teams:` may list. Projects are a
        row's detail and never a list of the file's, since `teams:` is the one bound. Read only."""
        teams, more = [], False
        for team in self._pages("list_teams", {"limit": TOOL_PAGE}, "teams"):
            if len(teams) == PICK_TEAMS:
                more = True
                break
            teams.append(team)
        rows = []
        for team in teams:
            key, name, id = _text(team, "key"), _text(team, "name"), _text(team, "id")
            if not key:
                continue
            arguments = {"team": id, "orderBy": "updatedAt", "limit": PROJECT_PAGE, "fields": ["id", "name"]}
            projects = [_text(row, "name") for row in self._pages("list_projects", arguments, "projects")] if id else []
            named = [p for p in projects if p]
            rows.append(Choice(key, f"{name} ({key})" if name else key,
                               one_line("projects: " + ", ".join(named), 300) if named else "no projects"))
        note = (f"The login sees more than the {PICK_TEAMS} teams shown here: list others under teams: by key.",) \
            if more else ()
        return Offer(*self.picks, tuple(rows), tuple(self.teams), note, head="Linear teams to read")

    def _object(self, tool: str, reply: Any, key: str) -> dict[str, Any]:
        if not isinstance(reply, dict):
            raise LinearError(f"{self.name}: {tool} answered no {key} the connector reads: {_shape(reply)}")
        inner = reply.get(key)
        return inner if isinstance(inner, dict) else reply

    # -- what a pass reads once
    def _listed(self, key: str) -> bool:
        return bool(key) and key.upper() in (t.upper() for t in self.teams)

    def _all_teams(self, run: _Pass) -> list[dict[str, Any]]:
        if run.teams is None:
            run.teams = list(self._pages("list_teams", {"limit": TOOL_PAGE}, "teams"))
        return run.teams

    def _team(self, run: _Pass, id: str = "", key: str = "", name: str = "") -> tuple[str, str] | None:
        """A team of `list_teams` by its id, key or name, as its key and name."""
        for team in self._all_teams(run):
            if ((id and _text(team, "id") == id) or (key and _text(team, "key").upper() == key.upper())
                    or (name and _text(team, "name").lower() == name.lower())):
                return _text(team, "key"), _text(team, "name")
        return None

    def _team_ids(self, run: _Pass, notices: list[str]) -> list[str]:
        return [id for id, _ in self._listed_teams(run, notices, self.teams)]

    def _listed_teams(self, run: _Pass, notices: list[str], keys: list[str]) -> list[tuple[str, str]]:
        """The id and key of each team of `keys`, with a notice for a key no team the login sees carries."""
        found = []
        for key in keys:
            team = next((t for t in self._all_teams(run) if _text(t, "key").upper() == key.upper()), None)
            if team is None or not _text(team, "id"):
                notices.append(f"{self.name}: no team the login can see has the key {key}, which teams: lists: "
                               f"fix the key, or ask for access to the team")
                continue
            found.append((_text(team, "id"), _text(team, "key")))
        return found

    def _issue_team(self, run: _Pass, issue: dict[str, Any]) -> tuple[str, str] | None:
        ref = _obj(issue, "team")
        if ref:
            return self._team(run, _text(ref, "id"), _text(ref, "key"), _text(ref, "name"))
        return self._team(run, id=_text(issue, "teamId")) or self._team(run, name=_text(issue, "team"))

    def _project_teams(self, run: _Pass, project: dict[str, Any]) -> list[str]:
        keys = []
        for team in _items(project, "teams"):
            key = _text(team, "key")
            if not key and (found := self._team(run, _text(team, "id"), name=_text(team, "name"))):
                key = found[0]
            if key:
                keys.append(key)
        return keys

    def _projects(self, run: _Pass, notices: list[str]) -> list[_Project]:
        """The projects of the listed teams, each once, kept when one of their own teams is listed."""
        if run.projects is None:
            run.projects, seen = [], set()
            for team in self._team_ids(run, notices):
                arguments = {"team": team, "orderBy": "updatedAt", "limit": PROJECT_PAGE, "fields": PROJECT_FIELDS}
                for row in self._pages("list_projects", arguments, "projects"):
                    id = _text(row, "id")
                    if not id or id in seen:
                        continue
                    seen.add(id)
                    teams = self._project_teams(run, row)
                    if not any(self._listed(k) for k in teams):
                        run.dropped.add(PROJECT_PREFIX + id)
                        continue
                    run.projects.append(_Project(id, _text(row, "uuid"), _text(row, "name"), teams, row))
        return run.projects

    def _project_of(self, run: _Pass, ref: str, name: str, team: str) -> _Project | None:
        """The listed project an issue or an update names: by its id or uuid, else by its name in the team."""
        projects = self._projects(run, [])
        found = next((p for p in projects if ref and ref in (p.id, p.uuid)), None)
        if found is None and name and team:
            found = next((p for p in projects if p.name == name and team.upper() in (t.upper() for t in p.teams)),
                         None)
        return found

    def _needs(self, run: _Pass) -> tuple[dict[str, list[_Need]], dict[str, list[_Need]]]:
        """Every customer request `list_customers` holds, once, by its issue's identifier and by its project's uuid
        for one attached to a project alone."""
        if run.needs is None:
            by_issue: dict[str, list[_Need]] = {}
            by_project: dict[str, list[_Need]] = {}
            seen: set[str] = set()
            for customer in self._pages("list_customers", {"includeNeeds": True, "limit": TOOL_PAGE}, "customers"):
                for row in _items(customer, "needs"):
                    id = _text(row, "id")
                    if not id or id in seen:
                        continue
                    seen.add(id)
                    need = _need(row, customer)
                    need.customer_id, need.archived = _text(customer, "id"), bool(row.get("archivedAt"))
                    if issue := _text(_obj(row, "issue"), "id"):
                        need.issue = issue
                        by_issue.setdefault(issue, []).append(need)
                    elif project := _text(_obj(row, "project"), "id"):
                        by_project.setdefault(project, []).append(need)
            run.needs = (by_issue, by_project)
        return run.needs

    def _email(self, run: _Pass, id: str) -> None:
        """A person's address by their id, from `get_user`, once a call. One it cannot say stays unknown."""
        if not id or id in run.people:
            return
        try:
            user = self._object("get_user", self._call("get_user", {"query": id}), "user")
        except (RecordNotFound, LinearError):
            user = {}
        run.people[id] = _text(user, "email")

    # -- records
    def _extract(self) -> str:
        return self.source.extract or "facts"

    def _issue_record(self, run: _Pass, issue: dict[str, Any], team: tuple[str, str]) -> Record:
        identifier = _text(issue, "id")
        changed = _when(_text(issue, "updatedAt"))
        for need in self._needs(run)[0].get(identifier, []):
            if need.at and (changed is None or need.at > changed):
                changed = need.at
        name = _text(issue, "project")
        keys = {"linear_issues": [identifier]}
        fields = {"kind": "issue", "team": team[0], "state": _text(issue, "status")}
        links = []
        if name:
            keys["linear_projects"] = [name]
            fields["project"] = name
            if project := self._project_of(run, _text(issue, "projectId"), name, team[0]):
                links.append(PROJECT_PREFIX + project.id)
        stamp = _stamp(changed) if changed else ""
        return Record(id=identifier, source=self.name, revision=stamp, at=stamp, url=_text(issue, "url"), keys=keys,
                      links=links, fields=fields, extract=self._extract(), into="")

    def _project_record(self, project: dict[str, Any]) -> Record:
        name = _text(project, "name")
        changed = _when(_text(project, "updatedAt"))
        stamp = _stamp(changed) if changed else ""
        return Record(id=PROJECT_PREFIX + _text(project, "id"), source=self.name, revision=stamp, at=stamp,
                      url=_text(project, "url"), keys={"linear_projects": [name]} if name else {},
                      fields={"kind": "project", "project": name, "state": _text(project, "status")},
                      extract=self._extract(), into="")

    def _update_record(self, update: dict[str, Any], project: _Project) -> Record:
        changed = _when(_text(update, "updatedAt")) or _when(_text(update, "createdAt"))
        stamp = _stamp(changed) if changed else ""
        return Record(id=UPDATE_PREFIX + _text(update, "id"), source=self.name, revision=stamp, at=stamp,
                      url=_text(update, "url"), keys={"linear_projects": [project.name]} if project.name else {},
                      links=[PROJECT_PREFIX + project.id],
                      fields={"kind": "project_update", "project": project.name},
                      extract=self._extract(), into="")

    # -- the window
    def _floor(self) -> datetime | None:
        return self.now() - self.behind if self.behind is not None else None

    def _since(self, arguments: dict[str, Any], floor: datetime | None) -> dict[str, Any]:
        if floor is not None:
            arguments["updatedAt"] = floor.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return arguments

    def _run(self, token: str) -> _Pass:
        """What a pass reads once, the teams, the projects and every customer request, kept for the pass that
        `token` names and for the gets that follow it, so neither a later page nor a get reads them again."""
        if self._held_run is None or self._held_run[0] != token:
            self._held_run = (token, _Pass())
        return self._held_run[1]

    def _begin(self, run: _Pass, notices: list[str]) -> list[list[str]]:
        """The steps of a new pass: each listed team's issues, then each project and its updates."""
        queue = [["issues", team, ""] for team in self._team_ids(run, notices)]
        for project in self._projects(run, notices):
            queue += [["project", project.id, ""], ["updates", project.id, ""]]
        return queue

    def _step(self, run: _Pass, unit: list[str], floor: datetime | None) -> list[Record]:
        """One page of one step: its records within the window. The step keeps the tool's cursor for its next
        page, or "" once it is done."""
        kind, ref, cursor = unit
        out: list[Record] = []
        if kind == "project":
            project = next((p for p in self._projects(run, []) if p.id == ref), None)
            out = [self._project_record(project.row)] if project is not None else []
            unit[2] = ""
        elif kind == "issues":
            arguments = self._since({"team": ref, "orderBy": "updatedAt", "limit": TOOL_PAGE,
                                     "fields": ISSUE_FIELDS}, floor)
            items, unit[2] = self._page("list_issues", {**arguments, **({"cursor": cursor} if cursor else {})},
                                        "issues")
            for issue in items:
                identifier = _text(issue, "id")
                found = self._issue_team(run, issue)
                if not identifier:
                    continue
                if found is None or not self._listed(found[0]):
                    run.dropped.add(identifier)
                    continue
                out.append(self._issue_record(run, issue, found))
        else:
            project = next((p for p in self._projects(run, []) if p.id == ref), None)
            if project is None:
                unit[2] = ""
                return []
            arguments = self._since({"type": "project", "project": ref, "limit": TOOL_PAGE}, floor)
            items, unit[2] = self._page("get_status_updates",
                                        {**arguments, **({"cursor": cursor} if cursor else {})}, "statusUpdates")
            for update in items:
                if not _text(update, "id") or _text(_obj(update, "project"), "id") not in (project.id, project.uuid):
                    run.dropped.add(UPDATE_PREFIX + _text(update, "id"))
                    continue
                out.append(self._update_record(update, project))
        return [r for r in out if floor is None or (r.at and _when(r.at) >= floor)]

    # -- the cursor
    def _cursor(self, seen: dict[str, str], current: dict[str, Any] | None = None, ask: str = "") -> str:
        state = json.dumps({"config": self._config, "seen": seen, "pass": current,
                            **({"ask": ask} if ask else {})}, sort_keys=True, separators=(",", ":"))
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(state.encode())).decode()

    def _pass(self, cursor: str) -> dict[str, Any] | None:
        state = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
        current = state.get("pass")
        if current is not None and not (isinstance(current, dict) and isinstance(current.get("queue"), list)
                                        and isinstance(current.get("found"), list)):
            raise CursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with no "
                              f"cursor to start over")
        return current

    def _seen(self, cursor: str) -> tuple[dict[str, str], bool]:
        try:
            if not cursor.startswith(_CURSOR):
                raise ValueError
            state = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
            seen = state["seen"]
            if not isinstance(seen, dict) or not all(isinstance(v, str) for v in seen.values()):
                raise ValueError
        except (ValueError, KeyError, TypeError, zlib.error, binascii.Error):
            raise CursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with no "
                              f"cursor to start over") from None
        return seen, state.get("config") == self._config

    lists_changes = True

    def cursor_for(self, cursor: str | None, *, full: bool) -> str | None:
        """The cursor set so the next pass reads the window anew and asks after what it no longer finds, when
        `full`, or asks each tool only for what changed since the latest time Linear gave a record, less `OVERLAP`,
        and calls nothing removed, when not. A pass under way goes on as it began, and a first listing reads the
        window anyway."""
        if cursor is None:
            return None
        seen, _ = self._seen(cursor)
        current = self._pass(cursor)
        if current is not None:
            return cursor
        return self._cursor(seen, None, "full" if full else "changes")

    def _ask_of(self, cursor: str | None) -> str:
        """How `cursor_for` set a cursor: "changes", "full", or "" for neither."""
        if cursor is None:
            return ""
        ask = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):]))).get("ask", "")
        return ask if ask in ("changes", "full") else ""

    # -- the contract
    def list(self, since: str | None = None) -> Listing:
        """One page of a pass. A pass walks each listed team's issues and each project and its updates, one page of
        a tool at a time, and the cursor carries the tool's own cursor, so a later page never reads an earlier one
        again. What the pass found is kept until it ends, when what it no longer found is asked about."""
        notices: list[str] = []
        seen, same = self._seen(since) if since is not None else ({}, True)
        current = self._pass(since) if since is not None and same else None
        if not same:
            seen = {}  # the source file changed, so every record is listed anew
        if not self.teams:
            notices.append(f"sources/{self.name}.yaml lists no teams, so nothing is read: add them by key as "
                           f"teams: [ENG]")
        floor = self._floor()
        if current is None:
            token = uuid.uuid4().hex[:12]  # names this pass, so its reads are kept for it alone
            ask = self._ask_of(since) if same else ""
            if ask == "changes" and seen:  # what Linear changed since the latest time it gave, less the overlap
                latest = max((when for when in map(_when, seen.values()) if when is not None), default=None)
                if latest is not None:
                    floor = max(floor, latest - OVERLAP) if floor is not None else latest - OVERLAP
            current = {"token": token, "floor": floor.isoformat() if floor else None, "found": [],
                       "queue": self._begin(self._run(token), notices) if self.teams else [],
                       **({"ask": ask} if ask else {})}
        run = self._run(current["token"])
        floor = datetime.fromisoformat(current["floor"]) if current["floor"] else None
        found, records = set(current["found"]), []
        while current["queue"] and len(records) < self.page_size:
            unit = current["queue"][0]
            for record in self._step(run, unit, floor):
                if record.id in found:
                    continue
                found.add(record.id)
                if seen.get(record.id) != record.revision:
                    seen[record.id] = record.revision
                    records.append(record)
            if not unit[2]:
                current["queue"].pop(0)
        if run.dropped:
            n = len(run.dropped)
            notices.append(f"{self.name}: {n} record{'s' if n > 1 else ''} named no listed team and "
                           f"{'were' if n > 1 else 'was'} left out")
        self.notices = notices
        if current["queue"]:
            current["found"] = sorted(found)
            return Listing(records, self._cursor(seen, current), [], True, list(notices))
        if current.get("ask") == "changes":  # absence proves nothing here: what was not found is kept as it was
            return Listing(records, self._cursor(seen), [], False, list(notices))
        absent = [id for id in seen if id not in found]
        removed = gone(absent, self._exists)
        kept = {id: rev for id, rev in seen.items() if id in found}
        return Listing(records, self._cursor(kept), removed, False, list(notices))

    def _exists(self, id: str) -> bool:
        """Whether linear still holds a record a listing no longer found: one that left the window still exists."""
        if id.startswith((PROJECT_PREFIX, UPDATE_PREFIX)):
            return holds(self.get)(id)
        try:
            self._call("get_issue", {"id": id})
        except RecordNotFound:
            return False
        return True

    # -- a curator's questions
    questions = QUESTIONS

    def _say(self, text: str) -> str:
        return f"linear source {self.name}: {text}"

    def _asked(self, question: str, params: Mapping[str, Any] | None, limit: int) -> dict[str, Any]:
        """The params of a question this source answers, each read as its type, or `QuestionError` in one line.
        Nothing is asked of Linear before they read."""
        read = asked(f"linear source {self.name}", QUESTIONS, TAKES, question, params, limit)
        team = read.get("team", "")
        if team and not self._listed(team):
            raise QuestionError(self._say(f"team {one_line(team, 40)} is not one sources/{self.name}.yaml lists "
                                          f"under teams: give one of {', '.join(self.teams) or 'none, as it lists none'}"))
        return read

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """A curator's question, as `QUESTIONS` lists them, answered from the teams the file lists alone, through
        the same `list_` tools a pass calls. A row carries names, ids, keys, dates and counts, and a project's
        description on one line, never an issue's description, a comment or a request's words. A question reads
        Linear anew and leaves a pass's reads alone. A colleague's source gives no rows and asks Linear nothing."""
        read = self._asked(question, params, limit)
        if self.source.owner not in ("", "me"):
            return Listing([], None, [], False, [self._say("is a colleague's linear login, and only your own feeds "
                                                           "admission: ask the linear source whose owner is me")])
        if not self.teams:
            nothing = f"sources/{self.name}.yaml lists no teams, so nothing is read: add them by key as teams: [ENG]"
            return Listing([], None, [], False, [nothing])
        limit = min(limit, MOST[question])
        floor = self.now() - timedelta(days=read.get("days", ASK_DAYS.get(question, 0)))
        notices: list[str] = []
        run = _Pass()
        if question == "projects":
            rows = self._ask_projects(run, notices)
        elif question == "issues":
            rows = self._ask_issues(run, read, floor, notices, limit + 1)
        elif question == "labels":
            rows = self._ask_labels(run, notices, limit + 1)
        else:
            rows = self._ask_needs(run, question, floor, notices)
        return Listing(rows[:limit], None, [], len(rows) > limit, list(dict.fromkeys(notices)))

    def _ask_day(self, when: datetime | None) -> str:
        """A row's day, in the workspace's zone, as a window counts days everywhere else."""
        return when.astimezone(self.zone).date().isoformat() if when else ""

    def _row(self, id: str, name: str, keys: dict[str, list[str]], fields: dict[str, Any]) -> Record:
        return Record(id=id, source=self.name, revision="", keys={k: v for k, v in keys.items() if v},
                      fields={"name": name, **{k: v for k, v in fields.items() if v not in ("", None, [])}})

    def _ask_projects(self, run: _Pass, notices: list[str]) -> list[Record]:
        projects = sorted(self._projects(run, notices), key=lambda p: _latest(_text(p.row, "updatedAt")),
                          reverse=True)
        return [self._row(PROJECT_PREFIX + p.id, p.name, {"linear_projects": [p.name] if p.name else []},
                          {"description": one_line(_text(p.row, "description"), DESCRIBED), "teams": p.teams,
                           "state": _text(p.row, "status"),
                           "updated": self._ask_day(_when(_text(p.row, "updatedAt")))}) for p in projects]

    def _ask_issues(self, run: _Pass, read: dict[str, Any], floor: datetime, notices: list[str],
                    most: int) -> list[Record]:
        keys = [read["team"]] if read["team"] else self.teams
        wanted = read.get("project", "")
        project = None
        if wanted:
            bare = wanted.removeprefix(PROJECT_PREFIX)
            projects = self._projects(run, notices)
            project = (next((p for p in projects if bare in (p.id, p.uuid)), None)
                       or next((p for p in projects if p.name.lower() == wanted.lower()), None))
            if project is None:
                notices.append(f"{self.name}: no project of the listed teams is named {one_line(wanted, 60)} or "
                               f"has that id: ask projects for those the teams hold")
                return []
        found: list[tuple[datetime, Record]] = []
        for id, key in self._listed_teams(run, notices, keys):
            arguments = self._since({"team": id, "orderBy": "updatedAt", "limit": min(TOOL_PAGE, most),
                                     "fields": ISSUE_FIELDS}, floor)
            kept = 0
            for issue in self._pages("list_issues", arguments, "issues"):
                identifier, team = _text(issue, "id"), self._issue_team(run, issue)
                changed = _when(_text(issue, "updatedAt"))
                if not identifier or team is None or team[0].upper() != key.upper() or changed is None or \
                        changed < floor:
                    continue
                if project is not None and self._project_of(run, _text(issue, "projectId"), _text(issue, "project"),
                                                            team[0]) is not project:
                    continue
                name = _text(issue, "project")
                found.append((changed, self._row(identifier, identifier, {"linear_issues": [identifier]}, {
                    "title": _text(issue, "title"), "team": team[0], "state": _text(issue, "status"),
                    "project": name, "created": self._ask_day(_when(_text(issue, "createdAt"))), "updated": self._ask_day(changed)})))
                kept += 1
                if kept >= most:
                    break
        found.sort(key=lambda pair: pair[1].id)
        found.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in found[:most]]

    def _ask_labels(self, run: _Pass, notices: list[str], most: int) -> list[Record]:
        rows: list[Record] = []
        seen: set[str] = set()
        for id, key in self._listed_teams(run, notices, self.teams):
            for label in self._pages("list_issue_labels", {"team": id, "limit": TOOL_PAGE}, "labels"):
                label_id, name = _text(label, "id"), _text(label, "name")
                if not label_id or not name or label_id in seen or label.get("isGroup") is True:
                    continue
                seen.add(label_id)
                rows.append(self._row(label_id, name, {}, {"team": key}))
                if len(rows) >= most:
                    return rows
        return rows

    def _requests(self, run: _Pass, floor: datetime, notices: list[str]) -> list[tuple[_Need, str]]:
        """The customer requests on an issue or a project of the listed teams made since `floor`, the latest first,
        each with the name of the project it is on alone, or "". An issue's team is the key its identifier
        starts with; a request on a project alone is of the projects of the listed teams."""
        by_issue, by_project = self._needs(run)
        found: list[tuple[_Need, str]] = []
        for identifier, needs in by_issue.items():
            match = _IDENTIFIER.match(identifier)
            if match is not None and self._listed(match.group(1)):
                found += [(need, "") for need in needs]
        projects = self._projects(run, notices) if by_project else []
        for ref, needs in by_project.items():
            project = next((p for p in projects if ref in (p.id, p.uuid)), None)
            if project is not None:
                found += [(need, project.name) for need in needs]
        found = [(need, name) for need, name in found if need.customer and need.at is not None and need.at >= floor]
        found.sort(key=lambda pair: pair[0].id)
        found.sort(key=lambda pair: pair[0].at or floor, reverse=True)
        return found

    def _ask_needs(self, run: _Pass, question: str, floor: datetime, notices: list[str]) -> list[Record]:
        requests = self._requests(run, floor, notices)
        if question == "customer_needs":
            return [self._row(need.id, need.customer,
                              {"linear_issues": [need.issue] if need.issue else [],
                               "linear_projects": [project] if project else []},
                              {"domains": need.domains, "issue": need.issue, "project": project,
                               "created": self._ask_day(need.at), "archived": need.archived or None})
                    for need, project in requests]
        customers: dict[str, dict[str, Any]] = {}
        for need, _ in requests:  # the latest first, so a customer's first request is its last seen
            day = self._ask_day(need.at)
            entry = customers.setdefault(need.customer_id or need.customer, {
                "name": need.customer, "domains": need.domains, "requests": 0, "first": day, "last": day})
            entry["requests"] += 1
            entry["first"] = day
        ranked = sorted(customers.items(), key=lambda pair: (-pair[1]["requests"], pair[1]["name"].lower()))
        return [self._row(id, entry["name"], {"domain": _domains(entry["domains"], self.ours)},
                          {"requests": entry["requests"], "first": entry["first"], "last": entry["last"]})
                for id, entry in ranked]

    def get(self, id: str) -> Record:
        if id.startswith(PROJECT_PREFIX):
            return self._get_project(id.removeprefix(PROJECT_PREFIX))
        if id.startswith(UPDATE_PREFIX):
            return self._get_update(id.removeprefix(UPDATE_PREFIX))
        return self._get_issue(id)

    def _missing(self, id: str, why: str = "") -> RecordNotFound:
        return RecordNotFound(f"{self.name} has no record {id}{why}: list the source for the ids it holds")

    def _get_issue(self, id: str) -> Record:
        match = _IDENTIFIER.match(id)
        if match is None or not self._listed(match.group(1)):
            raise self._missing(id, ", or its team is not under teams:")
        run = self._run(self._held_run[0] if self._held_run else "")
        issue = self._object("get_issue", self._call("get_issue", {"id": id, "includeCustomerNeeds": True}), "issue")
        team = self._issue_team(run, issue)
        if team is None or not self._listed(team[0]) or _text(issue, "id").upper() != id.upper():
            raise self._missing(id, ", or its team is not under teams:")
        record = self._issue_record(run, issue, team)
        comments = list(self._pages("list_comments", {"issueId": _text(issue, "uuid") or id, "limit": TOOL_PAGE},
                                    "comments"))
        for person in [issue.get("createdById"), issue.get("assigneeId")] + [_obj(c, "author").get("id")
                                                                           for c in comments]:
            self._email(run, person if isinstance(person, str) else "")
        own = [_need(n) for n in _items(issue, "customerNeeds")]
        needs = _merge_needs(self._needs(run)[0].get(record.id, []), own)
        record.body = render_issue(issue, team, comments, needs, run.people)
        return record

    def _get_project(self, id: str) -> Record:
        if not _PROJECT_ID.match(id):
            raise self._missing(PROJECT_PREFIX + id)
        run = self._run(self._held_run[0] if self._held_run else "")
        arguments: dict[str, Any] = {"query": id, "includeCustomerNeeds": True, "customerNeedsLimit": PROJECT_PAGE}
        project: dict[str, Any] | None = None
        own: list[_Need] = []
        seen: set[str] = set()
        while True:
            page = self._object("get_project", self._call("get_project", arguments), "project")
            project = project or page
            own += [_need(n) for n in _items(page, "customerNeeds") if not _obj(n, "issue")]
            info = _obj(page, "customerNeedsPageInfo")
            cursor = _text(info, "endCursor")
            if info.get("hasNextPage") is not True or not cursor or cursor in seen:
                break
            seen.add(cursor)
            arguments = {**arguments, "customerNeedsCursor": cursor}
        if _text(project, "id") != id or not any(self._listed(k) for k in self._project_teams(run, project)):
            raise self._missing(PROJECT_PREFIX + id, ", or none of its teams is under teams:")
        record = self._project_record(project)
        self._email(run, _text(_obj(project, "lead"), "id"))
        needs = _merge_needs(self._needs(run)[1].get(_text(project, "uuid"), []), own)
        record.body = render_project(project, needs, run.people)
        return record

    def _get_update(self, id: str) -> Record:
        if not _PROJECT_ID.match(id):
            raise self._missing(UPDATE_PREFIX + id)
        run = self._run(self._held_run[0] if self._held_run else "")
        reply = self._call("get_status_updates", {"type": "project", "id": id})
        if isinstance(reply, dict) and "statusUpdates" not in reply and _text(reply, "id"):
            items = [reply]
        elif isinstance(reply, dict) and isinstance(reply.get("statusUpdates"), list):
            items = _items(reply, "statusUpdates")
        else:
            raise LinearError(f"{self.name}: get_status_updates answered no update the connector reads: "
                              f"{_shape(reply)}: check the server is linear's own")
        update = next((u for u in items if _text(u, "id") == id), None)
        ref = _obj(update, "project")
        project = self._project_of(run, _text(ref, "id"), "", "") if update else None
        if project is None:
            raise self._missing(UPDATE_PREFIX + id, ", or its project is of no team under teams:")
        record = self._update_record(update, project)
        self._email(run, _text(_obj(update, "user"), "id"))
        record.body = render_update(update, project.name, run.people)
        return record
