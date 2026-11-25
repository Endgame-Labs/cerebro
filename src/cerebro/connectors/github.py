"""GitHub as a source, read only: the issues, pull requests and releases of the repositories a source file lists.

The file's `repos:` list, as `owner/name`, is the outer bound of what is read, and a check reads only the
repositories it is handed, the whole list by default: which repositories an article binds is decided elsewhere. A
repository no longer handed, or no longer listed, is read no more and forgotten; only a record github no
longer holds is in `removed`, never one that left the window or the scope. A record is
`owner/name#12` for an issue or a pull request and `owner/name@v1.2.0` for a release, its repository as the file
writes it, so a repository renamed on GitHub keeps its records' ids and keys, and `name_with_owner` says what GitHub
calls it now. Its revision is when it last changed, its url opens it on GitHub, its key is its repository under
`github_repos`, and its links are the issues a pull request closes. A bot's issue or pull request is left out
unless the file says `bots: include`.

The first read of a repository reaches back over the file's window, 90 days by default, whole days counted in the
caller's zone, and `all` for its whole history. Each read after it asks for what changed since the last one began,
and a light scan of the window's numbers finds what was deleted or aged out. Pull requests and issues come whole
through GraphQL, 40 or 50 a request, and releases from REST. A listing page is one page from GitHub. A repository
the token cannot read is skipped with a notice and not counted as read, so the first read that can reaches its
window.

The connector only reads: GET to REST and queries to GraphQL, never a mutation, and the token goes to the API's own
address alone. A rate limit waits for its reset when that is within five minutes, at most twice a request, and
otherwise stops the listing, which goes on from the last cursor. A server error is asked again twice. It keeps no
copy of a record's text: the cursor carries each record's revision and nothing more, and `get` asks GitHub again.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
import re
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from ..clock import system_clock
from ..connector import (
    Ask,
    Choice,
    Connector,
    Listing,
    Offer,
    QuestionError,
    Record,
    RecordNotFound,
    clip_line,
    gone,
    holds,
    one_line,
    retry_after,
)
from ..connector import CursorError as SharedCursorError
from ..errors import CerebroError
from ..source import SourceError, SourceFile
from .connecting import Connecting, KeyRefused, KeyUnproven, Need, Profile, Prompt
from .github_render import People, moment, render_item, render_release
from .questions import Days, Text, asked

__all__ = ["API", "QUESTIONS", "AnswerError", "CursorError", "GithubConnector", "GithubError", "RateLimited", "ReadOnly",
           "TokenRefused", "Unreachable", "parse_id"]

API = "https://api.github.com"
SOURCE_KEYS = ("auth", "extract", "window", "repos", "bots", "owner", "freshness", "full_every")
REFUSED_KEYS = {"into": "a github record names no collection, it distills into facts that cite it: delete into"}
KEY = "github_repos"
PULLS_PAGE = 40
ISSUES_PAGE = 50
SCAN_PAGE = 100
RELEASES_PAGE = 100
MAX_WAIT = 300.0
RESET_WINDOW = 3601.0  # seconds: GitHub's primary limit resets each hour, so a later reset is no real one
LIMIT_WAITS = 2
SERVER_RETRIES = 2
_CURSOR = "github1."
# How far before the latest time GitHub gave for a repository's records a listing the runner set for changes reaches
# back, for a change GitHub stamped late.
OVERLAP = timedelta(minutes=5)
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ID = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#([1-9][0-9]{0,9})|@(.+))$")

# The questions a curator may ask the source with `ask`, each with the params it takes. Every one reads the
# repositories the file lists alone. A repository's description is a name-like field, on one line; no row carries a
# body, a comment, a review or a diff.
QUESTIONS = {
    "repositories": "the repositories the source file lists, as owner/name, in its order: GitHub's name for one "
                    "renamed, its description on one line, its topics, whether it is archived, its default branch, "
                    "when it was last pushed, and its open issues and pull requests; params: none",
    "pull_requests": "the pull requests of a listed repository changed in the last `days` (30 by default, at most "
                     "90), the latest first: number, title, state, whether a draft, labels, author and dates; params: "
                     "repo (required, owner/name as the file lists it), days",
    "issues": "the issues of a listed repository changed in the last `days` (30 by default, at most 90), the latest "
              "first: number, title, state, labels, author and dates; params: repo (required), days",
    "releases": "the releases of a listed repository published in the last `days` (90 by default, at most 90), the "
                "latest first: tag, name, whether a prerelease, author and date; params: repo (required), days",
}
ASK_DAYS = {"pull_requests": 30, "issues": 30, "releases": 90}
MAX_ASK_DAYS = 90
TAKES = {"repositories": {}, **{q: {"repo": Text("owner/name", required=True), "days": Days(d, 1, MAX_ASK_DAYS)}
                               for q, d in ASK_DAYS.items()}}
DESCRIBED = 200  # characters a row's description keeps, on one line, as every text in a fetch reply
MOST = {"repositories": 100, "pull_requests": 200, "issues": 200, "releases": 200}  # rows at most

def _next_link(header: str) -> str:
    """The address a `Link` header marks `rel="next"`, or "". Read part by part, in one pass over the header."""
    for part in header.split(","):
        target, _, params = part.partition(";")
        target = target.strip()
        if target.startswith("<") and target.endswith(">") and any(
                p.strip().replace(" ", "") == 'rel="next"' for p in params.split(";")):
            return target[1:-1]
    return ""


_ACTOR = "__typename login ... on User { name }"
_COMMON = f"""number title body url state createdAt updatedAt closedAt
        author {{ {_ACTOR} }}
        labels(first: 20) {{ nodes {{ name }} }}
        assignees(first: 10) {{ nodes {{ {_ACTOR} }} }}
        milestone {{ title }}
        comments(last: 50) {{ totalCount nodes {{ body createdAt author {{ {_ACTOR} }} }} }}"""
_PULL = f"""{_COMMON}
        isDraft merged mergedAt baseRefName headRefName additions deletions changedFiles
        mergedBy {{ {_ACTOR} }}
        reviewRequests(first: 10) {{ nodes {{ requestedReviewer {{ ... on User {{ login name }} }} }} }}
        reviews(last: 30) {{ totalCount nodes {{ state body submittedAt author {{ {_ACTOR} }}
          comments(last: 20) {{ totalCount nodes {{ path body createdAt author {{ {_ACTOR} }} }} }} }} }}
        closingIssuesReferences(first: 10) {{ nodes {{ number repository {{ nameWithOwner }} }} }}"""
_ISSUE = f"{_COMMON}\n        stateReason"
_ORDER = "orderBy: {field: UPDATED_AT, direction: DESC}"
_PAGE = "pageInfo { hasNextPage endCursor }"
PULLS = f"""query($owner: String!, $name: String!, $first: Int!, $after: String) {{
  repository(owner: $owner, name: $name) {{ nameWithOwner
    pullRequests(first: $first, after: $after, {_ORDER}) {{ {_PAGE} nodes {{ {_PULL} }} }}
  }}
}}"""
ISSUES = f"""query($owner: String!, $name: String!, $first: Int!, $after: String, $since: DateTime!) {{
  repository(owner: $owner, name: $name) {{ nameWithOwner
    issues(first: $first, after: $after, {_ORDER}, filterBy: {{since: $since}}) {{ {_PAGE} nodes {{ {_ISSUE} }} }}
  }}
}}"""
ITEM = f"""query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{ nameWithOwner
    issueOrPullRequest(number: $number) {{ __typename
      ... on Issue {{ {_ISSUE} }}
      ... on PullRequest {{ {_PULL} }}
    }}
  }}
}}"""
SCAN_PULLS = f"""query($owner: String!, $name: String!, $first: Int!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(first: $first, after: $after, {_ORDER}) {{ {_PAGE} nodes {{ number updatedAt }} }}
  }}
}}"""
SCAN_ISSUES = f"""query($owner: String!, $name: String!, $first: Int!, $after: String, $since: DateTime!) {{
  repository(owner: $owner, name: $name) {{
    issues(first: $first, after: $after, {_ORDER}, filterBy: {{since: $since}}) {{ {_PAGE} nodes {{ number updatedAt }} }}
  }}
}}"""

# What a question reads: a repository's description, and never an item's body or comments.
REPOSITORY = """query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { nameWithOwner description isArchived pushedAt
    defaultBranchRef { name }
    repositoryTopics(first: 20) { nodes { topic { name } } }
    issues(states: OPEN) { totalCount }
    pullRequests(states: OPEN) { totalCount }
  }
}"""
_LISTED = "number title state createdAt updatedAt closedAt author { __typename login } labels(first: 20) { nodes { name } }"
LIST_PULLS = f"""query($owner: String!, $name: String!, $first: Int!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(first: $first, after: $after, {_ORDER}) {{ {_PAGE} nodes {{ {_LISTED} isDraft mergedAt }} }}
  }}
}}"""
LIST_ISSUES = f"""query($owner: String!, $name: String!, $first: Int!, $after: String, $since: DateTime!) {{
  repository(owner: $owner, name: $name) {{
    issues(first: $first, after: $after, {_ORDER}, filterBy: {{since: $since}}) {{ {_PAGE} nodes {{ {_LISTED} stateReason }} }}
  }}
}}"""


def _no_since(query: str) -> str:
    """A query that pages a repository's issues from no time at all, for a window of `all`."""
    return query.replace(", $since: DateTime!", "").replace(", filterBy: {since: $since}", "")


