"""A Gmail mailbox as a source, read only.

The connector reads `[Gmail]/All Mail`, found by its \\All attribute, opened read only, so sent and received mail
come together and nothing is marked read: every command passes the read-only gate in `gmail_imap`. A message is a
record and never an article, so a record lands nowhere and a source file that names `into:` is refused.

A record's id is the message's X-GM-MSGID, which Gmail never changes, and its url opens it in Gmail. Its revision is
a hash of its header block and its size, so it holds when Gmail renumbers the mailbox and moves when the message's
bytes do. Its time is its Date line, else when it arrived. Its keys are the addresses of the people on it and their
domains, never yours or your company's, which the caller hands in, and never a public mail service's domain. It links
to its thread. Bulk mail, by its headers, is no record.

A listing reads headers alone, in pages, from a cursor that carries the mailbox's UIDVALIDITY and the UID and
message id of each message seen, and no text. A pass searches the window, `window.behind` from now and 90 days when
the file says none, and lists the UIDs it has not seen. A message seen that the search no longer finds is listed as
removed when Gmail no longer holds its message id at all; one that only aged out of the window, or lives on under
another UID, is not. When Gmail renumbers the mailbox, the window is listed again, and what is gone is still listed
as removed. `get` fetches the
text part alone, plain before HTML, never an attachment, and reduces it to what the sender newly wrote, as
`gmail_text.reduce` says. The connector keeps no copy of a message: each `get` asks Gmail again.

A curator may ask `threads_by_domain`: one row for each outside
domain of the threads of the last days, from the same search and the same header fetch a listing sends, so no body
is fetched and nothing is kept. A thread's turns count each side's changes of speaker: a run of messages from people
on one side, until a person on the other side writes, is one turn, and a message from an address that reads as a
machine's is no turn and ends no run. Only your own mailbox answers it: a source whose `owner:` names a colleague
gives no rows, with a line that says so.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import imaplib
import json
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from ..clock import system_clock
from ..connector import (
    NOBODY,
    Connector,
    CursorError,
    Listing,
    Ours,
    Record,
    RecordNotFound,
    address_keys,
    gone,
    one_line,
    split_login,
)
from ..source import SourceError, SourceFile
from .connecting import Connecting, KeyRefused, KeyUnproven, Need, Profile, Prompt
from .gmail_imap import GMAIL_HOST, GmailError, Item, Session, gmail_login
from .gmail_text import Headers, cut, decode_body, html_to_text, lower_ascii, parse_headers, reduce
from .questions import Days, asked, machine_address

__all__ = ["GmailConnector", "GmailCursorError"]

SOURCE_KEYS = ("auth", "extract", "window", "owner", "freshness", "full_every")
_ELSEWHERE = "a gmail source reads imap.gmail.com and takes no url or command, so the login is sent nowhere else"
REFUSED_KEYS = {"url": f"{_ELSEWHERE}: delete url", "command": f"{_ELSEWHERE}: delete command",
                "into": "a message is a record and never an article, so mail lands nowhere: delete into"}
IMAP_TIMEOUT = 30.0  # seconds a connection may wait on the network
PAGE_SIZE = 100
PART_LIMIT = 1 << 20  # bytes of a text part fetched, in its transfer encoding, so a huge body never comes whole
TEXT_LIMIT = 200 << 10  # bytes of one message's text that land; a longer one is cut, and its text says so
BATCH = 500  # UIDs one fetch of message ids asks for
CUT_NOTE = "[The rest of this message was cut at 200 KB of text.]"
HEADER_FIELDS = ("FROM", "TO", "CC", "DATE", "SUBJECT", "MESSAGE-ID", "IN-REPLY-TO", "LIST-UNSUBSCRIBE", "LIST-ID",
                 "PRECEDENCE", "AUTO-SUBMITTED")
_HEADERS = f"BODY.PEEK[HEADER.FIELDS ({' '.join(HEADER_FIELDS)})]"
_LIST_ITEMS = f"(UID X-GM-MSGID X-GM-THRID INTERNALDATE RFC822.SIZE {_HEADERS})"
_GET_ITEMS = f"(UID X-GM-MSGID X-GM-THRID INTERNALDATE RFC822.SIZE BODYSTRUCTURE {_HEADERS})"
_CURSOR = "gmail1."
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# The question a curator may ask the source with `ask`, with the params it takes.
QUESTIONS = {
    "threads_by_domain": "one row for each outside domain of the mail threads of the last `days` (90 by default, at "
                         "most 90), from headers alone: the threads, how many someone on your side replied in, "
                         "whether replies went both ways, the first and last day, whether a person or a machine "
                         "began them and the messages, and under turns the turns each side took in each thread with "
                         "a turn from each side, the most turns each way first, with both_ways counting such "
                         "threads; params: days",
}
ASK_DAYS = MAX_ASK_DAYS = 90
TAKES = {"threads_by_domain": {"days": Days(ASK_DAYS, 1, MAX_ASK_DAYS)}}
ASK_MESSAGES = 5000  # the newest messages of the days asked whose headers one ask reads, at most
MAX_TURNS = 10  # the threads a row gives the turns of, at most; both_ways counts every one


class GmailCursorError(GmailError, CursorError):
    """A cursor this connector did not give, or one that does not read."""


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class _Thread:
    """What one thread's headers say for `threads_by_domain`: never a subject, a name or a word of its text."""

    began: datetime
    at: datetime
    messages: int = 0
    ours: bool = False  # someone on your side wrote in it
    theirs: bool = False  # someone outside wrote in it
    turns: tuple[int, int] = (0, 0)  # the turns your side and theirs took
    outside: list[str] = field(default_factory=list)  # the outside domains of everyone on it
    senders: list[str] = field(default_factory=list)  # those whose people wrote a turn
    person: bool = False  # a person began it
    machine: bool = False  # a machine began it


