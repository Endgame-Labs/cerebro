"""Slack as a source, read only.

A message of a channel in scope is one record, and never an article: it lands nowhere and distills into facts. A
channel is in scope when the source file's `channels:` list names it, by its name, with or without `#`, its id, or a
pattern of names such as `cust-*`, when the token's user belongs to it, and, when the caller hands in what articles
bind, when an article binds it. Nothing of any other channel is asked for, no direct message is ever listed, and only
methods that read are sent: `READS` names them all, and any other is refused before it leaves.

A record's id is `<channel>:<ts>`, its revision a hash of the text as Slack holds it and the stamp of its last
edit, its time the message's own, and its url the message's address in Slack. Its keys are the channel, by id and
name, under `slack_channels`, and the sender's email under `email`, which is how a user resolves to a person. A
reply links to its thread's first message. Its text, given by `get` alone, starts with the channel, the sender and
the date, then the message as a reader sees it, with mentions by name.

A first pass reads each channel's window. A later pass reads each channel from seven days before the last pass,
and a thread when it has a reply from an hour before the last pass or is followed, and lists a message new since
or changed. A message the source no longer holds where a pass read again is removed. One that left the window,
and one of a channel no longer in scope, still exists: it is forgotten, not removed. A pass pages channel by
channel. A rate limit waits as long as Slack's
`Retry-After` says, and when one listing's waits would pass ten minutes the pass stops where it is: what it read is
listed, and the next listing goes on from there.

A curator may ask `channels` and `channels_named`: the listed
channels the token's user belongs to, each with its id and name as the keys an article binds them by, its topic and
purpose from the channel list, each one line, whether it is shared with another company and the outside domains of its
members when it is, the ones an article binds first. They read the channel list and the members of a shared channel,
never a message, and nothing of a channel the file does not list. Only your own source answers them: one whose `owner:`
names a colleague gives no rows, with a line that says so.

The connector keeps no copy of a message's text. The cursor carries what a pass needs: the time of the last pass,
the channels it read, a short hash of each message's revision from the last seven days, and the threads it follows.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import fnmatch
import hashlib
import json
import re
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

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
    address_keys,
    masked_first,
    one_line,
    retry_after,
)
from ..errors import CerebroError
from ..secrets import SecretsError
from ..source import SourceError, SourceFile
from .connecting import Connecting, KeyRefused, KeyUnproven, Need, Profile, Prompt
from .questions import Text, asked

__all__ = ["API", "READS", "SlackConnector", "SlackCursorError", "SlackError", "SlackLoginError",
           "SlackRateLimited", "channel_named", "plain_text"]

API = "https://slack.com/api/"
# Every method the connector may send. Each one reads.
READS = ("auth.test", "users.list", "users.info", "conversations.list", "conversations.history",
         "conversations.replies", "conversations.members")
# The questions a curator may ask the source with `ask`, each with the params it takes. Neither reads a message.
QUESTIONS = {
    "channels": "the channels the source file lists that you belong to, the ones an article binds first, then by name, "
                "each with its id and name as the slack_channels keys, its topic and purpose, whether it is shared "
                "with another company, "
                "the outside domains of its members when it is; params: none",
    "channels_named": "the listed channels you belong to whose name holds `name`, such as an account's name, with "
                      "the same fields; params: name (required)",
}
NAMED = Text("acme", required=True, what="a word of the channel's name")
TAKES = {"channels": {}, "channels_named": {"name": NAMED}}
MAX_MEMBERS = 1000  # the members of one shared channel an ask reads, at most
PAGE = 200
WAIT_BUDGET = 600.0
DEFAULT_WAIT = 60.0
SERVER_RETRIES = 2
LOOKBACK = timedelta(hours=1)
REACH = timedelta(days=7)
# The kinds of message that hold words someone wrote. Joins, leaves and the like hold none.
KEPT = ("", "bot_message", "thread_broadcast", "file_share", "me_message")
DEAD_TOKEN = ("invalid_auth", "token_revoked", "token_expired", "account_inactive", "not_authed")
SOURCE_KEYS = ("auth", "extract", "window", "channels", "bots", "owner", "freshness")
_ELSEWHERE = "a slack source reads Slack's Web API and takes no url or command, so the token is not sent anywhere else"
REFUSED_KEYS = {"url": f"{_ELSEWHERE}: delete url", "command": f"{_ELSEWHERE}: delete command",
                "into": "a slack message is a record and never an article: delete into"}
_CURSOR = "slack1."
_WORD = re.compile(r"^[a-z][a-z0-9_.:,]{0,80}$")
_ID = re.compile(r"^([A-Z0-9]{2,32}):([0-9]{1,12}\.[0-9]{6})$")


class SlackError(CerebroError):
    """Slack refused or failed a request. `code` is the error Slack answered with, or empty."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class SlackLoginError(SlackError):
    """Slack no longer takes the token, or the app waits on an admin's approval."""


class SlackRateLimited(SlackError):
    """Waiting out Slack's rate limits would take one listing past the waits it may spend."""


class SlackCursorError(SlackError, CursorError):
    """A cursor this connector did not give, or one that does not read."""


# -- the text a reader sees

_MARKUP = re.compile(r"<([^<>\n]*)>")
# The marks of emphasis, each around text that starts and ends with a character that is not a space, with no
# letter or digit outside them, so snake_case and 2*3*4 stay as written.
_EMPHASIS = [re.compile(r"(^|[^\w" + m + r"]|_)" + m + r"([^\s" + m + r"](?:[^" + m + r"\n]*[^\s" + m + r"])?)"
                        + m + r"(\Z|[^\w" + m + r"]|_)") for m in (r"\*", "~")]
_EMPHASIS.insert(1, re.compile(r"(^|[^\w])_([^\s_](?:[^_\n]*[^\s_])?)_(\Z|[^\w])"))
_ESCAPES = re.compile(r"&(lt|gt|amp);")


def _unescape(text: str) -> str:
    return _ESCAPES.sub(lambda m: {"lt": "<", "gt": ">", "amp": "&"}[m.group(1)], text)


def _token(token: str, name: Callable[[str], str], channel: Callable[[str], str]) -> str:
    body, _, label = token.partition("|")
    if body.startswith("@"):
        if found := name(body[1:]):
            return "@" + found
        return "@" + label.removeprefix("@") if label else "@someone"
    if body.startswith("#"):
        # A channel's own label is never trusted: the name of a channel out of scope is never kept.
        found = channel(body[1:])
        return "#" + found if found else "#a-channel"
    if body.startswith("!"):
        word = body[1:]
        if word in ("here", "channel", "everyone"):
            return "@" + word
        if word.startswith("subteam^"):
            return "@" + label.removeprefix("@") if label else "@a-group"
        return label
    if body.startswith("mailto:"):
        return label or body.removeprefix("mailto:")
    link, label = _unescape(body), _unescape(label)
    if not label or link in (label, "http://" + label, "https://" + label):
        return link
    return f"{label} ({link})"


