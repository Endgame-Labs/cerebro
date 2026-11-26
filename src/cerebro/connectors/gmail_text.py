"""A mail message's text as a source reads it, with the standard library alone: its header block, a text part
in its transfer encoding and charset, HTML turned into text, and the message reduced to what its sender newly wrote.

`reduce` is the one place that decides what is new. It drops quoted lines, the "On ... wrote:" line that introduces
them, an Outlook header block or "-----Original Message-----" and everything after it, and the signature: a "-- "
line, however many spaces follow it, and what follows it, a "--" line or a sign-off such as "Best," with the few
short lines under it, a mobile footer, or a closing block of a name with a title, company, phone or address line
under it, which may end in a common abbreviation such as Inc. A P.S. under a sign-off stays. An "On ... wrote:" line
with unquoted text under it ends the message only when a mail client wrote it, with an address, a time or a year in
it, or when the text under it is indented as some clients quote. A sentence of the message that ends in "wrote:"
stays. Its rules are conservative: a line it cannot place as quote or signature stays.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import formataddr, getaddresses, parseaddr, parsedate_to_datetime

from ..connector import PUBLIC_MAIL, is_public_mail, masked_first
from .questions import machine_address

__all__ = [
    "PUBLIC_MAIL",
    "Address",
    "Headers",
    "cut",
    "decode_body",
    "html_to_text",
    "is_public_mail",
    "lower_ascii",
    "machine_address",
    "parse_addresses",
    "parse_headers",
    "reduce",
]

_S = r"[\t\n\f\r ]"  # whitespace as the parser reads it: ASCII alone
_ATTRIBUTION_END = ("wrote:", "wrote :", "a écrit :", "a écrit:", "schrieb:", "escribió:", "ha scritto:", "schreef:")
_ATTRIBUTION_OPEN = ("on ", "le ", "am ", "el ", "il ", "op ")
_ORIGINAL = re.compile(rf"^[-_ ]{{3,}}{_S}*(original message|original appointment|reply message){_S}*[-_ ]*$", re.IGNORECASE)
ORIGINAL_MAX = 200  # a separator line is short: a longer one is never tried, so the pattern stays linear


def _original(line: str) -> bool:
    """Whether a line is a "----- Original Message -----" separator. The pattern backtracks on long runs of dashes
    and spaces, so it runs only on a short line that holds one of its words."""
    lower = line.lower()
    return (len(line) <= ORIGINAL_MAX and ("original" in lower or "reply message" in lower)
            and _ORIGINAL.match(line) is not None)


_UNDERSCORE_RULE = re.compile(r"^_{8,}$")
_OUTLOOK_FROM = re.compile(r"^\*?\*?(from|de|von):\*?\*?[\t\n\f\r ]+[^\t\n\f\r ]", re.IGNORECASE)
_OUTLOOK_SENT = re.compile(rf"^\*?\*?(sent|date|envoy[ée]|gesendet):\*?\*?{_S}", re.IGNORECASE)
_OUTLOOK_TO = re.compile(rf"^\*?\*?(to|subject|cc|à|an|objet|betreff):\*?\*?{_S}", re.IGNORECASE)
_MOBILE_FOOTER = re.compile(r"^(sent from my |sent from mail for |sent from outlook for |get outlook for |sent via "
                            r"|sent from yahoo mail)", re.IGNORECASE)
_POSTSCRIPT = re.compile(rf"^p\.?{_S}?p?\.?{_S}?s\b[.:]?", re.IGNORECASE | re.ASCII)
_CLIENT_ATTRIBUTION = re.compile(r"@|[0-9]{1,2}:[0-9]{2}|\b[0-9]{4}\b", re.ASCII)
_LIST_MARKER = re.compile(rf"^([-*•]|[0-9]+[.)]){_S}")
_SIGN_OFF = re.compile(
    rf"^(thanks|thank you|(thanks|thank you)( and|,|{_S}&)?{_S}+((best|kind|warm|warmest){_S}+)?regards|many thanks"
    r"|thanks again|thanks so much|thanks very much|best|best regards|kind regards|warm regards|warmest regards"
    r"|regards|best wishes|cheers|sincerely|yours sincerely|yours truly|cordially|talk soon|speak soon|take care"
    rf"|with thanks|with appreciation|br|rgds|v/r){_S}*[,.!]?$", re.IGNORECASE)
_SIGNATURE_LINES = 6  # how many lines under a sign-off may still be signature
_SIGNATURE_WIDTH = 70  # the longest line, in characters, that counts as signature under a sign-off
_ABBREVIATIONS = ("inc", "ltd", "co", "corp", "llc", "llp", "plc", "st", "ave", "rd", "blvd", "dr", "ln", "ste", "jr",
                  "sr", "phd", "esq")

# Public mail services. An address at one says nothing about a company, so its domain is never a key.


# -- reduce

def reduce(text: str) -> str:
    """What a message's sender newly wrote: the message without its quoted replies and its signature. A forward or a
    quote with nothing but a signature, a sign-off or a footer above it is kept from where it begins, less its own
    signature, so its words are not lost. Line endings become \\n, the end of each line is trimmed, and runs of blank
    lines shrink to one."""
    lines = _split_lines(text)
    kept = _drop_quoted(lines)
    own = _drop_signature(kept)
    start = next((k for k, line in enumerate(kept) if line != lines[k]), len(kept))  # kept is lines up to the first one dropped
    if start < len(lines) and not any(_written(line) for line in own[:_closing_block(own)]):
        while start and not _written(lines[start - 1]):
            start -= 1
        own = _drop_signature(lines[start:])
    return _collapse_blank("\n".join(own))


def _written(line: str) -> bool:
    """Whether a line holds words, other than a line a mail client puts above a forward, such as
    "---------- Forwarded message ---------"."""
    t = line.strip()
    return t != "" and not (t.startswith("---") and t.endswith("---"))


def _split_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _quoted(line: str) -> bool:
    return line.lstrip(" \t").startswith(">")


def _drop_quoted(lines: list[str]) -> list[str]:
    """The lines a message keeps once quoted lines and what introduces them are gone. An Outlook header block or an
    original-message line ends the message: all that follows is the earlier mail."""
    out: list[str] = []
    wrote = False
    i = 0
    while i < len(lines):
        line = lines[i]
        trimmed = line.strip()
        if (_original(trimmed) or (_UNDERSCORE_RULE.match(trimmed) and _outlook_block_at(lines, i + 1))
                or (_OUTLOOK_FROM.match(trimmed) and _outlook_block_at(lines, i))):
            return out
        n = _attribution_lines(lines, i)
        if n > 0:
            following = _next_text(lines, i + n)
            if following == "" or _quoted(following):
                i += n  # it introduces quoted lines, or ends the message
                continue
            if _indented(following) or _CLIENT_ATTRIBUTION.search(" ".join(lines[i:i + n])):
                if wrote:  # quoted without markers, under a line a mail client wrote
                    return out
                i += n
                continue
            # A sentence of the message that ends in "wrote:", with the message going on below: nothing is quoted.
        if not _quoted(line):
            out.append(line)
            wrote = wrote or trimmed != ""
        i += 1
    return out


def _next_text(lines: list[str], start: int) -> str:
    return next((line for line in lines[start:] if line.strip()), "")


def _indented(line: str) -> bool:
    return line.startswith(("\t", "  "))


def _attribution_lines(lines: list[str], i: int) -> int:
    """How many lines from `i` make an "On ... wrote:" line, one to three as a client wraps it, or 0."""
    joined = lines[i].strip().lower()
    if not joined.startswith(_ATTRIBUTION_OPEN):
        return 0
    for n in range(1, 4):
        if joined.endswith(_ATTRIBUTION_END):
            return n
        if i + n >= len(lines) or len(joined) > 300:
            return 0
        following = lines[i + n].strip().lower()
        if not following:
            return 0
        joined += " " + following
    return 0


def _outlook_block_at(lines: list[str], i: int) -> bool:
    """Whether the lines from `i` read as the header block an Outlook reply puts above the earlier mail: a From,
    then a Sent or Date, then a To or Subject, within a few lines."""
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not _OUTLOOK_FROM.match(lines[i].strip()):
        return False
    sent = to = False
    for line in lines[i + 1:i + 9]:
        t = line.strip()
        if _OUTLOOK_SENT.match(t):
            sent = True
        elif _OUTLOOK_TO.match(t):
            to = True
    return sent and to


def _trim_blank_end(lines: list[str]) -> list[str]:
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def _drop_signature(lines: list[str]) -> list[str]:
    """The lines without a signature at the end. A P.S. under a sign-off is the sender's words and stays."""
    lines = _trim_blank_end(lines)
    while lines and _MOBILE_FOOTER.match(lines[-1].strip()):
        lines = _trim_blank_end(lines[:-1])
    i = len(lines) - 1
    while i >= 0 and len(lines) - i <= 21:
        # With a space after it, however much follows, it is the signature marker. Without one, as HTML gives it,
        # it is one only when what follows is short enough to be a signature: in the body it is a divider.
        if lines[i].rstrip(" \t") == "--" and (lines[i].startswith("-- ") or _short_block(lines[i + 1:])):
            return lines[:i]
        i -= 1
    non_blank = 0
    j = len(lines) - 1
    while j >= 0 and non_blank <= 10:
        t = lines[j].strip()
        if t:
            non_blank += 1
            if _SIGN_OFF.match(t):
                under = lines[j + 1:]
                ps = next((k for k, line in enumerate(under) if _POSTSCRIPT.match(line.strip())), len(under))
                if _short_block(under[:ps]):
                    return lines[:j] + [""] + under[ps:]
        j -= 1
    return _drop_closing_block(lines)