PICK_REPOS = 500  # the most repositories a person is offered to pick from: five pages of 100


class GithubError(CerebroError):
    """Something GitHub answered, or did not, that stops a read."""


class TokenRefused(GithubError):
    """GitHub refused the token."""


class RateLimited(GithubError):
    """A rate limit that does not lift soon: the listing stops, and goes on from its last cursor."""


class AnswerError(GithubError):
    """An answer that is not a success. `status` is its HTTP status, or the one a GraphQL error stands for."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class Unreachable(GithubError):
    """GitHub did not answer at all."""


class ReadOnly(GithubError):
    """Something tried to send GitHub a request that is not a read."""


class CursorError(GithubError, SharedCursorError):
    """A cursor this connector did not give, or one that does not read."""


def parse_id(id: str) -> tuple[str, int | None, str | None]:
    """A record's repository and its number or its tag. Raises ValueError for anything that is not a record id."""
    match = _ID.match(id or "")
    if match is None:
        raise ValueError(f"{id!r} is not a github record id, such as owner/name#12 or owner/name@v1.2.0")
    return match.group(1), int(match.group(2)) if match.group(2) else None, match.group(3)


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rate_wait(headers: httpx.Headers, now: datetime) -> float | None:
    """How long to wait out an answer that is a rate limit, primary or secondary, or None when it is not one."""
    retry = retry_after(headers.get("retry-after"), now)
    if retry is not None:
        return retry
    if headers.get("x-ratelimit-remaining") == "0":
        return _reset_wait(headers, now)
    return None