def _prose(text: str, name: Callable[[str], str], channel: Callable[[str], str]) -> str:
    text = _MARKUP.sub(lambda m: _token(m.group(1), name, channel), text)
    for mark in _EMPHASIS:
        # Twice, since a match takes the character after it, which may open the next one.
        text = mark.sub(r"\1\2\3", mark.sub(r"\1\2\3", text))
    return _unescape(text)


def _tidy(text: str) -> str:
    return "\n".join(line.rstrip(" \t") for line in text.split("\n")).strip("\n")


def plain_text(raw: str, name: Callable[[str], str], channel: Callable[[str], str]) -> str:
    """A message's text from Slack's markup to the words a reader sees. A mention of a user becomes `@` and their
    name, a channel's `#` and its name when `channel` names it and `#a-channel` when not, a link its label and its
    address, bold, italic and strike marks go, and the escaped `&`, `<` and `>` come back. Code, inline or in a
    block, keeps every character as written. `name` and `channel` give a name by id, or "" for none."""
    raw = raw.replace("\r\n", "\n")
    out: list[str] = []
    while raw:
        fence, tick = raw.find("```"), raw.find("`")
        if fence >= 0 and fence <= tick:
            end = raw.find("```", fence + 3)
            if end < 0:
                out.append(_prose(raw, name, channel))
                break
            out += [_prose(raw[:fence], name, channel), "```" + _unescape(raw[fence + 3:end]) + "```"]
            raw = raw[end + 3:]
        elif tick >= 0:
            end = raw.find("`", tick + 1)
            if end < 0 or "\n" in raw[tick + 1:end]:
                out.append(_prose(raw[:tick + 1], name, channel))
                raw = raw[tick + 1:]
                continue
            out += [_prose(raw[:tick], name, channel), "`" + _unescape(raw[tick + 1:end]) + "`"]
            raw = raw[end + 1:]
        else:
            out.append(_prose(raw, name, channel))
            raw = ""
    return _tidy("".join(out))


# -- channels and the source file

def _entry(entry: str) -> str:
    return entry.strip().removeprefix("#")


def _pattern(entry: str) -> bool:
    return any(c in entry for c in "*?[")


def channel_named(entries: Iterable[str], id: str, name: str) -> bool:
    """Whether an entry of a `channels:` list names the channel: by its id, by its name with or without `#`, or
    as a pattern its name matches, in any case. A pattern never matches an id."""
    for entry in entries:
        e = _entry(entry).casefold()
        if not e:
            continue
        if e in (id.casefold(), name.casefold()):
            return True
        if _pattern(e) and name and fnmatch.fnmatchcase(name.casefold(), e):
            return True
    return False


def _reads_as_pattern(entry: str) -> bool:
    """Whether every `[` of a pattern closes, so it can match a name."""
    rest = entry
    while (start := rest.find("[")) >= 0:
        close = rest.find("]", start + 2)
        if close < 0:
            return False
        rest = rest[close + 1:]
    return True


def _reconnect(name: str) -> str:
    if name == "slack":
        return "cerebro connect slack"
    return "cerebro connect slack --name " + name.removeprefix("slack-")


def _check(source: SourceFile) -> tuple[list[str], bool]:
    """The channels list and whether bots are dropped, or a `SourceError` that names the key and the fix."""
    label = f"sources/{source.name}.yaml"
    if source.kind != "slack":
        raise SourceError(f"{label} is kind {source.kind}: a slack connector reads kind slack alone")
    channels = source.settings.get("channels") or []
    if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
        raise SourceError(f"{label}: channels: write a list of channel names, ids or patterns, as channels: "
                          f"[acme-deal, cust-*]")
    for i, entry in enumerate(channels):
        if _pattern(entry) and not _reads_as_pattern(_entry(entry)):
            raise SourceError(f"{label}: channels[{i}] {entry} is not a pattern of channel names: close each [ "
                              f"with ], as cust-[ab]*")
    bots = str(source.settings.get("bots") or "include")
    if bots not in ("include", "exclude"):
        raise SourceError(f"{label}: bots {bots}: write include or exclude")
    return channels, bots == "exclude"


# -- times

def _key(ts: str) -> tuple[int, int]:
    """A Slack timestamp as a pair that sorts as time does. An empty one sorts first."""
    if not ts:
        return (-1, 0)
    seconds, _, fraction = ts.partition(".")
    try:
        return (int(seconds), int((fraction + "000000")[:6]))
    except ValueError:
        return (-1, 0)


def _ts(when: datetime) -> str:
    return f"{int(when.timestamp())}.{when.microsecond:06d}"


def _time(ts: str) -> datetime:
    seconds, micro = _key(ts)
    return datetime.fromtimestamp(max(seconds, 0), UTC).replace(microsecond=micro)


def _stamp(ts: str) -> str:
    return _time(ts).isoformat().replace("+00:00", "Z")


def _retry_after(value: str | None, now: datetime) -> float:
    """The wait a `Retry-After` asks for, at least a second and at most ten minutes, or a minute when it reads as
    neither seconds nor a date."""
    wait = retry_after(value, now)
    return DEFAULT_WAIT if wait is None else max(wait, 1.0)


# -- what Slack gives

@dataclass
class _User:
    id: str
    name: str
    email: str
    bot: bool


@dataclass
class _Channel:
    id: str
    name: str
    updated: int
    shared: bool = False  # a Slack Connect channel, shared with another organization
    topic: str = ""  # the channel's topic and purpose, each as one line: names a person set, never a message
    purpose: str = ""


@dataclass(frozen=True)
class _Offered:
    """A channel the token's user belongs to, as a person picks among them, never a message: for a shared channel,
    the outside domains among its members."""

    id: str
    name: str
    topic: str
    purpose: str
    shared: bool
    members: int
    domains: tuple[str, ...] = ()


GROUPING_PROMPT = "slack-channel-grouping"
REST_GROUP = "the rest"
GROUPING_TEXT = 80  # the most bytes of a channel's topic, and of its purpose, the grouping call sends


def _cut(text: str, n: int) -> str:
    """text masked as `masked_first` masks it, then cut to at most n bytes, whole characters only."""
    return masked_first(text).encode()[:n].decode("utf-8", "ignore")


