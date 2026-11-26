"""The Gmail connector: the `Connector` suite over a stand-in IMAP session, the mail parser's recorded edge cases, and
what it reads, skips, keys, renumbers and never sends."""

from __future__ import annotations

import hashlib
import imaplib
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from gmail_standin import Mail, StandIn
from suites.connector import *
from suites.connector import every_page

from cerebro.connector import Ours, QuestionError, Record, RecordNotFound
from cerebro.connectors.gmail import GmailConnector, GmailCursorError
from cerebro.connectors.gmail_imap import GmailError, ReadOnlyError, Session, gmail_login, tokens
from cerebro.connectors.gmail_text import (
    cut,
    decode_body,
    html_to_text,
    is_public_mail,
    parse_addresses,
    parse_headers,
    reduce,
)
from cerebro.source import SourceError, SourceFile, Window

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
DATA = Path(__file__).parent / "data" / "gmail"
OWN = Ours(("sam.rivera@quillmoor.example",), ("quillmoor.example",))
SAM = "Sam Rivera <sam.rivera@quillmoor.example>"
PRIYA = "Priya Natarajan <priya@acme.example>"

MAILS = {
    101: (9001, Mail(PRIYA, "Friday call", to=SAM, date=datetime(2026, 11, 10, 9, 14, tzinfo=UTC),
                     text="Sam, confirming the call is on for 3pm Friday.\n\nThanks,\nPriya\n\n"
                          "On Mon, Nov 9, 2026 at 9:14 AM Sam Rivera <sam.rivera@quillmoor.example> wrote:\n"
                          "> Priya, are you free Friday?\n")),
    102: (9001, Mail(SAM, "Re: Friday call", to=PRIYA, cc="Dana <bt-example@gmail.com>",
                     date=datetime(2026, 11, 11, 16, 0, tzinfo=UTC),
                     html="<div>Great, <b>3pm</b> it is.</div><blockquote>Sam, confirming the call</blockquote>")),
    103: (9003, Mail("Raj Mehta <raj@eu.globex.example>", "Contract", to=SAM, cc="morgan.lee@quillmoor.example",
                     date=datetime(2026, 11, 12, 8, 30, tzinfo=UTC), text="The signed contract is attached.\n",
                     attachment="%PDF-1.7 not text", text_file="Inline notes, not the message.")),
}
BODIES = {101: "Sam, confirming the call is on for 3pm Friday.", 102: "Great, 3pm it is.",
          103: "The signed contract is attached."}
OTHERS = {
    104: Mail("News <news@letters.example>", "Weekly news", to=SAM, date=datetime(2026, 11, 13, tzinfo=UTC),
              text="Read all about it.", headers=["List-Unsubscribe: <mailto:out@letters.example>"]),
    105: Mail(PRIYA, "Old thread", to=SAM, date=datetime(2026, 6, 1, tzinfo=UTC), text="From before the window."),
}


def gmail_source(behind: str = "", **settings) -> SourceFile:
    return SourceFile(name="gmail", kind="gmail", window=Window(behind=behind), owner="me", **settings)


def revision(standin: StandIn, msgid: int) -> str:
    """The revision a message should carry: a hash of its size and the header fields read of it."""
    stored = next(m for m in standin.messages if m.msgid == msgid)
    head = stored.raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    names = (b"from:", b"to:", b"cc:", b"date:", b"subject:")
    block = b"".join(line + b"\r\n" for line in head if line.lower().startswith(names)) + b"\r\n"
    return "sha256:" + hashlib.sha256(f"{len(stored.raw)}\n".encode() + block).hexdigest()


def words(record: Record) -> str:
    """What a message's sender newly wrote: its body after the header lines and before the closing newline."""
    return record.body.partition("\n\n")[2].removesuffix("\n")


def expected(standin: StandIn, msgid: int, keys: dict[str, list[str]], headers: str,
             ours: tuple[str, ...] = ("Sam Rivera",)) -> Record:
    thread, mail = MAILS[msgid]
    return Record(id=str(msgid), source="gmail", revision=revision(standin, msgid),
                  at=mail.date.isoformat().replace("+00:00", "Z"), url=f"https://mail.google.com/mail/u/0/#all/{msgid:x}",
                  keys=keys, links=[f"thread:{thread}"], body=f"{headers}\n{BODIES[msgid]}\n", extract="facts",
                  carried=list(keys),
                  fields={"thread": str(thread), "from": re.search(r"<?([^<>\s]+@[^<>\s]+?)>?$", mail.sender).group(1),
                          "subject": mail.subject} | ({"ours": list(ours)} if ours else {}))


@pytest.fixture
def standin() -> StandIn:
    box = StandIn()
    for msgid in sorted(MAILS):
        box.deliver(MAILS[msgid][1], msgid, MAILS[msgid][0])
    for msgid, mail in OTHERS.items():
        box.deliver(mail, msgid)
    return box