# What connect says and asks, in the drawn view and on a plain line. The
# address's default is the first address profile.md lists.
CONNECTING = Connecting(
    says=(("Provide an app password for your Gmail or Google Workspace email account. To create one, go to "
          "`myaccount.google.com/apppasswords`. 2-Step Verification must be enabled."), ""),
    plain=(("Create an app password at myaccount.google.com/apppasswords. 2-Step Verification must be on. Connect "
           "logs in once to verify it."), ""),
    asks=(Prompt("Email address", "Gmail address", hidden=False, default="addresses",
                 offered=" [{value}, from profile.md]", skip=" [Enter to skip]"),
          Prompt("App password", "Gmail app password")),
    later="Gmail is not connected yet. Run cerebro connect {name} when you have the address and an app password.",
    later_check=("Gmail is not connected yet: run cerebro connect {name} when you have the address and an app "
                 "password"),
    title="Gmail",
    needs="your address and an app password",
    connected="Connected to Gmail. The login is kept under {secret} in {where}.",
    profile=Profile("email", joins=True, added="Added your Gmail address to profile.md.",
                    already="Your Gmail address is in profile.md already.",
                    no_file="There is no profile.md to add your Gmail address to."),
    reads_kept=False)  # asked for each time


def _imap_open(host: str) -> imaplib.IMAP4_SSL:
    return imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT)