def _grouping_request(offered: list[_Offered], room: int) -> tuple[str, int]:
    """The grouping call's input, as JSON, fitted to `room` bytes, and how many channels it carries:
    every channel with its topic and purpose cut to GROUPING_TEXT bytes, else
    every channel with neither, else the most channels that fit, the largest by members first and then by name, with
    left_out counting the others. Raises ValueError when not one channel fits."""
    def build(chans: list[_Offered], text: bool, left: int) -> str:
        rows = []
        for o in chans:
            row: dict[str, Any] = {"name": o.name}
            if text and (topic := _cut(o.topic, GROUPING_TEXT)):
                row["topic"] = topic
            if text and (purpose := _cut(o.purpose, GROUPING_TEXT)):
                row["purpose"] = purpose
            row["shared"] = o.shared
            if o.domains:
                row["outside_domains"] = list(o.domains)
            if o.members:
                row["members"] = o.members
            rows.append(row)
        return json.dumps({"channels": rows, **({"left_out": left} if left else {})}, ensure_ascii=False,
                          separators=(",", ":"))  # compact, so the size measured is the size sent

    for text in (True, False):
        if len((whole := build(offered, text, 0)).encode()) <= room:
            return whole, len(offered)
    largest = sorted(offered, key=lambda o: (-o.members, o.name))

    def sent(n: int) -> str:
        return build(sorted(largest[:n], key=lambda o: o.name), False, len(largest) - n)

    low, high = 0, len(largest)  # the most n whose input fits, by binary search
    while low < high:
        mid = (low + high + 1) // 2
        low, high = (mid, high) if len(sent(mid).encode()) <= room else (low, mid - 1)
    if low <= 0:
        raise ValueError("the grouping prompt and your channels do not fit in one call to the extraction model")
    return sent(low), low


def _grouping(reply: str) -> list[tuple[str, list[tuple[str, bool]]]]:
    """The model's groups from its reply, each label with its rows, a pattern or a channel and whether to check it.
    A row with both is left out, a pattern that is no pattern is read as a channel's name, and a channel that is a
    pattern is left out. Raises ValueError for a reply that is not that JSON."""
    text = reply.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(text)
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise ValueError("its reply is not the JSON the prompt asks for")  # noqa: TRY004 - a reply that reads wrong
    out = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("rows"), list):
            continue
        label = " ".join(str(group.get("label") or "").split())[:60]
        label = "suggested" if not label or label == REST_GROUP else label
        picks = []
        for row in group["rows"]:
            if not isinstance(row, dict):
                continue
            pattern, channel = str(row.get("pattern") or ""), str(row.get("channel") or "")
            if (pattern and channel) or (channel and _pattern(_entry(channel))):
                continue
            if pattern or channel:
                picks.append((_entry(pattern or channel), row.get("checked") is True))
        out.append((label, picks))
    return out


@dataclass
class _Directory:
    """Who the token is, the workspace's users, and the channels the user belongs to. Never a message."""

    self: str
    team_url: str
    users: dict[str, _User | None] = field(default_factory=dict)
    channels: dict[str, _Channel] = field(default_factory=dict)


@dataclass
class _Message:
    channel: str
    ts: str
    thread: str
    user: str
    bot_id: str
    bot_name: str
    subtype: str
    text: str
    edited: str
    replies: int
    latest: str

    @classmethod
    def read(cls, channel: str, j: dict[str, Any]) -> _Message:
        bot_name = str(j.get("username") or (j.get("bot_profile") or {}).get("name") or "").strip()
        return cls(channel, str(j.get("ts") or ""), str(j.get("thread_ts") or ""), str(j.get("user") or ""),
                   str(j.get("bot_id") or ""), bot_name, str(j.get("subtype") or ""), str(j.get("text") or ""),
                   str((j.get("edited") or {}).get("ts") or ""), int(j.get("reply_count") or 0),
                   str(j.get("latest_reply") or ""))

    @property
    def id(self) -> str:
        return f"{self.channel}:{self.ts}"

    @property
    def reply(self) -> bool:
        return bool(self.thread) and self.thread != self.ts

    @property
    def revision(self) -> str:
        return "sha256:" + hashlib.sha256((self.text + "\x00" + self.edited).encode()).hexdigest()


def _about(j: dict[str, Any], key: str) -> str:
    """A channel's topic or purpose from the channel list, its `value` alone, as one line of at most 200
    characters."""
    about = j.get(key)
    return one_line(about.get("value") or "", 200) if isinstance(about, dict) else ""


def _user(j: dict[str, Any]) -> _User:
    profile = j.get("profile") or {}
    name = next((n.strip() for n in (profile.get("real_name"), j.get("real_name"), profile.get("display_name"),
                                     j.get("name")) if isinstance(n, str) and n.strip()), "")
    return _User(str(j.get("id") or ""), name, str(profile.get("email") or "").strip().lower(),
                 bool(j.get("is_bot")) or j.get("id") == "USLACKBOT")


def _safe(name: str) -> str:
    """A name that cannot read as more than a name in a header."""
    return " ".join(name.translate(str.maketrans({"<": "", ">": "", ",": " ", '"': ""})).split())


def _permalink(team_url: str, m: _Message) -> str:
    link = f"{team_url.rstrip('/')}/archives/{m.channel}/p{m.ts.replace('.', '', 1)}"
    return link + f"?thread_ts={m.thread}&cid={m.channel}" if m.reply else link


def _rev(revision: str) -> str:
    return revision.split(":")[-1][:16]


