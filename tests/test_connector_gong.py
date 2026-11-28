"""The Gong connector: the `Connector` suite over a stand-in for Gong's API that replays recorded replies, then
reads only, keeps nothing, the window, the throttle, Gong's rate limit, and a curator's questions, which list the
calls and never a transcript.

The replies under `data/gong/` are invented: every domain ends in `.example`, an address at a public mail service
starts `invented.`, the Salesforce ids hold `SAMPLE`, and the first call is a quarterly review with its transcript,
its expected text and a side added to each turn.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import zlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from suites.connector import *
from suites.connector import every_page
from test_fetch import Asking, curate

from cerebro.connector import Ours, QuestionError, Record, RecordNotFound
from cerebro.connectors.gong import (
    GongConnector,
    GongCursorError,
    GongKey,
    GongKeyMissing,
    GongKeyRefused,
    GongRateLimited,
    GongUnavailable,
    Throttle,
    is_gong_api,
    is_read,
    render_call,
)
from cerebro.curate import in_scope
from cerebro.fakes import MemoryStore
from cerebro.fetch import fetch_tool
from cerebro.identity import bound_to, reached
from cerebro.source import SourceError, SourceFile, Window

DATA = Path(__file__).parent / "data" / "gong"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
KEY = GongKey("key-1", "secret-1")
OURS = Ours(addresses=("sam.rivera@quillmoor.example", "so@home.example"),
            domains=("quillmoor.example",))


def load(name: str) -> Any:
    return json.loads((DATA / name).read_text())


class Time:
    """A wall clock and a monotonic clock that sleeping moves forward together, so no test sleeps."""

    def __init__(self, now: datetime = NOW):
        self.wall, self.seconds, self.slept = now, 0.0, []

    def now(self) -> datetime:
        return self.wall

    def clock(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.seconds += seconds
        self.wall += timedelta(seconds=seconds)


class StandIn:
    """Gong's API for one invented key, key-1 and secret-1, replaying recorded calls and transcripts, two calls a
    page. It answers 404 to a listing that matches no call, as Gong does, notes every request, and fails the test
    on any request that is not a read."""

    def __init__(self, calls: list[dict[str, Any]], transcripts: dict[str, Any], page_size: int = 2):
        self.calls = {c["metaData"]["id"]: c for c in calls}
        self.transcripts = transcripts
        self.page_size = page_size
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.fail: dict[str, tuple[int, str]] = {}
        self.next_id = 9000000000000000000

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        assert is_read(request.method, request.url.path), f"the connector sent a write: {request.method} {request.url}"
        expected = "Basic " + base64.b64encode(b"key-1:secret-1").decode()
        if request.headers.get("Authorization") != expected:
            return httpx.Response(401, json={"errors": ["unauthorized"]})
        if request.url.path in self.fail:
            status, retry_after = self.fail[request.url.path]
            return httpx.Response(status, json={"errors": ["refused"]},
                                  headers={"Retry-After": retry_after} if retry_after else {})
        if request.headers.get("Content-Type") != "application/json":
            return httpx.Response(415, json={"errors": ["unsupported media type"]})
        filter = body.get("filter", {})
        if request.url.path == "/v2/calls/extensive":
            matched = [self._shown(c, body.get("contentSelector", {})) for c in self.calls.values()
                       if self._matches(c, filter)]
            if not matched:
                return httpx.Response(404, json={"errors": ["No calls found corresponding to the provided filters"]})
            start = int(body.get("cursor", "page-0").removeprefix("page-"))
            page = matched[start:start + self.page_size]
            more = start + self.page_size < len(matched)
            records = {"totalRecords": len(matched), **({"cursor": f"page-{start + self.page_size}"} if more else {})}
            return httpx.Response(200, json={"records": records, "calls": page})
        if request.url.path == "/v2/calls/transcript":
            found = [self.transcripts[id] for id in filter.get("callIds", []) if id in self.transcripts]
            if not found:
                return httpx.Response(404, json={"errors": ["No calls found corresponding to the provided filters"]})
            return httpx.Response(200, json={"records": {}, "callTranscripts": found})
        return httpx.Response(404, json={"errors": ["not found"]})

    @staticmethod
    def _shown(call: dict[str, Any], selector: dict[str, Any]) -> dict[str, Any]:
        """A call as Gong shows it: its parties only when asked for, and its CRM context only in the extended
        context."""
        shown = {"metaData": call["metaData"]}
        if selector.get("exposedFields", {}).get("parties") is True and "parties" in call:
            shown["parties"] = call["parties"]
        if selector.get("context") == "Extended" and "context" in call:
            shown["context"] = call["context"]
        return shown

    @staticmethod
    def _matches(call: dict[str, Any], filter: dict[str, Any]) -> bool:
        if "callIds" in filter and call["metaData"]["id"] not in filter["callIds"]:
            return False
        started = call["metaData"].get("started")
        if filter.get("fromDateTime") and started:
            return datetime.fromisoformat(started) >= datetime.fromisoformat(filter["fromDateTime"])
        return True

    def add(self, record: Record) -> str:
        """A call for a suite record, whose rendered text is the record's body: a title, and a start when it has a
        time. A record the stand-in holds is changed in place and loses its transcript."""
        id = record.id
        if id not in self.calls:
            self.next_id += 1
            id = str(self.next_id)
        meta = {"id": id, "title": record.body or ""}
        if id not in self.calls and record.at:
            meta["started"] = record.at
        self.calls[id] = {"metaData": meta}
        self.transcripts.pop(id, None)
        return id

    def remove(self, id: str) -> None:
        del self.calls[id]
        self.transcripts.pop(id, None)

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]


def gong_source(behind: str = "", **lines: Any) -> SourceFile:
    return SourceFile(name="gong", kind="gong", auth="api_key", window=Window(behind=behind), **lines)


@pytest.fixture
def time_() -> Time:
    return Time()


@pytest.fixture
def server() -> StandIn:
    return StandIn(load("calls.json"), load("transcripts.json"))


def make(server: StandIn, time_: Time, source: SourceFile | None = None, key: Callable[[], GongKey] = lambda: KEY,
         **options: Any) -> GongConnector:
    client = httpx.Client(transport=httpx.MockTransport(server.handle))
    throttle = Throttle(clock=time_.clock, sleep=time_.sleep)
    return GongConnector(source or gong_source(), http=client, credentials=key, ours=OURS, now=time_.now,
                         throttle=throttle, **options)


@pytest.fixture
def connector(server: StandIn, time_: Time) -> GongConnector:
    return make(server, time_)


def revision(call: dict[str, Any], transcript: dict[str, Any] | None = None) -> str:
    """The call with its transcript, hashed together, so a transcript that comes later shows."""
    if transcript is None:
        transcript = load("transcripts.json").get(call["metaData"]["id"])
    canonical = json.dumps([call, transcript], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@pytest.fixture
def held() -> list[Record]:
    calls = {c["metaData"]["id"]: c for c in load("calls.json")}
    return [Record(id=id, source="gong", revision=revision(calls[id]), at=want["at"], url=want["url"],
                   keys=want["keys"], links=want["links"], fields=want["fields"], body="\n".join(want["body"]),
                   extract="facts", into="calls", carried=want.get("carried", []))
            for id, want in load("expected.json").items()]


@pytest.fixture
def add(server: StandIn) -> Callable[[Record], str]:
    return server.add


@pytest.fixture
def remove(server: StandIn) -> Callable[[str], None]:
    return server.remove


# -- rendering


def test_a_party_with_no_address_is_on_no_side_and_a_namesake_with_one_keeps_their_own() -> None:
    call = {"metaData": {"id": "7784", "title": "Acme QBR", "started": "2026-10-08T15:00:00Z"}, "parties": [
        {"name": "Jane Doe", "speakerId": "1"}, {"name": "Jane Doe", "emailAddress": "jane@quillmoor.example",
                                                 "speakerId": "2"}]}
    transcript = {"transcript": [{"speakerId": "1", "sentences": [{"start": 1000, "text": "One."}]},
                                 {"speakerId": "2", "sentences": []},
                                 {"speakerId": "2", "sentences": [{"start": 2000, "text": "Two."}]},
                                 {"speakerId": "4", "sentences": [{"start": 4000, "text": "Four."}]}]}
    assert render_call(call, transcript, OURS) == ("Acme QBR\nStarted: 2026-10-08T15:00:00Z\n"
                                                   "Attendees: Jane Doe, Jane Doe <jane@quillmoor.example>\n\n"
                                                   "[0:01] Jane Doe: One.\n\n"
                                                   "[0:02] Jane Doe <jane@quillmoor.example> (our side): Two.\n\n"
                                                   "[0:04] Speaker 4: Four.")


def test_a_call_with_no_transcript_gives_its_header_alone(connector: GongConnector, server: StandIn) -> None:
    record = connector.get("1000000000000937912")
    assert record.body == "Team standup\nStarted: 2026-11-16T09:00:00+01:00\nAttendees: Sam Rivera " \
                          "<sam.rivera@quillmoor.example>, Lee Moss <lee.moss@quillmoor.example>"
    assert server.paths() == ["/v2/calls/extensive", "/v2/calls/transcript"]


def test_a_title_gong_sends_escaped_reads_as_its_characters(connector: GongConnector, server: StandIn) -> None:
    server.calls["1000000000000937912"]["metaData"]["title"] = "Thing &lt;&gt; Other &amp; co"
    record = connector.get("1000000000000937912")
    assert record.body.splitlines()[0] == record.fields["title"] == "Thing <> Other & co"


# -- read only, and nothing kept


@pytest.mark.parametrize(("method", "path", "reads"), [
    ("GET", "/v2/users", True), ("POST", "/v2/calls/extensive", True), ("post", "/v2/calls/transcript", True),
    ("POST", "/v2/calls", False), ("POST", "/v2/calls/extensive/", False), ("PUT", "/v2/calls/extensive", False),
    ("PATCH", "/v2/calls", False), ("DELETE", "/v2/calls/1000000000000937895", False),
])
def test_only_a_get_or_a_post_to_the_two_filter_paths_counts_as_a_read(method: str, path: str, reads: bool) -> None:
    assert is_read(method, path) is reads


def test_no_pass_sends_a_request_that_writes(connector: GongConnector, server: StandIn, held: list[Record]) -> None:
    cursor = every_page(connector).cursor
    for record in held:
        connector.get(record.id)
    server.remove(held[0].id)
    every_page(connector, cursor)
    with pytest.raises(RecordNotFound):
        connector.get(held[0].id)
    assert server.requests and all(method == "POST" and path in ("/v2/calls/extensive", "/v2/calls/transcript")
                                   for method, path, _ in server.requests)


def test_the_connector_keeps_no_transcript_and_its_cursor_holds_none(connector: GongConnector,
                                                                     held: list[Record]) -> None:
    cursor = every_page(connector).cursor
    assert "routing pilot" in connector.get(held[0].id).body
    assert "routing pilot" not in repr(vars(connector))
    state = zlib.decompress(base64.urlsafe_b64decode(cursor.removeprefix("gong1."))).decode()
    assert "routing" not in state and "jane.doe" not in state


# -- the window and the cursor


def test_a_listing_asks_from_the_window_and_all_asks_from_no_time(server: StandIn, time_: Time) -> None:
    every_page(make(server, time_))
    every_page(make(server, time_, gong_source(behind="30d")))
    every_page(make(server, time_, gong_source(behind="all")))
    froms = [body["filter"].get("fromDateTime") for _, path, body in server.requests
             if "cursor" not in body and path == "/v2/calls/extensive"]
    assert [f and f[:16] for f in froms] == ["2026-08-19T12:00", "2026-10-18T12:00", None]  # throttle waits move secs


def test_a_call_that_ages_out_of_the_window_is_dropped_and_never_removed(server: StandIn, time_: Time) -> None:
    connector = make(server, time_)
    cursor = every_page(connector).cursor
    time_.advance(timedelta(days=51).total_seconds())
    later = every_page(connector, cursor)
    assert later.records == [] and later.removed == []  # it still exists in gong, so its facts stay
    assert "1000000000000937895" not in connector._read_cursor(later.cursor)[0]  # and the cursor forgets it


def test_a_changed_source_file_lists_every_call_again(server: StandIn, time_: Time) -> None:
    cursor = every_page(make(server, time_)).cursor
    again = every_page(make(server, time_, gong_source(into="meetings")), cursor)
    assert len(again.records) == 3 and {r.into for r in again.records} == {"meetings"} and again.removed == []


def test_gongs_404_for_no_calls_is_an_empty_listing(time_: Time) -> None:
    listing = make(StandIn([], {}), time_).list()
    assert listing.records == [] and listing.removed == [] and not listing.more


def test_a_cursor_this_source_did_not_give_is_refused(connector: GongConnector) -> None:
    with pytest.raises(GongCursorError, match="list with no cursor to start over"):
        connector.list(since="files1.abc")


# -- the source file and the key


@pytest.mark.parametrize(("url", "allowed"), [
    ("https://api.gong.io", True), ("https://us-12345.api.gong.io", True), ("https://US-12345.API.gong.io/", True),
    ("https://us-12345.api.gong.io:8443/", True), ("http://api.gong.io", False),
    ("https://api.gong.io.evil.example", False), ("https://evilapi.gong.io", False), ("https://gong.io", False),
    ("https://sam:pw@api.gong.io", False), ("https://api.gong.io/?next=x", False), ("https://mcp.example/gong", False),
    ("https://us-12345.api.gong.io#frag", False),
])
def test_the_key_goes_to_gongs_api_and_its_regional_hosts_alone(url: str, allowed: bool) -> None:
    assert is_gong_api(url) is allowed


@pytest.mark.parametrize(("source", "says"), [
    (SourceFile(name="gong", kind="files"), "is kind files"),
    (gong_source(url="https://mcp.example/gong"), "its url at mcp.example is not gong's api"),
    (SourceFile(name="gong", kind="gong", auth="oauth"), "auth oauth is not how gong is read"),
])
def test_a_source_file_that_would_send_the_key_elsewhere_is_refused(server: StandIn, time_: Time,
                                                                    source: SourceFile, says: str) -> None:
    with pytest.raises(SourceError, match=says):
        make(server, time_, source)
    assert server.requests == []


def test_a_regional_url_is_where_every_request_goes(server: StandIn, time_: Time) -> None:
    seen = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r.url.host) or server.handle(r)))
    GongConnector(gong_source(url="https://us-12345.api.gong.io"), http=client, credentials=lambda: KEY,
                  now=time_.now, throttle=Throttle(clock=time_.clock, sleep=time_.sleep)).list()
    assert set(seen) == {"us-12345.api.gong.io"}


def test_a_missing_key_asks_nothing_and_a_refused_key_says_to_replace_it(server: StandIn, time_: Time) -> None:
    with pytest.raises(GongKeyMissing, match="no gong access key and secret are stored"):
        make(server, time_, key=lambda: GongKey("key-1", " ")).list()
    assert server.requests == []
    with pytest.raises(GongKeyRefused, match=r"status 401\): replace the access key"):
        make(server, time_, key=lambda: GongKey("key-1", "wrong")).list()


def test_an_error_reply_says_its_status_and_never_its_body(connector: GongConnector, server: StandIn) -> None:
    server.fail["/v2/calls/extensive"] = (503, "")
    with pytest.raises(GongUnavailable) as raised:
        connector.list()
    assert "status 503" in str(raised.value) and "refused" not in str(raised.value)


# -- the throttle and Gong's rate limit


@pytest.mark.parametrize(("gaps", "starts"), [
    ([0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 1000, 1000, 1000, 2000]),
    ([0, 400, 400, 400, 400], [0, 400, 800, 1200, 1600]),
    ([0, 100, 100, 100], [0, 100, 200, 1000]),
], ids=["a burst of seven", "spaced requests never wait", "a fourth waits for the first to age out"])
def test_the_throttle_lets_three_start_in_any_window(gaps: list[int], starts: list[int]) -> None:
    time_ = Time()
    throttle = Throttle(3, 1.0, clock=time_.clock, sleep=time_.sleep)
    got = []
    for gap in gaps:
        time_.advance(gap / 1000)
        throttle.wait()
        got.append(round(time_.clock() * 1000))
    assert got == starts


def test_every_request_waits_for_the_throttle(connector: GongConnector, time_: Time, held: list[Record]) -> None:
    every_page(connector)
    for record in held:
        connector.get(record.id)
    assert time_.slept == [1.1, 1.1, 1.1]  # ten requests, a page's transcripts among them: every third waits


def test_a_rate_limit_stops_with_its_wait_and_nothing_is_asked_until_it_ends(connector: GongConnector,
                                                                             server: StandIn, time_: Time) -> None:
    server.fail["/v2/calls/transcript"] = (429, "30")
    with pytest.raises(GongRateLimited, match="asks to wait 30s: list again after then") as raised:
        connector.get("1000000000000937895")
    assert raised.value.wait == 30
    asked = len(server.requests)
    with pytest.raises(GongRateLimited):
        connector.list()
    assert len(server.requests) == asked
    del server.fail["/v2/calls/transcript"]
    time_.advance(30)
    assert connector.get("1000000000000937895").body


def test_a_retry_after_date_is_the_wait_until_then_and_a_far_one_shows_a_capped_wait(server: StandIn,
                                                                                     time_: Time) -> None:
    server.fail["/v2/calls/extensive"] = (429, format_datetime(NOW + timedelta(minutes=2), usegmt=True))
    with pytest.raises(GongRateLimited, match="asks to wait 2m0s") as raised:
        make(server, time_).list()
    assert raised.value.wait == 120
    server.fail["/v2/calls/extensive"] = (429, str(10 ** 9))
    with pytest.raises(GongRateLimited, match="asks to wait 10m0s") as raised:
        make(server, time_).list()
    assert raised.value.wait == 600


def test_no_retry_after_stops_gong_reads_for_more_than_ten_minutes(server: StandIn, time_: Time) -> None:
    connector = make(server, time_)
    server.fail["/v2/calls/extensive"] = (429, "9" * 400)
    with pytest.raises(GongRateLimited, match="asks to wait 10m0s"):
        connector.list()
    del server.fail["/v2/calls/extensive"]
    time_.advance(599)
    with pytest.raises(GongRateLimited):
        connector.list()
    time_.advance(1)
    assert connector.list().records


@pytest.mark.parametrize("value", ["-30", "soon", "nan", "-inf", "Tue, 01 Jan 2002 00:00:00 GMT", "1e999",
                                   "Fri, 31 Dec 9999 23:59:59 GMT"])
def test_a_negative_unreadable_or_odd_retry_after_waits_no_more_than_ten_minutes(server: StandIn, time_: Time,
                                                                                  value: str) -> None:
    server.fail["/v2/calls/extensive"] = (429, value)
    with pytest.raises(GongRateLimited) as raised:
        make(server, time_).list()
    assert 0 <= raised.value.wait <= 600


# -- a call whose recorded reply changes


def test_a_call_gong_changes_gets_a_new_revision_and_a_call_with_no_id_is_left_out(server: StandIn, time_: Time,
                                                                                    held: list[Record]) -> None:
    connector = make(server, time_)
    cursor = every_page(connector).cursor
    call = copy.deepcopy(server.calls[held[1].id])
    call["metaData"]["title"] = "Globex intro, rescheduled"
    server.calls[held[1].id] = call
    server.calls["none"] = {"metaData": {"title": "no id"}}
    later = every_page(connector, cursor)
    assert [r.id for r in later.records] == [held[1].id]
    assert later.records[0] == replace(held[1], revision=revision(call), body=None,
                                       fields={**held[1].fields, "title": "Globex intro, rescheduled"})
    assert connector.notices == ["gong listed a call with no id: it is left out"]


# -- what Gong answers besides calls


def test_a_refused_transcript_names_the_call_and_the_way_to_a_key_that_reads_it(connector: GongConnector,
                                                                                 server: StandIn) -> None:
    server.fail["/v2/calls/transcript"] = (403, "")
    with pytest.raises(GongKeyRefused, match="gong refuses the transcript of call 1000000000000937895 to the stored "
                                             "key \\(status 403\\)"):
        connector.get("1000000000000937895")


def test_a_redirect_is_never_followed_so_the_key_goes_nowhere_else(time_: Time) -> None:
    hosts = []

    def answer(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/v2/calls/extensive"})

    client = httpx.Client(transport=httpx.MockTransport(answer))
    connector = GongConnector(gong_source(), http=client, credentials=lambda: KEY, now=time_.now,
                              throttle=Throttle(clock=time_.clock, sleep=time_.sleep))
    with pytest.raises(GongUnavailable, match="status 302"):
        connector.list()
    assert hosts == ["api.gong.io"]


def test_a_page_cursor_gong_gives_twice_ends_the_listing_with_an_error(time_: Time) -> None:
    call = load("calls.json")[0]

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"records": {"cursor": "same"}, "calls": [call]})

    client = httpx.Client(transport=httpx.MockTransport(answer))
    connector = GongConnector(gong_source(), http=client, credentials=lambda: KEY, now=time_.now,
                              throttle=Throttle(clock=time_.clock, sleep=time_.sleep))
    first = connector.list()
    assert [r.id for r in first.records] == [call["metaData"]["id"]] and first.more
    with pytest.raises(GongUnavailable, match="the same page cursor twice"):
        connector.list(first.cursor)


def test_a_page_cursor_gong_gives_twice_ends_a_question_with_an_error_after_two_reads(time_: Time) -> None:
    call, seen = load("calls.json")[0], []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={"records": {"cursor": "same"}, "calls": [call]})

    connector = GongConnector(gong_source(), http=httpx.Client(transport=httpx.MockTransport(answer)),
                              credentials=lambda: KEY, now=time_.now,
                              throttle=Throttle(clock=time_.clock, sleep=time_.sleep))
    with pytest.raises(GongUnavailable, match="the same page cursor twice"):
        connector.ask("calls")
    assert seen == ["POST", "POST"]  # the first page, then its cursor, which gives the same cursor: no third read


def test_a_call_listed_before_its_transcript_is_listed_again_when_the_transcript_comes(server: StandIn,
                                                                                       time_: Time) -> None:
    id = "1000000000000937912"  # the standup, yesterday, with no transcript yet
    kept = {"callId": id, "transcript": [{"speakerId": "s1", "sentences": [{"start": 4000, "text": "Ship it."}]}]}
    connector = make(server, time_)
    first = every_page(connector)
    assert id in [r.id for r in first.records] and "[0:" not in connector.get(id).body
    assert every_page(connector, first.cursor).records == []  # nothing new: the recent call is asked about again
    server.transcripts[id] = kept
    later = every_page(connector, first.cursor)
    assert [r.id for r in later.records] == [id] and "[0:" in connector.get(id).body


def test_an_older_call_unchanged_is_not_asked_about_again(server: StandIn, time_: Time) -> None:
    connector = make(server, time_)
    cursor = every_page(connector).cursor
    server.requests.clear()
    every_page(connector, cursor)
    asked = [body["filter"]["callIds"] for _, path, body in server.requests if path == "/v2/calls/transcript"]
    assert asked == [["1000000000000937912"]]  # the one call of the last 72 hours alone


def test_a_listing_returns_every_few_gong_pages_so_a_rate_limit_loses_little(time_: Time) -> None:
    base = load("calls.json")[0]
    calls = [copy.deepcopy(base) for _ in range(14)]
    for n, call in enumerate(calls):
        call["metaData"]["id"] = str(1000000000000937000 + n)
    server = StandIn(calls, {}, page_size=2)
    connector = make(server, time_)
    cursor = every_page(connector).cursor
    first = connector.list(since=cursor)  # nothing changed: five pages are read, then the listing returns
    assert first.records == [] and first.more
    server.fail["/v2/calls/extensive"] = (429, "30")
    with pytest.raises(GongRateLimited):
        connector.list(since=first.cursor)
    del server.fail["/v2/calls/extensive"]
    time_.advance(31)
    assert every_page(connector, first.cursor).records == []  # it goes on from page six, not page one


# -- a curator's questions
ACME_CALL, GLOBEX_CALL = "1000000000000937895", "1000000000000937901"
INITECH_CALL = {  # a call you were not on, with one outside domain and the one CRM account Gong links
    "metaData": {"id": "1000000000000937950", "started": "2026-11-10T16:00:00Z", "title": "Initech pricing"},
    "parties": [{"name": "Ana Lima", "emailAddress": "ana@initech.example", "speakerId": "41"},
                {"name": "Lee Moss", "emailAddress": "lee.moss@quillmoor.example", "speakerId": "42"}],
    "context": [{"system": "HubSpot", "objects": [{"objectType": "Account", "objectId": "hs-sample-0002"}]},
                {"system": "Salesforce", "objects": [
                    {"objectType": "Opportunity", "objectId": "006SAMPLE0009"},
                    {"objectType": "Account", "objectId": "001SAMPLE000IN1",
                     "fields": [{"name": "Industry", "value": "Software"}, {"name": "Name", "value": "Initech"}]}]}]}


def ids(connector: GongConnector, question: str, params: dict[str, Any] | None = None) -> list[str]:
    return [r.id for r in connector.ask(question, params).records]


def test_a_curator_asks_for_the_calls_with_a_domain_admits_it_and_that_domains_calls_come_into_scope(
        connector: GongConnector, server: StandIn, tmp_path: Path) -> None:
    store = MemoryStore()
    acme, globex = connector.get(ACME_CALL), connector.get(GLOBEX_CALL)
    assert bound_to(store, acme) == [] and not in_scope(store, acme, curated={"calls"})
    server.requests.clear()
    asked: list[dict[str, Any]] = []

    def script(tools: dict[str, Callable[..., Any]]) -> None:
        asked.append(tools["fetch"](source="gong", question="calls_with", params={"domain": "acme.example"}))
        tools["admit"](name="Acme Corp", keys=asked[0]["rows"][0]["keys"], reason="a call you spoke on")

    done = curate(store, Asking(script), {"gong": connector}, tmp_path)
    assert asked[0]["rows"][0] == {
        "id": "acme.example", "keys": {"domain": ["acme.example"]}, "into": "",
        "fields": {"records": 1, "noun": "calls", "took_part": 1, "how": ["attended", "spoke"],
                   "directions": "both", "first": "2026-10-08", "last": "2026-10-08"}}
    assert [r["id"] for r in asked[0]["rows"]] == ["acme.example", "vandelay.example"]
    assert done.admitted == ["accounts/acme-corp"]
    # the call's parties' domain puts it in scope with the account, and binds it to nothing: it only carries it
    assert reached(store, acme) == ["accounts/acme-corp"] and in_scope(store, acme, curated={"calls"})
    assert bound_to(store, acme) == []
    assert reached(store, globex) == [] and not in_scope(store, globex, curated={"calls"})
    assert {path for _, path, _ in server.requests} == {"/v2/calls/extensive"}  # no transcript was asked for


def test_a_call_with_one_outside_domain_names_its_crm_account_on_the_row_and_one_with_two_does_not(
        connector: GongConnector, server: StandIn) -> None:
    server.calls[INITECH_CALL["metaData"]["id"]] = INITECH_CALL
    rows = {r.id: r for r in connector.ask("calls").records}
    assert rows["initech.example"].keys == {"domain": ["initech.example"], "salesforce_id": ["001SAMPLE000IN1"]}
    assert rows["initech.example"].fields == {"name": "Initech", "records": 1, "noun": "calls", "took_part": 0,
                                              "directions": "both", "first": "2026-11-10", "last": "2026-11-10"}
    assert rows["acme.example"].keys == {"domain": ["acme.example"]}  # its call has vandelay.example too
    assert rows["globex.co.example"].fields["how"] == ["attended"]  # you were there, and said nothing
    assert rows["globex.co.example"].keys == {"domain": ["globex.co.example"]}  # a HubSpot account is no key
    assert ids(connector, "calls_with", {"account": "hs-sample-0001"}) == []
    unnamed = copy.deepcopy(INITECH_CALL)
    unnamed["context"][1]["objects"][1]["fields"] = []
    server.calls[INITECH_CALL["metaData"]["id"]] = unnamed
    row = next(r for r in connector.ask("calls").records if r.id == "initech.example")
    assert row.keys["salesforce_id"] == ["001SAMPLE000IN1"] and "name" not in row.fields


def party(email: str = "", speaker: str = "", **more: str) -> dict[str, str]:
    return {"emailAddress": email, "speakerId": speaker, **more}


def test_a_side_spoke_when_gong_gives_one_of_its_parties_a_speaker_and_a_call_with_none_tells_no_direction(
        connector: GongConnector, server: StandIn) -> None:
    calls = {"quiet": [party("ana@quiet.example"), party("lee.moss@quillmoor.example")],
             "oneway": [party("lee.moss@quillmoor.example"), party("ana@oneway.example", "1"),
                        party("bo@oneway.example", "2")],
             "ourside": [party("ana@ourside.example"), party("lee.moss@quillmoor.example", "1")],
             "byuser": [party("ana@byuser.example", "1"), party("", "2", userId="u7")],
             "byaffiliation": [party("ana@byaffiliation.example", "1"),
                               party("kim@partner.example", "2", affiliation="INTERNAL")]}
    for n, (name, parties) in enumerate(calls.items()):
        id = f"10000000000009380{n:02d}"
        server.calls[id] = {"metaData": {"id": id, "started": "2026-11-12T10:00:00Z"}, "parties": parties}
    server.calls["1000000000000938099"] = {"metaData": {"id": "1000000000000938099", "started": "2026-11-01T10:00:00Z"},
                                           "parties": [party("ana@oneway.example")]}  # listed last, held first
    rows = {r.id: r.fields for r in connector.ask("calls").records}
    assert "directions" not in rows["quiet.example"]
    assert [rows[f"{name}.example"]["directions"] for name in ("oneway", "ourside", "byuser", "byaffiliation")] == [
        "one", "one", "both", "both"]
    assert (rows["oneway.example"]["records"], rows["oneway.example"]["first"], rows["oneway.example"]["last"]) == (
        2, "2026-11-01", "2026-11-12")
    server.calls["1000000000000938098"] = {"metaData": {"id": "1000000000000938098"},
                                           "parties": [party("ana@undated.example")]}  # gong gave it no start
    undated = next(r for r in connector.ask("calls").records if r.id == "undated.example")
    assert undated.fields == {"records": 1, "noun": "calls", "took_part": 0}
    assert rows["partner.example"]["directions"] == "both"  # gong says kim is internal; the address still names one


def test_calls_puts_the_latest_first_calls_by_domain_the_most_you_took_part_in_and_took_part_me_keeps_yours(
        connector: GongConnector, server: StandIn) -> None:
    server.calls = {INITECH_CALL["metaData"]["id"]: INITECH_CALL, **server.calls}  # one not yours, listed first
    assert ids(connector, "calls") == ["initech.example", "globex.co.example", "acme.example", "vandelay.example"]
    assert ids(connector, "calls_by_domain") == ["globex.co.example", "acme.example", "vandelay.example",
                                                 "initech.example"]
    assert ids(connector, "calls", {"took_part": "me"}) == ["globex.co.example", "acme.example", "vandelay.example"]
    assert ids(connector, "calls", {"days": 30}) == ["initech.example", "globex.co.example"]
    assert ids(connector, "calls", {"days": 1}) == []  # gong answers 404 when no call started in the days
    one = connector.ask("calls", limit=1)
    assert [r.id for r in one.records] == ["initech.example"] and one.more and one.cursor is None


@pytest.mark.parametrize(("params", "want"), [
    ({"domain": "https://www.Initech.example/pricing"}, ["initech.example"]),
    ({"domain": "ana@initech.example"}, ["initech.example"]),
    ({"domain": "http://initech.example"}, ["initech.example"]),
    ({"domain": "sales.initech.example"}, ["initech.example"]),
    ({"account": "001SAMPLE000IN1"}, ["initech.example"]),
    ({"account": "initech"}, ["initech.example"]),
    ({"account": "Acme Corp"}, ["acme.example", "vandelay.example"]),
    ({"domain": "nobody.example"}, []),
])
def test_calls_with_settles_one_account_by_its_domain_or_its_crm_account(
        connector: GongConnector, server: StandIn, params: dict[str, str], want: list[str]) -> None:
    server.calls[INITECH_CALL["metaData"]["id"]] = INITECH_CALL
    assert ids(connector, "calls_with", params) == want


def test_a_question_reads_only_the_calls_listing_and_a_row_carries_no_title_transcript_or_address(
        connector: GongConnector, server: StandIn) -> None:
    server.calls[INITECH_CALL["metaData"]["id"]] = INITECH_CALL
    for question in connector.questions:
        answer = connector.ask(question, {"domain": "acme.example"} if question == "calls_with" else None)
        text = json.dumps([[r.id, r.keys, r.fields, r.url, r.links, r.into, r.body] for r in answer.records])
        assert answer.records and all(r.body is None and r.source == "gong" for r in answer.records)
        for said in ("Acme QBR", "Initech pricing", "routing pilot", "Jane Doe", "Ana Lima", "@", "gong.io"):
            assert said not in text
    assert server.requests[0][2]["filter"] == {"fromDateTime": "2026-08-19T12:00:00Z"}  # 90 days, to the second
    assert all((method, path) == ("POST", "/v2/calls/extensive") and list(body["filter"]) == ["fromDateTime"]
               for method, path, body in server.requests)  # the throttle's waits move the clock a second or two
    with pytest.raises(AssertionError, match="the connector sent a write"):  # the stand-in fails any write it sees
        server.handle(httpx.Request("POST", "https://api.gong.io/v2/calls", json={"title": "Wrong"}))


@pytest.mark.parametrize(("question", "params", "limit", "says"), [
    ("calls_with", None, 25, "calls_with takes a domain or an account"),
    ("calls_with", {"domain": "acme"}, 25, "is not a domain"),
    ("calls_with", {"domain": "acme.example?ref=1"}, 25, "is not a domain"),
    ("calls_with", {"domain": "acme .example"}, 25, "is not a domain"),
    ("calls_with", {"took_part": "me"}, 25, "takes no param 'took_part': give domain, account, days"),
    ("calls", {"took_part": "you"}, 25, "is not one it reads: say anyone or me"),
    ("calls", {"days": 120}, 25, "outside 1 to 90"),
    ("calls_by_domain", {"domain": "acme.example"}, 25, "takes no param 'domain'"),
    ("calls", None, True, "is not a count of rows"),
    ("meetings", None, 25, "ask one of calls, calls_with, calls_by_domain"),
])
def test_a_question_with_params_it_does_not_take_is_refused_before_gong_is_asked(
        connector: GongConnector, server: StandIn, question: str, params: dict[str, Any] | None, limit: Any,
        says: str) -> None:
    with pytest.raises(QuestionError) as refused:
        connector.ask(question, params, limit=limit)
    assert says in str(refused.value) and "gong source gong" in str(refused.value) and server.requests == []


def test_a_question_reads_a_bounded_number_of_pages_and_says_when_it_stopped(
        server: StandIn, time_: Time, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.connectors.gong.ASK_PAGES", 1)
    server.calls[INITECH_CALL["metaData"]["id"]] = INITECH_CALL
    answer = make(server, time_).ask("calls")
    assert len(server.requests) == 1 and answer.notices == [
        "read the first 1 pages of calls alone: ask for fewer days to reach every call"]


def test_through_fetch_a_refused_key_is_one_line_back_to_the_curator(server: StandIn, time_: Time) -> None:
    reply = fetch_tool({"gong": make(server, time_, key=lambda: GongKey("key-1", "wrong"))})(
        source="gong", question="calls")
    assert reply["refused"].startswith("gong: gong refuses the calls of the last 90 days to the stored key (status "
                                       "401)") and "key-1" not in reply["refused"]


def test_a_turns_side_in_the_call_text_follows_the_same_rule_as_the_records_own_people() -> None:
    """A party the sides rule counts as ours, by Gong's internal affiliation or a user
    id, is never labelled their side in the text the same record carries."""
    from cerebro.connector import Ours
    from cerebro.connectors.gong import render_call

    call = {"metaData": {"id": "9101", "title": "Planning", "started": "2026-11-16T15:00:00Z"},
            "parties": [{"name": "Wile Coyote", "emailAddress": "wile@farside.example", "affiliation": "Internal",
                         "speakerId": "1"},
                        {"name": "Ada Byrne", "emailAddress": "ada@contractor.example", "userId": "u-77",
                         "speakerId": "2"},
                        {"name": "Bo Price", "emailAddress": "bo@farside.example", "speakerId": "3"}]}
    transcript = {"callId": "9101", "transcript": [
        {"speakerId": "1", "sentences": [{"start": 0, "text": "We ship Friday."}]},
        {"speakerId": "2", "sentences": [{"start": 4000, "text": "I can build it."}]},
        {"speakerId": "3", "sentences": [{"start": 9000, "text": "We will pay."}]}]}
    text = render_call(call, transcript, Ours(domains=("ourco.example",)))
    assert "[0:00] Wile Coyote <wile@farside.example> (our side): We ship Friday." in text
    assert "[0:04] Ada Byrne <ada@contractor.example> (our side): I can build it." in text
    assert "[0:09] Bo Price <bo@farside.example> (their side): We will pay." in text
