"""Gong as a source: the company's recorded calls, read only, with an access key.

A record is one call. Its id is Gong's call id. Its revision is a hash of the call as Gong lists it, so it moves
when Gong changes the call and not when nothing did. Its time is the call's start as Gong writes it, with Gong's
offset kept, since that offset gives the record's day. Its url is the call's page in Gong. Its keys are what can
bind it to an article: the call's own id, the addresses of its parties and the domains they are at, the
Salesforce accounts Gong links it to, and the calendar event it was booked from. An address of yours, or one at
your company's domains, is never a key, and a public mail service names no company, so it gives no domain. Its
links are the CRM records Gong ties the call to. It lands in `calls` unless the source file says `into:`.

A listing asks Gong for the calls that started in the source file's window, 90 days when it names none, page by
page, and gives only the calls whose revision moved since the cursor. The cursor carries what it needs, each
call's id and a short hash of its revision, so a call deleted at Gong, or aged out of the window, is in `removed`
once, and the cursor survives a restart because nothing else is kept. `get` asks for one call and its transcript
and renders them for extraction: who spoke, on which side, and how far into the call. No transcript is kept.

Every request is a read: a GET, or a POST to one of the two paths in `READS` that take a filter in their body.
Requests wait under a throttle of 3 in any 1.1 seconds, as Gong allows 3 a second. A 429 is not retried: it
stops the pass with the wait Gong asks for, and the connector asks Gong nothing until that wait ends. The access
key comes from the function handed in, the clock and the sleep are handed in, and nothing is read from the
environment. The one thing read from the disk is which Gong user is you: the ids `profile.md` lists under
`identity.gong_id`, read once and only when the connector was not handed them.
A party is you by one of your addresses or by one of those ids. Another user of the company is on our side, and is
not you.

A curator may ask it `calls`, `calls_with` and `calls_by_domain`, as `QUESTIONS` says: the calls of the days the
question names, read page by page with the same request `list` makes and never a transcript, and one row for each
outside domain of their parties, as `questions.rows` makes them. A row says how many calls you attended or spoke in
and whether both sides spoke, never a title or a word said. The domain or account `calls_with` names is matched
here and never sent to Gong.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import httpx

from ..clock import seconds, system_clock
from ..connector import (
    NOBODY,
    Connector,
    CursorError,
    Listing,
    Ours,
    QuestionError,
    Record,
    RecordNotFound,
    address_keys,
    event_key,
    gone,
    own_people,
    registrable,
    retry_after,
    split_login,
)
from ..errors import CerebroError
from ..secrets import SecretsError
from ..source import SourceError, SourceFile
from .connecting import Connecting, KeyRefused, KeyUnproven, Need, Profile, Prompt
from .questions import LATEST, MOST_ENGAGED, Conversation, Days, Text, Word, asked, rows

__all__ = ["GONG_API", "READS", "GongConnector", "GongCursorError", "GongKey", "GongKeyMissing", "GongKeyRefused",
           "GongRateLimited", "GongUnavailable", "Ours", "Throttle", "WriteRefused", "is_gong_api", "is_read",
           "render_call"]

GONG_API = "https://api.gong.io"
CALLS = "/v2/calls/extensive"
TRANSCRIPTS = "/v2/calls/transcript"
CHECK = "/v2/workspaces"  # the GET connect checks a key with
PROOF = "/v2/calls"  # the GET connect then proves the key reads calls with
PROOF_DAYS = 30
# The reads Gong takes as a POST, with a filter in the body. Every other POST, and any PUT, PATCH or DELETE, writes.
READS = frozenset({("POST", CALLS), ("POST", TRANSCRIPTS)})
COLLECTION = "calls"
# The keys a call only carries, which point at its parties and the CRM account it is linked to: its `gong_call_id`
# and its event's `calendar_event_id` name it.
CARRIED = ("email", "domain", "salesforce_id")
PER_WINDOW = 3
WINDOW_SECONDS = 1.1
WAIT_SHOWN = 3600
MAX_PAGES = 1000
PAGES_A_LISTING = 5  # gong pages one listing reads before it returns, so a rate limit loses little
LOOKBACK = timedelta(hours=72)  # a call this recent is checked again for its transcript
_CURSOR = "gong1."
# The questions a curator may ask the source with `ask`, each with the params it takes.
QUESTIONS = {
    "calls": "one row for each outside domain of the calls Gong recorded in the last `days` (90 by default, at most "
             "90), whoever attended, the latest call first: how many, how many you attended or spoke in, whether "
             "both sides spoke, the first and last day, and the CRM account Gong links; never a title or a "
             "transcript; params: days, took_part (me keeps the calls you took part in, anyone by default)",
    "calls_with": "the same rows for the calls with one outside domain, or that Gong links to one CRM account, to "
                  "settle one account; params: domain, such as acme.example, or account, a CRM account's id or "
                  "name; days",
    "calls_by_domain": "the same rows, the most calls you took part in first, for who you talk to most; params: "
                       "days, took_part",
}
MAX_ASK_DAYS = 90
ASK_PAGES = 20  # gong pages one question reads at most
_DAYS = Days(90, 1, MAX_ASK_DAYS)
_TOOK_PART = Word(("anyone", "me"))
TAKES = {"calls": {"days": _DAYS, "took_part": _TOOK_PART},
         "calls_with": {"domain": Text("acme.example"), "account": Text("Acme Corp"), "days": _DAYS},
         "calls_by_domain": {"days": _DAYS, "took_part": _TOOK_PART}}
_AUTHS = ("", "api_key")
SOURCE_KEYS = ("auth", "url", "extract", "window", "into", "freshness")


class GongKeyMissing(CerebroError, LookupError):
    """No Gong access key, or one with a part empty, came from the function that gives it."""


class GongKeyRefused(CerebroError, PermissionError):
    """Gong answered 401 or 403: the key is refused, or refused one call or its transcript. `why` says it in a few
    words, for a step's line."""

    def __init__(self, message: str, why: str = ""):
        super().__init__(message)
        self.why = why


