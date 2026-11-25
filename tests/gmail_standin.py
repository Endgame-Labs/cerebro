"""A stand-in for Gmail's IMAP, for tests: an object shaped as `imaplib.IMAP4` that answers from messages in memory.

It speaks the part of IMAP the Gmail connector uses, as imaplib hands it back: LIST with All Mail found by its \\All
attribute, a mailbox opened by SELECT or EXAMINE with its UIDVALIDITY and UIDNEXT, UID SEARCH by ALL, SINCE, X-GM-MSGID
and X-GM-THRID, and UID FETCH of the Gmail ids, INTERNALDATE, RFC822.SIZE, header fields, a BODYSTRUCTURE worked out
from each message's MIME, and body parts, whole or from a start. It also takes every command that changes a mailbox,
so a test can see that none was sent: each command it receives is kept in `commands`, with a password left out, and
a fetch of a body without PEEK marks the message read, as a real server does.

Nothing here is a real mailbox. Every name and address a test gives it is invented, at a .example domain.
"""

from __future__ import annotations

import base64
import email
import imaplib
import quopri
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from email.policy import compat32
from email.utils import format_datetime

__all__ = ["ALL_MAIL", "Mail", "StandIn", "Stored"]

ALL_MAIL = "[Gmail]/All Mail"
_SEARCH = re.compile(r"^(ALL|SINCE ([0-9]{1,2}-[A-Za-z]{3}-[0-9]{4})|X-GM-(MSGID|THRID) ([0-9]+))$")
_ITEM = re.compile(r"BODY(\.PEEK)?\[([^\]]*)\](?:<([0-9]+)\.([0-9]+)>)?|[A-Z0-9.-]+", re.IGNORECASE)


@dataclass
class Mail:
    """A message to build. Only `sender` and `subject` are needed. With both `text` and `html` it is an
    alternative. `attachment` adds a PDF, `text_file` a text file sent inline ahead of the body, and `forward` a
    message attached whole. `headers` are added as written, such as `List-Unsubscribe: <mailto:x@y.example>`."""

    sender: str
    subject: str = ""
    to: str = ""
    cc: str = ""
    date: datetime | None = None
    text: str = ""
    html: str = ""
    charset: str = "utf-8"
    encoding: str = "quoted-printable"
    attachment: str = ""
    text_file: str = ""
    forward: str = ""
    headers: list[str] = field(default_factory=list)
    in_reply_to: str = ""
    message_id: str = ""

    def build(self) -> bytes:
        lines = []
        for name, value in (("From", self.sender), ("To", self.to), ("Cc", self.cc),
                            ("Date", format_datetime(self.date) if self.date else ""), ("Subject", self.subject),
                            ("Message-ID", self.message_id), ("In-Reply-To", self.in_reply_to)):
            if value:
                lines.append(f"{name}: {value}")
        lines += self.headers + ["MIME-Version: 1.0"]
        text = _text_part(self.text, self.charset, self.encoding)
        html = ("Content-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n"
                + _qp(self.html))
        inner = (f'Content-Type: multipart/alternative; boundary="alt1"\r\n\r\n--alt1\r\n{text}\r\n--alt1\r\n'
                 f'{html}\r\n--alt1--\r\n') if self.text and self.html else html if self.html else text
        if self.attachment or self.text_file or self.forward:
            parts = []
            if self.text_file:
                parts.append('Content-Type: text/plain; charset=utf-8; name="notes.txt"\r\nContent-Disposition: '
                             'inline; filename="notes.txt"\r\nContent-Transfer-Encoding: 7bit\r\n\r\n'
                             + self.text_file)
            parts.append(inner)
            if self.forward:
                parts.append("Content-Type: message/rfc822\r\nContent-Disposition: inline\r\n\r\nFrom: "
                             "someone@forwarded.example\r\nSubject: Forwarded\r\nContent-Type: text/plain; "
                             "charset=utf-8\r\n\r\n" + self.forward)
            if self.attachment:
                parts.append('Content-Type: application/pdf; name="contract.pdf"\r\nContent-Disposition: '
                             'attachment; filename="contract.pdf"\r\nContent-Transfer-Encoding: base64\r\n\r\n'
                             + base64.b64encode(self.attachment.encode()).decode())
            body = ('Content-Type: multipart/mixed; boundary="mix1"\r\n\r\n--mix1\r\n'
                    + "\r\n--mix1\r\n".join(parts) + "\r\n--mix1--\r\n")
        else:
            body = inner
        return ("\r\n".join(lines) + "\r\n" + body).encode("utf-8")


def _qp(text: str) -> str:
    return quopri.encodestring(text.replace("\n", "\r\n").encode("utf-8")).decode("ascii")


def _text_part(text: str, charset: str, encoding: str) -> str:
    head = f"Content-Type: text/plain; charset={charset}\r\nContent-Transfer-Encoding: {encoding}\r\n\r\n"
    data = text.encode("latin-1" if charset.lower() in ("iso-8859-1", "latin1") else "utf-8")
    if encoding == "base64":
        return head + base64.b64encode(data).decode()
    if encoding == "quoted-printable":
        return head + quopri.encodestring(data.replace(b"\n", b"\r\n")).decode("ascii")
    return head + data.decode("utf-8", "surrogateescape").replace("\n", "\r\n")