def _reset_wait(headers: httpx.Headers, now: datetime) -> float:
    """The wait until `x-ratelimit-reset`, at most GitHub's hour-long window, or a minute when it is no time."""
    reset = headers.get("x-ratelimit-reset", "")
    return min(max(int(reset) - now.timestamp(), 0) + 1, RESET_WINDOW) if reset.isdigit() else 60.0


def _graphql_wait(headers: httpx.Headers, now: datetime) -> float:
    """How long to wait out a GraphQL `RATE_LIMITED` answer: what its headers say, or a minute."""
    wait = _rate_wait(headers, now)
    if wait is not None:
        return wait
    return _reset_wait(headers, now)


def _message(response: httpx.Response) -> str:
    try:
        text = str(response.json().get("message") or "")
    except (ValueError, AttributeError):
        return ""
    return clip_line(text, 200)


class _Api:
    """GitHub's API with one token: GET to REST and queries to GraphQL, each retried as GitHub asks."""

    def __init__(self, name: str, client: httpx.Client, token: Callable[[], str], base: str,
                 sleep: Callable[[float], None], now: Callable[[], datetime]):
        self.name, self.client, self.token, self.base = name, client, token, base.rstrip("/")
        self.sleep, self.now = sleep, now

    def _send(self, method: str, url: str, **options: Any) -> httpx.Response:
        if not url.startswith(self.base + "/"):
            raise ReadOnly(f"{self.name}: refusing to send the token to {url}: the token goes to {self.base} alone")
        headers = {"Authorization": f"Bearer {self.token()}", "User-Agent": "cerebro",
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            return self.client.request(method, url, headers=headers, follow_redirects=True, **options)
        except httpx.TransportError as error:
            raise Unreachable(f"{self.name}: github did not answer at {self.base} ({type(error).__name__}): check "
                              f"the network, then list again") from None

    def _wait(self, wait: float, waits: int) -> int:
        if wait > MAX_WAIT:
            until = self.now().astimezone(UTC) + timedelta(seconds=wait)
            raise RateLimited(f"{self.name}: github rate limits the token until {until:%H:%M} utc: list again from "
                              f"the last cursor after then")
        if waits >= LIMIT_WAITS:
            raise RateLimited(f"{self.name}: github still rate limits the token after two waits: list again from "
                              f"the last cursor later")
        self.sleep(wait)
        return waits + 1

    def _refused(self) -> TokenRefused:
        return TokenRefused(f"sources/{self.name}.yaml: github refused the token: connect github again with a token "
                            f"that can read its repositories")

    def _failed(self, status: int, message: str, what: str) -> AnswerError:
        detail = f": {message}" if message else ""
        return AnswerError(f"{self.name}: github answered {status} to {what}{detail}: list again later", status)

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """One query's data. Raises `ReadOnly` for anything but a query, before a byte is sent."""
        if not query.lstrip().startswith("query") or "mutation" in query:
            raise ReadOnly(f"{self.name}: the github connector only reads, and sends queries alone")
        server = limits = 0
        while True:
            response = self._send("POST", self.base + "/graphql", json={"query": query, "variables": variables})
            status = response.status_code
            if status == 401:
                raise self._refused()
            if status in (403, 429):
                wait = _rate_wait(response.headers, self.now())
                if wait is None:
                    raise self._failed(status, _message(response), "a query")
                limits = self._wait(wait, limits)
                continue
            if status >= 500 and server < SERVER_RETRIES:
                server += 1
                self.sleep(2.0 * server)
                continue
            if status != 200:
                raise self._failed(status, _message(response), "a query")
            try:
                reply = response.json()
                if not isinstance(reply, dict):
                    raise TypeError
            except (ValueError, TypeError):
                # GitHub cuts a heavy page short when it runs out of time, with a 200: asked again as a 5xx is.
                if server < SERVER_RETRIES:
                    server += 1
                    self.sleep(2.0 * server)
                    continue
                raise self._failed(status, "an answer that does not read", "a query") from None
            errors = reply.get("errors") or []
            if errors:
                error = errors[0] if isinstance(errors[0], dict) else {}
                if error.get("type") == "RATE_LIMITED":
                    limits = self._wait(_graphql_wait(response.headers, self.now()), limits)
                    continue
                code = {"NOT_FOUND": 404, "FORBIDDEN": 403}.get(str(error.get("type")), 400)
                raise self._failed(code, clip_line(error.get("message") or "", 200), "a query")
            return reply.get("data") or {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str]:
        """One REST answer, and the next page's url when it links one."""
        url = path if path.startswith("http") else self.base + path
        server = limits = 0
        while True:
            response = self._send("GET", url, params=params)
            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError:
                    raise self._failed(status, "an answer that does not read", f"GET {path}") from None
                return data, _next_link(response.headers.get("link", ""))
            if status == 401:
                raise self._refused()
            if status in (403, 429):
                wait = _rate_wait(response.headers, self.now())
                if wait is not None:
                    limits = self._wait(wait, limits)
                    continue
            elif status >= 500 and server < SERVER_RETRIES:
                server += 1
                self.sleep(2.0 * server)
                continue
            raise self._failed(status, _message(response), f"GET {path}")


def _settings(source: SourceFile) -> tuple[list[str], bool]:
    label = f"sources/{source.name}.yaml"
    raw = source.settings.get("repos")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise SourceError(f"{label} repos: write a list of owner/name, as repos: [octo-org/api]")
    repos: list[str] = []
    for i, value in enumerate(raw):
        if not isinstance(value, str) or not _REPO.match(value.strip()):
            raise SourceError(f"{label} repos[{i}] {value!r}: write owner/name, as repos: [octo-org/api]")
        if value.strip().lower() not in (r.lower() for r in repos):
            repos.append(value.strip())
    bots = source.settings.get("bots") or "exclude"
    if bots not in ("include", "exclude"):
        raise SourceError(f"{label} bots {bots}: write include or exclude")
    return repos, bots == "include"


def _bot(actor: dict[str, Any] | None) -> bool:
    login = str((actor or {}).get("login") or "")
    return (actor or {}).get("__typename") == "Bot" or login.endswith("[bot]")


# What connect says and asks.
CONNECTING = Connecting(
    says=("Make a fine-grained personal access token at https://github.com/settings/personal-access-tokens/new",
          ("with read-only access to the repositories the wiki should read: Contents, Issues, Pull requests and "
          "Metadata."),
          "Or store one first with: cerebro secrets set {secret}"),
    asks=(Prompt("GitHub token", "GitHub token, or Enter to connect later"),),
    later="GitHub is not connected yet. Run cerebro connect {name} when you have a token.",
    later_check="GitHub is not connected yet: run cerebro connect {name} when you have a token",
    asks_again=False,  # a refused kept token fails and names --replace
    title="GitHub",
    needs="a GitHub token",
    connected="Connected to GitHub as {who}. The token is kept under {secret} in {where}.",
    profile=Profile("github_login"),  # set with no line said
    kept="Connected to GitHub as {who}. The token is kept under {secret} in {where}.")


class GithubConnector(Connector):
    """A `kind: github` source. `client` sends every request and `token` gives the token each time one is sent.
    `now` is the clock the window is read by, `zone` the zone whole days are counted in, and `sleep` waits out a
    rate limit. `page_size` sets how many records a GitHub page holds, for tests."""

    connecting = CONNECTING
    IDENTITY_KEYS: ClassVar[Mapping[str, int]] = {KEY: 0}

    @staticmethod
    def find_you(source: SourceFile, value: str, *, who: str = "", **_: Any) -> tuple[str, str] | None:
        """Who the token is, for profile.md's `github_login`: the login its check read."""
        login = who.removeprefix("@")
        return (login, login) if login else None

    @classmethod
    def login_name(cls, source: SourceFile) -> str | None:
        """Every source of the kind keeps one login, under the kind's name."""
        return "github"

    @classmethod
    def needs(cls, source: SourceFile, item: str) -> Need | None:
        return Need("token", item, "the github token")

    @classmethod
    def prove_key(cls, source: SourceFile, value: str, *, http: httpx.Client, now: Callable[[], datetime],
                  **_: Any) -> str:
        """Who the token is, as `@<login>`, from one read-only call, or "" when GitHub names no one."""
        try:
            login = cls(dataclasses.replace(source, settings={}), client=http, token=lambda: value, now=now).whoami()
        except TokenRefused:
            raise KeyRefused("github refused the token", "the token was refused") from None
        except GithubError as error:
            raise KeyUnproven(str(error)) from None
        return f"@{login}" if login else ""

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> GithubConnector:
        return cls(source, client=app.http, token=app.login(source), now=app.clock, zone=app.zone)

    def __init__(self, source: SourceFile, *, client: httpx.Client, token: Callable[[], str], base: str = API,
                 now: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
                 zone: tzinfo = UTC, page_size: int | None = None):
        if source.kind != "github":
            raise SourceError(f"sources/{source.name}.yaml is kind {source.kind}: a github connector reads kind "
                              f"github alone")
        self.name = source.name
        self.source = source
        self.repos, self.bots = _settings(source)
        self.behind = source.behind()
        self.now = now or system_clock
        self.zone = zone
        self.page_size = max(1, page_size) if page_size else None
        self.notices: list[str] = []
        self._api = _Api(source.name, client, token, base, sleep, self.now)
        config = json.dumps([asdict(source.window), source.extract, "", self.bots], sort_keys=True)  # "": into, never set
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]

    # -- what is read
    def _read_set(self, repos: Iterable[str] | None, notices: list[str]) -> list[str]:
        if repos is None:
            return list(self.repos)
        asked = {r.strip().lower() for r in repos}
        listed = {r.lower() for r in self.repos}
        for value in sorted(asked - listed):
            notices.append(f"{value} binds nothing: sources/{self.name}.yaml does not list it under repos:")
        return [r for r in self.repos if r.lower() in asked]

    def _floor(self) -> datetime | None:
        if self.behind is None:
            return None
        now = self.now()
        if self.behind % timedelta(days=1) == timedelta(0):
            here = now.astimezone(self.zone)
            return (here - self.behind).astimezone(UTC)  # calendar days in the zone, as wall time
        return now - self.behind

    def _page(self, default: int) -> int:
        return self.page_size or default

    # -- records
    def _record(self, repo: str, number: int | None, tag: str | None, revision: str, url: str,
                fields: dict[str, Any], links: list[str]) -> Record:
        id = f"{repo}#{number}" if tag is None else f"{repo}@{tag}"
        return Record(id=id, source=self.name, revision=revision, at=revision, url=url, keys={KEY: [repo]},
                      links=links, fields={**fields, "repo": repo}, extract=self.source.extract or "facts",
                      into="")

    def _item(self, repo: str, current: str, node: dict[str, Any], pull: bool) -> Record:
        state = str(node.get("state") or "").lower()
        fields: dict[str, Any] = {"kind": "pull_request" if pull else "issue", "number": node["number"],
                                  "state": "closed" if state == "merged" else state}
        author = node.get("author") or {}
        if author.get("login") and not _bot(author):
            fields["author"] = author["login"]
        if current and current.lower() != repo.lower():
            fields["name_with_owner"] = current
        links = []
        for ref in (node.get("closingIssuesReferences") or {}).get("nodes") or []:
            named = str((ref.get("repository") or {}).get("nameWithOwner") or "")
            target = repo if current and named.lower() == current.lower() else next(
                (r for r in self.repos if r.lower() == named.lower()), None)
            if target is not None and ref.get("number"):
                links.append(f"{target}#{ref['number']}")
        return self._record(repo, node["number"], None, _stamp(moment(node["updatedAt"])), str(node.get("url") or ""),
                            fields, links)

    def _release(self, repo: str, release: dict[str, Any]) -> Record:
        tag = str(release["tag_name"])
        return self._record(repo, None, tag, _stamp(moment(release["published_at"])),
                            str(release.get("html_url") or ""), {"kind": "release", "tag": tag}, [])

    def _people(self) -> People:
        def lookup(login: str) -> dict[str, Any] | None:
            try:
                got, _ = self._api.get("/users/" + quote(login, safe=""))
            except GithubError:
                return None
            return got if isinstance(got, dict) else None
        return People(lookup)

    # -- the cursor
    def _encode(self, state: dict[str, Any]) -> str:
        text = json.dumps(state, sort_keys=True, separators=(",", ":"))
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(text.encode())).decode()

    def _decode(self, cursor: str) -> dict[str, Any]:
        try:
            if not cursor.startswith(_CURSOR):
                raise ValueError
            state = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
            if not isinstance(state, dict) or not isinstance(state.get("repos"), dict):
                raise TypeError
        except (ValueError, TypeError, zlib.error, binascii.Error):
            raise CursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with no "
                              f"cursor to start over") from None
        return state

    @staticmethod
    def _seen(state: dict[str, Any], repo: str) -> dict[str, Any]:
        return state["repos"].setdefault(repo, {"from": None, "pulls": {}, "issues": {}, "releases": {}})

    # -- one pass
    def _begin(self, state: dict[str, Any], read: list[str]) -> None:
        anew = state.get("config") != self._config
        state["config"] = self._config
        for repo in sorted(state["repos"]):
            if repo not in read:  # out of scope, not deleted: it is forgotten, and its records are not removed
                state["repos"].pop(repo)
        queue = []
        for repo in read:
            queue += [[repo, "pulls", None], [repo, "issues", None], [repo, "releases", None]]
            if repo in state["repos"]:
                queue += [[repo, "scan-pulls", None], [repo, "scan-issues", None]]
        ask = state.pop("ask", "")  # as `cursor_for` set it: "changes", "full", or "" for neither
        if ask == "changes":  # a listing of changes proves nothing by absence, so nothing is scanned
            queue = [unit for unit in queue if not unit[1].startswith("scan-")]
        state["pass"] = {"started": _stamp(self.now()), "anew": anew, "read": read, "queue": queue, "skipped": [],
                         "ask": ask}

    def _from(self, state: dict[str, Any], repo: str) -> datetime | None:
        """From when a pass asks a repository for what changed: the window's start on a first read, a listing in
        full, or one with no record seen yet; for a listing set for changes, the latest time GitHub gave for the
        repository's records less `OVERLAP`; for one set by neither, when the last pass began."""
        floor = self._floor()
        ask = state["pass"].get("ask", "")
        last = state["repos"].get(repo, {}).get("latest" if ask == "changes" else "from")
        if state["pass"]["anew"] or last is None or ask == "full":
            return floor
        since = moment(last) - OVERLAP if ask == "changes" else moment(last)
        return max(since, floor) if floor is not None else since

    def _emit(self, state: dict[str, Any], record: Record, kind: str, key: str, out: list[Record]) -> None:
        repo = self._seen(state, record.fields["repo"])
        if record.revision > (repo.get("latest") or ""):  # GitHub's own time, as UTC text that sorts as time
            repo["latest"] = record.revision
        seen = repo[kind]
        if state["pass"]["anew"] or seen.get(key) != record.revision:
            out.append(record)
        seen[key] = record.revision

    def _whole(self, state: dict[str, Any], unit: list[Any], out: list[Record]) -> None:
        """One page of a repository's pull requests or issues changed since its last read, whole."""
        repo, kind, after = unit
        pull = kind == "pulls"
        since = self._from(state, repo)
        owner, name = repo.split("/")
        variables: dict[str, Any] = {"owner": owner, "name": name, "first": self._page(PULLS_PAGE if pull else
                                                                                       ISSUES_PAGE)}
        if after:
            variables["after"] = after
        query = PULLS if pull else ISSUES if since is not None else _no_since(ISSUES)
        if not pull and since is not None:
            variables["since"] = _stamp(since)
        node = self._api.graphql(query, variables).get("repository")
        if node is None:
            raise AnswerError(f"{self.name}: github has no repository {repo}", 404)
        page = node.get("pullRequests" if pull else "issues") or {}
        stopped = False
        for item in page.get("nodes") or []:
            if since is not None and moment(item["updatedAt"]) < since:
                stopped = True
                break
            if _bot(item.get("author")) and not self.bots:
                continue
            record = self._item(repo, str(node.get("nameWithOwner") or ""), item, pull)
            self._emit(state, record, kind, str(item["number"]), out)
        info = page.get("pageInfo") or {}
        unit[2] = info.get("endCursor") if info.get("hasNextPage") and info.get("endCursor") and not stopped else None

    def _releases(self, state: dict[str, Any], repo: str, out: list[Record], removed: list[str]) -> None:
        """Every release in the window, newest first, of which those changed are listed and those gone removed. A
        listing set for changes reads only those published since it asks from, and removes none."""
        changes = state["pass"].get("ask") == "changes"
        floor = self._from(state, repo) if changes else self._floor()
        present: set[str] = set()
        url, params = f"/repos/{repo}/releases", {"per_page": self._page(RELEASES_PAGE)}
        while url:
            page, url = self._api.get(url, params)
            params = None
            for release in page if isinstance(page, list) else []:
                if release.get("draft") or not release.get("published_at"):
                    continue
                if floor is not None and moment(release["published_at"]) < floor:
                    url = ""
                    break
                present.add(str(release["tag_name"]))
                self._emit(state, self._release(repo, release), "releases", str(release["tag_name"]), out)
        if not changes:
            self._gone(state, repo, "releases", present, removed)

    def _scan(self, state: dict[str, Any], repo: str, kind: str, removed: list[str]) -> None:
        """The numbers in the window, a hundred a request, so what github no longer holds is removed."""
        floor = self._floor()
        pull = kind == "pulls"
        owner, name = repo.split("/")
        present: set[str] = set()
        after = None
        while True:
            variables: dict[str, Any] = {"owner": owner, "name": name, "first": self._page(SCAN_PAGE)}
            if after:
                variables["after"] = after
            query = SCAN_PULLS if pull else SCAN_ISSUES if floor is not None else _no_since(SCAN_ISSUES)
            if not pull and floor is not None:
                variables["since"] = _stamp(floor)
            node = self._api.graphql(query, variables).get("repository")
            if node is None:
                raise AnswerError(f"{self.name}: github has no repository {repo}", 404)
            page = node.get("pullRequests" if pull else "issues") or {}
            stopped = False
            for item in page.get("nodes") or []:
                if floor is not None and moment(item["updatedAt"]) < floor:
                    stopped = True
                    break
                present.add(str(item["number"]))
            info = page.get("pageInfo") or {}
            if stopped or not info.get("hasNextPage") or not info.get("endCursor"):
                break
            after = info["endCursor"]
        self._gone(state, repo, kind, present, removed)

    def _gone(self, state: dict[str, Any], repo: str, kind: str, present: set[str], removed: list[str]) -> None:
        seen = state["repos"].get(repo, {}).get(kind, {})
        absent = {(f"{repo}@{key}" if kind == "releases" else f"{repo}#{key}"): key for key in seen
                  if key not in present}
        removed += gone(absent, holds(self.get))  # one that only left the window still exists
        for key in absent.values():
            del seen[key]

    def _step(self, state: dict[str, Any], out: list[Record], removed: list[str], notices: list[str]) -> None:
        queue = state["pass"]["queue"]
        unit = queue[0]
        repo, kind = unit[0], unit[1]
        try:
            if kind in ("pulls", "issues"):
                self._whole(state, unit, out)
                if unit[2] is not None:
                    return
            elif kind == "releases":
                self._releases(state, repo, out, removed)
            else:
                self._scan(state, repo, kind.removeprefix("scan-"), removed)
        except AnswerError as error:
            if error.status not in (403, 404):
                raise
            notices.append(f"{self.name}: the token cannot read {repo} ({error.status}), so it is skipped: give the "
                           f"token access to it, or take it out of sources/{self.name}.yaml")
            state["pass"]["queue"] = [u for u in queue if u[0] != repo]
            state["pass"]["skipped"].append(repo)
            return
        queue.pop(0)

    def _finish(self, state: dict[str, Any]) -> None:
        run = state.pop("pass")
        for repo in run["read"]:
            if repo not in run["skipped"]:
                self._seen(state, repo)["from"] = run["started"]
        state["pass"] = None

    picks = ("repos", "repository", "repositories")

    def offer(self, ask: Ask | None = None) -> Offer:
        """The repositories the token can read, the most recently pushed first, up to `PICK_REPOS`, each with when it
        was last pushed and whether it is private or archived: what `repos:` may list, by owner/name. Read only."""
        rows: list[Choice] = []
        more = False
        page, following = self._api.get("/user/repos", {"sort": "pushed", "direction": "desc", "per_page": 100})
        while True:
            for repo in page if isinstance(page, list) else []:
                name = str(repo.get("full_name") or "") if isinstance(repo, dict) else ""
                if not name:
                    continue
                if len(rows) == PICK_REPOS:
                    more = True
                    break
                pushed = str(repo.get("pushed_at") or "")[:10]
                detail = f"pushed {pushed}" if pushed else "never pushed"
                detail = ("archived, " if repo.get("archived") else "") + ("private, " if repo.get("private") else "") \
                    + detail
                if about := one_line(repo.get("description") or "", 300):
                    detail += f": {about}"
                rows.append(Choice(name, name, detail))
            if more or not following:
                break
            page, following = self._api.get(following)
        note = ((f"The token reads more than the {PICK_REPOS} repositories pushed to most recently shown here: list "
                 "others under repos: by owner/name."),) if more else ()
        return Offer(*self.picks, tuple(rows), tuple(self.repos), note, head="GitHub repositories to read")

    def whoami(self) -> str:
        """The login the token belongs to, as GitHub says it, from one read-only call. Raises `TokenRefused` when
        GitHub refuses the token, and `GithubError` when it cannot be asked."""
        me, _ = self._api.get("/user")
        login = me.get("login") if isinstance(me, dict) else ""
        return login if isinstance(login, str) else ""

    lists_changes = True

    def cursor_for(self, cursor: str | None, *, full: bool) -> str | None:
        """The cursor set so the next pass reads each repository's window and scans it for what GitHub deleted,
        when `full`, or asks only for what changed since the latest time GitHub gave, less `OVERLAP`, and scans
        nothing, when not. A pass under way goes on as it began, and a first listing reads the window anyway."""
        if cursor is None:
            return None
        state = self._decode(cursor)
        if not state.get("pass"):
            state["ask"] = "full" if full else "changes"
        return self._encode(state)

    # -- the contract
    def list(self, since: str | None = None, *, repos: Iterable[str] | None = None) -> Listing:
        """What changed since the cursor `since` in the repositories `repos`, every one the file lists by default.
        A page is one page from GitHub. A pass in progress keeps the repositories it began with."""
        notices: list[str] = []
        if not self.repos:
            notices.append(f"sources/{self.name}.yaml lists no repos, so nothing is read: add them as "
                           f"repos: [owner/name]")
        state = self._decode(since) if since is not None else {"config": self._config, "repos": {}, "pass": None}
        records: list[Record] = []
        removed: list[str] = []
        if not state.get("pass"):
            self._begin(state, self._read_set(repos, notices))
        while state["pass"]["queue"] and not records:
            self._step(state, records, removed, notices)
        more = bool(state["pass"]["queue"])
        if not more:
            self._finish(state)
        self.notices = notices
        return Listing(records, self._encode(state), removed, more, list(notices))

    # -- a curator's questions
    questions = QUESTIONS

    def _say(self, text: str) -> str:
        return f"github source {self.name}: {text}"

    def _asked(self, question: str, params: Mapping[str, Any] | None, limit: int) -> dict[str, Any]:
        """The params of a question this source answers, each read as its type, the repository as the file writes
        it, or `QuestionError` in one line. Nothing is sent to GitHub before they read."""
        example = self.repos[0] if self.repos else "owner/name"
        repo = Text(example, required=True, what="a repository the file lists")
        takes = {q: {n: repo if n == "repo" else kind for n, kind in t.items()} for q, t in TAKES.items()}
        read = asked(f"github source {self.name}", QUESTIONS, takes, question, params, limit)
        if "repo" in read:
            asked_for = read["repo"]
            listed = next((r for r in self.repos if r.lower() == asked_for.lower()), None)
            if listed is None:
                raise QuestionError(self._say(f"{one_line(asked_for, 60)} is not a repository sources/{self.name}.yaml "
                                              f"lists under repos: ask repositories for those it lists"))
            read["repo"] = listed
        return read

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """A curator's question, as `QUESTIONS` lists them, of the repositories the file lists alone: each a query
        to GraphQL or a GET to REST through the same API a listing reads, and never a body. A repository the token
        cannot read gives no row and a notice. A colleague's source gives no rows and asks GitHub nothing."""
        read = self._asked(question, params, limit)
        if self.source.owner not in ("", "me"):
            return Listing([], None, [], False, [self._say("is a colleague's github login, and only your own feeds "
                                                           "admission: ask the github source whose owner is me")])
        limit = min(limit, MOST[question])
        notices: list[str] = []
        if question == "repositories":
            if not self.repos:
                notices.append(f"sources/{self.name}.yaml lists no repos, so nothing is read: add them as "
                               f"repos: [owner/name]")
            rows = self._ask_repositories(limit + 1, notices)
        else:
            here = self.now().astimezone(self.zone)
            floor = (here - timedelta(days=read.get("days", ASK_DAYS[question]))).astimezone(UTC)
            try:
                if question == "releases":
                    rows = self._ask_releases(read["repo"], floor, limit + 1)
                else:
                    rows = self._ask_items(read["repo"], question == "pull_requests", floor, limit + 1)
            except AnswerError as error:
                if error.status not in (403, 404):
                    raise
                notices.append(self._unread(read["repo"], error.status))
                rows = []
        return Listing(rows[:limit], None, [], len(rows) > limit, notices)

    def _unread(self, repo: str, status: int) -> str:
        return (f"{self.name}: the token cannot read {repo} ({status}), so it gives no row: give the token access "
                f"to it, or take it out of sources/{self.name}.yaml")

    def _day(self, text: Any) -> str:
        return moment(text).astimezone(self.zone).date().isoformat() if isinstance(text, str) and text else ""

    def _row(self, id: str, name: str, repo: str, fields: dict[str, Any]) -> Record:
        return Record(id=id, source=self.name, revision="", keys={KEY: [repo]},
                      fields={"name": name, **{k: v for k, v in fields.items() if v not in ("", None)}})

    def _ask_repositories(self, most: int, notices: list[str]) -> list[Record]:
        rows = []
        for repo in self.repos[:most]:
            owner, name = repo.split("/")
            try:
                node = self._api.graphql(REPOSITORY, {"owner": owner, "name": name}).get("repository")
            except AnswerError as error:
                if error.status not in (403, 404):
                    raise
                notices.append(self._unread(repo, error.status))
                continue
            if not isinstance(node, dict):
                notices.append(self._unread(repo, 404))
                continue
            current = str(node.get("nameWithOwner") or "")
            topics = [str((n.get("topic") or {}).get("name") or "") for n in
                      (node.get("repositoryTopics") or {}).get("nodes") or [] if isinstance(n, dict)]
            rows.append(self._row(repo, repo, repo, {
                "name_with_owner": current if current and current != repo else "",
                "description": one_line(node.get("description") or "", DESCRIBED),
                "topics": [t for t in topics if t], "archived": node.get("isArchived") is True,
                "default_branch": str((node.get("defaultBranchRef") or {}).get("name") or ""),
                "pushed": self._day(node.get("pushedAt")),
                "open_issues": int((node.get("issues") or {}).get("totalCount") or 0),
                "open_pull_requests": int((node.get("pullRequests") or {}).get("totalCount") or 0)}))
        return rows

    def _ask_items(self, repo: str, pull: bool, floor: datetime, most: int) -> list[Record]:
        """The pull requests or the issues of `repo` changed since `floor`, the latest first, a bot's left out
        unless the file keeps bots, as a listing leaves them."""
        owner, name = repo.split("/")
        rows: list[Record] = []
        after = None
        while len(rows) < most:
            variables: dict[str, Any] = {"owner": owner, "name": name, "first": min(most, self._page(SCAN_PAGE))}
            if after:
                variables["after"] = after
            if not pull:
                variables["since"] = _stamp(floor)
            node = self._api.graphql(LIST_PULLS if pull else LIST_ISSUES, variables).get("repository")
            if node is None:
                raise AnswerError(f"{self.name}: github has no repository {repo}", 404)
            page = node.get("pullRequests" if pull else "issues") or {}
            older = False
            for item in page.get("nodes") or []:
                if moment(item["updatedAt"]) < floor:
                    older = True
                    break
                if _bot(item.get("author")) and not self.bots:
                    continue
                author = item.get("author") or {}
                state = str(item.get("state") or "").lower()
                rows.append(self._row(f"{repo}#{item['number']}", str(item.get("title") or ""), repo, {
                    "repo": repo, "number": item["number"], "state": state,
                    "draft": item.get("isDraft") is True if pull else None,
                    "state_reason": str(item.get("stateReason") or "").lower(),
                    "labels": [str(n.get("name")) for n in (item.get("labels") or {}).get("nodes") or []
                               if isinstance(n, dict) and n.get("name")],
                    "author": str(author.get("login") or ""), "created": self._day(item.get("createdAt")),
                    "updated": self._day(item.get("updatedAt")), "merged": self._day(item.get("mergedAt")),
                    "closed": self._day(item.get("closedAt"))}))
                if len(rows) >= most:
                    break
            info = page.get("pageInfo") or {}
            if older or not info.get("hasNextPage") or not info.get("endCursor"):
                break
            after = info["endCursor"]
        return rows

    def _ask_releases(self, repo: str, floor: datetime, most: int) -> list[Record]:
        """The releases of `repo` published since `floor`, the latest first, through the REST read a listing
        makes. A draft is not published, so it is left out, as a listing leaves it."""
        rows: list[Record] = []
        url, params = f"/repos/{repo}/releases", {"per_page": self._page(RELEASES_PAGE)}
        while url and len(rows) < most:
            page, url = self._api.get(url, params)
            params = None
            for release in page if isinstance(page, list) else []:
                if not isinstance(release, dict) or release.get("draft") or not release.get("published_at"):
                    continue
                if moment(release["published_at"]) < floor:
                    url = ""
                    break
                tag = str(release.get("tag_name") or "")
                rows.append(self._row(f"{repo}@{tag}", str(release.get("name") or "") or tag, repo, {
                    "repo": repo, "tag": tag, "prerelease": release.get("prerelease") is True,
                    "author": str((release.get("author") or {}).get("login") or ""),
                    "published": self._day(release["published_at"])}))
                if len(rows) >= most:
                    break
        return rows

    def get(self, id: str, *, repos: Iterable[str] | None = None) -> Record:
        """One issue, pull request or release whole, its text rendered for extraction. A record of a repository not
        read now, as `repos` says, is refused before GitHub is asked."""
        missing = RecordNotFound(f"{self.name} has no record {id}: list the source for the ids it holds")
        try:
            repo, number, tag = parse_id(id)
        except ValueError:
            raise missing from None
        if repo not in self._read_set(repos, []):
            raise missing
        people = self._people()
        if tag is not None:
            try:
                release, _ = self._api.get(f"/repos/{repo}/releases/tags/{quote(tag, safe='')}")
            except AnswerError as error:
                if error.status == 404:
                    raise missing from None
                raise
            if not isinstance(release, dict) or release.get("draft") or not release.get("published_at"):
                raise missing
            record = self._release(repo, release)
            record.body = render_release(repo, release, people=people)
            return record
        owner, name = repo.split("/")
        try:
            data = self._api.graphql(ITEM, {"owner": owner, "name": name, "number": number})
        except AnswerError as error:
            if error.status == 404:
                raise missing from None
            raise
        node = data.get("repository") or {}
        item = node.get("issueOrPullRequest")
        if not item or (_bot(item.get("author")) and not self.bots):
            raise missing
        pull = item.get("__typename") == "PullRequest"
        record = self._item(repo, str(node.get("nameWithOwner") or ""), item, pull)
        record.body = render_item(repo, item, pull=pull, people=people, bots=self.bots)
        return record
