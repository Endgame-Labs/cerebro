"""The Granola connector: the `Connector` suite over a stand-in for Granola's MCP server, then its reads, its
window, its folders, what it never writes, what it never keeps, and a curator's questions, which list and read
nothing more.

The stand-in answers the four tools that read, from a
synthetic account under `data/granola/`, and refuses every other tool. It records each call. Asked for the meetings
of the login's own person, it leaves out those in `others`, which the person neither captured nor sat in.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from suites.connector import *
from suites.connector import every_page
from test_fetch import Asking, curate

from cerebro.connector import Ours, QuestionError, Record, RecordNotFound
from cerebro.connectors.granola import (
    READ_TOOLS,
    CursorError,
    FolderNotFound,
    GranolaConnector,
    NotLoggedIn,
    Reply,
    ToolError,
    WriteRefused,
)
from cerebro.connectors.granola_parse import ReplyError, parse_meetings, parse_people, parse_time, parse_transcript
from cerebro.fakes import MemoryStore
from cerebro.fetch import fetch_tool
from cerebro.identity import bound_to, reached
from cerebro.source import Select, SourceError, SourceFile, Window

DATA = Path(__file__).parent / "data" / "granola"
WORLD = json.loads((DATA / "meetings.json").read_text())
NOW = datetime.fromisoformat(WORLD["now"])
# Every tool Granola's hosted server offers that changes something. The connector must never send one.
WRITE_TOOLS = ("update_private_notes", "update_meeting_title", "create_folder", "update_folder", "add_note_to_folder",
               "remove_note_from_folder", "create_meeting_note_from_transcript", "generate_summary", "give_feedback",
               "sign_out")


@dataclass
class Meeting:
    id: str
    title: str = ""
    start: str = ""
    updated: str = ""
    calendar_event_id: str = ""
    attendees: list[list[str]] = field(default_factory=list)
    summary: str = ""
    notes: str = ""
    transcript: list[list[Any]] = field(default_factory=list)
    withheld: bool = False
    folders: list[str] = field(default_factory=list)


class StandIn:
    """Granola's MCP server, as far as a check reaches it. `real` answers in the shape a real account's replies
    have: escaped brackets, decorated names and a url on each meeting. `blocks` gives transcripts as tagged blocks."""

    def __init__(self, now: Callable[[], datetime] = lambda: NOW, *, real: bool = False, blocks: bool = False):
        self.meetings = {m["id"]: Meeting(**m) for m in json.loads(json.dumps(WORLD["meetings"]))}
        self.folders = WORLD["folders"]
        self.now, self.real, self.blocks = now, real, blocks
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.replies: dict[str, Reply] = {}
        self.no_detail: set[str] = set()  # meetings get_meetings leaves out
        self.others: set[str] = set()  # meetings the login's person neither captured nor sat in

    def __call__(self, tool: str, arguments: dict[str, Any]) -> Reply:
        self.calls.append((tool, json.loads(json.dumps(arguments))))
        if tool not in READ_TOOLS:
            raise AssertionError(f"the connector sent {tool}, a tool that is not one of the four that read")
        if tool in self.replies:
            return self.replies[tool]
        return getattr(self, tool)(arguments)

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    def list_meeting_folders(self, arguments: dict[str, Any]) -> Reply:
        rows = "".join(f'<folder id="{f["id"]}" title="{html.escape(f["title"])}"/>\n' for f in self.folders)
        return Reply("<folders>\n" + rows + "</folders>")

    def _people(self, m: Meeting) -> str:
        if self.real:
            parts = [html.escape(name) + (" (note creator) from Home" if i == 0 else "") +
                     (f" &lt;{email}&gt;" if email else "") for i, (name, email) in enumerate(m.attendees)]
            return ", ".join(parts)
        return "".join(f"{html.escape(name)} &lt;{email}&gt;\n" if email else f"{html.escape(name)}\n"
                       for name, email in m.attendees)

    def _open(self, m: Meeting, detail: bool) -> str:
        row = f'<meeting id="{m.id}" title="{html.escape(m.title)}"'
        if m.start:
            row += f' date="{m.start}"'
        if m.updated:
            row += f' updated_at="{m.updated}"'
        if detail and m.calendar_event_id:
            row += f' calendar_event_id="{m.calendar_event_id}"'
        if self.real:
            row += f' url="https://notes.granola.example/d/{m.id}"'
        return row + ">\n"

    def list_meetings(self, arguments: dict[str, Any]) -> Reply:
        start = datetime.fromisoformat(arguments["custom_start"])
        start = start.replace(hour=0, minute=0, second=0)  # Granola reads a range by day
        end = datetime.fromisoformat(arguments["custom_end"])
        folder = arguments.get("folder_id", "")
        rows = []
        for m in self.meetings.values():
            if m.start and not start <= datetime.fromisoformat(m.start) <= end:
                continue
            if folder and folder not in m.folders:
                continue
            if arguments.get("involvement") and m.id in self.others:
                continue
            rows.append(self._open(m, False) + f"<known_participants>\n{self._people(m)}</known_participants>\n"
                        "</meeting>\n")
        lead = "Meetings in the range, newest first.\n" if self.real else ""
        return Reply(f'{lead}<meetings_data count="{len(rows)}">\n' + "".join(rows) + "</meetings_data>")

    def get_meetings(self, arguments: dict[str, Any]) -> Reply:
        ids = arguments["meeting_ids"]
        assert 1 <= len(ids) <= 10, "get_meetings takes 1 to 10 ids"
        rows = []
        for id in ids:
            if (m := self.meetings.get(id)) is None or id in self.no_detail:
                continue
            rows.append(self._open(m, True) + f"<known_participants>\n{self._people(m)}</known_participants>\n"
                        f"<summary>\n{html.escape(m.summary)}\n</summary>\n"
                        f"<private_notes>\n{html.escape(m.notes)}\n</private_notes>\n"
                        '<attachments>\n<attachment type="screenshot" id="s1">A slide.</attachment>\n</attachments>\n'
                        "</meeting>\n")
        return Reply(f'<meetings_data count="{len(rows)}">\n' + "".join(rows) + "</meetings_data>")

    def get_meeting_transcript(self, arguments: dict[str, Any]) -> Reply:
        m = self.meetings.get(arguments["meeting_id"])
        if m is None:
            return Reply("Meeting not found.", is_error=True)
        if m.withheld:
            return Reply("Access denied: this meeting's workspace does not permit transcript access.", is_error=True)
        lines = []
        for speaker, at, text in m.transcript:
            mark = f"{at // 60}:{at % 60:02d}"
            if self.blocks:
                lines.append(f'<block source="Microphone" speaker="{html.escape(speaker)}" start="{mark}">'
                             f"{html.escape(text)}</block>")
            else:
                lines.append(f"[{mark}] {speaker}: {text}")
        if self.blocks and lines:
            lines = ["<transcript>", *lines, "</transcript>"]
        return Reply("\n".join(lines))


def granola_source(*folders: str, behind: str = "", extract: str = "") -> SourceFile:
    return SourceFile(name="granola", kind="granola", auth="keychain: granola", extract=extract,
                      window=Window(behind=behind), select=[Select(folders=list(folders))] if folders else [])


def expected_records() -> list[Record]:
    return [Record(**r) for r in json.loads((DATA / "records.json").read_text())["records"]]


@pytest.fixture
def server() -> StandIn:
    return StandIn()


@pytest.fixture
def connector(server: StandIn) -> GranolaConnector:
    return GranolaConnector(granola_source(), server, now=lambda: NOW, page_size=2,
                            ours=Ours(domains=("quillmoor.example",)))


@pytest.fixture
def held() -> list[Record]:
    return expected_records()


@pytest.fixture
def add(server: StandIn) -> Callable[[Record], str]:
    def add(record: Record) -> str:
        """A new meeting, or a change to one: its title becomes the record's body and the rest of it is cleared, so
        the notes read back as that body alone."""
        id = record.id.removeprefix("notes:")
        server.meetings[id] = Meeting(id=id, title=record.body or "", start="" if id in server.meetings else record.at,
                                      updated=f"2026-11-17T10:{len(server.calls) % 60:02d}:00Z")
        return "notes:" + id
    return add


@pytest.fixture
def remove(server: StandIn) -> Callable[[str], None]:
    return lambda id: server.meetings.pop(id.partition(":")[2])


def test_the_notes_and_the_transcript_read_as_their_expected_text(connector: GranolaConnector) -> None:
    notes = connector.get("notes:11111111-1111-4111-8111-111111111111")
    assert "Your notes:\nSend the order form." in notes.body and "Summary:\nAcme will renew in November." in notes.body
    assert notes.revision == "granola-updated:2026-11-12T16:00:00Z"
    transcript = connector.get("granola:transcript:11111111-1111-4111-8111-111111111111")
    assert "\n[0:05] Jane Doe: We will renew the contract in November.\n" in transcript.body
    assert transcript.links == ["notes:11111111-1111-4111-8111-111111111111"]
    assert "A slide" not in notes.body + transcript.body


def test_a_record_carries_what_a_later_merge_with_another_sources_call_needs(connector: GranolaConnector) -> None:
    notes = connector.get("notes:11111111-1111-4111-8111-111111111111")
    assert notes.at == notes.fields["starts"] == "2026-11-12T15:00:00Z"
    # the event's id is a key that names the meeting, as the calendar writes it, so its recording and its event meet
    assert notes.fields["calendar_event_id"] == "cal-acme-1" and notes.keys["calendar_event_id"] == ["cal-acme-1#20261112"]
    assert notes.fields["outside"] == ["jane.doe@acme.example"]
    assert notes.keys["email"] == ["jane.doe@acme.example"] and notes.keys["domain"] == ["acme.example"]
    assert notes.carried == ["email", "domain"]  # its parties' keys point at them: they never name the meeting
    assert (notes.into, notes.extract, notes.url) == ("calls", "facts",
                                                      "https://notes.granola.ai/d/11111111-1111-4111-8111-111111111111")


def test_the_connector_sends_no_call_that_writes(server: StandIn) -> None:
    connector = GranolaConnector(granola_source("Customers"), server, now=lambda: NOW)
    for record in every_page(connector).records:
        connector.get(record.id)
    for id in ("no-such-record", "transcript:44444444-4444-4444-8444-444444444444"):
        with pytest.raises(RecordNotFound):
            connector.get(id)
    assert set(server.tools()) == set(READ_TOOLS)
    assert not set(READ_TOOLS) & set(WRITE_TOOLS)
    for tool in WRITE_TOOLS:
        with pytest.raises(WriteRefused, match=f"{tool} is not a tool that reads"):
            connector._ask(tool, {})
    assert set(server.tools()) == set(READ_TOOLS)


def test_the_connector_keeps_no_copy_of_a_meeting(connector: GranolaConnector) -> None:
    listing = every_page(connector)
    for record in listing.records:
        connector.get(record.id)
    kept = repr(vars(connector)) + str(listing.cursor)
    for words in ("renew", "Acme renewal", "Jane", "acme.example", "order form", "Globex"):
        assert words not in kept


def test_a_withheld_transcript_leaves_the_notes_alone_with_a_notice(connector: GranolaConnector) -> None:
    ids = [r.id for r in every_page(connector).records]
    assert "notes:44444444-4444-4444-8444-444444444444" in ids
    assert "transcript:44444444-4444-4444-8444-444444444444" not in ids
    with pytest.raises(RecordNotFound, match="withholds it"):
        connector.get("transcript:44444444-4444-4444-8444-444444444444")


def test_a_check_reads_only_what_changed_and_a_transcript_that_arrives_later_is_listed(server: StandIn) -> None:
    clock = [NOW]
    connector = GranolaConnector(granola_source(), server, now=lambda: clock[0])
    cursor = every_page(connector).cursor
    server.calls.clear()
    clock[0] = NOW + timedelta(hours=1)
    assert every_page(connector, cursor).records == []
    assert server.tools() == ["list_meetings", "get_meetings", "get_meeting_transcript"]  # the withheld one, recent
    server.meetings["44444444-4444-4444-8444-444444444444"].withheld = False
    server.meetings["44444444-4444-4444-8444-444444444444"].transcript = [["Tomas Alvarez", 3, "We need a review."]]
    later = every_page(connector, cursor)
    assert [r.id for r in later.records] == ["transcript:44444444-4444-4444-8444-444444444444"]
    clock[0] = NOW + timedelta(days=5)
    quiet = every_page(connector, later.cursor)
    server.calls.clear()
    assert every_page(connector, quiet.cursor).records == []
    assert server.tools() == ["list_meetings"]  # three days past the last check, an unchanged meeting is not read


def test_the_window_lists_by_start_and_a_meeting_that_ages_out_is_dropped_not_removed(server: StandIn) -> None:
    clock = [NOW]
    connector = GranolaConnector(granola_source(behind="7d"), server, now=lambda: clock[0])
    first = every_page(connector)
    assert server.calls[0] == ("list_meetings", {"time_range": "custom", "custom_start": "2026-11-10T12:00:00Z",
                                                  "custom_end": "2026-11-17T12:00:00Z"})
    assert [r.id for r in first.records] == ["notes:11111111-1111-4111-8111-111111111111",
                                             "transcript:11111111-1111-4111-8111-111111111111",
                                             "notes:44444444-4444-4444-8444-444444444444"]  # oldest first
    clock[0] = datetime(2026, 11, 20, 12, tzinfo=UTC)
    later = every_page(connector, first.cursor)
    assert later.removed == [] and later.records == []  # granola still holds it: it only left the window
    whole = every_page(GranolaConnector(granola_source(behind="all"), server, now=lambda: NOW))
    assert "notes:55555555-5555-4555-8555-555555555555" in [r.id for r in whole.records]


def test_folders_are_found_by_title_or_id_and_one_granola_does_not_hold_is_refused(server: StandIn) -> None:
    every_page(GranolaConnector(granola_source("customers", "fold-internal"), server, now=lambda: NOW))
    assert [args.get("folder_id") for tool, args in server.calls if tool == "list_meetings"] == [
        "fold-customers", "fold-internal"]
    with pytest.raises(FolderNotFound, match="names no folder Prospects in granola"):
        GranolaConnector(granola_source("Prospects"), server, now=lambda: NOW).list()


def test_a_real_accounts_replies_read_the_same(connector: GranolaConnector) -> None:
    real = StandIn(real=True, blocks=True)
    other = GranolaConnector(granola_source(), real, now=lambda: NOW, ours=Ours(domains=("quillmoor.example",)))
    for want in expected_records():
        got = other.get(want.id)
        assert (got.keys, got.at, got.revision.startswith("granola-updated:")) == (
            want.keys, want.at, want.revision.startswith("granola-updated:"))
        assert got.url == "https://notes.granola.example/d/" + want.id.partition(":")[2]


def test_a_login_that_is_gone_and_a_tool_error_stop_with_a_line_that_says_what_to_do(server: StandIn) -> None:
    connector = GranolaConnector(granola_source(), server, now=lambda: NOW)
    server.replies["list_meetings"] = Reply("Access token expired", is_error=True)
    with pytest.raises(NotLoggedIn, match="run cerebro connect granola --replace, then list again"):
        connector.list()
    server.replies["list_meetings"] = Reply("Internal error with Acme's notes", is_error=True)
    with pytest.raises(ToolError, match="list_meetings answered with an error") as error:
        connector.list()
    assert "Acme" not in str(error.value)
    server.replies["list_meetings"] = Reply("Something went sideways, try later.")
    with pytest.raises(ReplyError):
        connector.list()


def test_a_cursor_from_elsewhere_is_refused_and_a_changed_source_file_lists_every_record_anew(server: StandIn) -> None:
    cursor = every_page(GranolaConnector(granola_source(), server, now=lambda: NOW)).cursor
    again = every_page(GranolaConnector(granola_source(extract="records"), server, now=lambda: NOW), cursor)
    assert len(again.records) == 3 and {r.extract for r in again.records} == {"records"}
    with pytest.raises(CursorError, match="list with no cursor to start over"):
        GranolaConnector(granola_source(), server, now=lambda: NOW).list(since="files1.abc")
    with pytest.raises(SourceError, match="reads kind granola alone"):
        GranolaConnector(SourceFile(name="notes", kind="files"), server)


@pytest.mark.parametrize("case", json.loads((DATA / "replies.json").read_text())["cases"], ids=lambda c: c["name"])
def test_each_recorded_meetings_reply_reads_as_its_meetings_or_is_refused(case: dict[str, Any]) -> None:
    if case["want"] is None:
        with pytest.raises(ReplyError):
            parse_meetings(case["reply"])
        return
    got = [{"id": m.id, "title": m.title, "start": m.start.isoformat().replace("+00:00", "Z") if m.start else "",
            "updated": m.updated, "calendar_event_id": m.calendar_event_id,
            "people": [[p.name, p.email] for p in m.people], "summary": m.summary, "notes": m.notes}
           for m in parse_meetings(case["reply"])]
    assert got == case["want"]


@pytest.mark.parametrize("case", json.loads((DATA / "parse_people.json").read_text())["cases"], ids=lambda c: c[0])
def test_each_participants_block_reads_as_names_and_emails(case: list[Any]) -> None:
    _, block, want = case
    assert [[p.name, p.email] for p in parse_people(block)] == want


@pytest.mark.parametrize("case", json.loads((DATA / "parse_transcripts.json").read_text())["cases"],
                         ids=lambda c: c[0])
def test_each_transcript_reply_reads_as_speaker_time_and_text(case: list[Any]) -> None:
    _, reply, want = case
    assert [[t.speaker, t.at, t.text] for t in parse_transcript(reply)] == want


def test_times_read_in_the_forms_granola_writes_them() -> None:
    want = datetime(2026, 10, 7, 14, tzinfo=UTC)
    for text in ("Oct 7, 2026 2:00 PM", "Wed, Oct 7, 2026 at 2:00 pm", "2026-10-07T07:00:00.000-07:00",
                 "2026-10-07 14:00", "10/7/2026 14:00", "1791381600", "1791381600000", "Oct 7, 2026 2:00 PM UTC"):
        assert parse_time(text) == want, text
    assert parse_time("sometime last week") is None and parse_time("") is None


def test_a_pass_reads_each_meeting_once_ten_ids_a_call_however_many_pages_it_takes(server: StandIn) -> None:
    for n in range(12):
        server.meetings[f"m{n:02d}"] = Meeting(id=f"m{n:02d}", title=f"Sync {n}", start=f"2026-11-16T{n:02d}:00:00Z")
    every_page(GranolaConnector(granola_source(), server, now=lambda: NOW))
    asked = [args["meeting_ids"] for tool, args in server.calls if tool == "get_meetings"]
    assert [len(ids) for ids in asked] == [10, 4]
    clock = [NOW]
    paged = GranolaConnector(granola_source(), server, now=lambda: clock[0], page_size=3)
    cursor = every_page(paged).cursor
    clock[0] = NOW + timedelta(hours=1)
    for n in range(12):
        server.meetings[f"m{n:02d}"].updated = "2026-11-17T12:30:00Z"
    server.calls.clear()
    assert len(every_page(paged, cursor).records) == 12
    read = [id for tool, args in server.calls if tool == "get_meetings" for id in args["meeting_ids"]]
    assert sorted(read) == sorted(set(server.meetings) - {"11111111-1111-4111-8111-111111111111",
                                                          "55555555-5555-4555-8555-555555555555"})


def test_a_transcript_granola_no_longer_gives_is_removed(server: StandIn) -> None:
    connector = GranolaConnector(granola_source(), server, now=lambda: NOW)
    cursor = every_page(connector).cursor
    server.meetings["11111111-1111-4111-8111-111111111111"].withheld = True
    server.meetings["11111111-1111-4111-8111-111111111111"].updated = "2026-11-17T11:00:00Z"
    later = every_page(connector, cursor)
    assert [r.id for r in later.records] == ["notes:11111111-1111-4111-8111-111111111111"]
    assert later.removed == ["transcript:11111111-1111-4111-8111-111111111111"]
    assert ("transcript:11111111-1111-4111-8111-111111111111: the meeting's workspace withholds its transcript, so "
            "the notes are read alone") in connector.notices


def test_an_attendee_with_a_name_or_an_address_alone_and_a_turn_no_one_is_named_for_read_plainly(
        server: StandIn) -> None:
    server.meetings["m1"] = Meeting(id="m1", title="Pilot", start="2026-11-16T10:00:00Z",
                                    attendees=[["Wren Alone", ""], ["", "lee@eu.quillmoor.example"],
                                               ["Kit Bo", "kit@initech.example"]],
                                    transcript=[["", 0, "Can everyone hear me?"]])
    server.replies["get_meeting_transcript"] = Reply("Can everyone hear me?\n[0:00] Kit Bo: Yes.")
    connector = GranolaConnector(granola_source(), server, now=lambda: NOW, ours=Ours(domains=("@Quillmoor.example",)))
    transcript = connector.get("transcript:m1")
    assert transcript.body == ("Pilot\nStarted: 2026-11-16T10:00:00Z\n"
                               "Attendees: Wren Alone, lee@eu.quillmoor.example, Kit Bo <kit@initech.example>\n\n"
                               "Speaker: Can everyone hear me?\n\n[0:00] Kit Bo: Yes.")
    assert transcript.fields["outside"] == ["kit@initech.example"]


def test_a_tool_error_on_a_read_says_which_tool_and_a_batch_that_fails_in_a_listing_waits(server: StandIn) -> None:
    connector = GranolaConnector(granola_source(), server, now=lambda: NOW)
    server.replies["get_meeting_transcript"] = Reply("Meeting not found.", is_error=True)
    with pytest.raises(ToolError, match="get_meeting_transcript answered with an error"):
        connector.get("transcript:11111111-1111-4111-8111-111111111111")
    server.replies["get_meetings"] = Reply("Internal error", is_error=True)
    with pytest.raises(ToolError, match="get_meetings answered with an error"):
        connector.get("notes:11111111-1111-4111-8111-111111111111")
    listing = connector.list()
    assert listing.records == [] and connector.notices[0].startswith("get_meetings: a batch of 2 meetings")
    del server.replies["get_meetings"], server.replies["get_meeting_transcript"]
    server.no_detail.add("11111111-1111-4111-8111-111111111111")
    assert [r.id for r in connector.list(listing.cursor).records] == ["notes:44444444-4444-4444-8444-444444444444"]
    assert connector.notices[0] == ("notes:11111111-1111-4111-8111-111111111111: listed, but get_meetings gave "
                                    "nothing for it, so it waits for the next listing")


def test_a_meeting_whose_transcript_fails_waits_and_stops_no_other(server: StandIn) -> None:
    connector = GranolaConnector(granola_source(), server, now=lambda: NOW)
    server.replies["get_meeting_transcript"] = Reply("Internal error", is_error=True)
    first = every_page(connector)
    assert first.records == [] and any("get_meeting_transcript answered with an error" in n for n in first.notices)
    del server.replies["get_meeting_transcript"]
    assert {r.id.partition(":")[0] for r in every_page(connector, first.cursor).records} == {"notes", "transcript"}


def test_started_is_written_to_the_second() -> None:
    from cerebro.connectors.granola import notes_text
    from cerebro.connectors.granola_parse import Meeting as Read

    text = notes_text(Read(id="m1", title="Sync", start=datetime(2026, 11, 12, 15, 0, 0, 250000, tzinfo=UTC)))
    assert "Started: 2026-11-12T15:00:00Z" in text


# -- a curator's questions
ACME_RENEWAL, GLOBEX_REVIEW = "11111111-1111-4111-8111-111111111111", "44444444-4444-4444-8444-444444444444"
ASKED = ("list_meeting_folders", "list_meetings")  # all a question calls


def asking(server: StandIn, source: SourceFile | None = None) -> GranolaConnector:
    return GranolaConnector(source or granola_source(), server, now=lambda: NOW, ours=Ours(domains=("quillmoor.example",)))


def test_a_curator_asks_for_meetings_admits_a_domain_and_that_domains_notes_and_transcript_reach_it(
        server: StandIn, tmp_path: Path) -> None:
    connector, store = asking(server), MemoryStore()
    notes, transcript = connector.get(f"notes:{ACME_RENEWAL}"), connector.get(f"transcript:{ACME_RENEWAL}")
    server.calls.clear()
    asked: list[dict[str, Any]] = []

    def script(tools: dict[str, Callable[..., Any]]) -> None:
        asked.append(tools["fetch"](source="granola", question="meetings"))
        row = next(r for r in asked[0]["rows"] if r["id"] == "acme.example")
        tools["admit"](name="Acme", keys=row["keys"], reason="a meeting you sat in")

    done = curate(store, Asking(script), {"granola": connector}, tmp_path)
    assert asked[0]["rows"] == [
        {"id": "globex.example", "keys": {"domain": ["globex.example"]}, "into": "",
         "fields": {"records": 1, "noun": "meetings", "took_part": 1, "how": ["attended"], "first": "2026-11-15",
                    "last": "2026-11-15"}},
        {"id": "acme.example", "keys": {"domain": ["acme.example"]}, "into": "",
         "fields": {"records": 1, "noun": "meetings", "took_part": 1, "how": ["attended"], "first": "2026-11-12",
                    "last": "2026-11-12"}}]
    assert done.admitted == ["accounts/acme"]
    # the domain of its parties reaches the account, and binds the meeting to nothing
    assert reached(store, notes) == reached(store, transcript) == ["accounts/acme"]
    assert bound_to(store, notes) == bound_to(store, transcript) == []
    assert reached(store, connector.get(f"notes:{GLOBEX_REVIEW}")) == []
    assert [tool for tool, _ in server.calls[:1]] == ["list_meetings"]  # the question itself read no note


def test_a_question_lists_your_meetings_over_its_days_and_never_reads_a_note_or_a_transcript(server: StandIn) -> None:
    connector = asking(server, granola_source("Customers"))
    for question in connector.questions:
        server.calls.clear()
        connector.ask(question, {"days": 7})
        assert server.tools() == ["list_meeting_folders", "list_meetings"]
        assert server.calls[1][1] == {"time_range": "custom", "custom_start": "2026-11-10T12:00:00Z",
                                      "custom_end": "2026-11-17T12:00:00Z", "folder_id": "fold-customers",
                                      "involvement": {"captured_by_me": True, "listed_as_participant": True}}
    with pytest.raises(AssertionError, match="not one of the four that read"):
        server("update_private_notes", {"meeting_id": ACME_RENEWAL})  # the stand-in fails any write it is sent


def test_a_row_carries_no_title_note_transcript_or_address(server: StandIn) -> None:
    answer = asking(server).ask("meetings_by_domain")
    text = json.dumps([[r.id, r.keys, r.fields, r.url, r.links, r.into, r.body] for r in answer.records])
    assert answer.records and all(r.body is None and r.source == "granola" for r in answer.records)
    for meeting in server.meetings.values():
        for said in [meeting.title, meeting.summary, meeting.notes, *(t[2] for t in meeting.transcript),
                     *(name for name, _ in meeting.attendees)]:
            assert not said or said not in text
    assert "@" not in text and set(server.tools()) <= set(ASKED)


def test_meetings_by_domain_puts_the_most_meetings_first_and_a_meeting_not_yours_gives_no_row(
        server: StandIn) -> None:
    server.meetings["66666666-6666-4666-8666-666666666666"] = Meeting(
        id="66666666-6666-4666-8666-666666666666", title="Acme follow-up", start="2026-11-03T15:00:00Z",
        attendees=[["Jane Doe", "jane.doe@acme.example"]])
    server.meetings["77777777-7777-4777-8777-777777777777"] = Meeting(
        id="77777777-7777-4777-8777-777777777777", title="A colleague's call", start="2026-11-16T09:00:00Z",
        attendees=[["Ana Lima", "ana@initech.example"]])
    server.others.add("77777777-7777-4777-8777-777777777777")
    server.meetings["88888888-8888-4888-8888-888888888888"] = Meeting(
        id="88888888-8888-4888-8888-888888888888", title="Team sync", start="2026-11-16T10:00:00Z",
        attendees=[["Sam Rivera", "sam.rivera@quillmoor.example"]])  # only colleagues: no row
    connector = asking(server)
    assert [(r.id, r.fields["records"]) for r in connector.ask("meetings").records] == [
        ("globex.example", 1), ("acme.example", 2)]
    assert [r.id for r in connector.ask("meetings_by_domain").records] == ["acme.example", "globex.example"]
    assert [r.id for r in connector.ask("meetings", {"days": 4}).records] == ["globex.example"]
    one = connector.ask("meetings", limit=1)
    assert [r.id for r in one.records] == ["globex.example"] and one.more and one.cursor is None
    assert any(r.id == "notes:77777777-7777-4777-8777-777777777777" for r in every_page(connector).records)
    assert [r.id for r in connector.ask("meetings", {"days": 90}).records] == ["globex.example", "acme.example"]


def test_a_meeting_granola_gives_no_start_still_counts_and_gives_its_row_no_days(server: StandIn) -> None:
    server.meetings["99999999-9999-4999-8999-999999999999"] = Meeting(
        id="99999999-9999-4999-8999-999999999999", title="Undated", attendees=[["Ana Lima", "ana@initech.example"]])
    rows = {r.id: r.fields for r in asking(server).ask("meetings").records}
    assert rows["initech.example"] == {"records": 1, "noun": "meetings", "took_part": 1, "how": ["attended"]}
    assert list(rows) == ["globex.example", "acme.example", "initech.example"]  # an undated meeting ranks last


def test_a_rows_days_are_the_workspaces_zones_as_a_window_counts_them(server: StandIn) -> None:
    server.meetings["99999999-9999-4999-8999-999999999999"] = Meeting(
        id="99999999-9999-4999-8999-999999999999", start="2026-11-16T23:30:00Z",
        attendees=[["Ana Lima", "ana@initech.example"]])
    tokyo = GranolaConnector(granola_source(), server, now=lambda: NOW, ours=Ours(domains=("quillmoor.example",)),
                             zone=ZoneInfo("Asia/Tokyo"))
    assert {r.id: r.fields["first"] for r in asking(server).ask("meetings").records}["initech.example"] == "2026-11-16"
    assert {r.id: r.fields["first"] for r in tokyo.ask("meetings").records}["initech.example"] == "2026-11-17"


@pytest.mark.parametrize(("question", "params", "limit", "says"), [
    ("meetings", {"days": 91}, 25, "outside 1 to 90"),
    ("meetings", {"days": 0}, 25, "outside 1 to 90"),
    ("meetings_by_domain", {"days": "a while"}, 25, "is not a whole number of days"),
    ("meetings", {"folder": "Customers"}, 25, "takes no param 'folder': give days"),
    ("meetings", None, -3, "is not a count of rows"),
    ("calls_by_domain", None, 25, "ask one of meetings, meetings_by_domain"),
])
def test_a_question_with_params_it_does_not_take_is_refused_before_granola_is_asked(
        server: StandIn, question: str, params: dict[str, Any] | None, limit: int, says: str) -> None:
    with pytest.raises(QuestionError) as refused:
        asking(server).ask(question, params, limit=limit)
    assert says in str(refused.value) and "granola" in str(refused.value) and server.calls == []


def test_a_colleagues_granola_answers_no_question_and_is_not_asked(server: StandIn) -> None:
    source = SourceFile(name="granola", kind="granola", auth="keychain: granola", owner="people/casey-quinn")
    answer = asking(server, source).ask("meetings")
    assert answer.records == [] and server.calls == [] and (answer.removed, answer.more) == ([], False)
    assert answer.notices[0].startswith("granola is people/casey-quinn's granola, and only your own answers")


def test_through_fetch_an_expired_login_is_one_line_back_to_the_curator(server: StandIn) -> None:
    server.replies["list_meetings"] = Reply("401 Unauthorized: token expired", is_error=True)
    reply = fetch_tool({"granola": asking(server)})(source="granola", question="meetings")
    assert reply["refused"] == ("granola: the granola login is gone or expired: run cerebro connect granola "
                                "--replace, then list again")