@dataclass
class Stored:
    """A message the stand-in holds: its UID, Gmail ids, the date it arrived, its bytes and its flags."""

    uid: int
    msgid: int
    thrid: int
    arrived: datetime
    raw: bytes
    flags: set[str] = field(default_factory=set)


def _quoted(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _params(part: Message) -> str:
    params = [(k, v) for k, v in (part.get_params() or [])[1:] if v]
    return "(" + " ".join(f"{_quoted(k.upper())} {_quoted(str(v))}" for k, v in params) + ")" if params else "NIL"


def _payload(part: Message) -> bytes:
    payload = part.get_payload()
    if isinstance(payload, list):
        return b"".join(p.as_bytes() for p in payload)
    return payload.encode("ascii", "surrogateescape")


def _structure(part: Message) -> str:
    kind, sub = part.get_content_maintype().upper(), part.get_content_subtype().upper()
    if part.is_multipart() and kind == "MULTIPART":
        return "(" + "".join(_structure(p) for p in part.get_payload()) + f" {_quoted(sub)} NIL NIL NIL)"
    encoding = _quoted(str(part.get("Content-Transfer-Encoding", "7bit")).upper())
    disposition = "NIL"
    if part.get("Content-Disposition"):
        params = [(k, v) for k, v in part.get_params(header="Content-Disposition")[1:] if v]
        inner = " ".join(f"{_quoted(k.upper())} {_quoted(str(v))}" for k, v in params)
        disposition = f"({_quoted(part.get_content_disposition().upper())} {f'({inner})' if inner else 'NIL'})"
    data = _payload(part)
    head = f"({_quoted(kind)} {_quoted(sub)} {_params(part)} NIL NIL {encoding} {len(data)}"
    if kind == "TEXT":
        return f"{head} {data.count(b'\n')} NIL {disposition} NIL)"
    if (kind, sub) == ("MESSAGE", "RFC822"):
        return f"{head} NIL NIL 0 NIL {disposition} NIL)"
    return f"{head} NIL {disposition} NIL)"


def _part(message: Message, path: str) -> bytes:
    node = message
    for number in path.split("."):
        if node.is_multipart() and node.get_content_maintype() == "multipart":
            node = node.get_payload()[int(number) - 1]
        elif number != "1":
            return b""
    return _payload(node)


def _header_fields(raw: bytes, names: list[str]) -> bytes:
    head = raw.split(b"\r\n\r\n", 1)[0]
    entries: list[bytes] = []
    for line in head.split(b"\r\n"):
        if line[:1] in (b" ", b"\t") and entries:
            entries[-1] += b"\r\n" + line
        else:
            entries.append(line)
    wanted = {n.upper() for n in names}
    kept = [e for e in entries if e.split(b":", 1)[0].strip().decode("ascii", "replace").upper() in wanted]
    return b"".join(e + b"\r\n" for e in kept) + b"\r\n"


class StandIn:
    """An `imaplib.IMAP4`-shaped Gmail mailbox in memory. `commands` keeps every command it received."""

    error = imaplib.IMAP4.error
    abort = imaplib.IMAP4.abort

    def __init__(self, mails: Iterable[tuple[int, int, Mail]] = (), *, validity: int = 7, all_mail: bool = True):
        self.validity = validity
        self.next_uid = 1
        self.messages: list[Stored] = []
        self.commands: list[str] = []
        self.all_mail = all_mail
        self.selected = ""
        self.read_only = False
        self.refusals: list[tuple[str, str]] = []
        self.logged_out = False
        for msgid, thrid, mail in mails:
            self.deliver(mail, msgid, thrid)

    # -- what a test does to the mailbox, as the outside world changing would
    def deliver(self, mail: Mail | bytes, msgid: int, thrid: int = 0, arrived: datetime | None = None) -> int:
        """Add a message to All Mail, as a new one arriving, and give its UID."""
        raw = mail.build() if isinstance(mail, Mail) else mail
        when = arrived or (mail.date if isinstance(mail, Mail) and mail.date else datetime(2026, 11, 17, tzinfo=UTC))
        self.messages.append(Stored(self.next_uid, msgid, thrid or msgid + 1_000_000, when.astimezone(UTC), raw))
        self.next_uid += 1
        return self.next_uid - 1

    def expunge_message(self, msgid: int) -> None:
        """Delete a message for good, as a person emptying the trash would."""
        self.messages = [m for m in self.messages if m.msgid != msgid]

    def renumber(self) -> None:
        """Number every message again from 1 under a new UIDVALIDITY, as Gmail sometimes does, so a UID seen before
        may now name another message."""
        self.validity += 1
        for uid, stored in enumerate(self.messages, 1):
            stored.uid = uid
        self.next_uid = len(self.messages) + 1

    def refuse_next(self, command: str, answer: str) -> None:
        """Answer the next `command`, such as SEARCH, with NO and `answer`, as Google does when it throttles."""
        self.refusals.append((command.upper(), answer))

    # -- imaplib's shape
    def _log(self, line: str) -> None:
        self.commands.append(line)

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self._log(f"LOGIN {user} ***")
        return "OK", [b"logged in"]

    def logout(self) -> tuple[str, list[bytes]]:
        self._log("LOGOUT")
        self.logged_out = True
        return "BYE", [b"logging out"]

    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, list[bytes]]:
        self._log(f"LIST {directory} {pattern}")
        lines = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
                 b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']
        if self.all_mail:
            lines.append(b'(\\All \\HasNoChildren) "/" "' + ALL_MAIL.encode() + b'"')
        return "OK", lines

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self._log(f"{'EXAMINE' if readonly else 'SELECT'} {mailbox}")
        self.selected, self.read_only = mailbox.strip('"'), readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code: str) -> tuple[str, list[bytes | None]]:
        value = {"UIDVALIDITY": self.validity, "UIDNEXT": self.next_uid}.get(code.upper())
        return code, [str(value).encode() if value is not None else None]

    def uid(self, command: str, *args: str) -> tuple[str, list]:
        command = command.upper()
        self._log(" ".join(["UID", command, *map(str, args)]))
        for i, (refused, answer) in enumerate(self.refusals):
            if refused == command:
                del self.refusals[i]
                return "NO", [answer.encode()]
        if command == "SEARCH":
            return self._search(" ".join(args))
        if command == "FETCH":
            return self._fetch(args[0], args[1])
        if command in ("STORE", "COPY", "MOVE", "EXPUNGE"):
            return "OK", [b"done"]
        return "BAD", [b"unknown command"]

    def __getattr__(self, name: str):
        if name in ("store", "copy", "expunge", "append", "create", "delete", "rename", "move", "subscribe",
                    "setacl", "setannotation", "setquota", "close"):
            def changes(*args: object) -> tuple[str, list[bytes]]:
                self._log(" ".join([name.upper(), *map(str, args)]))
                return "OK", [b"done"]
            return changes
        raise AttributeError(name)

    def _search(self, criteria: str) -> tuple[str, list[bytes]]:
        match = _SEARCH.match(criteria.strip())
        if match is None:
            return "BAD", [b"criteria not understood"]
        if match.group(2):
            since = datetime.strptime(match.group(2), "%d-%b-%Y").date()
            found = [m.uid for m in self.messages if m.arrived.date() >= since]
        elif match.group(3):
            number, key = int(match.group(4)), "msgid" if match.group(3) == "MSGID" else "thrid"
            found = [m.uid for m in self.messages if getattr(m, key) == number]
        else:
            found = [m.uid for m in self.messages]
        return "OK", [" ".join(map(str, sorted(found))).encode()]

    def _uids(self, spec: str) -> set[int]:
        top = max((m.uid for m in self.messages), default=0)
        found: set[int] = set()
        for piece in spec.split(","):
            low, _, high = piece.partition(":")
            low_n = top if low == "*" else int(low)
            high_n = low_n if not high else top if high == "*" else int(high)
            found |= set(range(min(low_n, high_n), max(low_n, high_n) + 1))
        return found

    def _fetch(self, spec: str, items: str) -> tuple[str, list]:
        wanted = self._uids(spec)
        asked = [m.group(0) for m in _ITEM.finditer(items.strip()[1:-1])]
        data: list = []
        for seq, stored in enumerate(self.messages, 1):
            if stored.uid not in wanted:
                continue
            message = email.message_from_bytes(stored.raw, policy=compat32)
            text, literals = f"{seq} (", []
            for item in asked:
                body = _ITEM.match(item)
                upper = item.upper()
                if upper == "UID":
                    text += f"UID {stored.uid} "
                elif upper == "X-GM-MSGID":
                    text += f"X-GM-MSGID {stored.msgid} "
                elif upper == "X-GM-THRID":
                    text += f"X-GM-THRID {stored.thrid} "
                elif upper == "INTERNALDATE":
                    text += f'INTERNALDATE "{stored.arrived.strftime("%d-%b-%Y %H:%M:%S +0000")}" '
                elif upper == "RFC822.SIZE":
                    text += f"RFC822.SIZE {len(stored.raw)} "
                elif upper == "BODYSTRUCTURE":
                    text += f"BODYSTRUCTURE {_structure(message)} "
                elif body is not None and upper.startswith("BODY"):
                    peek, section, start, count = body.groups()
                    if not peek:
                        stored.flags.add("\\Seen")
                    section = section.upper()
                    if section.startswith("HEADER.FIELDS"):
                        names = section[section.index("(") + 1:section.rindex(")")].split()
                        value = _header_fields(stored.raw, names)
                    elif section == "":
                        value = stored.raw
                    else:
                        value = _part(message, section)
                    key = f"BODY[{section}]"
                    if start is not None:
                        value = value[int(start):int(start) + int(count)]
                        key += f"<{start}>"
                    literals.append((text + f"{key} {{{len(value)}}}", value))
                    text = " "
            if literals:
                data.extend((head.encode(), value) for head, value in literals)
                data.append((text.rstrip() + ")").encode())
            else:
                data.append((text.rstrip() + ")").encode())
        return "OK", data or [None]
