"""Gmail's IMAP, read only, over an `imaplib.IMAP4`-shaped session.

A session is handed in already logged in, by a function the caller owns, so this module never sees a password. It
asks the session for five things alone: the folder list, `[Gmail]/All Mail` opened read only, a UID search, a UID
fetch that peeks, and a logout. Every command passes one gate before it is sent, and the gate refuses anything else:
a mailbox opened for writing, a fetch that would mark a message read, and every command that stores, copies, moves,
appends, deletes or expunges. So nothing is ever marked read or changed, whatever a caller asks.

It reads imaplib's replies into tokens, the Gmail ids and dates of a fetch, and a message's body structure, from
which it picks the text parts to read, plain before HTML, never an attachment or a message attached whole.
"""

from __future__ import annotations

import imaplib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..connector import masked_first, one_line
from ..errors import CerebroError

__all__ = ["GMAIL_HOST", "GmailError", "Item", "Part", "ReadOnlyError", "Session", "gmail_login", "parse_structure",
           "quote", "tokens", "uid_set"]

GMAIL_HOST = "imap.gmail.com"
_FETCH_ALLOWED = re.compile(r"^\((?:UID|X-GM-MSGID|X-GM-THRID|INTERNALDATE|RFC822\.SIZE|BODYSTRUCTURE"
                            r"|BODY\.PEEK\[[A-Z0-9.]*(?: \([A-Z0-9 -]*\))?\](?:<[0-9]+\.[0-9]+>)?| )+\)$")
_SEARCH_ALLOWED = re.compile(r"^(ALL|SINCE [0-9]{1,2}-[A-Z][a-z]{2}-[0-9]{4}|X-GM-(MSGID|THRID) [0-9]+"
                             r"|UID [0-9]+:\*(?: SINCE [0-9]{1,2}-[A-Z][a-z]{2}-[0-9]{4})?)$")
_LITERAL = re.compile(rb"\{([0-9]+)\}$")


class GmailError(CerebroError, RuntimeError):
    """Gmail refused a command, or answered in a way this connector cannot read."""


class ReadOnlyError(GmailError):
    """A command that could change the mailbox, or mark a message read, was about to be sent. It never is."""


def quote(s: str) -> str:
    """`s` as an IMAP quoted string. A value with a line break cannot be quoted and is refused."""
    if any(c in s for c in "\r\n\x00"):
        raise GmailError(f"the imap value {s[:40]!r} holds a line break: it cannot be sent")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def uid_set(uids: Iterable[int]) -> str:
    """UIDs as an IMAP set, with runs of consecutive numbers written as ranges."""
    ordered = sorted(set(uids))
    parts, i = [], 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(f"{ordered[i]}:{ordered[j]}" if j > i else str(ordered[i]))
        i = j + 1
    return ",".join(parts)


# -- reading replies

@dataclass(frozen=True)
class Literal:
    """A literal string of a reply, as bytes."""

    data: bytes


def _scan(text: str, stack: list[list[Any]]) -> None:
    i = 0
    while i < len(text):
        c = text[i]
        if c in " \r\n":
            i += 1
        elif c == "(":
            stack.append([])
            i += 1
        elif c == ")":
            if len(stack) > 1:
                done = stack.pop()
                stack[-1].append(done)
            i += 1
        elif c == '"':
            j, value = i + 1, []
            while j < len(text) and text[j] != '"':
                if text[j] == "\\" and j + 1 < len(text):
                    j += 1
                value.append(text[j])
                j += 1
            stack[-1].append("".join(value))
            i = j + 1
        else:
            j, depth = i, 0
            while j < len(text) and (depth or text[j] not in " ()\r\n"):
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth = max(depth - 1, 0)
                j += 1
            atom = text[i:j]
            stack[-1].append(None if atom.upper() == "NIL" else atom)
            i = j


def tokens(data: Iterable[Any]) -> list[Any]:
    """The tokens of an imaplib reply's data, in which a literal comes as a pair of the text before it and its
    bytes. Parenthesized lists nest as lists; NIL is None."""
    stack: list[list[Any]] = [[]]
    for entry in data:
        if entry is None:
            continue
        if isinstance(entry, tuple):
            head, literal = entry[0], entry[1]
            match = _LITERAL.search(head)
            _scan((head[:match.start()] if match else head).decode("utf-8", "replace"), stack)
            stack[-1].append(Literal(literal))
        else:
            _scan(entry.decode("utf-8", "replace") if isinstance(entry, bytes) else str(entry), stack)
    while len(stack) > 1:  # a reply cut short still gives what it held
        done = stack.pop()
        stack[-1].append(done)
    return stack[0]


def _text(token: Any) -> str:
    if isinstance(token, Literal):
        return token.data.decode("utf-8", "replace")
    if isinstance(token, bytes):
        return token.decode("utf-8", "replace")
    return "" if token is None or isinstance(token, list) else str(token)