def _short_block(lines: list[str]) -> bool:
    found = [line.strip() for line in lines if line.strip()]
    return len(found) <= _SIGNATURE_LINES and all(len(line) <= _SIGNATURE_WIDTH for line in found)


def _drop_closing_block(lines: list[str]) -> list[str]:
    """The lines without a closing block, after the message's own text."""
    start = _closing_block(lines)
    return lines[:start] if any(line.strip() for line in lines[:start]) else lines


def _closing_block(lines: list[str]) -> int:
    """Where a closing block at the end of the lines begins, or their length for none: a last paragraph that starts
    with a full name, has a title, company, phone or address line under it and holds no sentence. Names alone are
    none."""
    start = len(lines)
    while start > 0 and lines[start - 1].strip():
        start -= 1
    block = lines[start:]
    if not 2 <= len(block) <= _SIGNATURE_LINES or not _full_name(block[0]):
        return len(lines)
    if not all(_signature_line(line) for line in block) or all(_full_name(line) for line in block):
        return len(lines)
    return start


def _full_name(line: str) -> bool:
    words = line.split()
    return 2 <= len(words) <= 4 and all(
        w[0].isupper() and all(c.isalpha() or c in ".'-" for c in w) for w in words)


def _signature_line(line: str) -> bool:
    t = line.strip()
    if not t or len(t) > 60 or len(t.split()) > 8 or _LIST_MARKER.match(t):
        return False
    if t[-1] in ".?!:;," and not _ends_in_abbreviation(t):
        return False
    first = t[0]
    return first.isupper() or first.isdecimal() or first in "+(" or "@" in t or "://" in t or t.startswith("www.")


