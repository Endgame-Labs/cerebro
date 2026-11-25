"""How a GitHub record reads: the text `get` gives for extraction.

A record's text is what a reader would see. It opens with header lines, `Repository:` first, then the number and
title, the state (merged into which branch, when and by whom, or closed as completed or not planned), for a pull
request its branch, the size of its change and each reviewer's latest verdict with who is asked, then the author,
assignees, labels, milestone and dates. A header with nothing to say is left out. The body follows, with a
template's HTML comments taken out, then the discussion oldest first, each comment with its author and, for a review
comment, its file. People read as `Jane Doe (@jane)`, and a bot as `ci (bot)`. A bot's comment is left out unless
the source keeps bots. A long body or comment is cut and marked, and the newest comments are kept when the
discussion runs long, with a line that says how many were left out.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

from ..connector import guard, masked_first

__all__ = ["BODY_CAP", "COMMENT_CAP", "DISCUSSION_CAP", "People", "Person", "clean", "cut", "moment",
           "render_item", "render_release", "without_comments"]

BODY_CAP = 8000
DISCUSSION_CAP = 16000
COMMENT_CAP = 2500
_CUT = " [cut]"
_BLANK_RUN = re.compile(r"\n{3,}")
# The lines the renderer writes around a discussion: a body that holds one is marked so it cannot forge a comment.
OWN_LINES = re.compile(r"(Discussion, oldest first:|\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2} UTC, |"
                       r"\[[0-9]+ earlier comments left out\])")


def moment(text: str) -> datetime:
    """A time as GitHub writes one, such as `2026-10-03T15:00:00Z`."""
    when = datetime.fromisoformat(text)
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def without_comments(text: str) -> str:
    """Text without its HTML comments, in one pass. A comment never closed hides the rest, as a browser shows it."""
    out, at = [], 0
    while (start := text.find("<!--", at)) >= 0:
        out.append(text[at:start])
        end = text.find("-->", start + 4)
        at = len(text) if end < 0 else end + 3
    out.append(text[at:])
    return "".join(out)


def clean(text: str | None) -> str:
    """Text without what a reader never sees, such as a template's HTML comments, and no runs of blank lines."""
    text = (text or "").replace("\r\n", "\n")
    text = without_comments(text)
    return guard(_BLANK_RUN.sub("\n\n", text).strip(), OWN_LINES)


def cut(text: str, limit: int) -> str:
    """Text cut to `limit` bytes of UTF-8 and marked, or as it is when it fits."""
    text = masked_first(text)
    data = text.encode()
    if len(data) <= limit:
        return text
    return data[:limit - len(_CUT)].decode("utf-8", "ignore") + _CUT


@dataclass
class Person:
    """An account as a record names it. `bot` is an app's or a bot's account."""

    login: str
    name: str = ""
    bot: bool = False
    email: str = ""


class People:
    """The people one call names, each asked of GitHub at most once. `lookup` gives a user as the REST API does, or
    None when it cannot. A person seen with a name is never asked for."""

    def __init__(self, lookup: Callable[[str], dict[str, Any] | None]):
        self._lookup = lookup
        self._known: dict[str, Person] = {}

    def of(self, actor: dict[str, Any] | None) -> Person:
        """An author as GraphQL or REST gives one. A deleted account is GitHub's `ghost`."""
        if not actor:
            return Person("ghost")
        login = str(actor.get("login") or "")
        kind = actor.get("__typename") or actor.get("type") or ""
        person = Person(login, str(actor.get("name") or ""), kind == "Bot" or login.endswith("[bot]"))
        if not person.bot and login and "name" in actor:
            self._known.setdefault(login, person)
        return person

    def who(self, person: Person) -> str:
        """A person as a reader would name them: name and login, an email when public, or a bot's login marked."""
        if not person.login:
            return ""
        if person.bot:
            return person.login.removesuffix("[bot]") + " (bot)"
        full = self._known.get(person.login)
        if full is None:
            got = self._lookup(person.login) or {}
            full = Person(person.login, str(got.get("name") or ""), email=str(got.get("email") or ""))
            self._known[person.login] = full
        name = " ".join(re.sub(r"[<>()\"]", "", full.name).split())
        out = f"{name} (@{person.login})" if name else f"@{person.login}"
        return out + (f" <{full.email}>" if full.email else "")


class _Text:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def header(self, key: str, value: str) -> None:
        value = " ".join(value.split())
        if value:
            self.parts.append(f"{key}: {value}\n")

    def body(self, text: str | None, limit: int) -> None:
        self.parts.append("\n")
        text = clean(text)
        if text:
            self.parts.append(cut(text, limit) + "\n")

    def discussion(self, parts: list[str]) -> None:
        if parts:
            self.parts.append("\nDiscussion, oldest first:\n\n" + "\n\n".join(parts) + "\n")

    def __str__(self) -> str:
        return "".join(self.parts)


def _rfc1123(text: str) -> str:
    return format_datetime(moment(text))


def _day(text: str) -> str:
    return moment(text).astimezone(UTC).strftime("%Y-%m-%d")


def _nodes(value: Any) -> list[Any]:
    return list((value or {}).get("nodes") or [])


def _left_out(value: Any) -> int:
    value = value or {}
    return max(int(value.get("totalCount") or 0) - len(value.get("nodes") or []), 0)


def _start(repo: str) -> _Text:
    text = _Text()
    text.header("Repository", repo)
    return text