class GongRateLimited(CerebroError):
    """Gong refused a request over the key's rate limit. `wait` is the seconds it asked to wait, 0 when it named
    none."""

    def __init__(self, message: str, wait: float = 0.0):
        super().__init__(message)
        self.wait = wait


class GongUnavailable(CerebroError, ConnectionError):
    """Gong did not answer, answered with an error, or answered with something that does not read: never a refusal
    of the key. `why` says which in a few words, for a step's line."""

    def __init__(self, message: str, why: str = ""):
        super().__init__(message)
        self.why = why


class GongCursorError(CursorError):
    """A cursor this connector did not give, or one that does not read."""


class WriteRefused(CerebroError, PermissionError):
    """A request that would write to Gong. The connector never sends one: this is a bug to report."""


@dataclass(frozen=True)
class GongKey:
    """A Gong access key and its secret, as a Gong tech admin makes them. Never kept by the connector."""

    access_key: str
    secret: str


def is_read(method: str, path: str) -> bool:
    """Whether a request only reads: a GET, or a POST to one of the paths in `READS`."""
    method = method.upper()
    return method == "GET" or (method, path) in READS


def is_gong_api(url: str) -> bool:
    """Whether a url is Gong's API or one of its regional hosts: https, at api.gong.io or a host under it, with no
    user, query or fragment, so the key goes nowhere else."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - a port that does not read raises here
    except ValueError:
        return False
    if parts.scheme != "https" or "@" in parts.netloc or parts.query or parts.fragment:
        return False
    return host == "api.gong.io" or host.endswith(".api.gong.io")


def _wait_text(seconds: float) -> str:
    """A wait written as a duration, as 30s, 1m0s or 1h0m0s, or `over 1h0m0s` past an hour."""
    if seconds > WAIT_SHOWN:
        return "over " + _wait_text(WAIT_SHOWN)
    whole = round(seconds)
    hours, minutes, secs = whole // 3600, whole // 60 % 60, whole % 60
    if hours:
        return f"{hours}h{minutes}m{secs}s"
    return f"{minutes}m{secs}s" if minutes else f"{secs}s"


def _json(reply: httpx.Response) -> bool:
    """Whether a reply's body reads as a json object: a reply that does not is taken as no answer."""
    try:
        return isinstance(reply.json(), dict)
    except ValueError:
        return False