@pytest.fixture
def held(standin: StandIn) -> list[Record]:
    return [
        expected(standin, 101, {"email": ["priya@acme.example"], "domain": ["acme.example"]},
                 f"From: {PRIYA}\nTo: {SAM}\nDate: Tue, 10 Nov 2026 09:14:00 +0000\nSubject: Friday call\n"),
        expected(standin, 102, {"email": ["bt-example@gmail.com", "priya@acme.example"], "domain": ["acme.example"]},
                 f"From: {SAM}\nTo: {PRIYA}\nCc: Dana <bt-example@gmail.com>\nDate: Wed, 11 Nov 2026 16:00:00 +0000\n"
                 f"Subject: Re: Friday call\n"),
        expected(standin, 103, {"email": ["raj@eu.globex.example"], "domain": ["globex.example"]},
                 "From: Raj Mehta <raj@eu.globex.example>\nTo: Sam Rivera <sam.rivera@quillmoor.example>\n"
                 "Cc: morgan.lee@quillmoor.example\nDate: Thu, 12 Nov 2026 08:30:00 +0000\nSubject: Contract\n",
                 ("Sam Rivera", "morgan.lee@quillmoor.example")),
    ]


def make(standin: StandIn, source: SourceFile | None = None, **options) -> GmailConnector:
    return GmailConnector(source or gmail_source(), lambda: standin, ours=OWN, now=lambda: NOW, **options)


@pytest.fixture
def connector(standin: StandIn) -> GmailConnector:
    return make(standin, page_size=2)


@pytest.fixture
def add(standin: StandIn) -> Callable[[Record], str]:
    def add(record: Record) -> str:
        if record.id.isdigit() and int(record.id) in MAILS:
            thread, mail = MAILS[int(record.id)]
            standin.expunge_message(int(record.id))
            mail = Mail(mail.sender, mail.subject, to=mail.to, date=mail.date, text=record.body or "")
            standin.deliver(mail, int(record.id), thread)
            return record.id
        msgid = 200 + len(standin.messages)
        when = datetime.fromisoformat(record.at) if record.at else NOW
        standin.deliver(Mail(PRIYA, f"New {record.id}", to=SAM, date=when, text=record.body or ""), msgid)
        return str(msgid)
    return add


@pytest.fixture
def remove(standin: StandIn) -> Callable[[str], None]:
    return lambda id: standin.expunge_message(int(id))


# -- the mail parser's recorded cases

@pytest.mark.parametrize("case", sorted(p.stem for p in DATA.glob("*.in")))
def test_a_message_reduces_to_what_its_sender_newly_wrote(case: str) -> None:
    text = (DATA / f"{case}.in").read_text()
    assert reduce(text) + "\n" == (DATA / f"{case}.reduced.golden").read_text()


@pytest.mark.parametrize("case", sorted(p.stem for p in DATA.glob("*.html")))
def test_an_html_message_turns_into_text_and_then_reduces(case: str) -> None:
    text = html_to_text((DATA / f"{case}.html").read_text())
    assert text + "\n" == (DATA / f"{case}.text.golden").read_text()
    assert reduce(text) + "\n" == (DATA / f"{case}.reduced.golden").read_text()


EDGE_CASES = json.loads((DATA / "edge-cases.json").read_text())


@pytest.mark.parametrize(("text", "want"), EDGE_CASES["reduce"])
def test_quotes_forwards_and_signatures_reduce_as_these_recorded_edges_expect(text: str,
                                                                                              want: str) -> None:
    assert reduce(text) == want


@pytest.mark.parametrize(("markup", "text", "reduced"), EDGE_CASES["html"])
def test_html_edges_turn_into_their_expected_text(markup: str, text: str, reduced: str) -> None:
    assert (html_to_text(markup), reduce(html_to_text(markup))) == (text, reduced)


@pytest.mark.parametrize(("given", "text", "was_cut"), EDGE_CASES["cut"])
def test_a_cut_ends_at_a_character_boundary_and_a_late_line_break(given: list, text: str,
                                                                                        was_cut: bool) -> None:
    assert cut(given[0], int(given[1])) == (text, was_cut)


@pytest.mark.parametrize(("header", "bulk"), [
    ("From: a@x.example\r\n", False), ("From: noreply@x.example\r\nTo: b@y.example\r\n", False),
    ("List-Unsubscribe: <mailto:u@x.example>\r\n", True), ("List-Id: <news.x.example>\r\n", True),
    ("Precedence: bulk\r\n", True), ("Precedence: List\r\n", True), ("Precedence: junk\r\n", True),
    ("Precedence: first-class\r\n", False), ("Auto-Submitted: auto-generated\r\n", True),
    ("Auto-Submitted: no\r\n", False)])
def test_bulk_mail_is_known_by_its_headers_and_never_by_its_sender(header: str, bulk: bool) -> None:
    assert parse_headers(header.encode()).bulk() is bulk