def _issue_state(node: dict[str, Any]) -> str:
    if str(node.get("state") or "").upper() == "OPEN":
        return "open"
    reason = str(node.get("stateReason") or "").upper()
    state = {"COMPLETED": "closed as completed", "NOT_PLANNED": "closed as not planned"}.get(reason, "closed")
    if node.get("closedAt"):
        state += " on " + _day(node["closedAt"])
    return state


def _pull_state(node: dict[str, Any], people: People) -> str:
    if node.get("merged") and node.get("mergedAt"):
        state = f"merged into {node.get('baseRefName') or ''} on {_day(node['mergedAt'])}"
        merger = node.get("mergedBy")
        if merger and merger.get("login"):
            state += " by " + people.who(people.of(merger))
        return state
    if str(node.get("state") or "").upper() in ("CLOSED", "MERGED"):
        return "closed without merging"
    return "open, draft" if node.get("isDraft") else "open"


def _verdict(state: str) -> str:
    return state.replace("_", " ").lower()


def _reviews(node: dict[str, Any], people: People, bots: bool) -> str:
    latest: dict[str, str] = {}
    order: list[Person] = []
    for review in _nodes(node.get("reviews")):
        person, state = people.of(review.get("author")), str(review.get("state") or "")
        if not person.login or state == "PENDING" or (person.bot and not bots):
            continue
        if state == "COMMENTED" and latest.get(person.login):
            continue
        if person.login not in latest:
            order.append(person)
        latest[person.login] = _verdict(state)
    parts = [f"{people.who(person)} {latest[person.login]}" for person in order]
    for request in _nodes(node.get("reviewRequests")):
        asked = request.get("requestedReviewer") or {}
        if asked.get("login") and asked["login"] not in latest:
            parts.append(people.who(people.of(asked)) + " asked to review")
    return "; ".join(parts)


@dataclass
class _Entry:
    at: datetime
    person: Person
    body: str
    note: str = ""


def _discussion(entries: list[_Entry], people: People, bots: bool) -> list[str]:
    """The comments and reviews oldest first, without bots unless kept, the newest kept within the cap."""
    entries = sorted(entries, key=lambda e: e.at)
    parts: list[str] = []
    used = 0
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        if entry.person.bot and not bots:
            continue
        text = clean(entry.body)
        if not text and not entry.note:
            continue
        head = f"[{entry.at.astimezone(UTC):%Y-%m-%d %H:%M} UTC, {people.who(entry.person)}"
        head += f", {entry.note}]" if entry.note else "]"
        part = head + ("\n" + cut(text, COMMENT_CAP) if text else "")
        size = len(part.encode())
        if used + size > DISCUSSION_CAP:
            parts.append(f"[{i + 1} earlier comments left out]")
            break
        used += size
        parts.append(part)
    parts.reverse()
    return parts


def render_item(repo: str, node: dict[str, Any], *, pull: bool, people: People, bots: bool) -> str:
    """An issue's or a pull request's text, from the node GraphQL gives, with `repo` as the source file writes it."""
    entries = [_Entry(moment(c["createdAt"]), people.of(c.get("author")), c.get("body") or "")
               for c in _nodes(node.get("comments"))]
    left_out = _left_out(node.get("comments"))
    text = _start(repo)
    number = f"#{node['number']} {node.get('title') or ''}"
    if not pull:
        text.header("Issue", number)
        text.header("State", _issue_state(node))
    else:
        text.header("Pull request", number)
        text.header("State", _pull_state(node, people))
        text.header("Branch", f"{node.get('headRefName') or ''} into {node.get('baseRefName') or ''}")
        text.header("Changes", f"{node.get('changedFiles') or 0} files, +{node.get('additions') or 0} "
                               f"-{node.get('deletions') or 0}")
        text.header("Reviews", _reviews(node, people, bots))
        left_out += _left_out(node.get("reviews"))
        for review in _nodes(node.get("reviews")):
            left_out += _left_out(review.get("comments"))
            state, body = str(review.get("state") or ""), review.get("body") or ""
            if state != "PENDING" and not (state == "COMMENTED" and not body.strip()):
                entries.append(_Entry(moment(review["submittedAt"]), people.of(review.get("author")), body,
                                      "review: " + _verdict(state)))
            entries += [_Entry(moment(c["createdAt"]), people.of(c.get("author")), c.get("body") or "",
                               f"on {c.get('path') or ''}") for c in _nodes(review.get("comments"))]
    text.header("Author", people.who(people.of(node.get("author"))))
    text.header("Assignees", ", ".join(people.who(people.of(a)) for a in _nodes(node.get("assignees"))))
    text.header("Labels", ", ".join(str(label.get("name") or "") for label in _nodes(node.get("labels"))))
    if node.get("milestone"):
        text.header("Milestone", str(node["milestone"].get("title") or ""))
    text.header("Opened", _rfc1123(node["createdAt"]))
    text.header("Date", _rfc1123(node["updatedAt"]))
    text.body(node.get("body"), BODY_CAP)
    parts = _discussion(entries, people, bots)
    if left_out > 0:
        parts.insert(0, f"[{left_out} older {'comment' if left_out == 1 else 'comments'} not in this record]")
    text.discussion(parts)
    return str(text)


def render_release(repo: str, release: dict[str, Any], *, people: People) -> str:
    """A release's text, from the release REST gives."""
    text = _start(repo)
    tag, name = str(release.get("tag_name") or ""), str(release.get("name") or "")
    text.header("Release", tag + (f" ({name})" if name and name != tag else ""))
    if release.get("prerelease"):
        text.header("State", "pre-release")
    text.header("Author", people.who(people.of(release.get("author"))))
    text.header("Date", _rfc1123(release["published_at"]))
    text.body(release.get("body"), BODY_CAP)
    return str(text)