class GmailConnector(Connector):
    """A `kind: gmail` source over the mailbox a session from `open` reaches. `open` returns an `imaplib.IMAP4`-shaped
    session already logged in, and is called again when a connection drops. `ours` holds your addresses and your
    company's domains, which are never a key. `now` is the clock the window is read by."""

    connecting = CONNECTING

    @staticmethod
    def find_you(source: SourceFile, value: str, **_: Any) -> tuple[str, str] | None:
        """Who the login is, for profile.md's `email`: its address."""
        address = value.partition(":")[0].strip().lower()
        return (address, address) if address else None

    @classmethod
    def login_name(cls, source: SourceFile) -> str | None:
        """Each source keeps its own login, under its name."""
        return source.name

    @classmethod
    def needs(cls, source: SourceFile, item: str) -> Need | None:
        return Need("pair", item, "the gmail address and app password", parts=("Gmail address", "App password"),
                    shown=(True, False))

    @classmethod
    def prove_key(cls, source: SourceFile, value: str, *, app: Any, **_: Any) -> str:
        """The address the login is for, once Gmail takes it over IMAP, opened as `app.imap_connect` opens it when set."""
        address, _, password = value.partition(":")
        address, password = address.strip(), password.strip()
        try:
            session = gmail_login(address.lower(), "".join(password.split()), connect=app.imap_connect or _imap_open)()
        except GmailError as error:
            if "refused the login" in str(error):
                raise KeyRefused(str(error), "the login was refused") from None
            raise KeyUnproven(str(error)) from None
        except OSError as error:
            raise KeyUnproven(type(error).__name__) from None
        except imaplib.IMAP4.error:  # a greeting that is not IMAP, or a connection dropped before the login
            raise KeyUnproven(f"{GMAIL_HOST} did not answer as an IMAP server") from None
        logout = getattr(session, "logout", None)
        if callable(logout):
            try:
                logout()
            except Exception:  # noqa: BLE001, S110 - the login was checked; a session that will not end is no matter
                pass
        return address.lower()

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> GmailConnector:
        login = app.login(source)

        def open_session() -> Any:
            address, password = split_login(login(), "gmail login", f"run cerebro connect {source.name} --replace "
                                            "with the address and a new app password")
            password = "".join(password.split())  # Google shows an app password in groups with spaces
            app.mask.add(password)
            try:
                return gmail_login(address.lower(), password, connect=app.imap_connect or _imap_open)()
            except OSError as error:
                raise GmailError(f"gmail did not answer ({type(error).__name__}): check the network, then check "
                                 f"again") from None

        return cls(source, open_session, ours=app.ours, now=app.clock, zone=app.zone)

    def __init__(self, source: SourceFile, open: Callable[[], Any], *, ours: Ours = NOBODY,
                 page_size: int = PAGE_SIZE, now: Callable[[], datetime] | None = None, zone: tzinfo = UTC):
        where = f"sources/{source.name}.yaml"
        if source.kind != "gmail":
            raise SourceError(f"{where} is kind {source.kind}: a gmail connector reads kind gmail alone")
        self.name = source.name
        self.source = source
        self.page_size = max(1, page_size)
        self.now = now or system_clock
        self.zone = zone  # the workspace's zone, in which a question's days are counted
        self.behind = source.behind()
        self.extract = source.extract or "facts"
        self.ours = Ours(tuple(lower_ascii(a.strip()) for a in ours.addresses if a.strip()),
                         tuple(lower_ascii(d.strip()).lstrip("@.") for d in ours.domains if d.strip()))
        self._open = open
        self._session: Session | None = None
        config = json.dumps([source.window.behind, self.extract], sort_keys=True)
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]

    # -- the connection
    def _connect(self) -> Session:
        if self._session is None:
            try:
                session = Session(self._open(), self.name)
                session.open_all_mail()
            except (imaplib.IMAP4.error, OSError) as error:
                raise GmailError(f"{self.name}: gmail could not be reached ({type(error).__name__}: "
                                 f"{one_line(error, 120)}): check the network and the login, then list again") from None
            self._session = session
        return self._session

    def _ask(self, ask: Callable[[Session], Any]) -> Any:
        """Run one request, and open the connection again once if Gmail dropped it, as it drops an idle one."""
        session = self._connect()
        try:
            return ask(session)
        except (imaplib.IMAP4.abort, OSError):
            validity = session.validity
            self.close()
            again = self._connect()
            if again.validity != validity:
                raise GmailError(f"{self.name}: gmail renumbered the mailbox while this listing ran: list again "
                                 f"from the same cursor") from None
            return ask(again)

    def close(self) -> None:
        """Log out, if a connection is open."""
        if self._session is not None:
            self._session.close()
            self._session = None

    # -- records
    @staticmethod
    def _block(item: Item) -> bytes:
        return next((v for k, v in item.parts.items() if k.startswith("BODY[HEADER.FIELDS")), b"")

    def _record(self, item: Item) -> tuple[Record, bool, str]:
        """The record a fetch of a message's headers makes, whether the message is bulk mail, and the header lines a
        reader sees, which open the body."""
        block = self._block(item)
        headers = parse_headers(block)
        when = headers.date or item.arrived
        keys = address_keys((a.email for a in headers.participants()), self.ours)
        lines = [f"{name}: {' '.join(value.split())}\n" for name, value in (
            ("From", ", ".join(a.format() for a in headers.from_)), ("To", ", ".join(a.format() for a in headers.to)),
            ("Cc", ", ".join(a.format() for a in headers.cc)),
            ("Date", format_datetime(when.astimezone(UTC)) if when else ""), ("Subject", headers.subject))
            if value.strip()]
        fields: dict[str, Any] = {"thread": str(item.thrid), "from": headers.from_[0].email if headers.from_ else "",
                                  "subject": headers.subject}
        if mine := list(dict.fromkeys(a.name or a.email for a in headers.participants() if self.ours.holds(a.email))):
            fields["ours"] = mine  # who on it is yours, by your addresses and domains: never a key
        revision = "sha256:" + hashlib.sha256(f"{item.size}\n".encode() + block).hexdigest()
        record = Record(id=str(item.msgid), source=self.name, revision=revision, at=_stamp(when) if when else "",
                        url=f"https://mail.google.com/mail/u/0/#all/{item.msgid:x}", keys=keys,
                        links=[f"thread:{item.thrid}"], fields=fields, extract=self.extract,
                        carried=list(keys))  # its parties' keys point at them: a message names nothing
        return record, headers.bulk(), "".join(lines)

    def _text(self, session: Session, uid: int, item: Item) -> str:
        """The message's own new words: its first text part that holds any, plain before HTML, decoded, reduced and
        cut at the limit, with a note when it was cut."""
        for part in item.structure.text_parts() if item.structure else []:
            fetched = session.fetch([uid], f"(UID BODY.PEEK[{part.path}]<0.{PART_LIMIT}>)")
            raw = next((v for it in fetched for k, v in it.parts.items() if k.startswith(f"BODY[{part.path}]")), b"")
            text = decode_body(raw, part.encoding, part.charset)
            own = reduce(html_to_text(text) if part.subtype == "html" else text)
            if not own.strip():
                continue
            own, was_cut = cut(own, TEXT_LIMIT)
            return own + (f"\n\n{CUT_NOTE}" if was_cut or len(raw) >= PART_LIMIT else "")
        return ""

    # -- the cursor
    def _cursor(self, validity: int, seen: dict[int, int], next_uid: int = 0, ask: str = "") -> str:
        uids = sorted(seen)
        steps = [b - a for a, b in zip([0, *uids], uids, strict=False)]
        state = json.dumps({"v": validity, "c": self._config, "u": steps, "m": [seen[u] for u in uids],
                            **({"n": next_uid} if next_uid else {}), **({"a": ask} if ask else {})},
                           separators=(",", ":"))
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(state.encode())).decode()

    def _seen(self, cursor: str) -> tuple[int, dict[int, int], bool]:
        """The UIDVALIDITY a cursor saw, the message id of each UID it saw, 0 for one that is no record, and whether
        the source file read the same then."""
        try:
            if not cursor.startswith(_CURSOR):
                raise ValueError
            state = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
            uids, total = [], 0
            for step in state["u"]:
                total += int(step)
                uids.append(total)
            ids = [int(m) for m in state["m"]]
            if len(ids) != len(uids):
                raise ValueError
            return int(state["v"]), dict(zip(uids, ids, strict=True)), state.get("c") == self._config
        except (ValueError, KeyError, TypeError, zlib.error, binascii.Error):
            raise GmailCursorError(f"{self.name}: the cursor {cursor[:24]!r} is not one this source gave: list with "
                                   f"no cursor to start over") from None

    def _set(self, cursor: str) -> tuple[int, str]:
        """The UID gmail said its next message would get when a cursor was made, 0 for none, and how `cursor_for`
        set it: "changes", "full", or "" for neither."""
        state = json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):])))
        ask = state.get("a", "")
        return int(state.get("n") or 0), ask if ask in ("changes", "full") else ""

    lists_changes = True

    def cursor_for(self, cursor: str | None, *, full: bool) -> str | None:
        """The cursor set so the next listing searches the window and asks after each message it no longer finds,
        when `full`, or asks gmail only for the UIDs from the next one it named when the cursor was made, and calls
        nothing removed, when not. A first listing searches the window anyway."""
        if cursor is None:
            return None
        validity, seen, _ = self._seen(cursor)
        return self._cursor(validity, seen, self._set(cursor)[0], "full" if full else "changes")

    def _criteria(self) -> str:
        if self.behind is None:
            return "ALL"
        day = (self.now() - self.behind).astimezone(UTC)
        return f"SINCE {day.day}-{_MONTHS[day.month - 1]}-{day.year}"

    def _from_uid(self, start: int) -> str:
        """The search for the UIDs from `start` on that are in the window: a message gmail numbers anew, as one
        brought back from the trash, keeps its old date, and one older than the window is no record."""
        window = self._criteria()
        return f"UID {start}:*" + ("" if window == "ALL" else f" {window}")

    # -- the contract
    def list(self, since: str | None = None) -> Listing:
        before, seen, same = self._seen(since) if since is not None else (0, {}, True)
        start, ask = self._set(since) if since is not None else (0, "")
        if ask == "changes" and same and seen:
            return self._changes(before, seen, start or max(seen) + 1)
        validity, current, next_uid = self._ask(
            lambda s: (s.examine(), s.validity, s.search(self._criteria()), s.next_uid)[1:])
        present = set(current)
        if since is not None and before != validity:
            lost, seen = seen, {}  # gmail renumbered the mailbox: every UID seen means nothing now
        else:
            lost = {uid: m for uid, m in seen.items() if uid not in present}
            seen = {uid: m for uid, m in seen.items() if uid in present}
        fresh = [uid for uid in current if uid not in seen]
        removed: list[str] = []
        if any(lost.values()):
            alive = set(seen.values())
            for start in range(0, len(fresh), BATCH):
                batch = fresh[start:start + BATCH]
                alive |= {it.msgid for it in self._ask(lambda s, b=batch: s.fetch(b, "(UID X-GM-MSGID)"))}
            missing = sorted({str(m) for m in lost.values() if m and m not in alive})
            removed = gone(missing, self._exists)  # one that only left the window is still in the mailbox
        if not same:
            seen, fresh = {}, current  # the source file changed, so every record is listed anew
        records = self._take(fresh[:self.page_size], seen)
        more = len(fresh) > self.page_size
        return Listing(records, self._cursor(validity, seen, next_uid, ask if more else ""), removed, more)

    def _take(self, page: list[int], seen: dict[int, int]) -> list[Record]:
        """The records of a page of UIDs, from one fetch of their headers, each UID kept in `seen` with its message
        id, or 0 for one that is no record, such as bulk mail."""
        items = {it.uid: it for it in self._ask(lambda s: s.fetch(page, _LIST_ITEMS))}
        records = []
        for uid in page:
            item = items.get(uid)
            if item is None or not item.msgid:
                seen[uid] = 0
                continue
            record, bulk, _ = self._record(item)
            seen[uid] = 0 if bulk else item.msgid
            if not bulk:
                records.append(record)
        return records

    def _changes(self, before: int, seen: dict[int, int], start: int) -> Listing:
        """A listing set for changes: the messages in the window gmail gave a UID from `start` on, the next UID it
        named when the cursor was made, so the whole window is not searched and nothing is called removed. A message's bytes never
        change in gmail, so what it cannot see is a message deleted or one that left the window: the listing in
        full finds those. A mailbox gmail renumbered since is listed in full at once."""
        validity, current, next_uid = self._ask(
            lambda s: (s.examine(), s.validity, s.search(self._from_uid(start)), s.next_uid)[1:])
        if validity != before:
            return self.list(self._cursor(before, seen, start, "full"))
        fresh = [uid for uid in current if uid >= start and uid not in seen]  # n:* gives the last UID when none is new
        records = self._take(fresh[:self.page_size], seen)
        more = len(fresh) > self.page_size
        kept = start if more else max(next_uid, start)  # the next page goes on from where this one began
        return Listing(records, self._cursor(validity, seen, kept, "changes" if more else ""), [], more)

    # -- a curator's question
    questions = QUESTIONS

    def _say(self, line: str) -> str:
        return f"gmail source {self.name}: {line}"

    def _asked(self, question: str, params: Mapping[str, Any] | None, limit: int) -> int:
        """The days a question asks for, or `QuestionError` in one line, before anything is sent."""
        return asked(f"gmail source {self.name}", QUESTIONS, TAKES, question, params, limit)["days"]

    def _thread(self, messages: list[tuple[datetime, Headers]]) -> _Thread:
        """A thread from its messages' headers: its sides, its turns, its outside
        domains and those whose people wrote, and who began it."""
        messages = sorted(messages, key=lambda m: m[0])
        thread = _Thread(messages[0][0], messages[-1][0], len(messages))
        ours = theirs = 0
        spoke = ""  # the side whose person wrote last, so a run from one side counts once
        everyone: list[str] = []
        wrote: list[str] = []
        for _, headers in messages:
            sender = headers.from_[0].email if headers.from_ else ""
            if sender and self.ours.holds(sender):
                thread.ours = True
            elif sender:
                thread.theirs = True
            if sender and not machine_address(sender):
                side = "ours" if self.ours.holds(sender) else "theirs"
                if side != spoke:
                    ours, theirs = (ours + 1, theirs) if side == "ours" else (ours, theirs + 1)
                spoke = side
                if side == "theirs":
                    wrote.append(sender)
            everyone += [a.email for a in headers.participants()]
        first = messages[0][1].from_[0].email if messages[0][1].from_ else ""
        if first:
            thread.machine = not self.ours.holds(first) and machine_address(first)
            thread.person = not thread.machine
        thread.turns = (ours, theirs)
        thread.outside = address_keys(everyone, self.ours).get("domain", [])
        thread.senders = address_keys(wrote, self.ours).get("domain", [])
        return thread

    def _day(self, when: datetime) -> str:
        """A row's day, in the workspace's zone, as a window counts days everywhere else."""
        return when.astimezone(self.zone).date().isoformat()

    def _row(self, domain: str, threads: list[_Thread]) -> tuple[Record, tuple[Any, ...]]:
        """One domain's row, and the key it ranks by: the most turns each way in one thread first, then the most
        threads with a turn from each side, then the most threads you replied in, the most threads, the latest."""
        turns = sorted(((t.turns[0], t.turns[1]) for t in threads if domain in t.senders and min(t.turns) > 0),
                       key=lambda pair: (-min(pair), -sum(pair)))
        replied = sum(1 for t in threads if t.ours)
        last = max(t.at for t in threads)
        person, machine = any(t.person for t in threads), any(t.machine for t in threads)
        fields: dict[str, Any] = {
            "threads": len(threads), "replied": replied,
            "directions": "both" if any(t.ours and t.theirs for t in threads) else "one",
            "first": self._day(min(t.began for t in threads)), "last": self._day(last),
            "messages": sum(t.messages for t in threads)}
        if person or machine:
            fields["sender"] = "person and machine" if person and machine else "person" if person else "machine"
        if turns:
            fields["turns"] = [{"ours": o, "theirs": t} for o, t in turns[:MAX_TURNS]]
            fields["both_ways"] = len(turns)
        row = Record(id=domain, source=self.name, revision="", keys={"domain": [domain]}, fields=fields)
        rank = (-(min(turns[0]) if turns else 0), -len(turns), -replied, -len(threads), -last.timestamp(), domain)
        return row, rank

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """A curator's question, as `QUESTIONS` lists it: one row for each outside domain of the threads of the last
        days, its key that domain, so an admit with it binds every message of the domain. It sends the search and
        the header fetch a listing sends, through the same read-only gate, fetches no body and keeps nothing."""
        days = self._asked(question, params, limit)
        if self.source.owner not in ("", "me"):
            return Listing([], None, [], False, [self._say("is a colleague's mailbox, and only your own feeds "
                                                           "admission: ask the gmail source whose owner is me")])
        floor = (self.now() - timedelta(days=days)).astimezone(self.zone).replace(hour=0, minute=0, second=0,
                                                                              microsecond=0)
        since = min(floor.date(), floor.astimezone(UTC).date())  # imap counts a day in no zone: take the earlier
        criteria = f"SINCE {since.day}-{_MONTHS[since.month - 1]}-{since.year}"
        uids = self._ask(lambda s: (s.examine(), s.search(criteria))[1])
        notices = []
        if len(uids) > ASK_MESSAGES:
            notices.append(self._say(f"read the newest {ASK_MESSAGES} of the {len(uids)} messages of the last "
                                     f"{days} days: ask for fewer days to count every one"))
            uids = uids[-ASK_MESSAGES:]
        messages: dict[int, list[tuple[datetime, Headers]]] = {}
        for start in range(0, len(uids), BATCH):
            batch = uids[start:start + BATCH]
            for item in self._ask(lambda s, b=batch: s.fetch(b, _LIST_ITEMS)):
                headers = parse_headers(self._block(item))
                when = headers.date or item.arrived
                if not item.msgid or headers.bulk() or when is None or when < floor:
                    continue  # bulk mail is no message, and one dated before the days asked is not of them
                messages.setdefault(item.thrid or item.msgid, []).append((when, headers))
        by: dict[str, list[_Thread]] = {}
        for id in sorted(messages):
            thread = self._thread(messages[id])
            for domain in thread.outside:
                by.setdefault(domain, []).append(thread)
        rows = sorted((self._row(domain, threads) for domain, threads in by.items()), key=lambda r: r[1])
        return Listing([row for row, _ in rows[:limit]], None, [], len(rows) > limit, notices)

    def _exists(self, id: str) -> bool:
        return bool(self._ask(lambda s: s.search(f"X-GM-MSGID {id}")))

    def get(self, id: str) -> Record:
        missing = RecordNotFound(f"{self.name} has no message {id!r}: list the source for the ids it holds")
        if not (id.isascii() and id.isdigit()) or str(int(id)) != id or not 0 < int(id) < 1 << 64:
            raise missing
        uids = self._ask(lambda s: s.search(f"X-GM-MSGID {id}"))
        if not uids:
            raise missing
        uid = uids[-1]

        def read(session: Session) -> Record | None:
            item = next((it for it in session.fetch([uid], _GET_ITEMS) if it.msgid == int(id)), None)
            if item is None:
                return None
            record, bulk, lines = self._record(item)
            if bulk:
                return None
            record.body = f"{lines}\n{self._text(session, uid, item)}\n"
            return record

        record = self._ask(read)
        if record is None:
            raise missing
        return record