@pytest.mark.parametrize(("body", "encoding", "charset", "want"), [
    (b"caf=C3=A9 =\r\nau lait", "quoted-printable", "utf-8", "café au lait"),
    (b"caf=E9", "quoted-printable", "iso-8859-1", "café"), (b"Y2Fmw6k=", "base64", "utf-8", "café"),
    (b"Y2Fmw6", "base64", "utf-8", "caf\ufffd"), (b"\x93hi\x94 \x81", "7bit", "windows-1252", "“hi” \x81"),
    (b"plain \xff\xfe end", "7bit", "x-unknown", "plain � end"), (b"caf\xe9", "8bit", "", "caf�"),
    (b"caf\xe9", "8bit", "Latin1", "café")])
def test_a_body_decodes_its_transfer_encoding_then_its_charset(body: bytes, encoding: str, charset: str,
                                                               want: str) -> None:
    assert decode_body(body, encoding, charset) == want


def test_an_address_lowers_its_ascii_letters_alone_and_a_public_mail_domain_is_known() -> None:
    assert [a.email for a in parse_addresses("Lee <LEE@Quillmoor.Example>, Mal <MAL@quİllmoor.example>")] == [
        "lee@quillmoor.example", "mal@quİllmoor.example"]
    assert [is_public_mail(e) for e in ("a-example@gmail.com", "a-example@mail.yahoo.co.uk", "a@acme.example", "nobody")] == [
        True, True, False, False]


def test_the_recorded_cases_are_synthetic_with_every_address_and_site_at_an_example_domain() -> None:
    text = " ".join(p.read_text() for p in DATA.iterdir())
    hosts = re.findall(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)|https?://([A-Za-z0-9.-]+)", text)
    assert hosts and all((a or b).endswith(".example") for a, b in hosts)


def test_headers_decode_names_and_subjects_and_skip_an_address_that_does_not_read() -> None:
    headers = parse_headers(b"From: =?utf-8?q?Ra=C3=BAl_Pe=C3=B1a?= <Raul.Pena@Globex.Example>\r\n"
                            b"To: sam@quillmoor.example, \"Rivera, Sam\" <sam.rivera@quillmoor.example>, broken\r\n"
                            b"Subject: =?iso-8859-1?q?Pr=E9sentation?= Q4\r\nDate: Mon, 12 Oct 2026 09:14:00 -0700\r\n")
    assert (headers.from_[0].name, headers.from_[0].email) == ("Raúl Peña", "raul.pena@globex.example")
    assert [a.name for a in headers.to] == ["", "Rivera, Sam"] and headers.subject == "Présentation Q4"
    assert headers.date == datetime(2026, 10, 12, 16, 14, tzinfo=UTC)


# -- the connector

def test_bulk_mail_and_mail_older_than_the_window_are_no_records(standin: StandIn) -> None:
    listed = [r.id for r in every_page(make(standin)).records]
    assert listed == ["101", "102", "103"]
    for id in ("104", "0101", "18446744073709551616"):
        with pytest.raises(RecordNotFound):
            make(standin).get(id)
    assert [r.id for r in every_page(make(standin, gmail_source(behind="all"))).records] == ["101", "102", "103",
                                                                                              "105"]


def test_keys_are_the_correspondents_and_never_yours_your_companys_or_a_public_mail_domain(standin: StandIn) -> None:
    keys = [r.keys for r in every_page(make(standin)).records]
    flat = [value for k in keys for values in k.values() for value in values]
    assert "priya@acme.example" in flat and "bt-example@gmail.com" in flat
    assert not any("quillmoor" in value or value == "gmail.com" for value in flat)
    everyone = GmailConnector(gmail_source(), lambda: standin, now=lambda: NOW)
    assert "sam.rivera@quillmoor.example" in everyone.get("101").keys["email"]


def test_your_own_address_at_a_public_service_and_a_colleague_at_a_subdomain_are_never_keys(
        standin: StandIn) -> None:
    standin.deliver(Mail("Sam <So-example@gmail.com>", "Personal", to="ops@eu.quillmoor.example, raj@globex.example",
                         date=NOW, text="x"), 309)
    connector = GmailConnector(gmail_source(), lambda: standin, ours=Ours(("so-example@gmail.com",), (" @quillmoor.example ",)),
                               now=lambda: NOW)
    assert connector.get("309").keys == {"email": ["raj@globex.example"], "domain": ["globex.example"]}


def test_get_reads_the_text_part_alone_and_never_fetches_an_attachment(standin: StandIn) -> None:
    connector = make(standin)
    assert words(connector.get("103")) == "The signed contract is attached."
    fetched = [c for c in standin.commands if "BODY.PEEK[" in c and "HEADER" not in c]
    assert fetched == ["UID FETCH 3 (UID BODY.PEEK[2]<0.1048576>)"]


def test_plain_text_comes_before_html_and_a_latin1_body_reads(standin: StandIn) -> None:
    standin.deliver(Mail(PRIYA, "Both", date=NOW, text="Plain wins.\n", html="<p>HTML loses.</p>"), 301)
    standin.deliver(Mail(PRIYA, "Latin", date=NOW, text="Café at noon.\n", charset="iso-8859-1"), 302)
    standin.deliver(Mail(PRIYA, "Encoded", date=NOW, text="Sent as base64.\n", encoding="base64"), 303)
    connector = make(standin)
    assert [words(connector.get(id)) for id in ("301", "302", "303")] == ["Plain wins.", "Café at noon.",
                                                                        "Sent as base64."]