def _ends_in_abbreviation(t: str) -> bool:
    last = t.split()[-1].lower()
    return last.endswith(".") and last[:-1] in _ABBREVIATIONS


def cut(text: str, limit: int) -> tuple[str, bool]:
    """`text` cut to at most `limit` bytes of UTF-8, at a line break when one lies in the last quarter and always at
    a character boundary, and whether it was cut. It is masked as `masked_first` masks it first."""
    text = masked_first(text)
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    end = limit
    while end > 0 and (data[end] & 0xC0) == 0x80:
        end -= 1
    newline = data.rfind(b"\n", 0, end)
    if newline > limit - limit // 4:
        end = newline
    return data[:end].decode("utf-8").rstrip(" \t\r\n"), True


# -- HTML

_BLOCK_TAGS = frozenset(("p", "div", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section",
                         "article", "header", "footer", "pre", "address", "dl", "dt", "dd", "center", "form"))
_SKIP_TAGS = frozenset(("style", "script", "head", "title", "noscript"))
_HTML_SPACE = " \n\t\r "


@dataclass
class _Writer:
    lines: list[str] = field(default_factory=list)
    cur: str = ""
    depth: int = 0  # the blockquote depth the current line opened at
    pending: bool = False  # a space is owed before the next word on the line

    def text(self, s: str) -> None:
        for c in s:
            if c in _HTML_SPACE:
                self.pending = True
                continue
            if self.pending and self.cur:
                self.cur += " "
            self.pending = False
            self.cur += c

    def raw(self, s: str) -> None:
        if self.cur and self.pending:
            self.cur += " "
        self.pending = False
        self.cur += s

    def newline(self, depth: int) -> None:
        prefix = "> " * self.depth
        if not self.cur and self.depth:
            prefix = prefix.rstrip(" ")
        self.lines.append(prefix + self.cur)
        self.cur, self.pending, self.depth = "", False, depth

    def ensure_break(self, depth: int) -> None:
        if self.cur:
            self.newline(depth)
        else:
            self.depth = depth