# The user token scopes the manifest asks for, and all the connector uses.
SCOPES = ("channels:read", "channels:history", "groups:read", "groups:history", "users:read", "users:read.email")
# The app manifest connect prints: a user token with the six read scopes, no bot, no
# direct messages and no token rotation.
MANIFEST = """display_information:
  name: Cerebro
oauth_config:
  scopes:
    user:
      - channels:read
      - channels:history
      - groups:read
      - groups:history
      - users:read
      - users:read.email
settings:
  token_rotation_enabled: false
"""
# What connect says and asks, in the drawn view and on a plain line.
# A refused kept token is asked for again (`asks_again`, left True).
CONNECTING = Connecting(
    says=(("Install a custom app on your Slack workspace using the app manifest below, and provide the OAuth user "
          "token issued by the app."), ""),
    block=MANIFEST,
    after=("",
           ('1. Go to `https://api.slack.com/apps` and click "`Create New App`". Select "`From manifest`", pick your '
           'workspace, then paste in the manifest shown above.'), "",
           ("2. From `Install App` in the app's settings, select `Install App` or `Request to Install` if admin "
           "approval is required."), "",
           ("3. From `OAuth & Permissions` in the app's settings, copy the `User OAuth Token` (starts with `xoxp-`), "
           "and paste it below."), "",
           ("If you are waiting for admin approval, you may return to complete this step anytime with `cerebro "
           "connect {name}`."), ""),
    plain=(("connect {name} reads Slack with your own user token, from an app you create in your workspace from this "
           "manifest:"), ""),
    plain_after=("",
                 "1. Create the app from this manifest: at https://api.slack.com/apps choose",
                 "   Create New App, then From a manifest, pick your workspace and paste it.",
                 "2. Install it to your workspace from Install App in the app's settings.",
                 "   Where the workspace requires app approval, Slack offers Request to",
                 "   Install instead, and the install waits on an admin.",
                 "3. Copy the User OAuth Token, which starts with xoxp-, from OAuth & Permissions",
                 "   in the app's settings, and paste it below.", ""),
    asks=(Prompt("OAuth token", "Slack user token (xoxp-...), or Enter to connect later"),),
    later="Slack is not connected yet. Run cerebro connect {name} when you have the token.",
    later_check="Slack is not connected yet: run cerebro connect {name} when you have the token",
    title="Slack",
    needs="an app you create from our manifest, and its user token",
    connected="Connected to Slack at {who}. The token is kept under {secret} in {where}.",
    kept=("Slack is connected at {who} already, and the token kept under {secret} in {where} stays. Pass --replace "
          "to replace it."),
    profile=Profile("slack_id", joins=True, added="Added your Slack user to profile.md as slack_id.",
                    already="Your Slack user is in profile.md as slack_id already.",
                    no_file="There is no profile.md to add your Slack user to.",
                    failed="warning: could not ask Slack who you are, so profile.md gets no slack_id: {error}. Run "
                           "cerebro connect {name} again to add it."))