def test_a_text_part_with_no_new_words_gives_way_to_the_next_one(standin: StandIn) -> None:
    standin.deliver(Mail(PRIYA, "Signed", date=NOW, text="Thanks,\nPriya\n", html="<p>New words.</p>"), 305)
    standin.deliver(Mail(PRIYA, "Nothing", date=NOW, attachment="%PDF-1.7"), 306)
    assert [words(make(standin).get(id)) for id in ("305", "306")] == ["New words.", ""]


def test_a_records_time_is_its_date_line_and_when_it_arrived_when_it_has_none(standin: StandIn) -> None:
    arrived = datetime(2026, 11, 16, 7, 5, tzinfo=UTC)
    standin.deliver(Mail(PRIYA, "Dated", date=datetime(2026, 11, 14, 10, tzinfo=UTC), text="x"), 307, arrived=arrived)
    standin.deliver(Mail(PRIYA, "Undated", text="x"), 308, arrived=arrived)
    connector = make(standin)
    assert [connector.get(id).at for id in ("307", "308")] == ["2026-11-14T10:00:00Z", "2026-11-16T07:05:00Z"]
    assert "Date: Mon, 16 Nov 2026 07:05:00 +0000\n" in connector.get("308").body


def test_bulk_mail_deleted_at_gmail_is_never_listed_as_removed(standin: StandIn) -> None:
    connector = make(standin)
    cursor = every_page(connector).cursor
    standin.expunge_message(104)
    assert every_page(connector, cursor).removed == []


def test_a_long_message_is_cut_and_its_text_says_so(standin: StandIn) -> None:
    standin.deliver(Mail(PRIYA, "Long", date=NOW, text=("A line of the long message.\n" * 9000)), 304)
    body = words(make(standin).get("304"))
    assert body.endswith("\n\n[The rest of this message was cut at 200 KB of text.]")
    assert len(body.encode()) <= 200 * 1024 + 60


def test_a_body_is_the_header_lines_then_the_new_words(
        standin: StandIn, held: list[Record]) -> None:
    assert make(standin).get("101").body == (
        f"From: {PRIYA}\nTo: {SAM}\nDate: Tue, 10 Nov 2026 09:14:00 +0000\nSubject: Friday call\n\n"
        "Sam, confirming the call is on for 3pm Friday.\n")


def test_the_connector_never_sends_a_command_that_changes_the_mailbox_or_marks_mail_read(
        standin: StandIn, held: list[Record]) -> None:
    connector = make(standin, page_size=2)
    cursor = every_page(connector).cursor
    for record in held:
        connector.get(record.id)
    standin.expunge_message(101)
    standin.renumber()
    every_page(connector, cursor)
    connector.close()
    allowed = re.compile(r'^(LIST "" \*|EXAMINE "\[Gmail\]/All Mail"|LOGOUT|UID SEARCH .+'
                         r"|UID FETCH [0-9:,]+ \((?:UID|X-GM-MSGID|X-GM-THRID|INTERNALDATE|RFC822\.SIZE|BODYSTRUCTURE"
                         r"|BODY\.PEEK\[[^\]]*\](?:<[0-9.]+>)?| )+\))$")
    assert standin.commands and [c for c in standin.commands if not allowed.match(c)] == []
    assert all(not m.flags for m in standin.messages)


def test_the_read_only_gate_refuses_a_fetch_that_would_mark_mail_read_before_it_is_sent(standin: StandIn) -> None:
    session = Session(standin)
    for items in ("(BODY[TEXT])", "(RFC822)", "(UID FLAGS) +FLAGS (\\Seen)"):
        with pytest.raises(ReadOnlyError, match="reads only"):
            session.fetch([1], items)
    with pytest.raises(ReadOnlyError):
        session.search("UNSEEN) STORE 1 +FLAGS (\\Deleted")
    assert standin.commands == []


def test_a_message_deleted_at_gmail_is_listed_as_removed_and_one_that_aged_out_is_not(standin: StandIn) -> None:
    clock = [NOW]
    connector = GmailConnector(gmail_source(behind="6d"), lambda: standin, ours=OWN, now=lambda: clock[0])
    first = every_page(connector)
    assert [r.id for r in first.records] == ["102", "103"]
    clock[0] = datetime(2026, 11, 18, 12, tzinfo=UTC)
    standin.expunge_message(103)
    later = every_page(connector, first.cursor)
    assert later.records == [] and later.removed == ["103"]  # 102 is still in the mailbox: it only left the window