def html_to_text(src: str) -> str:
    """An HTML body as text: tags dropped, block tags and line breaks become line breaks, list items get a dash,
    entities decoded, a blockquote's lines marked with > so `reduce` takes them for quotes, and what a reader does
    not see, such as style and script, left out."""
    w = _Writer()
    quote = pre = 0
    skip = ""
    i = 0

    def emit(s: str) -> None:
        if not pre:
            w.text(s)
            return
        for k, part in enumerate(s.split("\n")):
            if k:
                w.newline(w.depth)
            w.raw(part.rstrip("\r"))

    while i < len(src):
        lt = src.find("<", i)
        if lt < 0:
            if not skip:
                emit(html.unescape(src[i:]))
            break
        if lt > i and not skip:
            emit(html.unescape(src[i:lt]))
        i = lt
        if src.startswith("<!--", i):
            end = src.find("-->", i + 4)
            if end < 0:
                break
            i = end + 3
            continue
        end = _tag_end(src, i)
        if end < 0:
            if not skip:
                emit(html.unescape(src[i:]))
            break
        tag = src[i + 1:end]
        i = end + 1
        closing = tag.startswith("/")
        name = _tag_name(tag.removeprefix("/"))
        if skip:
            if closing and name == skip:
                skip = ""
            continue
        if name in _SKIP_TAGS and not closing and not tag.endswith("/"):
            skip = name
        elif name == "br":
            w.newline(quote)
        elif name == "hr":
            w.ensure_break(quote)
            w.raw("_" * 32)
            w.newline(quote)
        elif name == "blockquote":
            w.ensure_break(quote)
            quote = max(quote - 1, 0) if closing else quote + 1
            w.depth = quote
        elif name == "li":
            w.ensure_break(quote)
            if not closing:
                w.raw("- ")
                w.pending = False
        elif name == "pre":
            w.ensure_break(quote)
            pre = max(pre - 1, 0) if closing else pre + 1
        elif name in ("td", "th"):
            w.pending = True
        elif name in _BLOCK_TAGS:
            w.ensure_break(quote)
    w.ensure_break(0)
    return _collapse_blank("\n".join(w.lines))


def _tag_end(src: str, i: int) -> int:
    quote = ""
    for j in range(i + 1, len(src)):
        c = src[j]
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == ">":
            return j
    return -1


def _tag_name(tag: str) -> str:
    match = re.search(r"[ \t\r\n/]", tag)
    return (tag[:match.start()] if match else tag).lower()


def _collapse_blank(s: str) -> str:
    out: list[str] = []
    blank = 0
    for line in s.replace("\r\n", "\n").split("\n"):
        line = line.rstrip(" \t")
        if ">" in line and not line.strip("> "):
            line = ">"
        if line:
            blank = 0
        else:
            blank += 1
            if blank > 1:
                continue
        out.append(line)
    return "\n".join(out).strip()


# -- bodies and charsets

_CP1252_HIGH = ("€\x81‚ƒ„…†‡ˆ‰Š‹Œ\x8dŽ\x8f"
                "\x90‘’“”•–—˜™š›œ\x9džŸ")


def _decode_charset(charset: str, data: bytes) -> str:
    """Bytes in a charset as text. UTF-8, US-ASCII, ISO-8859-1 and Windows-1252 decode as they are, and any other
    charset is read as UTF-8, with a replacement character for each run of bytes that is not."""
    name = charset.strip().lower()
    if name in ("iso-8859-1", "latin1", "iso8859-1", "l1"):
        return data.decode("latin-1")
    if name in ("windows-1252", "cp1252", "iso-8859-15"):
        return "".join(_CP1252_HIGH[b - 0x80] if 0x80 <= b <= 0x9F else chr(b) for b in data)
    return re.sub("�+", "�", data.decode("utf-8", errors="replace"))