class SlackConnector(Connector):
    """A `kind: slack` source. `client` sends every request and `token` gives the user token each time one is sent.
    `sleep` waits out a rate limit and `now` is the clock. `bound`, when given, gives the channels articles bind by
    `slack_channels`, by name or id, and a listed channel no article binds is not read; without it every listed
    channel is."""

    connecting = CONNECTING
    IDENTITY_KEYS: ClassVar[Mapping[str, int]] = {"slack_channels": 0}

    @classmethod
    def login_name(cls, source: SourceFile) -> str | None:
        """Each source keeps its own login, under its name."""
        return source.name

    @classmethod
    def needs(cls, source: SourceFile, item: str) -> Need | None:
        return Need("token", item, "the slack token")

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> SlackConnector:
        return cls(source, app.http, app.login(source), now=app.clock, bound=lambda: app.admitted("slack_channels"),
                   ours=app.ours)

    def __init__(self, source: SourceFile, client: httpx.Client, token: Callable[[], str], *,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] | None = None,
                 bound: Callable[[], Iterable[str]] | None = None, ours: Ours = NOBODY, base_url: str = API,
                 page_size: int = PAGE,
                 history_limit: int = PAGE, wait_budget: float = WAIT_BUDGET):
        self.channels, self.drop_bots = _check(source)
        self.name = source.name
        self.source = source
        self.client = client
        self.token = token
        self.sleep = sleep
        self.now = now or system_clock
        self.bound = bound
        self.ours = ours  # your addresses and your company's domains, which a message's keys never hold
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.page_size = max(1, page_size)
        self.history_limit = max(1, history_limit)
        self.wait_budget = wait_budget
        self.behind = source.behind()
        self.notices: list[str] = []
        self._waited = 0.0
        self._directory: _Directory | None = None
        self.scopes: list[str] | None = None  # the token's scopes, as the last answer's X-OAuth-Scopes lists them
        config = json.dumps([self.channels, self.drop_bots, source.window.behind, source.extract], sort_keys=True)
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]

    @staticmethod
    def read_key(value: str) -> None:
        """Refuse a token by its form, before anything is sent: a bot token, one that is
        not a user token, and one with a space or a control character in it. `KeyRefused`."""
        token = value.strip()
        if token.startswith("xoxb-"):
            raise KeyRefused("that is a bot token, which reads only the channels its bot is in: paste the User OAuth "
                             "Token, which starts with xoxp-", "the token is a bot token")
        if not token.startswith("xoxp-"):
            raise KeyRefused("that is not a Slack user token: paste the User OAuth Token, which starts with xoxp-",
                             "the token is no user token")
        if any(c <= " " or c == "\x7f" for c in token):
            raise KeyRefused("the token holds a space or a control character: paste the User OAuth Token alone",
                             "the token does not read")

    @classmethod
    def prove_key(cls, source: SourceFile, value: str, *, http: httpx.Client, now: Callable[[], datetime],
                  **_: Any) -> str:
        """Prove a user token before connect keeps it, and give the host of its
        workspace: its kind is read from its form before anything is sent, so a bot token, a token that is not a
        user token, and one with a space in it are refused unsent; then `auth.test`, and a token short of any of the
        manifest's scopes, by the scopes Slack lists with its answer, is refused. `KeyRefused` or `KeyUnproven`."""
        token = value.strip()
        cls.read_key(token)
        connector = cls(dataclasses.replace(source, settings={}), http, lambda: token, now=now)
        try:
            who = connector._call("auth.test", {})
        except SlackError as error:  # any code Slack answers is the token not working
            if error.code:
                raise KeyRefused(f"the token did not work with Slack, so nothing was kept: {error.code}",
                                 "the token was refused") from None
            raise KeyUnproven(str(error)) from None
        missing = [scope for scope in SCOPES if connector.scopes is not None and scope not in connector.scopes]
        if missing:
            raise KeyRefused(f"the token lacks the scopes {', '.join(missing)}: create the app from the manifest "
                             f"connect prints, or add them under the app's user token scopes and reinstall it",
                             "the token lacks scopes")
        address = str(who.get("url") or "")
        return urlsplit(address).hostname or address or str(who.get("team") or "")

    @classmethod
    def find_you(cls, source: SourceFile, value: str, *, http: httpx.Client, now: Callable[[], datetime],
                 **_: Any) -> tuple[str, str] | None:
        """Who the token is, for profile.md's `slack_id`, beside the ids of any other Slack workspace: the user id
        `auth.test` gives. `KeyUnproven` when Slack cannot be asked."""
        token = value.strip()
        try:
            who = cls(dataclasses.replace(source, settings={}), http, lambda: token, now=now)._call("auth.test", {})
        except SlackError as error:
            raise KeyUnproven(str(error).removeprefix(f"{source.name}: ")) from None
        user = str(who.get("user_id") or "")
        return (user, user) if user else None

    # -- requests
    def whoami(self) -> tuple[str, str]:
        """The user and the workspace the token belongs to, as Slack's `auth.test` says, from one read-only call.
        Raises `SlackLoginError` when Slack refuses the token, and `SlackError` when it cannot be asked."""
        who = self._call("auth.test", {})
        return str(who.get("user") or ""), str(who.get("team") or "")

    def _call(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        """Ask one method. A 429 waits its `Retry-After` and asks again, a 5xx is asked again twice, and an answer
        that is not ok is an error that names the method and what to do."""
        if method not in READS:
            raise SlackError(f"{self.name}: {method} is not a slack method that reads: the slack connector sends "
                             f"none that writes")
        server = 0
        while True:
            try:
                response = self.client.post(self.base_url + method, data=params,
                                            headers={"Authorization": f"Bearer {self.token()}"})
            except httpx.HTTPError as error:
                raise SlackError(f"{self.name}: could not reach slack for {method} ({type(error).__name__}): check "
                                 f"the network, then list again") from None
            try:
                body = response.json()
            except ValueError:
                body = {}
            body = body if isinstance(body, dict) else {}
            if response.status_code == 429 or body.get("error") == "ratelimited":
                self._wait(_retry_after(response.headers.get("Retry-After"), self.now()), method, params)
                continue
            if response.status_code >= 500 and server < SERVER_RETRIES:
                server += 1
                self.sleep(float(server * server))
                continue
            if response.status_code != 200:
                raise SlackError(f"{self.name}: {method} answered http {response.status_code}: list again later")
            if listed := response.headers.get("X-OAuth-Scopes", "").strip():
                self.scopes = [scope.strip() for scope in listed.split(",") if scope.strip()]
            if not body.get("ok"):
                raise self._refusal(method, body)
            return body

    def _wait(self, wait: float, method: str, params: dict[str, str]) -> None:
        if self._waited + wait > self.wait_budget:
            raise SlackRateLimited(f"{self.name}: slack rate limited this listing past the {self.wait_budget:.0f} "
                                   f"seconds it may wait: list again from its cursor later and it goes on")
        self._waited += wait
        what = f"{method} on {params['channel']}" if "channel" in params else method
        self.notices.append(f"{self.name}: waiting {wait:.0f}s for slack's rate limit before {what}")
        self.sleep(wait)

    def _refusal(self, method: str, body: dict[str, Any]) -> SlackError:
        code = str(body.get("error") or "")
        fix = _reconnect(self.name)
        if code in DEAD_TOKEN:
            return SlackLoginError(f"{self.name}: slack no longer takes the token ({code}): run {fix}", code)
        if code == "team_access_not_granted":
            return SlackLoginError(f"{self.name}: the app waits on a workspace admin's approval: once it is "
                                   f"installed, run {fix}", code)
        if code == "missing_scope":
            needed = str(body.get("needed") or "").strip()
            needed = needed if _WORD.match(needed) else f"the method {method} needs"
            return SlackError(f"{self.name}: the token lacks the scope {needed}: add it to the app's user token "
                              f"scopes, reinstall the app, then run {fix}", code)
        code = code if _WORD.match(code) else "an error"
        return SlackError(f"{self.name}: {method} answered {code}: list again later, or run {fix} if it lasts", code)

    def _pages(self, method: str, params: dict[str, str], key: str) -> Iterable[dict[str, Any]]:
        cursor, seen = "", set()
        while True:
            body = self._call(method, {**params, "limit": str(PAGE), **({"cursor": cursor} if cursor else {})})
            yield from body.get(key) or []
            cursor = self._next(body, method, seen)
            if not cursor:
                return

    def _next(self, body: dict[str, Any], method: str, seen: set[str]) -> str:
        """The cursor of the next page, or "" for none. A cursor given twice would page forever, so it stops."""
        cursor = str((body.get("response_metadata") or {}).get("next_cursor") or "")
        if cursor in seen:
            raise SlackError(f"{self.name}: slack gave the same page cursor twice for {method}, which would page "
                             f"forever: list again later, and report it if it repeats")
        seen.add(cursor)
        return cursor

    # -- what a person may pick
    picks = ("channels", "channel", "channels")

    def offer(self, ask: Ask | None = None) -> Offer:
        """The channels the token's user belongs to, by name, never a message: what `channels:` may list. A shared
        channel's members are read for the outside domains among them. `ask`, when given,
        has the workspace's model group the channels once with the `slack-channel-grouping` prompt:
        patterns of one channel per thing, channels shared with another organization, channels across many
        things, then the rest. A reply with no row shows every channel under the rest, unchecked. With no `ask`, or
        a reply that cannot be read or names no channel you belong to, every channel shows ungrouped, with a line
        that says why. Read only."""
        kinds = {"types": "public_channel,private_channel", "exclude_archived": "true"}
        found = [c for c in self._pages("conversations.list", kinds, "channels")
                 if c.get("is_member") and not (c.get("is_im") or c.get("is_mpim") or c.get("is_archived"))
                 and c.get("id") and c.get("name")]
        directory = _Directory("", "")
        if any(c.get("is_ext_shared") for c in found):
            for j in self._pages("users.list", {}, "members"):
                user = _user(j)
                directory.users[user.id] = user
        offered = sorted((_Offered(str(c["id"]), str(c["name"]), _about(c, "topic"), _about(c, "purpose"),
                                   bool(c.get("is_ext_shared")), int(c.get("num_members") or 0),
                                   tuple(self._outside(directory, _Channel(str(c["id"]), str(c["name"]), 0, True)))
                                   if c.get("is_ext_shared") else ())
                          for c in found), key=lambda o: o.name)
        note: tuple[str, ...] = ()
        rows: list[Choice] | None = None
        if ask is None:
            note = ("Every channel shows ungrouped: no model was asked to group them.",)
        elif offered:
            try:
                request, sent = _grouping_request(offered, ask.room(GROUPING_PROMPT))
                rows = self._grouped(offered, _grouping(ask(GROUPING_PROMPT, request)))
                if sent < len(offered):
                    note = ((f"Grouped the {sent} largest of your {len(offered)} channels. The others show under "
                             "the rest, or under a pattern that takes them in."),)
            except SecretsError as error:  # no key for the model
                note = (f"No channel suggestions yet: {one_line(str(error), 200)}.",
                        f"Then run cerebro connect {self.name} again to group the channels.")
            except Exception as error:  # noqa: BLE001 - a grouping that fails leaves the channels to pick by hand
                why = one_line(str(error), 200) or type(error).__name__
                note = (f"The model gave no grouping, so every channel shows ungrouped: {why}",)
        group = "" if rows is None else f"in sources/{self.name}.yaml"
        if rows is None:
            rows = [self._choice(o.name, offered) for o in offered]
        # An entry of the file no row shows comes first, as its own: a pattern, or a channel a pattern takes in.
        shown = {row.value.casefold() for row in rows}
        mine = []
        for e in self.channels:
            if e.casefold() in shown:
                continue
            if _pattern(_entry(e)):
                mine.append(self._choice(_entry(e), offered, group))
            elif (named := next((o for o in offered if channel_named([e], o.id, o.name)), None)) is not None:
                mine.append(self._choice(named.name, offered, group))
            shown.add(e.casefold())
        return Offer(*self.picks, tuple(mine + rows), tuple(self.channels), note, len(offered),
                     head="Slack channels to import", verb="Import")

    def _choice(self, entry: str, offered: list[_Offered], group: str = "", checked: bool = False) -> Choice:
        """A row for a channel or a pattern: the file's own entry for it when the file names it, so its tick shows,
        a shared channel's outside domains in its label, and the channels a pattern takes in now."""
        mine = next((e for e in self.channels if _entry(e).casefold() == entry.casefold()), None)
        if mine is None and not _pattern(entry):
            channel = next((o for o in offered if o.name.casefold() == entry.casefold()), None)
            mine = next((e for e in self.channels if not _pattern(_entry(e)) and channel is not None
                         and channel_named([e], channel.id, channel.name)), None)
        if _pattern(entry):
            n = sum(1 for o in offered if channel_named([entry], o.id, o.name))
            return Choice(mine or entry, f"#{entry}", f"{n} channel{'' if n == 1 else 's'} now, and those made later",
                          group, checked)
        channel = next(o for o in offered if o.name.casefold() == entry.casefold())
        detail = ", ".join(x for x in ("shared with another organization" if channel.shared else "",
                                       channel.topic or channel.purpose) if x)
        label = f"#{channel.name}" + (f"  ({', '.join(channel.domains)})" if channel.domains else "")
        return Choice(mine or channel.name, label, detail, group, checked)

    def _grouped(self, offered: list[_Offered], groups: list[tuple[str, list[tuple[str, bool]]]]) -> list[Choice]:
        """The model's groups as rows: a row that names no channel you belong to, or a pattern that takes in none, is
        left out, each channel or pattern shows once, however the reply named it, and the channels no row takes in
        show under the rest. A reply that gives no row at all is a right answer when no channel is about what the
        wiki is for, so every channel shows under the rest, unchecked. Raises
        ValueError when the reply gave rows and none of them is left."""
        rows: list[Choice] = []
        seen: set[str] = set()
        taken: list[str] = []
        for label, picks in groups:
            for entry, checked in picks:
                if not any(channel_named([entry], o.id, o.name) for o in offered):
                    continue
                if not _pattern(entry):
                    entry = next(o.name for o in offered if channel_named([entry], o.id, o.name))
                if entry.casefold() in seen:
                    continue
                seen.add(entry.casefold())
                taken.append(entry)
                rows.append(self._choice(entry, offered, label, checked))
        if not rows and any(picks for _, picks in groups):
            raise ValueError("its reply grouped no channel you belong to")
        rows += [self._choice(o.name, offered, REST_GROUP) for o in offered if not channel_named(taken, o.id, o.name)]
        return rows

    # -- who and where
    def _read_directory(self) -> _Directory:
        who = self._call("auth.test", {})
        found = _Directory(str(who.get("user_id") or ""), str(who.get("url") or ""))
        for j in self._pages("users.list", {}, "members"):
            user = _user(j)
            found.users[user.id] = user
        kinds = {"types": "public_channel,private_channel", "exclude_archived": "true"}
        for c in self._pages("conversations.list", kinds, "channels"):
            # A direct message or a group of them is never read, even when a list holds one, and nothing of a
            # channel the file does not list is kept, not even its name.
            if c.get("is_im") or c.get("is_mpim") or c.get("is_archived") or not c.get("is_member"):
                continue
            if not channel_named(self.channels, str(c["id"]), str(c.get("name") or "")):
                continue
            found.channels[str(c["id"])] = _Channel(str(c["id"]), str(c.get("name") or ""), int(c.get("updated") or 0),
                                                    bool(c.get("is_ext_shared")), _about(c, "topic"),
                                                    _about(c, "purpose"))
        self._directory = found
        return found

    def _user(self, d: _Directory, id: str) -> _User | None:
        """A user by id: from the workspace's list, else asked for, as a member of another organization met in a
        Slack Connect channel is."""
        if not id:
            return None
        if id not in d.users:
            try:
                d.users[id] = _user(self._call("users.info", {"user": id}).get("user") or {})
            except SlackError as error:
                if error.code not in ("user_not_found", "user_not_visible"):
                    raise
                d.users[id] = None
        return d.users[id]

    def _scope(self, d: _Directory) -> list[_Channel]:
        """The channels a pass reads, the most active first: listed, yours, and bound when binding is known."""
        bound = None if self.bound is None else {_entry(v).casefold() for v in self.bound()}
        found = [c for c in d.channels.values()
                 if bound is None or c.id.casefold() in bound or c.name.casefold() in bound]
        return sorted(found, key=lambda c: (-c.updated, c.name, c.id))

    def _bot(self, d: _Directory, m: _Message) -> bool:
        if m.bot_id or m.subtype == "bot_message":
            return True
        user = self._user(d, m.user)
        return user is not None and user.bot

    def _kept(self, d: _Directory, m: _Message) -> bool:
        return m.subtype in KEPT and bool(m.text.strip()) and not (self.drop_bots and self._bot(d, m))

    # -- records
    def _record(self, d: _Directory, m: _Message, *, body: bool) -> Record:
        channel = d.channels[m.channel]
        user = self._user(d, m.user)
        bot = self._bot(d, m)
        keys = {"slack_channels": [channel.id, channel.name]}
        if user is not None and user.email and not bot:
            keys |= address_keys([user.email], self.ours)
        fields: dict[str, Any] = {"channel": m.channel}
        if author := self._author(d, m, user, bot):
            fields["from"] = author
        if m.thread:
            fields["thread"] = m.thread
        record = Record(id=m.id, source=self.name, revision=m.revision, at=_stamp(m.ts),
                        url=_permalink(d.team_url, m), keys=keys,
                        links=[f"{m.channel}:{m.thread}"] if m.reply else [], fields=fields,
                        extract=self.source.extract or "facts",
                        carried=[k for k in ("email", "domain") if k in keys])  # its author's keys point at them
        if body:
            record.body = self._text(d, m, channel, user, bot)
        return record

    def _author(self, d: _Directory, m: _Message, user: _User | None, bot: bool) -> str:
        if bot:
            return m.bot_name or (user.name if user else "")
        if user is None:
            return ""
        return "me" if user.id == d.self else user.email or user.name

    def _text(self, d: _Directory, m: _Message, channel: _Channel, user: _User | None, bot: bool) -> str:
        if bot:
            sender = _safe(m.bot_name or (user.name if user else "") or "an app") + " (bot)"
        elif user is None:
            sender = "someone in Slack"
        else:
            name = _safe(user.name)
            sender = f"{name} <{user.email}>" if name and user.email else name or user.email
        heads = [("Channel", "#" + channel.name), ("From", sender), ("Date", format_datetime(_time(m.ts)))]
        lines = [f"{k}: {v}" for k, v in ((k, " ".join(v.split())) for k, v in heads) if v]
        in_scope = {c.id: c.name for c in self._scope(d)}

        def name(id: str) -> str:
            found = self._user(d, id)
            return found.name if found else ""

        return "\n".join(lines) + "\n\n" + plain_text(m.text, name, lambda id: in_scope.get(id, "")) + "\n"

    # -- reading
    def _thread(self, channel: str, ts: str, oldest: str = "", latest: str = "") -> list[_Message]:
        params = {"channel": channel, "ts": ts, "limit": str(self.history_limit)}
        if oldest or latest:
            params |= {"oldest": oldest or "0", "inclusive": "true"} | ({"latest": latest} if latest else {})
        found, cursor, seen = [], "", set()
        while True:
            body = self._call("conversations.replies", params | ({"cursor": cursor} if cursor else {}))
            found += [_Message.read(channel, j) for j in body.get("messages") or []]
            cursor = self._next(body, "conversations.replies", seen)
            if not cursor or not body.get("has_more", True):
                return found

    def _floor(self, at: str) -> str:
        return "" if self.behind is None else _ts(_time(at) - self.behind)

    def _begin(self, state: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
        """A new pass: the directory read afresh and the channels in scope queued. A message whose channel left the
        scope, or that left the window, still exists in slack: it is forgotten, never removed. Only a message read
        again and no longer found is removed, at the end of its channel."""
        d = self._read_directory()
        at = _ts(self.now())
        queue = [c.id for c in self._scope(d)]
        removed: list[str] = []
        fresh = state is None or state.get("config") != self._config
        return {"at": at, "queue": queue, "chan": "", "page": "", "fresh": fresh, "found": [], "covered": {},
                "seen": {}, "follow": {}, "channels": []}, removed

    def _step(self, d: _Directory, state: dict[str, Any] | None, p: dict[str, Any]) -> tuple[list[Record], list[str]]:
        """Read one page of one channel's history and the threads it calls for, then commit it to the pass. A
        rate limit past the budget leaves the pass as it was before the page."""
        chan = p["chan"] or p["queue"][0]
        if chan not in d.channels:  # gone from the directory since the pass began
            p["queue"] = [c for c in p["queue"] if c != chan]
            p["chan"], p["page"] = "", ""
            return [], []
        old = state or {"seen": {}, "follow": {}, "channels": []}
        prev = None if p["fresh"] else state
        full = prev is None or chan not in prev["channels"]
        floor = self._floor(p["at"])
        since = _ts(_time(prev["at"]) - LOOKBACK) if prev else ""
        oldest = floor if full else max(floor, _ts(_time(prev["at"]) - REACH), key=_key)
        params = {"channel": chan, "oldest": oldest or "0", "latest": p["at"], "inclusive": "true",
                  "limit": str(self.history_limit)}
        if p["chan"] == chan and p["page"]:
            params["cursor"] = p["page"]
        body = self._call("conversations.history", params)
        page = str((body.get("response_metadata") or {}).get("next_cursor") or "") if body.get("has_more") else ""
        read = [_Message.read(chan, j) for j in body.get("messages") or []]
        covered: dict[str, str] = {}
        follow: dict[str, str] = {}
        reach = _ts(_time(p["at"]) - REACH)
        for m in list(read):
            if m.reply or not (m.replies or m.latest):
                continue
            if m.latest and _key(m.latest) >= _key(reach):
                follow[m.id] = m.latest
            if full or not m.latest or _key(m.latest) >= _key(since):
                thread = self._thread(chan, m.ts)
                read += thread
                covered[m.ts] = ""
                newest = max((t.ts for t in thread if t.reply), key=_key, default="")
                if newest and _key(newest) >= _key(reach):
                    follow[m.id] = newest
        if not page and prev:
            # Threads followed since an earlier pass whose first message is older than this history read.
            for key, newest in old.get("follow", {}).items():
                channel, _, ts = key.partition(":")
                if channel != chan or _key(ts) >= _key(oldest) or ts in covered or ts in p["covered"]:
                    continue
                thread = self._thread(chan, ts, since)
                read += thread
                covered[ts] = since
                newest = max([newest] + [t.ts for t in thread if t.reply], key=_key)
                if _key(newest) >= _key(reach):
                    follow[key] = newest
        accepted: dict[str, _Message] = {}
        for m in read:
            if m.id in accepted or m.id in p["seen"] or (floor and _key(m.ts) < _key(floor)):
                continue
            if self._kept(d, m):
                accepted[m.id] = m
        records, seen = [], {}
        for m in sorted(accepted.values(), key=lambda m: _key(m.ts)):
            before = (prev or {}).get("seen", {}).get(m.id)
            changed = full or (before[0] != _rev(m.revision) if before else
                               _key(m.ts) >= _key(_ts(_time(prev["at"]) - REACH)) or _key(m.edited) >= _key(since))
            if changed:
                records.append(self._record(d, m, body=False))
            if _key(m.ts) >= _key(reach):
                seen[m.id] = [_rev(m.revision), m.thread]
        found = [id for id in accepted if id in old["seen"]]
        # Commit the page to the pass.
        if p["chan"] != chan:
            p["queue"] = p["queue"][1:]
        p["seen"] |= seen
        p["follow"] |= follow
        p["found"] += found
        p["covered"] |= covered
        p["chan"], p["page"] = (chan, page) if page else ("", "")
        removed: list[str] = []
        if not page:
            removed = self._end(chan, old, p, oldest, floor, reach)
        return records, removed

    def _end(self, chan: str, old: dict[str, Any], p: dict[str, Any], oldest: str, floor: str,
             reach: str) -> list[str]:
        """A channel read to its end: what this pass read again and no longer found is removed, and what it did
        not read again, a reply of a quiet thread, is still seen."""
        removed = []
        found = set(p["found"])
        for id, (rev, thread) in old["seen"].items():
            channel, _, ts = id.partition(":")
            if channel != chan or (floor and _key(ts) < _key(floor)):
                continue
            if not thread or thread == ts:
                read_again = _key(ts) >= _key(oldest)
            else:
                start = p["covered"].get(thread)
                read_again = start is not None and (start == "" or _key(ts) >= _key(start))
            if read_again and id not in found:
                removed.append(id)
            elif not read_again and _key(ts) >= _key(reach) and id not in p["seen"]:
                p["seen"][id] = [rev, thread]
        p["channels"].append(chan)
        p["found"], p["covered"] = [], {}
        return removed

    # -- the cursor
    def _encode(self, state: dict[str, Any] | None, progress: dict[str, Any] | None) -> str:
        text = json.dumps({"s": state, "p": progress}, sort_keys=True, separators=(",", ":"))
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(text.encode())).decode()

    def _decode(self, cursor: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        try:
            if not cursor.startswith(_CURSOR):
                raise ValueError
            whole = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
            state, progress = whole["s"], whole["p"]
            if state is None and progress is None:
                raise ValueError
            if state is not None and not isinstance(state["seen"], dict):
                raise ValueError
        except (ValueError, KeyError, TypeError, zlib.error, binascii.Error):
            raise SlackCursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with "
                                   f"no cursor to start over") from None
        return state, progress

    # -- the contract
    def list(self, since: str | None = None) -> Listing:
        self._waited = 0.0
        self.notices = []
        state, progress = self._decode(since) if since is not None else (None, None)
        removed: list[str] = []
        if progress is None:
            progress, removed = self._begin(state)
        d = self._directory or self._read_directory()
        records: list[Record] = []
        try:
            while len(records) < self.page_size and (progress["chan"] or progress["queue"]):
                page, gone = self._step(d, state, progress)
                records += page
                removed += gone
        except SlackRateLimited as error:
            # The waits are spent: list what was read, and the next listing goes on from here.
            self.notices.append(str(error))
            return Listing(records, self._encode(state, progress), sorted(removed), False, list(self.notices))
        if progress["chan"] or progress["queue"]:
            return Listing(records, self._encode(state, progress), sorted(removed), True, list(self.notices))
        finished = {"config": self._config, "at": progress["at"], "channels": progress["channels"],
                    "seen": progress["seen"], "follow": progress["follow"]}
        return Listing(records, self._encode(finished, None), sorted(removed), False, list(self.notices))

    # -- a curator's questions
    questions = QUESTIONS

    def _say(self, line: str) -> str:
        return f"slack source {self.name}: {line}"

    def _asked(self, question: str, params: Mapping[str, Any] | None, limit: int) -> str:
        """The word a question asks for, "" for none, or `QuestionError` in one line, before anything is sent."""
        say = f"slack source {self.name}"
        read = asked(say, QUESTIONS, TAKES, question, params, limit)
        word = _entry(read.get("name", "")).casefold()
        if "name" in read and not word:  # a name that is only marks, such as "#", names no channel
            raise QuestionError(f"{say}: {question} needs name: give {NAMED.what}, as {{\"name\": \"acme\"}}")
        return word

    def _outside(self, d: _Directory, channel: _Channel) -> list[str]:
        """The outside domains of a shared channel's members, from at most `MAX_MEMBERS` of them, bots left out."""
        emails = []
        for n, id in enumerate(self._pages("conversations.members", {"channel": channel.id}, "members")):
            if n >= MAX_MEMBERS:
                self.notices.append(self._say(f"read the first {MAX_MEMBERS} members of #{channel.name} for its "
                                              "outside domains"))
                break
            user = self._user(d, str(id))
            if user is not None and user.email and not user.bot:
                emails.append(user.email)
        return address_keys(emails, self.ours).get("domain", [])

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """A curator's question, as `QUESTIONS` lists them: one row for each listed channel you belong to, its keys
        the channel's id and name under `slack_channels`, the keys its messages carry, so an admit with them binds
        the channel. It reads the channel list and a shared channel's members through the methods `list` sends,
        and never a message."""
        word = self._asked(question, params, limit)
        if self.source.owner not in ("", "me"):
            return Listing([], None, [], False, [self._say("is a colleague's workspace login, and only your own "
                                                           "feeds admission: ask the slack source whose owner is me")])
        self._waited, self.notices = 0.0, []
        d = self._read_directory()
        bound = None if self.bound is None else {_entry(v).casefold() for v in self.bound()}

        def binds(c: _Channel) -> bool:
            return bound is not None and (c.id.casefold() in bound or c.name.casefold() in bound)

        found = sorted((c for c in d.channels.values() if word in c.name.casefold()),
                       key=lambda c: (not binds(c), c.name, c.id))
        answer = []
        for channel in found[:limit]:
            fields: dict[str, Any] = {"name": channel.name, "topic": channel.topic, "purpose": channel.purpose,
                                      "shared": channel.shared}
            if channel.shared:
                fields["outside_domains"] = self._outside(d, channel)
            answer.append(Record(id=channel.id, source=self.name, revision="",
                                 keys={"slack_channels": [channel.id, channel.name]}, fields=fields))
        return Listing(answer, None, [], len(found) > limit, list(self.notices))

    def get(self, id: str) -> Record:
        """One message whole. A message of a channel out of scope is refused before Slack is asked for it, and so
        is one Slack no longer holds or one with no words of its own."""
        self._waited = 0.0
        match = _ID.match(id) if isinstance(id, str) else None
        if match is None:
            raise RecordNotFound(f"{self.name} has no record {id!r}: a slack record's id is <channel>:<ts>, as a "
                                 f"listing gives it")
        channel, ts = match.groups()
        d = self._directory or self._read_directory()
        if channel not in {c.id for c in self._scope(d)}:
            d = self._read_directory()
            if channel not in {c.id for c in self._scope(d)}:
                raise RecordNotFound(f"{self.name} has no record {id}: its channel is not one the source reads: "
                                     f"list it under channels in sources/{self.name}.yaml")
        try:
            thread = self._thread(channel, ts, ts, ts)
        except SlackError as error:
            if error.code in ("thread_not_found", "message_not_found", "channel_not_found"):
                thread = []
            else:
                raise
        message = next((m for m in thread if m.ts == ts), None)
        if message is None or not self._kept(d, message):
            raise RecordNotFound(f"{self.name} has no record {id}: slack no longer holds it, or it holds no words "
                                 f"of its own: list the source for the ids it holds")
        return self._record(d, message, body=True)