def test_when_gmail_renumbers_the_mailbox_the_window_is_listed_again_under_the_same_ids(standin: StandIn,
                                                                                        held: list[Record]) -> None:
    connector = make(standin)
    first = every_page(connector)
    standin.expunge_message(102)
    standin.renumber()
    again = every_page(connector, first.cursor)
    assert [(r.id, r.revision) for r in again.records] == [(held[0].id, held[0].revision),
                                                            (held[2].id, held[2].revision)]
    assert again.removed == ["102"]
    assert every_page(connector, again.cursor).records == []


def test_a_dropped_connection_is_opened_again_once(standin: StandIn) -> None:
    opened = []

    def open_session() -> StandIn:
        opened.append(1)
        return standin

    connector = GmailConnector(gmail_source(), open_session, ours=OWN, now=lambda: NOW)
    connector.list()
    original = standin.uid
    calls = []

    def drop_once(command: str, *args: str):
        if not calls:
            calls.append(command)
            raise imaplib.IMAP4.abort("socket error: EOF")
        return original(command, *args)

    standin.uid = drop_once
    assert words(connector.get("101")) == BODIES[101] and len(opened) == 2


def test_gmail_refusing_a_command_is_one_line_that_says_what_to_do(standin: StandIn) -> None:
    standin.refuse_next("SEARCH", "[THROTTLED] Account exceeded command or bandwidth limits.")
    with pytest.raises(GmailError, match="google is limiting this mailbox's imap use: list again later"):
        make(standin).list()
    with pytest.raises(GmailError, match="imap shows no all mail folder"):
        make(StandIn(all_mail=False)).list()


def test_a_source_file_of_another_kind_is_refused() -> None:
    with pytest.raises(SourceError, match="kind gmail alone"):
        GmailConnector(SourceFile(name="notes", kind="files"), lambda: None)


def test_a_cursor_from_elsewhere_is_refused_and_a_changed_source_file_lists_every_record_anew(
        standin: StandIn) -> None:
    cursor = every_page(make(standin)).cursor
    with pytest.raises(GmailCursorError, match="list with no cursor to start over"):
        make(standin).list(since="files1.abc")
    again = every_page(make(standin, gmail_source(extract="records")), cursor)
    assert [(r.id, r.extract) for r in again.records] == [("101", "records"), ("102", "records"), ("103", "records")]


def test_the_connector_keeps_no_copy_of_a_messages_text(standin: StandIn, held: list[Record]) -> None:
    connector = make(standin)
    listing = every_page(connector)
    for record in held:
        connector.get(record.id)
    kept = repr(vars(connector)) + repr(vars(connector._session)) + str(listing.cursor)
    assert not any(word in kept for word in ("confirming", "Friday", "contract", "acme", "priya"))


def test_the_login_goes_to_gmail_alone_and_a_refusal_never_repeats_the_password() -> None:
    reached = []

    class Refusing:
        def __init__(self, host: str):
            reached.append(host)

        def login(self, user: str, password: str) -> None:
            raise imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] Invalid credentials for {password}")

    with pytest.raises(GmailError, match="make a new app password") as refused:
        gmail_login("sam.rivera@quillmoor.example", "abcd efgh", connect=Refusing)()
    assert reached == ["imap.gmail.com"] and "abcd efgh" not in str(refused.value)


def test_a_fetch_reply_with_literals_reads_into_nested_tokens() -> None:
    reply = [(b'1 (UID 5 X-GM-MSGID 9 BODY[HEADER.FIELDS (FROM)] {20}', b"From: a@b.example\r\n\r\n"), b")",
             b'2 (UID 6 FLAGS (\\Seen) INTERNALDATE "17-Nov-2026 09:00:00 +0000")']
    assert tokens(reply) == ["1", ["UID", "5", "X-GM-MSGID", "9", "BODY[HEADER.FIELDS (FROM)]",
                                   tokens([(b"{20}", b"From: a@b.example\r\n\r\n")])[0]],
                             "2", ["UID", "6", "FLAGS", ["\\Seen"], "INTERNALDATE", "17-Nov-2026 09:00:00 +0000"]]


def test_a_separator_line_is_found_and_a_long_line_of_dashes_is_read_in_linear_time() -> None:
    assert reduce("Thanks, see you then.\n\n-----Original Message-----\nFrom: x\nold text") == "Thanks, see you then."
    started = time.perf_counter()
    reduce("- " * 20000 + "x")
    reduce("a<tel:1>" * 10000)
    assert time.perf_counter() - started < 0.5  # a backtracking pattern would take seconds on a line this long


def test_a_body_structure_nested_past_the_bound_is_refused_with_one_line() -> None:
    from cerebro.connectors.gmail_imap import GmailError, parse_structure

    leaf: list = ["text", "plain", None, None, None, "7bit", "5"]
    token = leaf
    for _ in range(3000):
        token = [token, "mixed"]
    with pytest.raises(GmailError, match="nested more than 64 parts deep: the message is not read"):
        parse_structure(token)
    assert parse_structure([[leaf, "alternative"], "mixed"]).children[0].children[0].path == "1.1"


# -- a curator's question: threads_by_domain