@dataclass
class Part:
    """One node of a message's body structure. `path` is the part number to fetch, such as 1 or 2.1. A part is an
    attachment when its disposition says so, or when it carries a file name, as a text file sent inline does."""

    path: str
    type: str = ""
    subtype: str = ""
    charset: str = ""
    encoding: str = ""
    size: int = 0
    attachment: bool = False
    children: list[Part] = field(default_factory=list)

    def text_parts(self) -> list[Part]:
        """The parts to read the text from, best first: plain before HTML. Attachments, parts that are not text, and
        a message attached whole with everything in it are never among them, so nothing else is fetched."""
        plain: list[Part] = []
        markup: list[Part] = []

        def walk(node: Part) -> None:
            if node.attachment or node.type == "message":
                return
            if node.type == "multipart":
                for child in node.children:
                    walk(child)
            elif node.type == "text" and node.subtype == "plain":
                plain.append(node)
            elif node.type == "text" and node.subtype == "html":
                markup.append(node)

        walk(self)
        return plain + markup


MAX_NESTING = 64  # parts within parts a message may hold; real mail holds a handful


def parse_structure(token: Any, path: str = "", depth: int = 0) -> Part:
    """A BODYSTRUCTURE value read into parts. `path` is the node's part number, empty for the top of a message. A
    structure nested past `MAX_NESTING` is refused, so a hostile message cannot exhaust the stack."""
    if depth > MAX_NESTING:
        raise GmailError(f"gmail sent a body structure nested more than {MAX_NESTING} parts deep: the message is not "
                         f"read; open it in gmail to see what it holds")
    if not isinstance(token, list) or not token:
        raise GmailError("gmail sent a body structure that is not a list: get the message again")
    part = Part(path=path)
    if isinstance(token[0], list):
        part.type = "multipart"
        i = 0
        while i < len(token) and isinstance(token[i], list):
            part.children.append(parse_structure(token[i], f"{path}.{i + 1}" if path else str(i + 1), depth + 1))
            i += 1
        if i < len(token):
            part.subtype = _text(token[i]).lower()
        return part
    part.path = path or "1"
    if len(token) < 7:
        raise GmailError("gmail sent a body structure too short to read: get the message again")
    part.type, part.subtype = _text(token[0]).lower(), _text(token[1]).lower()
    if isinstance(token[2], list):
        for key, value in zip(token[2][::2], token[2][1::2], strict=False):
            if _text(key).lower() == "charset":
                part.charset = _text(value)
            elif _text(key).lower() == "name":
                part.attachment = True
    part.encoding = _text(token[5]).lower()
    part.size = int(_text(token[6])) if _text(token[6]).isdigit() else 0
    at = 11 if (part.type, part.subtype) == ("message", "rfc822") else 9 if part.type == "text" else 8
    if at < len(token) and isinstance(token[at], list) and token[at]:
        disposition = token[at]
        if _text(disposition[0]).lower() == "attachment":
            part.attachment = True
        if len(disposition) > 1 and isinstance(disposition[1], list):
            part.attachment = part.attachment or any(_text(k).lower() == "filename" for k in disposition[1][::2])
    return part


@dataclass
class Item:
    """What a UID FETCH gave for one message. `parts` holds the bodies and header blocks by their key in upper case
    without a PEEK, such as BODY[HEADER.FIELDS (FROM TO)] or BODY[1]<0>."""

    uid: int = 0
    msgid: int = 0
    thrid: int = 0
    arrived: datetime | None = None
    size: int = 0
    structure: Part | None = None
    parts: dict[str, bytes] = field(default_factory=dict)


def _number(token: Any) -> int:
    text = _text(token)
    return int(text) if text.isdigit() else 0


def _item(pairs: list[Any]) -> Item:
    item = Item()
    for key, value in zip(pairs[::2], pairs[1::2], strict=False):
        key = _text(key).upper()
        if key == "UID":
            item.uid = _number(value)
        elif key == "X-GM-MSGID":
            item.msgid = _number(value)
        elif key == "X-GM-THRID":
            item.thrid = _number(value)
        elif key == "RFC822.SIZE":
            item.size = _number(value)
        elif key == "INTERNALDATE":
            try:
                item.arrived = datetime.strptime(_text(value).strip(), "%d-%b-%Y %H:%M:%S %z").astimezone(UTC)
            except ValueError:
                pass
        elif key == "BODYSTRUCTURE":
            item.structure = parse_structure(value)
        elif key.startswith(("BODY[", "BODY.PEEK[")):
            data = value.data if isinstance(value, Literal) else _text(value).encode()
            item.parts[key.replace("BODY.PEEK", "BODY", 1)] = data
    return item