def _clock(ms: float) -> str:
    """Milliseconds into a call as m:ss, or h:mm:ss from an hour."""
    s = int(ms / 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _started(text: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _ours(party: dict[str, Any], ours: Ours) -> bool:
    """Whether a party of a call is on the workspace's own side, the sides rule:
    you, a party Gong gives a user id, an address `ours` holds, or a party Gong
    calls internal."""
    email = _text(party.get("emailAddress"))
    return bool(email and ours.holds(email)) or bool(_text(party.get("userId"))) or \
        _text(party.get("affiliation")).lower() == "internal"


def _party_label(party: dict[str, Any]) -> str:
    name, email = _text(party.get("name")), _text(party.get("emailAddress"))
    if name and email:
        return f"{name} <{email}>"
    return name or email


def _meta(call: dict[str, Any]) -> dict[str, Any]:
    meta = call.get("metaData")
    return meta if isinstance(meta, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _side(party: dict[str, Any], ours: Ours) -> str:
    """The side a speaker's turn is labelled with, by the same sides rule as the record's `ours` field: our side for a
    party `_ours` counts, their side for one with an address it does not, and none for a party with neither."""
    if _ours(party, ours):
        return "our side"
    return "their side" if _text(party.get("emailAddress")) else ""


def render_call(call: dict[str, Any], transcript: dict[str, Any] | None, ours: Ours) -> str:
    """A call and its transcript as text for extraction: the title, the start as
    Gong wrote it, the attendees, then one line a speaker's turn, with how far into the call it began, who spoke,
    and their address. A speaker no party names is `Speaker <id>`, on no side. Each turn
    also says its side, by the same rule as the record's `ours` field (`_side`)."""
    meta = _meta(call)
    lines = [" ".join(title.split())] if (title := _text(meta.get("title"))) else []
    if started := _text(meta.get("started")):
        lines.append(f"Started: {started}")
    parties = [p for p in call.get("parties") or [] if isinstance(p, dict)]
    people = [label for p in parties if (label := _party_label(p))]
    if people:
        lines.append("Attendees: " + ", ".join(people))
    speakers = {_text(p.get("speakerId")): p for p in parties if _text(p.get("speakerId"))}
    for turn in (transcript or {}).get("transcript") or []:
        sentences = [s for s in turn.get("sentences") or [] if isinstance(s, dict)]
        if not sentences:
            continue
        party = speakers.get(_text(turn.get("speakerId")))
        if party is not None and (label := _party_label(party)):
            side = _side(party, ours)
            who = f"{label} ({side})" if side else label
        else:
            who = f"Speaker {turn.get('speakerId', '')}"
        said = " ".join(" ".join(t.split()) for s in sentences if (t := _text(s.get("text"))))
        start = sentences[0].get("start")
        lines += ["", f"[{_clock(start if isinstance(start, (int, float)) else 0)}] {who}: {said}"]
    return "\n".join(lines)


def _revision(call: dict[str, Any], transcript: dict[str, Any] | None = None) -> str:
    """A hash of the call and of its transcript as Gong gives it, none until it is ready,
    so a call listed before its transcript is listed again once the transcript comes."""
    canonical = json.dumps([call, transcript], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _short(revision: str) -> str:
    return revision.split(":")[-1][:16]


class Throttle:
    """Lets at most `n` requests start in any `window` seconds. `clock` gives seconds and `sleep` waits, both
    handed in so a test never sleeps. One throttle may be shared by every connector that uses one key."""

    def __init__(self, n: int = PER_WINDOW, window: float = WINDOW_SECONDS, *,
                 clock: Callable[[], float] = seconds, sleep: Callable[[float], None] = time.sleep):
        self.n, self.window, self.clock, self.sleep = n, window, clock, sleep
        self._starts: list[float] = []

    def wait(self) -> None:
        """Return when a request may start."""
        while True:
            now = self.clock()
            self._starts = [s for s in self._starts if now < s + self.window]
            if len(self._starts) < self.n:
                self._starts.append(now)
                return
            self.sleep(self._starts[0] + self.window - now)


_THROTTLES: WeakKeyDictionary[Any, Throttle] = WeakKeyDictionary()  # each workspace's one throttle


@dataclass
class _Pass:
    """A listing under way across pages: Gong's cursor for the next page, the floor it lists from, the revisions
    found so far, and whether every call is listed anew because the source file changed."""

    page: str = ""
    floor: str = ""
    found: dict[str, str] = field(default_factory=dict)
    anew: bool = False


# What connect says and asks, in the drawn view and on a plain line.
CONNECTING = Connecting(
    says=(('Provide a Gong access key and access key secret. These can be created by a Gong tech admin. To create '
          'one, go to `Admin Center > Settings > Ecosystem > API`, then click "`Get API Key`".'), ""),
    plain=("Gong keys are made by a Gong tech admin. Send yours this:", "",
           "  Could you make me a Gong API key? In Gong, open Admin center, then",
           "  Settings, Ecosystem, API, and click Get API Key. Please send me the",
           "  access key and the access key secret.", "",
           "Paste the key and secret when you have them, or press Enter to connect later.", ""),
    asks=(Prompt("Gong access key", "Gong access key, or Enter to connect later"),
          Prompt("Gong access key secret", "Gong access key secret")),
    later="Gong is not connected yet. Run cerebro connect {name} when you have the key and secret.",
    later_check="Gong is not connected yet: run cerebro connect {name} when you have the key and secret",
    asks_again=False,  # a refused kept key fails and names --replace
    title="Gong",
    needs="an API key and secret from your Gong tech admin",
    connected="Connected to Gong. The key is kept under {secret} in {where}.",
    profile=Profile("gong_id", added="Found your Gong user by {by} and added it to profile.md as gong_id.",
                    already="Your Gong user, found by {by}, is in profile.md as gong_id already.",
                    no_email=("profile.md has no email to find your Gong user by, so it gets no gong_id: add the "
                              "email you use in Gong there, then run cerebro connect {name} again."),
                    not_found=("No Gong user has the email {by}, so profile.md gets no gong_id: add the email you use "
                               "in Gong to profile.md, then run cerebro connect {name} again."),
                    failed=("warning: could not look up your Gong user by {by}, so profile.md gets no gong_id: "
                            "{error}. Run cerebro connect {name} again to add it.")),
    kept="Gong is connected already, and the key kept under {secret} in {where} stays. Pass --replace to replace it.")


class GongConnector(Connector):
    """A `kind: gong` source. `http` is the client every request goes through, `credentials` gives the access key
    when a request needs it, and `ours` says who is on our side. `now` is the clock the window and a wait are read
    by, and `throttle` holds requests to Gong's rate."""

    IDENTITY_KEYS: ClassVar[Mapping[str, int]] = {"calendar_event_id": 500}  # one meeting, by its event: what its calendar entry, recording and notes share
    # A turn as `render_call` writes it, the name alone in `speaker`: `[1:02:05] Name <address> (their side): `.
    speaker_pattern: ClassVar[str] = r"\[\d+(?::\d\d){1,2}\] (?P<speaker>[^<>\n]+?)(?: <[^<>\n]*>)?(?: \((?:our|their) side\))?: "

    connecting = CONNECTING

    @classmethod
    def login_name(cls, source: SourceFile) -> str | None:
        """Every source of the kind keeps one login, under the kind's name."""
        return "gong"

    @classmethod
    def needs(cls, source: SourceFile, item: str) -> Need | None:
        return Need("pair", item, "the gong access key and its secret", parts=("Gong access key", "Its secret"))

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> GongConnector:
        """Every Gong source of one workspace uses one key, and so one throttle."""
        login = app.login(source)

        def credentials() -> GongKey:
            try:
                value = login()
            except SecretsError as error:
                raise GongKeyMissing(str(error)) from None
            key, secret = split_login(value, "gong key", f"run cerebro connect {source.name} --replace with the "
                                      "access key and its secret")
            app.mask.add(key, secret, f"{key}:{secret}")
            return GongKey(key, secret)

        return cls(source, http=app.http, credentials=credentials, ours=app.ours, now=app.clock,
                   throttle=_THROTTLES.setdefault(app, Throttle()), zone=app.zone)

    def __init__(self, source: SourceFile, *, http: httpx.Client, credentials: Callable[[], GongKey],
                 ours: Ours = NOBODY, now: Callable[[], datetime] | None = None, throttle: Throttle | None = None,
                 zone: tzinfo = UTC, you: tuple[str, ...] | None = None):
        label = f"sources/{source.name}.yaml"
        if source.kind != "gong":
            raise SourceError(f"{label} is kind {source.kind}: a gong connector reads kind gong alone, so the gong "
                              f"key is not sent where it points")
        if source.url and not is_gong_api(source.url):
            host = urlsplit(source.url).hostname or "it"
            raise SourceError(f"{label}: its url at {host} is not gong's api, so the gong key is not sent there: give "
                              f"https://api.gong.io or the regional address under api.gong.io that your gong api "
                              f"page shows")
        if source.auth not in _AUTHS:
            raise SourceError(f"{label}: auth {source.auth} is not how gong is read: write auth: api_key")
        self.name = source.name
        self.source = source
        self.base = (source.url or GONG_API).rstrip("/")
        self.http = http
        self.credentials = credentials
        self.ours = ours
        self._you = None if you is None else frozenset(i.strip().lower() for i in you if i.strip())
        self.now = now or system_clock
        self.zone = zone  # the workspace's zone, in which a question's days are counted
        self.throttle = throttle or Throttle()
        self.notices: list[str] = []
        self.behind = source.behind()
        config = json.dumps([asdict(source.window), source.extract, source.into, self.base], sort_keys=True)
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]
        self._refused: GongRateLimited | None = None
        self._until: datetime | None = None

    @property
    def you(self) -> frozenset[str]:
        """The Gong user ids that are you, as handed in, or as the workspace's `profile.md` lists them under
        `identity.gong_id`, read once. With no profile, or no id in it, there are none, and you are known by your
        addresses alone."""
        if self._you is None:
            ids: tuple[str, ...] = ()
            if self.source.path is not None:
                from ..project import WorkspaceError, _identity_list  # here: the project module reads far more

                profile = self.source.path.parent.parent / "profile.md"
                try:
                    ids = _identity_list(profile, "gong_id")
                except WorkspaceError as error:
                    raise SourceError(f"gong source {self.name}: {error}: write your Gong user id as gong_id: "
                                      "[<id>] under identity") from None
            self._you = frozenset(ids)
        return self._you

    def _is_you(self, party: dict[str, Any]) -> bool:
        """Whether a party is you, by one of your addresses or by your Gong user id."""
        email, user = _text(party.get("emailAddress")), _text(party.get("userId")).lower()
        return bool(email) and Ours(addresses=self.ours.addresses).holds(email) or bool(user) and user in self.you

    # -- asking Gong
    def _authorization(self) -> str:
        key = self.credentials()
        if not isinstance(key, GongKey) or not key.access_key.strip() or not key.secret.strip():
            raise GongKeyMissing(f"{self.name}: no gong access key and secret are stored: store the pair a gong "
                                 f"tech admin made, then list again")
        pair = f"{key.access_key.strip()}:{key.secret.strip()}".encode()
        return "Basic " + base64.b64encode(pair).decode()

    def _post(self, path: str, body: dict[str, Any], what: str) -> dict[str, Any] | None:
        """Gong's reply to one read, or None for a 404. Raises for anything else that is not a 2xx."""
        if not is_read("POST", path):
            raise WriteRefused(f"{self.name}: POST {path} would write to gong, and a source never writes: report "
                               f"this as a bug")
        if self._refused is not None and self._until is not None and self.now() < self._until:
            raise self._refused
        headers = {"Authorization": self._authorization(), "Content-Type": "application/json"}
        self.throttle.wait()
        try:
            reply = self.http.request("POST", self.base + path, content=json.dumps(body).encode(), headers=headers,
                                      follow_redirects=False)
        except httpx.HTTPError as error:
            raise GongUnavailable(f"{self.name}: gong did not answer the request for {what} "
                                  f"({type(error).__name__}): check the network, then list again") from None
        status = reply.status_code
        if status == 404:
            return None
        if status in (401, 403):
            raise GongKeyRefused(f"{self.name}: gong refuses {what} to the stored key (status {status}): replace "
                                 f"the access key, or ask a gong tech admin for one that reads calls and transcripts")
        if status == 429:
            wait = self._retry_after(reply.headers.get("Retry-After", ""))
            asks = f", and asks to wait {_wait_text(wait)}" if wait > 0 else ""
            refused = GongRateLimited(f"{self.name}: gong is refusing requests over the key's rate limit{asks}: "
                                      f"list again after then", wait)
            if wait > 0:
                self._refused, self._until = refused, self.now() + timedelta(seconds=wait)
            raise refused
        if not 200 <= status < 300:
            raise GongUnavailable(f"{self.name}: gong answered status {status} to the request for {what}: try "
                                  f"again later")
        try:
            data = reply.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise GongUnavailable(f"{self.name}: gong's reply to the request for {what} does not read as json: try "
                                  f"again later")
        return data

    def _retry_after(self, value: str) -> float:
        """The seconds a Retry-After asks for, as a count or a date, at most ten minutes, or 0."""
        return float(round(retry_after(value, self.now()) or 0.0))

    def _calls(self, filter: dict[str, Any], cursor: str, what: str) -> tuple[list[dict[str, Any]], str] | None:
        body: dict[str, Any] = {"filter": filter,
                                "contentSelector": {"context": "Extended", "exposedFields": {"parties": True}}}
        if cursor:
            body["cursor"] = cursor
        data = self._post(CALLS, body, what)
        if data is None:
            return None
        calls = [c for c in data.get("calls") or [] if isinstance(c, dict)]
        records = data.get("records") if isinstance(data.get("records"), dict) else {}
        return calls, _text(records.get("cursor"))

    @classmethod
    def prove_key(cls, source: SourceFile, value: str, *, http: httpx.Client, now: Callable[[], datetime],
                  **_: Any) -> None:
        """Prove a key, kept as `<access key>:<secret>`, before connect keeps it: `KeyRefused`
        when it does not read as both halves or Gong refuses it, `KeyUnproven` when Gong cannot be asked."""
        access, _, secret = value.partition(":")
        try:
            cls(dataclasses.replace(source, settings={}, auth=""), http=http, now=now,
                credentials=lambda: GongKey(access.strip(), secret.strip())).prove()
        except GongKeyMissing:
            raise KeyRefused("the gong key does not read as an access key and its secret joined by a colon",
                             "the key does not read") from None
        except GongKeyRefused as error:
            raise KeyRefused(str(error).removeprefix(f"{source.name}: "), error.why) from None
        except GongUnavailable as error:
            raise KeyUnproven(str(error).removeprefix(f"{source.name}: "), error.why) from None
        except SourceError as error:
            raise KeyUnproven(str(error)) from None

    @classmethod
    def find_you(cls, source: SourceFile, value: str, *, http: httpx.Client, now: Callable[[], datetime],
                 emails: tuple[str, ...] = (), **_: Any) -> tuple[str, str] | None:
        """Your Gong user id, for profile.md's `gong_id`, and the address it was found by: Gong's list of users, in turn,
        for one whose address or alias is one of `emails`, an active user first. None when
        no one has one; `KeyUnproven` when Gong cannot be asked."""
        access, _, secret = value.partition(":")
        gong = cls(dataclasses.replace(source, settings={}, auth=""), http=http, now=now,
                   credentials=lambda: GongKey(access.strip(), secret.strip()))
        wanted = {e.strip().lower() for e in emails if e.strip()}
        found: tuple[str, str] | None = None
        cursor, seen = "", set()
        while True:
            try:
                listed = gong._users(cursor)
            except (GongUnavailable, GongKeyRefused) as error:
                raise KeyUnproven(str(error).removeprefix(f"{source.name}: ")) from None
            for user in listed.get("users") or []:
                if not isinstance(user, dict) or not _text(user.get("id")):
                    continue
                aliases = user.get("emailAliases") if isinstance(user.get("emailAliases"), list) else []
                for email in [user.get("emailAddress"), *aliases]:
                    email = _text(email).lower()
                    if email in wanted:
                        if user.get("active") is True:
                            return _text(user.get("id")), email
                        found = found or (_text(user.get("id")), email)
            records = listed.get("records")
            cursor = _text(records.get("cursor")) if isinstance(records, dict) else ""
            if not cursor:
                return found
            if cursor in seen:
                raise KeyUnproven("gong gave the same part of its list of users twice")
            seen.add(cursor)

    def _users(self, cursor: str) -> dict[str, Any]:
        """One part of Gong's list of users, and its cursor to the next: a read."""
        self.throttle.wait()
        try:
            reply = self.http.get(self.base + "/v2/users", params={"cursor": cursor} if cursor else {},
                                  headers={"Authorization": self._authorization()}, follow_redirects=False)
        except httpx.HTTPError as error:
            raise GongUnavailable(f"gong did not answer ({type(error).__name__})") from None
        if reply.status_code in (401, 403):
            raise GongKeyRefused(f"gong did not take the key (status {reply.status_code}) for its users")
        if not 200 <= reply.status_code < 300:
            raise GongUnavailable(f"gong answered status {reply.status_code} to the list of users")
        if not _json(reply):
            raise GongUnavailable("gong returned invalid json for the list of users")
        data: dict[str, Any] = reply.json()
        return data

    def prove(self, days: int = PROOF_DAYS) -> None:
        """Prove the key in two steps, before connect keeps it: Gong's workspaces, which
        a 401 or 403 refuses and any other answer that is not a 2xx leaves unproven, as a wrong address does; then
        the calls of the last `days`, where a 404 is Gong's answer for a list with no call in it, and any other
        answer that is not a 2xx means the key does not list calls. Raises `GongKeyRefused` or `GongUnavailable`."""
        headers = {"Authorization": self._authorization()}

        def get(path: str, params: dict[str, str], what: str) -> httpx.Response:
            self.throttle.wait()
            try:
                return self.http.get(self.base + path, params=params, headers=headers, follow_redirects=False)
            except httpx.HTTPError as error:
                raise GongUnavailable(f"{self.name}: gong at {self.base} did not answer ({type(error).__name__}) "
                                      f"to {what}", "no answer") from None

        checked = get(CHECK, {}, "the list of workspaces")
        if checked.status_code in (401, 403):
            raise GongKeyRefused(f"{self.name}: gong did not take the key (status {checked.status_code})",
                                 "the key was refused")
        if not 200 <= checked.status_code < 300:
            raise GongUnavailable(f"{self.name}: gong at {self.base} answered status {checked.status_code} to the "
                                  f"list of workspaces")
        if not _json(checked):  # an answer that is not json is no answer
            raise GongUnavailable(f"{self.name}: gong at {self.base} returned invalid json for the list of "
                                  f"workspaces")
        listed = get(PROOF, {"fromDateTime": _stamp(self.now() - timedelta(days=days))}, "the list of calls")
        status = listed.status_code
        if status in (401, 403):
            raise GongKeyRefused(f"{self.name}: gong did not take the key (status {status}) for the list of calls",
                                 "the key was refused")
        if status == 429:
            raise GongUnavailable(f"{self.name}: gong at {self.base} is busy (status 429) at the list of calls",
                                  "gong was busy")
        if status >= 500 or status == 408:
            raise GongUnavailable(f"{self.name}: gong at {self.base} answered status {status} to the list of calls",
                                  "gong answered an error")
        if status != 404 and not 200 <= status < 300:
            raise GongKeyRefused(f"{self.name}: gong did not list calls with the key (status {status})",
                                 "the key listed no calls")
        if status != 404 and not _json(listed):  # an answer cut off or not Gong's, as at the workspaces
            raise GongUnavailable(f"{self.name}: gong at {self.base} returned invalid json for the list of calls",
                                  "gong answered an error")

    # -- a call as a record
    def _record(self, call: dict[str, Any], notices: list[str],
                transcript: dict[str, Any] | None = None) -> Record | None:
        meta = _meta(call)
        id = meta.get("id")
        id = str(id).strip() if isinstance(id, (str, int)) and not isinstance(id, bool) else ""
        if not id:
            notices.append("gong listed a call with no id: it is left out")
            return None
        started = _text(meta.get("started"))
        when = _started(started) if started else None
        if started and when is None:
            notices.append(f"call {id}: its start {started!r} does not read, so the record has no time")
        keys: dict[str, list[str]] = {"gong_call_id": [id]}

        def add(key: str, value: str) -> None:
            if value and value not in keys.setdefault(key, []):
                keys[key].append(value)

        attendees, emails, mine = [], [], []
        for party in call.get("parties") or []:
            if not isinstance(party, dict):
                continue
            if label := _party_label(party):
                attendees.append(label)
            emails.append(email := _text(party.get("emailAddress")))
            if _ours(party, self.ours):
                mine.append((_text(party.get("name")), email))
        keys |= address_keys(emails, self.ours)
        links: list[str] = []
        for context in call.get("context") or []:
            if not isinstance(context, dict) or _text(context.get("system")).lower() != "salesforce":
                continue
            for item in context.get("objects") or []:
                if not isinstance(item, dict) or not (object_id := _text(item.get("objectId"))):
                    continue
                if object_id not in links:
                    links.append(object_id)
                if item.get("objectType") == "Account":
                    add("salesforce_id", object_id)
        fields: dict[str, Any] = {}
        if event := _text(meta.get("calendarEventId")):
            fields["calendar_event_id"] = event  # as Gong writes it
            if named := event_key(event, when):  # the event's id as the calendar writes it: one key for both
                keys["calendar_event_id"] = [named]
        if title := _text(meta.get("title")):
            fields["title"] = title
        duration = meta.get("duration")
        if when is not None and isinstance(duration, (int, float)) and duration > 0:
            fields["ends"] = _stamp(when + timedelta(seconds=duration))
        if attendees:
            fields["attendees"] = attendees
        if mine := own_people(mine):
            fields["ours"] = mine  # who on it is the workspace's own, by `_ours`: never a key
        return Record(id=id, source=self.name, revision=_revision(call, transcript), at=_stamp(when) if when else "",
                      url=_text(meta.get("url")), keys=keys, links=links, fields=fields,
                      extract=self.source.extract or "facts", into=self.source.into or COLLECTION,
                      carried=[k for k in CARRIED if k in keys])

    # -- the cursor
    def _cursor(self, seen: dict[str, str], current: _Pass | None) -> str:
        state = {"config": self._config, "seen": seen, "pass": asdict(current) if current else None}
        data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(data)).decode()

    def _read_cursor(self, cursor: str) -> tuple[dict[str, str], _Pass | None, bool]:
        """The revisions the last finished pass saw, the pass under way, and whether the source file read the same."""
        try:
            if not cursor.startswith(_CURSOR):
                raise ValueError
            state = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
            seen = state["seen"]
            if not isinstance(seen, dict) or not all(isinstance(v, str) for v in seen.values()):
                raise ValueError
            current = _Pass(**state["pass"]) if state.get("pass") else None
        except (ValueError, KeyError, TypeError, zlib.error, binascii.Error):
            raise GongCursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with "
                                  f"no cursor to start over") from None
        return seen, current, state.get("config") == self._config

    def _floor(self) -> str:
        return "" if self.behind is None else _stamp((self.now() - self.behind).astimezone(UTC).replace(microsecond=0))

    # -- the contract
    def list(self, since: str | None = None) -> Listing:
        seen, current, same = self._read_cursor(since) if since is not None else ({}, None, True)
        if current is None or not same:
            current = _Pass(floor=self._floor(), anew=not same)
        notices: list[str] = []
        records: list[Record] = []
        asked: set[str] = set()
        recent = self.now() - LOOKBACK
        for read in range(1, MAX_PAGES + 1):
            filter = {"fromDateTime": current.floor} if current.floor else {}
            page = self._calls(filter, current.page, "the calls of the window")
            if page is None and current.found:
                raise GongUnavailable(f"{self.name}: gong found no calls on a later page of a listing: list with no "
                                      f"cursor to start over")
            calls, next_page = page if page is not None else ([], "")
            records += self._page(calls, seen, current, recent, notices)
            if next_page and (next_page == current.page or next_page in asked):
                raise GongUnavailable(f"{self.name}: gong gave the same page cursor twice: list with no cursor to "
                                      f"start over")
            asked.add(current.page)
            current.page = next_page
            if not next_page or records or read >= PAGES_A_LISTING:
                break
        self.notices = notices
        if current.page:
            return Listing(records, self._cursor(seen, current), [], True, list(self.notices))
        removed = gone([id for id in seen if id not in current.found], self._exists)
        return Listing(records, self._cursor(current.found, None), removed, False, list(self.notices))

    def _page(self, calls: list[dict[str, Any]], seen: dict[str, str], current: _Pass, recent: datetime,
              notices: list[str]) -> list[Record]:
        """The calls of one page that changed. What the cursor keeps of a call is a short hash of the call alone and
        one of the call with its transcript. A call that is new, changed, or started in the last 72 hours has its
        transcript asked for, in one request for the page, so one listed before its transcript was ready is
        listed again when it comes; an older call unchanged is not asked about."""
        due: dict[str, tuple[dict[str, Any], str]] = {}
        for call in calls:
            id = _text(_meta(call).get("id"))
            alone = _short(_revision(call))
            was = seen.get(id, "")
            started = _started(_text(_meta(call).get("started")))
            if current.anew or not was.startswith(alone + ".") or started is None or started >= recent:
                due[id] = (call, alone)
            elif id:
                current.found[id] = was
        transcripts: dict[str, dict[str, Any]] = {}
        if due:
            reply = self._post(TRANSCRIPTS, {"filter": {"callIds": sorted(i for i in due if i)}},
                               "the transcripts of a page of calls")
            transcripts = {str(t.get("callId")): t for t in (reply or {}).get("callTranscripts") or []
                           if isinstance(t, dict)}
        out = []
        for id, (call, alone) in due.items():
            record = self._record(call, notices, transcripts.get(id))
            if record is None:
                continue
            kept = f"{alone}.{_short(record.revision)}"
            current.found[record.id] = kept
            if current.anew or seen.get(record.id) != kept:
                out.append(record)
        return out

    def _exists(self, id: str) -> bool:
        """Whether gong still holds a call a listing no longer found: one that left the window still exists."""
        page = self._calls({"callIds": [id]}, "", f"call {id}")
        return any(str(_meta(c).get("id")) == id for c in (page[0] if page else []))

    # -- a curator's questions
    questions = QUESTIONS

    def _accounts(self, call: dict[str, Any]) -> list[tuple[str, str]]:
        """The Salesforce accounts Gong links a call to, each as its id and its name."""
        out = []
        for context in call.get("context") or []:
            if not isinstance(context, dict) or _text(context.get("system")).lower() != "salesforce":
                continue
            for item in context.get("objects") or []:
                if not isinstance(item, dict) or item.get("objectType") != "Account":
                    continue
                name = next((_text(f.get("value")) for f in item.get("fields") or []
                             if isinstance(f, dict) and f.get("name") == "Name"), "")
                if (id := _text(item.get("objectId"))) or name:
                    out.append((id, name))
        return out

    def _conversation(self, call: dict[str, Any]) -> Conversation:
        """What a call says of who took part, never what was said: you attended when you are among its parties,
        and spoke when Gong gives your party a speaker id; each side spoke when Gong gives a party of that side
        one, so a call with no speaker id tells no sides. Gong names no organizer, so a call says nothing of who
        sent it."""
        meta = _meta(call)
        when = _started(_text(meta.get("started")))
        emails, attended, spoke, told, ours, theirs = [], False, False, False, False, False
        for party in call.get("parties") or []:
            if not isinstance(party, dict):
                continue
            email = _text(party.get("emailAddress"))
            emails.append(email)
            mine = self._is_you(party)
            attended = attended or mine
            if not _text(party.get("speakerId")):
                continue
            told, spoke = True, spoke or mine
            if mine or _ours(party, self.ours):
                ours = True
            else:
                theirs = True
        crm = self._accounts(call)
        return Conversation(at=when, day=when.astimezone(self.zone).date().isoformat() if when else "",
                            outside=address_keys(emails, self.ours).get("domain", []),
                            how=["attended", "spoke"] if spoke else ["attended"] if attended else [],
                            sides=(ours, theirs) if told else None,
                            party_keys={"salesforce_id": [crm[0][0]]} if len(crm) == 1 else {},
                            party_name=crm[0][1] if len(crm) == 1 else "")

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """A curator's question, as `QUESTIONS` lists them: the calls that started in the days the question names,
        read as `list` reads them, at most `ASK_PAGES` pages, with no transcript asked for, and one row for each
        outside domain of their parties. `took_part` me keeps the calls you attended; `calls_with` keeps those with
        its domain among their outside domains, or that Gong links to its account by id or name."""
        say = f"gong source {self.name}"
        read = asked(say, QUESTIONS, TAKES, question, params, limit)
        domain = account = ""
        if question == "calls_with":
            host = read["domain"].lower().rpartition("@")[2].removeprefix("https://").removeprefix("http://")
            host = host.partition("/")[0].removeprefix("www.")
            domain, account = registrable(host) if host else "", read["account"].lower()
            if read["domain"] and ("." not in host or any(c.isspace() or c in ":?#[]" for c in host)):
                raise QuestionError(f"{say}: calls_with domain {read['domain'][:60]!r} is not a domain: give one, "
                                    "such as acme.example")
            if not domain and not account:
                raise QuestionError(f"{say}: calls_with takes a domain or an account: give one, as "
                                    '{"domain": "acme.example"}')
        floor = _stamp((self.now() - timedelta(days=read["days"])).astimezone(UTC).replace(microsecond=0))
        talks, notices, page, asked_pages = [], [], "", set()
        for _ in range(ASK_PAGES):
            found = self._calls({"fromDateTime": floor}, page, f"the calls of the last {read['days']} days")
            calls, next_page = found if found is not None else ([], "")
            for call in calls:
                talk = self._conversation(call)
                if read.get("took_part") == "me" and not talk.how:
                    continue
                if question == "calls_with" and domain not in talk.outside and not any(
                        account and (id.lower() == account or name.lower() == account)
                        for id, name in self._accounts(call)):
                    continue
                talks.append(talk)
            if next_page and (next_page == page or next_page in asked_pages):
                raise GongUnavailable(f"{self.name}: gong gave the same page cursor twice: ask again later")
            asked_pages.add(page)
            page = next_page
            if not page:
                break
        else:
            notices.append(f"read the first {ASK_PAGES} pages of calls alone: ask for fewer days to reach every call")
        order = MOST_ENGAGED if question == "calls_by_domain" else LATEST
        answer = rows(self.name, "calls", talks, order, limit)
        answer.notices = notices
        return answer

    def get(self, id: str) -> Record:
        if not isinstance(id, str) or not id.isdigit() or not id.isascii():
            raise RecordNotFound(f"{self.name} has no call {id!r}: a gong call's id is its number, as a listing "
                                 f"gives it")
        page = self._calls({"callIds": [id]}, "", f"call {id}")
        call = next((c for c in (page[0] if page else []) if str(_meta(c).get("id")) == id), None)
        if call is None or self._record(call, []) is None:
            raise RecordNotFound(f"{self.name} has no call {id}: it was deleted, or the key cannot see it: list the "
                                 f"source for the calls it holds")
        reply = self._post(TRANSCRIPTS, {"filter": {"callIds": [id]}}, f"the transcript of call {id}")
        transcript = next((t for t in (reply or {}).get("callTranscripts") or []
                           if isinstance(t, dict) and str(t.get("callId")) == id), None)
        record = self._record(call, [], transcript)
        if record is None:  # it read a moment ago, so this does not happen
            raise RecordNotFound(f"{self.name} has no call {id}: list the source for the calls it holds")
        record.body = render_call(call, transcript, self.ours)
        return record