LEE, MIA, RAJ = "Lee Park <lee@initech.example>", "Mia Chen <mia@northwind.example>", "Raj Mehta <raj@globex.example>"


def on(day: int, month: int = 11) -> datetime:
    return datetime(2026, month, day, 9, 0, tzinfo=UTC)


# Each thread by its Gmail thread id: its messages in order, each with its sender, who it went to, and its day.
THREADS = {
    500: [(PRIYA, SAM, on(1)), (SAM, PRIYA, on(2)), (PRIYA, SAM, on(3)),
          ("Acme Calendar <calendar@acme.example>", SAM, on(3)),  # a machine's message: no turn, and the run goes on
          (PRIYA, SAM, on(4)), (SAM, PRIYA, on(5)), (PRIYA, f"{SAM}, {LEE}", on(6))],  # Initech only copied
    501: [(PRIYA, SAM, on(8))],
    502: [(MIA, f"{SAM}, Ari <ari@partner.example>", on(9)), (SAM, MIA, on(10)), (MIA, SAM, on(11))],
    503: [("Billing <noreply@billing.vendor.example>", SAM, on(12))],
    508: [("Ola <ola@vendor.example>", SAM, on(12))],  # a person at the vendor too
    505: [("A Friend <friend-example@gmail.com>", SAM, on(14))],
    506: [(RAJ, SAM, on(20, 9)), (SAM, RAJ, on(21, 9))],  # 57 days back
    507: [("morgan.lee@quillmoor.example", SAM, on(15))],
}
BULK = Mail("News <news@letters.example>", "Weekly", to=SAM, date=on(13), text="Read all about it.",
            headers=["List-Unsubscribe: <mailto:out@letters.example>"])


@pytest.fixture
def mailbox() -> StandIn:
    box = StandIn()
    for thread, messages in THREADS.items():
        for n, (sender, to, when) in enumerate(messages):
            box.deliver(Mail(sender, f"Thread {thread}", to=to, date=when, text="Words never read by an ask."),
                        thread * 10 + n, thread)
    box.deliver(BULK, 5040, 504)
    return box


def rows(connector: GmailConnector, **params: object) -> list[tuple[str, dict]]:
    return [(r.id, r.fields) for r in connector.ask("threads_by_domain", params).records]


def test_threads_by_domain_counts_each_sides_turns_and_ranks_the_most_turns_each_way_first(mailbox: StandIn) -> None:
    answer = make(mailbox).ask("threads_by_domain")
    assert [r.id for r in answer.records] == ["acme.example", "northwind.example", "globex.example",
                                              "partner.example", "initech.example", "vendor.example"]
    assert answer.records[0].fields == {
        "threads": 2, "replied": 1, "directions": "both", "first": "2026-11-01", "last": "2026-11-08",
        "messages": 8, "sender": "person", "turns": [{"ours": 2, "theirs": 3}], "both_ways": 1}
    assert all(r.keys == {"domain": [r.id]} and r.body is None and r.source == "gmail" and r.revision == ""
               for r in answer.records)
    assert answer.cursor is None and not answer.more and answer.notices == []


def test_a_domain_only_copied_gets_the_thread_without_its_turns_and_a_machine_begins_none(mailbox: StandIn) -> None:
    found = dict(rows(make(mailbox)))
    assert found["initech.example"] == {"threads": 1, "replied": 1, "directions": "both", "first": "2026-11-01",
                                        "last": "2026-11-06", "messages": 7, "sender": "person"}
    assert found["vendor.example"] == {"threads": 2, "replied": 0, "directions": "one", "first": "2026-11-12",
                                       "last": "2026-11-12", "messages": 2, "sender": "person and machine"}
    assert found["partner.example"]["messages"] == 3  # copied on the first message alone, and so on the thread
    assert found["northwind.example"]["turns"] == [{"ours": 1, "theirs": 2}]
    # bulk mail is no message, a public mail service and your own company are never a row
    assert not {"letters.example", "gmail.com", "quillmoor.example"} & set(found)


def test_days_bounds_the_threads_and_a_limit_says_more_remain(mailbox: StandIn) -> None:
    connector = make(mailbox)
    assert "globex.example" not in dict(rows(connector, days=30))
    assert "globex.example" in dict(rows(connector, days="90"))
    one = connector.ask("threads_by_domain", limit=1)
    assert [r.id for r in one.records] == ["acme.example"] and one.more
    every = connector.ask("threads_by_domain", limit=6)
    assert len(every.records) == 6 and (every.more, every.removed) == (False, [])
    assert len(connector.ask("threads_by_domain", {"days": 1}).records) == 0  # nothing since yesterday began