# -- the session

class Session:
    """One read-only conversation with a Gmail mailbox over `imap`, an `imaplib.IMAP4`-shaped object already logged
    in. Each command passes `_gate` first."""

    def __init__(self, imap: Any, name: str = "gmail"):
        self.imap = imap
        self.name = name
        self.validity = 0
        self.next_uid = 0
        self.folder = ""

    def _gate(self, command: str, argument: str) -> None:
        allowed = ((command == "SEARCH" and _SEARCH_ALLOWED.match(argument))
                   or (command == "FETCH" and _FETCH_ALLOWED.match(argument.upper())))
        if not allowed:
            raise ReadOnlyError(f"{self.name}: refused to send uid {command.lower()} {argument[:60]}: the gmail "
                                f"connector reads only, and this command could change the mailbox or mark mail read")

    def _check(self, typ: Any, data: Any, what: str) -> Any:
        if typ != "OK":
            said = " ".join(masked_first(" ".join(_text(t) for t in tokens(data or []) if not isinstance(t, list))).split())
            throttled = any(word in said.lower() for word in ("throttled", "bandwidth limits", "exceeded command"))
            hint = ("google is limiting this mailbox's imap use: list again later" if throttled else
                    "list again later, and if it repeats, check the mailbox in gmail")
            raise GmailError(f"{self.name}: gmail answered {str(typ).lower()} to {what}: {said or 'no reason'}: "
                             f"{hint}")
        return data

    def open_all_mail(self) -> None:
        """Find All Mail by its \\All attribute, whatever its name in this account's language, and open it read
        only. Record its UIDVALIDITY and the UID the next message will get."""
        data = self._check(*self.imap.list(), "list")
        folder = ""
        for line in data or []:
            found = tokens([line])
            if len(found) >= 3 and isinstance(found[0], list) and any(_text(a).lower() == "\\all" for a in found[0]):
                folder = _text(found[-1])
                break
        if not folder:
            raise GmailError(f"{self.name}: imap shows no all mail folder: in gmail's settings under forwarding and "
                             f"pop/imap, let all mail show in imap")
        self.folder = folder
        self.examine()

    def examine(self) -> None:
        """Open All Mail read only again, and read its UIDVALIDITY and next UID as they are now."""
        self._check(*self.imap.select(quote(self.folder), readonly=True), "examine")
        self.validity = _number((self.imap.response("UIDVALIDITY")[1] or [b"0"])[-1])
        self.next_uid = _number((self.imap.response("UIDNEXT")[1] or [b"0"])[-1])

    def search(self, criteria: str) -> list[int]:
        """The UIDs a UID SEARCH finds, such as for SINCE 1-Nov-2026 or X-GM-MSGID 123, in order."""
        self._gate("SEARCH", criteria)
        data = self._check(*self.imap.uid("SEARCH", criteria), "uid search")
        return sorted({int(t) for t in tokens(data or []) if isinstance(t, str) and t.isdigit()})

    def fetch(self, uids: Iterable[int], items: str) -> list[Item]:
        """One Item per message the server answered for, in UID order. A FETCH a server sends unasked, such as a
        flag that changed, is no answer and is left out, and answers for one UID over several lines are one."""
        asked = set(uids)
        if not asked:
            return []
        self._gate("FETCH", items)
        data = self._check(*self.imap.uid("FETCH", uid_set(asked), items), "uid fetch")
        found: dict[int, Item] = {}
        flat = tokens(data or [])
        for token in flat:
            if not isinstance(token, list):
                continue
            item = _item(token)
            if item.uid not in asked:
                continue
            if item.uid in found:
                old = found[item.uid]
                for name in ("msgid", "thrid", "arrived", "size", "structure"):
                    if not getattr(old, name):
                        setattr(old, name, getattr(item, name))
                old.parts.update(item.parts)
            else:
                found[item.uid] = item
        return [found[uid] for uid in sorted(found)]

    def close(self) -> None:
        """Say goodbye. A connection already gone is fine."""
        try:
            self.imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def gmail_login(address: str, password: str, *, connect: Callable[..., Any] = imaplib.IMAP4_SSL) -> Callable[[], Any]:
    """A function that opens a logged-in session to Gmail's IMAP over TLS for one login. The caller owns the login:
    this module reads no password from anywhere. A refused login raises `GmailError` without the password."""

    def open_session() -> Any:
        imap = connect(GMAIL_HOST)
        try:
            imap.login(address, password)
        except imaplib.IMAP4.error as error:
            reason = str(error).replace(password, "***") if password else str(error)
            if hasattr(imap, "shutdown"):
                imap.shutdown()
            raise GmailError(f"gmail refused the login for {address}: {one_line(reason, 120)}: make a new app password "
                             f"and connect gmail again") from None
        return imap

    return open_session