def decode_body(data: bytes, encoding: str, charset: str) -> str:
    """A text part's bytes as text: its transfer encoding, base64 or quoted-printable, undone, then its charset. A
    body cut short, as a partial fetch leaves it, decodes as far as it goes."""
    kind = encoding.strip().lower()
    if kind == "base64":
        clean = bytes(b for b in data if b not in b"\r\n \t").rstrip(b"=")
        if len(clean) % 4 == 1:
            clean = clean[:-1]
        try:
            data = base64.b64decode(clean + b"=" * (-len(clean) % 4))
        except binascii.Error:
            data = b""
    elif kind == "quoted-printable":
        data = binascii.a2b_qp(data)
    if charset.strip().lower() in ("", "utf-8", "us-ascii"):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return _decode_charset(charset, data)


# -- headers

@dataclass
class Address:
    """One address of a header, with its display name when it has one. `email` has its ASCII letters lowered."""

    name: str
    email: str

    def format(self) -> str:
        """The address as a header writes it: `Name <email>`, or the bare address."""
        return formataddr((self.name, self.email)) if self.name else self.email

    def domain(self) -> str:
        return self.email.partition("@")[2]


@dataclass
class Headers:
    """What the connector reads of a message's header block. A header that is absent or does not read is empty."""

    from_: list[Address] = field(default_factory=list)
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    date: datetime | None = None
    subject: str = ""
    list_unsubscribe: bool = False
    list_id: bool = False
    precedence: str = ""
    auto_submitted: str = ""

    def bulk(self) -> bool:
        """Whether the headers mark the message as bulk or automatic mail: a List-Unsubscribe or List-Id header,
        Precedence bulk, list or junk, or an Auto-Submitted header other than no. The sender is not looked at, so a
        noreply sender inside a human thread stays."""
        return (self.list_unsubscribe or self.list_id or self.precedence in ("bulk", "list", "junk")
                or self.auto_submitted not in ("", "no"))

    def participants(self) -> list[Address]:
        """The From, To and Cc addresses, once each by address."""
        seen: dict[str, Address] = {}
        for address in self.from_ + self.to + self.cc:
            if address.email:
                seen.setdefault(address.email, address)
        return list(seen.values())


def lower_ascii(s: str) -> str:
    """`s` with its ASCII letters lowered and no other, so a letter that lowers to an ASCII one stays as written."""
    return s.translate(_LOWER)


_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def _decode_words(value: str) -> str:
    try:
        return str(make_header(decode_header(value))).strip()
    except (LookupError, ValueError, UnicodeError, binascii.Error):
        return value.strip()


def _split_addresses(value: str) -> list[str]:
    """A header split on the commas outside quotes and angle brackets, for a list one bad address spoiled."""
    out, quoted, angle, start = [], False, False, 0
    for i, c in enumerate(value):
        if c == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif c == "<":
            angle = True
        elif c == ">":
            angle = False
        elif c == "," and not angle:
            out.append(value[start:i])
            start = i + 1
    return out + [value[start:]]


def _address(name: str, email: str) -> Address | None:
    email = email.strip()
    if "@" not in email or any(c in email for c in " \t\r\n<>\""):
        return None
    return Address(_decode_words(name), lower_ascii(email))


def parse_addresses(value: str) -> list[Address]:
    """The addresses of an address header. One that does not read is skipped and the rest kept."""
    value = value.strip()
    if not value:
        return []
    pairs = getaddresses([value])
    if pairs == [("", "")] or any(not email for _, email in pairs):
        pairs = [parseaddr(part) for part in _split_addresses(value)]
    return [a for a in (_address(name, email) for name, email in pairs) if a is not None]


def parse_headers(block: bytes) -> Headers:
    """A header block, such as the reply to a fetch of some header fields. A header that does not read is left
    empty: a message is never refused for one bad header."""
    message = BytesHeaderParser(policy=compat32).parsebytes(block)

    def get(name: str) -> str:
        value = message.get(name)
        return "" if value is None else str(value)

    found = Headers(from_=parse_addresses(get("From")), to=parse_addresses(get("To")), cc=parse_addresses(get("Cc")),
                    subject=" ".join(_decode_words(get("Subject")).split()), list_unsubscribe="List-Unsubscribe" in message,
                    list_id="List-Id" in message, precedence=get("Precedence").strip().lower(),
                    auto_submitted=get("Auto-Submitted").strip().lower())
    if get("Date"):
        try:
            when = parsedate_to_datetime(get("Date"))
            found.date = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
        except (TypeError, ValueError, IndexError):
            pass
    return found