@pytest.mark.parametrize(("params", "limit", "says"), [
    ({"days": 91}, 25, "threads_by_domain days 91 is outside 1 to 90"),
    ({"days": 0}, 25, "threads_by_domain days 0 is outside 1 to 90"),
    ({"days": "ninety"}, 25, "days 'ninety' is not a whole number of days"),
    ({"days": True}, 25, "days 'True' is not a whole number of days"),
    ({"query": "from:anyone"}, 25, "threads_by_domain takes no param 'query': give days"),
    ({}, 0, "limit 0 is not a count of rows"),
    ({}, True, "limit True is not a count of rows"),
])
def test_params_are_checked_before_gmail_is_asked_anything(mailbox: StandIn, params: dict, limit: int,
                                                           says: str) -> None:
    with pytest.raises(QuestionError, match=re.escape(says)) as refused:
        make(mailbox).ask("threads_by_domain", params, limit=limit)
    assert str(refused.value).startswith("gmail source gmail: ") and mailbox.commands == []


def test_a_colleagues_mailbox_answers_with_no_rows_and_a_line_and_asks_gmail_nothing(mailbox: StandIn) -> None:
    theirs = SourceFile(name="morgan-mail", kind="gmail", owner="morgan.lee@quillmoor.example")
    answer = make(mailbox, theirs).ask("threads_by_domain")
    assert (answer.records, answer.cursor, answer.removed, answer.more) == ([], None, [], False)
    assert mailbox.commands == []
    assert answer.notices == [("gmail source morgan-mail: is a colleague's mailbox, and only your own feeds admission: "
                              "ask the gmail source whose owner is me")]


def test_an_ask_past_the_messages_it_reads_reads_the_newest_and_says_so(mailbox: StandIn,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connectors import gmail

    monkeypatch.setattr(gmail, "ASK_MESSAGES", 3)
    answer = make(mailbox).ask("threads_by_domain")
    assert answer.notices == [("gmail source gmail: read the newest 3 of the 18 messages of the last 90 days: ask "
                              "for fewer days to count every one")]
    assert [r.id for r in answer.records] == ["globex.example"]  # the last three to arrive: Sam's reply to Raj,
    # a colleague's note and bulk mail, whatever their dates


def test_the_last_days_begin_at_midnight_by_each_messages_date_line_and_an_ask_at_its_bound_says_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connectors import gmail

    box = StandIn()
    box.deliver(BULK, 1, 1)  # bulk mail first, which a fetch skips and reads on past
    box.deliver(Mail("Eve <eve@early.example>", "Late", to=SAM, date=datetime(2026, 10, 17, 23, 59, tzinfo=UTC),
                     text="x"), 2, 2, arrived=datetime(2026, 10, 18, 8, 0, tzinfo=UTC))  # dated the day before
    box.deliver(Mail("Ed <ed@edge.example>", "Edge", to=SAM, date=datetime(2026, 10, 18, tzinfo=UTC), text="x"), 3, 3)
    box.deliver(Mail("Nia <nia@nodate.example>", "Undated", to=SAM, text="x"), 4, 4,
                arrived=datetime(2026, 10, 20, tzinfo=UTC))  # no Date line: when it arrived
    monkeypatch.setattr(gmail, "ASK_MESSAGES", 4)
    answer = make(box).ask("threads_by_domain", {"days": 30})
    assert [r.id for r in answer.records] == ["nodate.example", "edge.example"] and answer.notices == []
    assert answer.records[0].fields["first"] == "2026-10-20"


def test_threads_by_domain_ranks_by_turns_each_way_then_threads_both_ways_then_threads_and_sorts_turns_alike() -> None:
    box = StandIn()
    back_and_forth = [("a@deep.example", SAM), (SAM, "a@deep.example")] * 3
    for thread, messages in {
        1: [(sender, to, on(1 + n)) for n, (sender, to) in enumerate(back_and_forth)],  # three turns each way
        2: [("b@wide.example", SAM, on(7)), (SAM, "b@wide.example", on(8))],  # one each way
        3: [("b@wide.example", SAM, on(9)), (SAM, "b@wide.example", on(10)), ("b@wide.example", SAM, on(11)),
            (SAM, "b@wide.example", on(12))],  # two each way
        4: [("c@many.example", SAM, on(13))], 5: [("c@many.example", SAM, on(14))],
        6: [("noreply@few.example", SAM, on(15))]}.items():
        for n, (sender, to, when) in enumerate(messages):
            box.deliver(Mail(sender, "x", to=to, date=when, text="x"), thread * 10 + n, thread)
    answer = make(box).ask("threads_by_domain")
    assert [r.id for r in answer.records] == ["deep.example", "wide.example", "many.example", "few.example"]
    assert answer.records[1].fields["turns"] == [{"ours": 2, "theirs": 2}, {"ours": 1, "theirs": 1}]
    assert answer.records[1].fields["both_ways"] == 2 and answer.records[0].fields["both_ways"] == 1
    assert answer.records[3].fields["sender"] == "machine" and "turns" not in answer.records[3].fields


def test_an_ask_that_reads_every_message_up_to_its_bound_says_nothing_of_it(mailbox: StandIn,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connectors import gmail

    monkeypatch.setattr(gmail, "ASK_MESSAGES", 18)
    assert make(mailbox).ask("threads_by_domain").notices == []


def test_threads_by_domain_reads_headers_alone_through_the_read_only_gate_and_never_writes(mailbox: StandIn) -> None:
    connector = make(mailbox)
    for days in (90, 7):
        connector.ask("threads_by_domain", {"days": days})
    connector.close()
    allowed = re.compile(r'^(LIST "" \*|EXAMINE "\[Gmail\]/All Mail"|LOGOUT|UID SEARCH SINCE [0-9]{1,2}-[A-Z][a-z]{2}-'
                         r"2026|UID FETCH [0-9:,]+ \(UID X-GM-MSGID X-GM-THRID INTERNALDATE RFC822\.SIZE "
                         r"BODY\.PEEK\[HEADER\.FIELDS \([A-Z -]+\)\]\))$")
    assert mailbox.commands and [c for c in mailbox.commands if not allowed.match(c)] == []
    assert all(not m.flags for m in mailbox.messages) and mailbox.read_only
    assert {"UID SEARCH SINCE 19-Aug-2026", "UID SEARCH SINCE 10-Nov-2026"} <= set(mailbox.commands)
    assert "Words never read" not in repr(connector.ask("threads_by_domain"))


def test_the_write_check_above_sees_a_write_when_one_is_sent(mailbox: StandIn) -> None:
    Session(mailbox).imap.store("1", "+FLAGS", "(\\Seen)")
    assert any(c.startswith("STORE") for c in mailbox.commands)  # the check above would fail on this line


def test_a_curator_asks_threads_by_domain_admits_a_domain_and_its_mail_is_then_in_scope(
        mailbox: StandIn, tmp_path: Path) -> None:
    from test_fetch import Asking, curate

    from cerebro.curate import in_scope
    from cerebro.fakes import MemoryStore
    from cerebro.identity import bound_to, reached

    store, connector = MemoryStore(), make(mailbox)
    listed = list(every_page(connector).records)
    acme = [r for r in listed if "acme.example" in r.keys.get("domain", [])]
    assert acme and all(bound_to(store, r) == [] for r in listed)
    asked: list[dict] = []

    def script(tools: dict) -> None:
        asked.append(tools["fetch"](source="mail", question="threads_by_domain"))
        top = asked[0]["rows"][0]
        turns = top["fields"]["turns"][0]
        tools["admit"](name="Acme", keys=top["keys"],
                       reason=f"1 thread with replies both ways, {turns['ours']} and {turns['theirs']} turns")

    run = Asking(script)
    done = curate(store, run, {"mail": connector}, tmp_path)
    assert "fetch" in run.offered and done.admitted == ["accounts/acme"]
    assert asked[0]["rows"][0]["id"] == "acme.example" and asked[0]["rows"][0]["keys"] == {"domain": ["acme.example"]}
    # its parties' domain puts the mail in scope with the account, and binds it to nothing: it only carries it
    assert all(reached(store, r) == ["accounts/acme"] and in_scope(store, r, curated={"accounts"}) for r in acme)
    assert all(bound_to(store, r) == [] for r in listed)


@pytest.mark.parametrize(("address", "machine"), [
    ("noreply@acme.example", True), ("no-reply+x@acme.example", True), ("NoReply-Billing@acme.example", True),
    ("calendar@acme.example", True), ("bounces+12@acme.example", True), ("ada@notifications.acme.example", True),
    ("ada@bounce.mail.acme.example", True), ("no-reply-billing@acme.example", True), ("donotreply.us@acme.example",
    True), ("bounce-12@acme.example", True), ("priya@acme.example", False), ("notice@acme.example", False),
    ("ada@calendar.example", False), ("ada@mail.calendar.example", False), ("not-an-address", False),
])
def test_a_machines_address_reads_by_its_local_part_or_a_host_label_before_the_company(address: str,
                                                                                       machine: bool) -> None:
    from cerebro.connectors.gmail_text import machine_address

    assert machine_address(address) is machine


def test_a_mailbox_refusal_and_a_messages_text_are_masked_before_they_are_cut_while_the_mask_is_held() -> None:
    from stored_shapes import swept

    from cerebro.connectors.gmail_imap import GmailError, Session
    from cerebro.connectors.gmail_text import cut

    def refused(text: str) -> str:
        with pytest.raises(GmailError) as said:
            Session(None)._check("NO", [text.encode()], "list")
        return str(said.value)

    assert not swept(refused, 200)
    assert not swept(lambda text: cut(text, 300)[0], 300)


def test_a_mailbox_refusal_is_kept_whole_on_one_line() -> None:
    from cerebro.connector import masking
    from cerebro.connectors.gmail_imap import GmailError, Session

    said = "Lookup failed:\n  " + " ".join(f"folder_{i} is not open" for i in range(30)) + " https://mail.example/" + "a" * 300
    for mask in (None, lambda text: text):
        with pytest.raises(GmailError) as refused:
            if mask is None:
                Session(None)._check("NO", [said.encode()], "list")
            else:
                with masking(mask):
                    Session(None)._check("NO", [said.encode()], "list")
        assert f"gmail answered no to list: {' '.join(said.split())}: list again later" in str(refused.value)
