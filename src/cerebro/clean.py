"""Clean an article's markdown at rest: one canonical form, and nothing in it that runs as code or reaches out.

`clean` does two things, kept apart.

Format, with no change of meaning: Unix line ends, NFC, plain spaces for odd ones, tabs expanded, no trailing space
(a hard break of two spaces becomes a backslash), one blank line between blocks and none at either end, one final
newline, `#` headings for underlined ones, `-` bullets, `***` for a rule, a table one row to a line with its short
rows padded, a reference link written inline, and a fence left open closed where a closing line closes it. None of it
is listed.

Make safe, each change listed in `Cleaned.removed` with why. Safe means safe in cmark-gfm and markdown-it,
each with raw HTML allowed. The test of record is those renderers, not
this module's own reading of Markdown: a thing stays raw only where every one of them reads it the same way, and where
two could read it differently it is written so that all of them agree, or escaped. Outside code, a `<` that could
open HTML is written `&lt;`; a backtick run that closes no code span is escaped; a table's cells are cleaned one by one;
a paragraph's lines in a quote or a list carry its marks; a line that could read as a link's definition is escaped;
a bare address a renderer would link to another scheme, or would swallow a `<` into, is shown as text or code; a whole
part starts at the left margin. A link whose address is not http, https or mailto keeps its words and loses the
address; an image whose address the caller did not hand in becomes its words; a footnote definition the text writes
goes; control and invisible characters go; a block of front matter at the top goes; a heading at or above the part's
own level moves below it; a fence's info string that is not a plain language name goes. `Cleaned.noted` lists what is
reported and left: an outside link no handed-in address holds.

Other renderers are not promised: MDX runs `{...}` and `import` lines as code, Python-Markdown and mistune read
some escapes otherwise, and a site builder may run template syntax.

Inside a yaml fence nothing changes, byte for byte, nor inside any other fence but for invisible characters.

`land` is the one way text lands: masked, as `masked` says, then cleaned, and masked and cleaned once more when the
cleaned text holds a value the mask now sees.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import pairwise
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt
from markdown_it.common.utils import normalizeReference
from markdown_it.token import Token

if TYPE_CHECKING:
    from .article import Citation

__all__ = ["MASK_WORDS", "Cleaned", "clean", "inert", "label", "land", "land_heading", "land_line", "masked",
           "underlined_as_atx", "unlabeled"]

NEST = 32  # parentheses nested in a link's destination past which CommonMark renderers stop reading it as one
ROUNDS = 8  # a pass that changes text is run again on its own output, up to this many times in all
SHOWN = 60  # characters of a removed thing quoted in its line

SAFE_SCHEMES = frozenset(("http", "https", "mailto"))
OUTSIDE_SCHEMES = frozenset(("http", "https"))
DATA_LANGUAGES = frozenset(("yaml", "yml"))

INVISIBLE = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\u00ad\\u061c\\u180e\\u200b-\\u200f\\u202a-\\u202e"
                       "\\u2028\\u2029\\u2060-\\u2064\\u2066-\\u2069\\ufeff\\ufff9-\\ufffb"
                       "\\U000e0000-\\U000e007f\\U000e0100-\\U000e01ef]")
ODD_SPACE = re.compile("[\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]")
TRANSPARENT = re.compile(INVISIBLE.pattern[:-1] + ODD_SPACE.pattern[1:])  # what a held value may hide behind
JOINER = "\u200d"
MASK_WORDS = ("[secret]", "[a token, masked]")  # what the workspace's mask writes in place of a value or a token

ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
ASCII_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*):")

OPENS = re.compile(r"<(?=[A-Za-z/!?])")  # a `<` that could open HTML: a tag, a closing tag, a comment, a PI, a declaration
AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*)>")
EMAIL_LINK = re.compile(r"<([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)>")
MARKER = re.compile(r"\^[^\]\s]+")
SEEN_THROUGH = re.compile(TRANSPARENT.pattern + r"|\n[\s>]*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+)?|&(?:#[0-9]{1,7}|#[xX]"
                          r"[0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});|\\[!-/:-@\[-`{-~]")
MARKS = re.compile(r"[ \t>]*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+[ \t>]*)*")  # a line's quotes, items and indent
TASK = re.compile(r"\[[ xX]\][ \t]+$")  # a task list's box, at the end of what comes before
UNESCAPED = re.compile(r"\\[!-/:-@\[-`{-~]|[!-/:-@\[-`{-~]")  # an escape, or a mark that no backslash escapes
DELIMITER = re.compile(r"\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")  # what GitHub reads as a table's second row
CITED = re.compile(r"\[\^([^\]\s]+)\]")  # a citation's marker, as the wiki's evidence reads one
ADDRESS = re.compile(r"(?:https?://|ftp://|www\.)", re.IGNORECASE)  # where any renderer may start a bare address
BARE_FTP = re.compile(r"ftp:", re.IGNORECASE)
MAILISH = re.compile(r"(?:[A-Za-z0-9]|\\?[.+_-]|(?:mailto|xmpp)\\?:)*?(mailto|xmpp)\\?:(?:[A-Za-z0-9]|\\?[.+_-])*\\?@"
                     r"(?:[A-Za-z0-9]|\\?[._-])+(?:/[^\s<`\\]*)?", re.IGNORECASE)
MAIL_SCHEME = re.compile(r"mailto|xmpp", re.IGNORECASE)
FENCE = re.compile(r"^((?:[ >]|[-+*] +|\d{1,9}[.)] +)*?)(`{3,}|~{3,})(.*)$")
PLAIN_INFO = re.compile(r"^[A-Za-z0-9_+#.-]+$")
FOOTNOTE = re.compile(r"^[ >]*\[\^([^\]\s]+)\]:")
BLOCK_START = re.compile(r"^(?:#|>|[-+*](?:\s|$)|\d{1,9}[.)]|\||`{3}|~{3}|\[\^[^\]\s]+\]:)")
LINE_START = re.compile(r"^(?:[#>=|]|[-+*](?:\s|$)|`{3}|~{3}|\d{1,9}[.)](?:\s|$))")
FRONT = re.compile(r"^(?:[\w\"'.-][^:=]*(?::|\s=)(?:\s|$)|\s|- |#|\[)")
HEADING = re.compile(r"^([ >]*)(#{1,6})(?:[ \t]+|$)(.*)$")
RULE = re.compile(r"^([ >]*)([-*_])(?:[ \t]*\2){2,}[ \t]*$")
CELL_SPLIT = re.compile(r"(?<!\\)\|")


@dataclass
class Cleaned:
    """Text as `clean` leaves it, a line for each thing it removed or escaped to make the text safe, and a line for
    each thing it reports and leaves as written."""

    text: str
    removed: list[str] = field(default_factory=list)
    noted: list[str] = field(default_factory=list)


def clean(text: str, *, images: tuple[str, ...] | frozenset[str] | set[str] | list[str] = (),
          links: tuple[str, ...] | frozenset[str] | set[str] | list[str] | None = None, top: int = 1,
          cited: Iterable[str] = ()) -> Cleaned:
    """`text`, an article's markdown or one part of it, in canonical form and made safe, as the module says.

    `images` are the addresses an image may load, the ones the cited evidence holds: any other image becomes its
    words. `links`, when given, are the outside addresses the cited evidence holds, and an outside link to any other
    is noted, never removed. `top` is the level of the heading the text sits under: a heading in it at that level or
    above moves to the level just below it, and every other heading stays where it is. `cited` are the ids of the
    citations the text's markers name, each written as its label. Pure: the same text and arguments always give the
    same result."""
    allowed = frozenset(_key(url) for url in images)
    known = None if links is None else frozenset(_key(url) for url in links)
    top = min(max(top, 1), 5)
    out = Cleaned(text)
    for _ in range(ROUNDS):
        before = out.text
        _Pass(out, allowed, known, top, frozenset(cited)).run()
        if out.text == before:
            break
    out.removed = list(dict.fromkeys(out.removed))
    out.noted = list(dict.fromkeys(out.noted))
    return out


def _view(text: str) -> tuple[str, list[int], list[int]]:
    """`text` as a reader reads its characters, with where each came from: entities read, escapes read, and invisible
    characters, soft hyphens, no-break spaces and each line break with the marks after it taken out."""
    view: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    at = 0
    for m in SEEN_THROUGH.finditer(text):
        for k in range(at, m.start()):
            view.append(text[k])
            starts.append(k)
            ends.append(k + 1)
        said = m.group()
        if said.startswith("&"):
            said = html.unescape(said)
        elif said.startswith("\\") and len(said) == 2:
            said = said[1]
        else:
            said = ""
        for c in said:
            if not TRANSPARENT.match(c):
                view.append(c)
                starts.append(m.start())
                ends.append(m.end())
        at = m.end()
    for k in range(at, len(text)):
        view.append(text[k])
        starts.append(k)
        ends.append(k + 1)
    return "".join(view), starts, ends


def _read(m: re.Match[str]) -> str:
    """What a reader reads of one thing `SEEN_THROUGH` matched, as `_view` reads it."""
    said = m.group()
    if said.startswith("&"):
        return TRANSPARENT.sub("", html.unescape(said))
    return said[1] if said.startswith("\\") and len(said) == 2 else ""


def masked(text: str, mask: Callable[[str], str] | None) -> str:
    """`text` with `mask` run on it as written, then again on it as a reader reads it, so a held value is masked
    whole when an invisible character, a soft hyphen, a no-break space or a line break with a quote's or a list's
    marks breaks it, or when it is written with entities or escapes. Every character the second mask did not cover
    stays as it was. Nothing is folded, joined or escaped before both have run."""
    if mask is None:
        return text
    first = mask(text)
    if not SEEN_THROUGH.search(first):
        return first
    plain = SEEN_THROUGH.sub(_read, first)
    if mask(plain) == plain:
        return first
    view, starts, ends = _view(first)
    seen = mask(view)
    out: list[str] = []
    at = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, view, seen, autojunk=False).get_opcodes():
        if tag == "equal":
            out.append(first[at:ends[i2 - 1]])
            at = ends[i2 - 1]
            continue
        start = starts[i1] if i1 < len(starts) else len(first)
        out += [first[at:max(at, start)], seen[j1:j2]]
        at = max(at, ends[i2 - 1]) if i2 > i1 else max(at, start)
    out.append(first[at:])
    kept = "".join(out)
    plain = _view(kept)[0]
    return kept if mask(plain) == plain else seen  # an alignment that left a value readable gives way to the reading


def land(text: str, mask: Callable[[str], str] | None = None, *, images: Iterable[str] = (),
         links: Iterable[str] | None = None, top: int = 1, cited: Iterable[str] = ()) -> Cleaned:
    """`text` as it lands in an article: masked by `mask`, as `masked` says, then cleaned as `clean` says, with
    `images`, `links`, `top` and `cited` as there. Masking comes first, so cleaning never escapes, joins or folds a held value
    out of the mask's sight, and the mask's own words, such as `[secret]`, are shown as written and never read as
    markup. When the cleaned text holds a value the mask now sees, it is masked and cleaned once more, and no more
    than that. What cleaning says quotes only text the mask has seen."""
    first = masked(text, mask)
    done = clean(first, images=tuple(images), links=None if links is None else tuple(links), top=top, cited=cited)
    if mask is not None and (again := masked(done.text, mask)) != done.text:
        more = clean(again, images=tuple(images), links=None if links is None else tuple(links), top=top,
                     cited=cited)
        done = Cleaned(more.text, done.removed + more.removed, done.noted + more.noted)
    done.removed, done.noted = list(dict.fromkeys(done.removed)), list(dict.fromkeys(done.noted))
    return done


def land_line(text: str, mask: Callable[[str], str] | None = None) -> Cleaned:
    """`text` on one line, landed as `land` lands a part, read as words in the middle of a line, as a footnote's quote
    and a part's heading sit: nothing in it starts a block, so a quote that begins `#` or `-` stays words. It is
    masked before its line breaks are folded."""
    lead = "q "  # words before it, so the cleaner reads it where it will stand
    done = land(lead + " ".join(masked(text, mask).split()), mask, top=6)
    said = " ".join(done.text.split())
    done.text = said.removeprefix(lead) if said != lead.strip() else ""
    return done


def land_heading(text: str, mask: Callable[[str], str] | None = None) -> Cleaned:
    """A part's heading as it lands: as `land_line` lands it, and with `#` marks at its end escaped, which a renderer
    would read as the heading's closing marks and drop."""
    done = land_line(text, mask)
    done.text = re.sub(r"(^|\s)(#+)$", r"\1\\\2", done.text)
    return done


PLAIN_ID = re.compile(r"[^\W_][\w .:/@#%+=,()'-]*")  # words no renderer reads as markup, from a letter or digit
LABEL_KEPT = re.compile(r"[\w.\-/#=+,@:]+")  # what a footnote's label holds as written
LABELLED = re.compile(r"(?:[\w.\-/#=+,@:]|%[0-9A-F]{2})+")  # a label already written


def _bare_bad(text: str) -> bool:
    """Whether a renderer that links bare addresses would link something in `text` to a scheme other than http,
    https or mailto: an ftp address, or an address to xmpp or to a longer scheme ending in mailto, which GitHub's
    renderer reads past any escape."""
    return bool(BARE_FTP.search(text)) or any(m.group(1).lower() == "xmpp" or m.start(1) > m.start()
                                               for m in MAILISH.finditer(text))


def inert(text: str) -> str:
    """`text` on one line, written so no renderer reads markup in it, for what is shown and never cleaned, such as a
    record's id: as it is when it is plain words and addresses to http, https or mailto, else as code, and with no
    invisible character either way. Only what is shown changes: the id itself is kept as the source gave it."""
    said = " ".join(INVISIBLE.sub("", text).split())
    if not said:
        return ""
    if PLAIN_ID.fullmatch(said) and not re.match(r"\d+[.)](?: |$)", said) and not _bare_bad(said) and \
            all(_kind(word)[0] != "bad" for word in said.split()):
        return said
    fence = "`" * (max((len(run) for run in re.findall("`+", said)), default=0) + 1)
    pad = " " if said.startswith("`") or said.endswith("`") else ""
    return f"{fence}{pad}{said}{pad}{fence}"


def label(id: str) -> str:
    """The label a citation of `id` has, both where the text cites it, `[^label]`, and on its footnote: the id as
    written when it is letters, digits and `_.-/#=+,@:`, else with every other character, `%` too, written as `%` and
    its UTF-8 bytes in hex, a colon too when a renderer could link it, so no renderer reads markup in it and no two
    ids share a label."""
    bad = _bare_bad(id)
    if LABEL_KEPT.fullmatch(id) and not bad:
        return id
    return "".join(c if LABEL_KEPT.fullmatch(c) and not (bad and c == ":") else
                   "".join(f"%{b:02X}" for b in c.encode()) for c in id)


def _marker(inner: str, cited: frozenset[str]) -> str:
    """What a citation's marker holding `inner` is written as: a label already written as it stands, the label of the
    citation it names, or, when it names no citation, the label `inner` would have. A label comes first, so landing
    what landed gives the same bytes; an id written as another cited id's label reads as that label."""
    if inner in {label(id) for id in cited}:
        return inner
    if inner in cited:
        return label(inner)
    if LABELLED.fullmatch(inner) and not _bare_bad(inner) and ("%" in inner or not cited):
        return inner
    return label(inner)


def unlabeled(text: str, citations: Iterable[Citation]) -> str:
    """`text` with each citation's label written back as its id, `[^label]` as `[^id]`, as a writer cites it."""
    back = {label(c.id): c.id for c in citations if label(c.id) != c.id}
    if not back:
        return text
    return CITED.sub(lambda m: f"[^{back.get(m.group(1), m.group(1))}]", text)


DEEP = 48  # blocks nested this deep or deeper are written flat: the parser reads no further, and cmark does


DEEPISH = re.compile(rf"^[ \t>*+\-0-9.)]{{{DEEP - 8},}}", re.MULTILINE)


def _parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"maxNesting": DEEP + 8}).enable("table").disable("inline")  # blocks alone
    md.validateLink = lambda url: True  # a renderer that checks no link target is what a reader may have
    return md


PARSER = _parser()


def _front_matter(lines: list[str]) -> tuple[int, int] | None:
    """The first and last line of a block of front matter at the top of `lines`, or None when it opens none."""
    first = next((n for n, line in enumerate(lines) if line.strip()), None)
    if first is None or (mark := lines[first].rstrip()) not in ("---", "+++"):
        return None
    ends = ("---", "...") if mark == "---" else ("+++",)
    close = next((n for n in range(first + 1, len(lines)) if lines[n].rstrip() in ends), None)
    inner = [line for line in lines[first + 1:close] if line.strip()] if close is not None else []
    if close is None or not inner or not all(FRONT.match(line) for line in inner):
        return None
    return first, close


def underlined_as_atx(text: str) -> str:
    """`text` with each underlined heading written as the `#` heading `clean` makes of it, but in a block of front
    matter at the top, which `clean` takes out whole. Text over `---` that cites or ends in a full stop is a sentence
    over a rule, as a writer means it: a blank line goes between them."""
    lines: list[str | None] = list(text.split("\n"))
    front = _front_matter(text.split("\n"))
    tokens = PARSER.parse(text, {})
    for tok, inline in pairwise(tokens):
        if tok.type != "heading_open" or tok.markup not in ("=", "-") or not tok.map:
            continue
        start, end = tok.map
        if front is not None and start <= front[1]:
            continue
        if tok.markup == "-" and (CITED.search(inline.content) or inline.content.rstrip().endswith(".")):
            rule = lines[end - 1] or ""
            lines[end - 1] = f"{re.match(r'^[ >]*', rule).group().rstrip()}\n{rule}"  # type: ignore[union-attr]
            continue
        prefix = _prefix(lines[start] or "", inline.content.split("\n")[0])
        lines[start] = f"{prefix}{'#' * int(tok.tag[1])} {' '.join(inline.content.split())}"
        lines[start + 1:end] = [None] * (end - start - 1)
    return "\n".join(line for line in lines if line is not None)


def _quoted(text: str) -> str:
    said = " ".join(text.split())
    said = INVISIBLE.sub(lambda m: f"\\u{ord(m.group()):04x}", said)
    return said if len(said) <= SHOWN else said[:SHOWN] + "…"


def _key(url: str) -> str:
    """An address as two that name the same thing compare: entities read, escapes and invisible characters taken
    out, and a trailing slash dropped."""
    url = html.unescape(re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", url.strip().removeprefix("<").removesuffix(">")))
    return "".join(c for c in url if not (c.isspace() or unicodedata.category(c) in ("Cc", "Cf"))).rstrip("/")


def _kind(url: str) -> tuple[str, str]:
    """What a link's address reaches: ("outside", scheme), ("mailto", ""), ("inside", ""), or ("bad", scheme) for a
    scheme that is neither, read as a browser would, past entities, case, spaces and control characters."""
    plain = _key(url).lower()
    if plain.startswith(("//", "\\\\", "/\\", "\\/")):
        return "outside", ""
    m = SCHEME.match(plain)
    if not m:
        return "inside", ""
    scheme = m.group(1)
    if scheme in OUTSIDE_SCHEMES:
        return "outside", scheme
    if scheme in SAFE_SCHEMES:
        return "mailto", scheme
    return "bad", scheme


def _expand_tabs(line: str) -> str:
    if "\t" not in line:
        return line
    out: list[str] = []
    col = 0
    for c in line:
        if c == "\t":
            pad = 4 - col % 4
            out.append(" " * pad)
            col += pad
        else:
            out.append(c)
            col += 1
    return "".join(out)


def _joiner_kept(text: str, at: int) -> bool:
    """Whether a zero-width joiner joins two pictographs, as in an emoji sequence, where it shows as one glyph."""
    before = text[at - 1] if at else ""
    after = text[at + 1] if at + 1 < len(text) else ""
    return bool(before and after) and all(unicodedata.category(c) in ("So", "Sk") or c == "\ufe0f" or
                                          0x1F3FB <= ord(c) <= 0x1F3FF for c in (before, after))


def _visible(text: str) -> tuple[str, int]:
    """`text` without control and invisible characters, and how many went."""
    gone = 0

    def keep(m: re.Match[str]) -> str:
        nonlocal gone
        if m.group() == JOINER and _joiner_kept(text, m.start()):
            return m.group()
        gone += 1
        return ""

    return INVISIBLE.sub(keep, text), gone


def _verbatim_lines(tokens: list[Token]) -> dict[int, str]:
    """The lines inside a fence or an indented code block, each with the kind of block: "yaml", "fence" or
    "code". A fence's own opening and closing lines are not in it."""
    held: dict[int, str] = {}
    for tok in tokens:
        if tok.map is None:
            continue
        if tok.type == "fence":
            kind = "yaml" if _language(tok.info) in DATA_LANGUAGES else "fence"
            start, end = tok.map
            closed = tok.meta.get("closed", True)
            for n in range(start + 1, end - 1 if closed else end):
                held[n] = kind
        elif tok.type == "code_block":
            for n in range(*tok.map):
                held[n] = "code"
    return held


def _language(info: str) -> str:
    words = info.strip().split()
    return words[0].strip("{}.").lower() if words else ""


class _Pass:
    def __init__(self, out: Cleaned, images: frozenset[str], links: frozenset[str] | None, top: int,
                 cited: frozenset[str] = frozenset()):
        self.out, self.images, self.links, self.top, self.cited = out, images, links, top, cited
        self.refs: dict[str, dict[str, str]] = {}

    def remove(self, line: str) -> None:
        self.out.removed.append(line)

    def note(self, line: str) -> None:
        self.out.noted.append(line)

    def run(self) -> None:
        text = self.out.text.replace("\r\n", "\n").replace("\r", "\n")
        text = self.characters(text)
        text = self.flat(text)
        text = self.front_matter(text)
        text = self.footnotes(text)
        text = self.blocks(text)
        self.out.text = self.emit(text)

    # -- characters, lines and the blocks that go whole

    def characters(self, text: str) -> str:
        lines = text.split("\n")
        tokens = PARSER.parse(text, {})
        _mark_closed(tokens, lines)
        held = _verbatim_lines(tokens)
        gone = kept = 0
        for n, line in enumerate(lines):
            kind = held.get(n)
            if kind == "yaml":
                if INVISIBLE.search(line):
                    kept += 1
                continue
            line, k = _visible(line)
            gone += k
            if kind is None:
                line = _expand_tabs(ODD_SPACE.sub(" ", unicodedata.normalize("NFC", line)))
            elif kind == "code" and "\t" in (lead := MARKS.match(line).group()):  # type: ignore[union-attr]
                line = _expand_tabs(lead) + line[len(lead):]  # renderers count a tab among a quote's marks apart
            lines[n] = line
        if gone:
            self.remove(f"removed {gone} control or invisible character{'s' if gone != 1 else ''}: they hide or "
                        "reorder what a reader sees")
        if kept:
            self.note("kept invisible characters inside a yaml block as written: the block is data and stays byte for "
                      "byte")
        return "\n".join(lines)

    def flat(self, text: str) -> str:
        """`text` with each line the parser reads no block on, past its depth, written with every mark escaped, so
        no renderer, however deep it reads, finds markup in it."""
        if not DEEPISH.search(text):
            return text  # no line's marks reach that deep
        env: dict[str, object] = {}
        tokens = PARSER.parse(text, env)
        if max((t.level for t in tokens), default=0) < DEEP:
            return text
        lines = text.split("\n")
        covered: set[int] = set()
        for tok in tokens:
            if tok.map and (tok.nesting == 0 or tok.type in ("paragraph_open", "heading_open", "table_open")):
                covered.update(range(*tok.map))
        refs = env.get("references", {})
        for ref in refs.values() if isinstance(refs, dict) else []:
            if ref.get("map"):
                covered.update(range(*ref["map"]))
        gone = 0
        for n, line in enumerate(lines):
            if n not in covered and line.strip():
                lines[n] = UNESCAPED.sub(lambda m: m.group() if len(m.group()) == 2 else "\\" + m.group(), line)
                gone += lines[n] != line
        if gone:
            self.remove(f"escaped the marks of {gone} line(s) nested past {DEEP} quotes or lists: no renderer reads "
                        "them alike")
        return "\n".join(lines)

    def front_matter(self, text: str) -> str:
        lines = text.split("\n")
        if (found := _front_matter(lines)) is None:
            first = next((n for n, line in enumerate(lines) if line.strip()), None)
            if first is not None and lines[first].rstrip() == "+++":
                self.remove('escaped "+++" on the first line: a site builder takes it for the start of front matter')
                lines[first] = "\\+++"
            return "\n".join(lines)
        first, close = found
        inner = [line for line in lines[first + 1:close] if line.strip()]
        self.remove(f'removed a block of front matter at the top of the text, "{_quoted(" ".join(inner))}": the '
                    "article's front matter is written apart, and a reader would take this for it")
        return "\n".join(lines[:first] + lines[close + 1:])

    def footnotes(self, text: str) -> str:
        lines = text.split("\n")
        tokens = PARSER.parse(text, {})
        _mark_closed(tokens, lines)
        held = _verbatim_lines(tokens)
        fences = {n for tok in tokens if tok.type == "fence" and tok.map for n in range(*tok.map)}
        out: list[str] = []
        n = 0
        while n < len(lines):
            m = FOOTNOTE.match(lines[n]) if n not in held and n not in fences else None
            if not m:
                out.append(lines[n])
                n += 1
                continue
            self.remove(f"removed the footnote [^{m.group(1)}] the text defines: each citation's footnote is written "
                        "apart, from its evidence")
            n += 1
            while n < len(lines) and lines[n].strip() and n not in fences and not BLOCK_START.match(
                    lines[n].lstrip(" >")):
                n += 1
            while n < len(lines):
                k = n
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and lines[k].startswith("    ") and k not in fences:
                    n = k
                    while n < len(lines) and (not lines[n].strip() or lines[n].startswith("    ")):
                        n += 1
                else:
                    break
        return "\n".join(out)

    def blocks(self, text: str) -> str:
        """`text` with each raw HTML block written to show as written, and without its link reference definitions,
        whose targets `self.refs` keeps."""
        env: dict[str, object] = {}
        lines = text.split("\n")
        tokens = PARSER.parse(text, env)
        drop: set[int] = set()
        for tok in tokens:
            if tok.type == "html_block" and tok.map:
                self.shown_as_written(tok.content)
                for n in range(*tok.map):
                    lines[n] = OPENS.sub("&lt;", lines[n])
        refs = env.get("references", {})
        assert isinstance(refs, dict)
        words = {normalizeReference(word[1:-1]): word for word in MASK_WORDS}
        for name, ref in refs.items():
            if name in words and ref.get("map"):  # the mask's own words define nothing: shown as written
                n = ref["map"][0]
                lines[n] = re.sub(re.escape(words[name]) + ":", lambda m: m.group()[:-1] + "\\:", lines[n], count=1,
                                  flags=re.IGNORECASE)
                continue
            self.refs[name] = {"href": ref.get("href", ""), "title": ref.get("title", "")}
            if ref.get("map"):
                drop.update(range(*ref["map"]))
                self.remove(f'removed the definition of the link "{_quoted(name)}" to "{_quoted(ref.get("href", ""))}"'
                            ": a link that uses it is written with its address")
        return "\n".join(line for n, line in enumerate(lines) if n not in drop)

    def shown_as_written(self, markup: str) -> None:
        what = "an HTML comment" if markup.lstrip().startswith("<!--") else "raw HTML"
        self.remove(f'escaped {what} "{_quoted(markup)}" so that it shows as written: a browser would run it')

    # -- the canonical form, and what of the inline text is made safe

    def emit(self, text: str) -> str:
        lines: list[str | None] = list(text.split("\n"))
        tokens = PARSER.parse(text, {})
        _mark_closed(tokens, [line or "" for line in lines])
        verbatim = set(_verbatim_lines(tokens))
        extra: dict[int, list[str]] = {}
        lists: dict[int, str] = {}
        markers: dict[int, list[tuple[str, str]]] = {}
        inside_table = False
        open_: list[Token] = []
        for i, tok in enumerate(tokens):
            if tok.nesting == -1 and open_:
                open_.pop()
            elif tok.nesting == 1:
                open_.append(tok)
            if tok.type == "table_open":
                inside_table = True
                self.table(lines, tok)
            elif tok.type == "table_close":
                inside_table = False
            elif tok.type == "fence" and tok.map:
                self.fence(lines, tok, extra, listed=any(t.type == "list_item_open" for t in open_))
            elif tok.type == "hr" and tok.map:
                m = RULE.match(lines[tok.map[0]] or "")
                if m:
                    lines[tok.map[0]] = f"{m.group(1)}***"
            elif tok.type == "heading_open" and tok.map:
                self.heading(lines, tok, tokens[i + 1])
            elif tok.type == "inline" and tok.map and not inside_table and tokens[i - 1].type == "paragraph_open":
                self.paragraph(lines, tok)
            elif tok.type == "bullet_list_open":
                previous = tokens[i - 1] if i else None
                mark = "-"
                if previous is not None and previous.type == "bullet_list_close" and previous.level == tok.level:
                    mark = "*" if lists.get(id(previous)) == "-" else "-"
                lists[id(tok)] = mark
                close = _closing(tokens, i)
                lists[id(tokens[close])] = mark
            elif tok.type == "list_item_open" and tok.map:
                parent = _parent(tokens, i)
                mark = lists.get(id(parent), "") if parent is not None and parent.type == "bullet_list_open" else ""
                markers.setdefault(tok.map[0], []).append((tok.markup, mark))
        for n, items in markers.items():
            if lines[n] is not None:
                lines[n] = _bullets(lines[n] or "", items)
        tops = [(t.map[0], t.map[1], t) for t in tokens if t.level == 0 and t.map and t.nesting >= 0]
        pieces: list[list[str]] = []
        covered: set[int] = set()
        for start, end, top in tops:
            piece: list[tuple[str, bool]] = []
            for n in range(start, end):
                covered.add(n)
                line = lines[n]
                if line is not None:
                    held = n in verbatim
                    line = line if held else line.rstrip()
                    blank = not held and not line.strip(" >")
                    if not (blank and piece and not piece[-1][1] and not piece[-1][0].strip(" >")):
                        piece.append((line, held))
                piece += [(more, False) for more in extra.get(n, [])]
            while piece and not piece[-1][0].strip(" >"):
                piece.pop()  # code ends before blank lines, and a fence left open ends as the block it sits in
            while piece and not piece[0][1] and not piece[0][0].strip():
                piece.pop(0)
            if piece:
                pieces.append([line for line, _ in piece])
                if self.top == 1 and len(pieces) == 1:
                    pieces[0] = _flush(pieces[0], top.type == "code_block")
        for n, line in enumerate(lines):
            if n not in covered and line and line.strip():
                pieces.append([line.rstrip()])
        joined = "\n\n".join("\n".join(piece) for piece in pieces)
        return joined + "\n" if joined else ""

    def fence(self, lines: list[str | None], tok: Token, extra: dict[int, list[str]], listed: bool = False) -> None:
        start, end = tok.map  # type: ignore[misc]
        opening = lines[start] or ""
        m = FENCE.match(opening)
        if not m:
            return
        prefix, mark, info = m.groups()
        if _language(tok.info) not in DATA_LANGUAGES and info.strip():
            words = info.strip().split()
            plain = words[0] if PLAIN_INFO.match(words[0]) else ""
            if plain != info.strip():
                self.remove(f'removed the info "{_quoted(info.strip())}" of a code fence: a site builder may run or '
                            "embed what it names")
                lines[start] = f"{prefix}{mark}{plain}"
        quotes = prefix.count(">")
        if quotes and not listed and re.match(r" {4,}>", prefix):  # cmark reads a lazy line here, markdown-it a fence
            lines[start] = prefix + "".join("\\" + c for c in mark) + info
            self.remove(f'escaped the fence "{_quoted(opening.strip())}": renderers read its quote two ways')
            return
        if quotes:  # markdown-it reads on a fence in a quote over lines that lack its marks, and cmark ends the quote
            marks = re.sub(r"[^>]", " ", prefix).rstrip()
            for n in range(start + 1, end):
                line = lines[n] or ""
                if not line.startswith(marks):
                    lines[n] = marks + (" " if line and not line.startswith(" ") else "") + line
        if not tok.meta.get("closed", True):
            close = mark if not tok.level else re.sub(r"[^ >]", " ", prefix) + mark
            block = [line or "" for line in lines[start:end]] + [close]
            found = [t for t in PARSER.parse("\n".join(block), {}) if t.type == "fence" and t.map]
            if found and found[0].map == [0, len(block)]:  # else the quote or list it sits in closes it as it ends
                extra.setdefault(end - 1, []).append(close)

    def heading(self, lines: list[str | None], tok: Token, inline: Token) -> None:
        start, end = tok.map  # type: ignore[misc]
        level = int(tok.tag[1])
        new = max(level, self.top + 1)  # only the wiki writes a heading at the part's own level or above it
        words = " ".join(inline.content.split())
        if new != level:
            self.remove(f'moved the heading "{_quoted(words)}" from level {level} to level {new}: only the part\'s own '
                        f"heading sits at level {self.top} or above")
        first = lines[start] or ""
        if tok.markup in ("=", "-"):
            prefix = _prefix(first, inline.content.split("\n")[0])
            for n in range(start + 1, end):
                lines[n] = None
        else:
            m = HEADING.match(first)
            prefix = m.group(1) if m else ""
        cleaned = " ".join(self.inline(words).split())
        lines[start] = f"{prefix}{'#' * new} {cleaned}".rstrip() if cleaned else f"{prefix}{'#' * new}"

    def paragraph(self, lines: list[str | None], tok: Token) -> None:
        """A paragraph's lines made safe as one run of text. Inside a quote or a list, each line after the first
        carries the marks the first does: cmark lets a line that continues a paragraph lazily open an HTML block,
        where markdown-it reads it on as the paragraph's text."""
        start, end = tok.map  # type: ignore[misc]
        content = tok.content.split("\n")
        if len(content) != end - start:
            for n in range(start, end):
                lines[n] = self.inline(lines[n] or "")
            return
        prefixes, breaks = [], []
        for k, said in enumerate(content):
            line = lines[start + k] or ""
            prefixes.append(_prefix(line, said))
            breaks.append(k + 1 < len(content) and len(line) - len(line.rstrip(" ")) >= 2)
        if tok.level:  # cmark takes the space after a `>` on every line, though the first line wrote none
            prefixes[1:] = [re.sub(r"[^>]", " ", re.sub(r">(?! )", "> ", prefixes[0]))] * (len(prefixes) - 1)
        cleaned = self.inline(tok.content, defs=True).split("\n")
        if len(cleaned) != len(content):
            for n in range(start, end):
                lines[n] = self.inline(lines[n] or "")
            return
        for k, (said, now) in enumerate(zip(content, cleaned, strict=True)):
            now = now.strip()
            if now != said.strip() and LINE_START.match(now) and not LINE_START.match(said.strip()):
                now = "\\" + now
            elif k and "|" in now and DELIMITER.fullmatch(now):
                now = "\\" + now  # GitHub would read the line before as a table's head, though it holds no pipe
            if not now:
                lines[start + k] = None
                continue
            lines[start + k] = prefixes[k] + now + ("\\" if breaks[k] and not now.endswith("\\") else "")

    def table(self, lines: list[str | None], tok: Token) -> None:
        """A table written one row to a line, `| a | b |`, each cell made safe on its own, as both renderers that
        read tables split a row at its pipes before they read a cell's text."""
        start, end = tok.map  # type: ignore[misc]
        rows = [n for n in range(start, end) if n != start + 1]
        marks = TABLE_PREFIX if tok.level else r"^ *"
        width = len(_cells(_row(lines[start] or "", marks)[1]))
        for n in rows:
            prefix, row = _row(lines[n] or "", marks)
            cells = _cells(row)
            if len(cells) > width:
                self.remove(f'removed {len(cells) - width} cell(s) past the last column of a table, "'
                            f'{_quoted(" | ".join(cells[width:]))}": a renderer does not show them')
            cells = (cells + [""] * width)[:width]
            cells = [self.inline(cell, cell=" | ".join(cells[k + 1:]) or " ") for k, cell in enumerate(cells)]
            lines[n] = prefix + "| " + " | ".join(cells) + " |"
        prefix, separator = _row(lines[start + 1] or "", marks)
        aligns = (_cells(separator) + ["---"] * width)[:width]
        lines[start + 1] = re.sub(r"[^ >]", " ", prefix) + "| " + " | ".join(a or "---" for a in aligns) + " |"

    def inline(self, text: str, *, cell: str = "", defs: bool = False) -> str:
        return _Inline(self, text, cell=cell, defs=defs).run()


class _Inline:
    """One run of inline text made safe: code spans kept as written, raw HTML out, each link and image checked.

    Where renderers could read the text two ways, it is written so they read it one way: a backtick run that closes
    no code span, or that a bare address takes, is escaped; an address a renderer would link to another scheme, or
    whose `<` it would swallow, is shown as text; a line that one renderer would read as a link's definition is
    escaped; and in a table's cell, a bracket that pairs with none in the cell is escaped, so nothing pairs across the
    cell's pipe. `cell`, for a cell, is the rest of its row; `defs` says the text is a paragraph's, whose lines may
    start a definition."""

    def __init__(self, owner: _Pass, text: str, *, cell: str = "", defs: bool = False):
        self.p, self.s, self.cell, self.defs = owner, text, cell, defs
        self.opaque: dict[int, int] = {}
        self.pairs: dict[int, int] = {}
        self.closers: set[int] = set()
        self.plain: set[int] = set()  # backticks written escaped: closing no code span, or taken by a bare address
        self.words: set[int] = set()  # where a mask's own words start
        self.mails: set[int] = set()  # where an address starts that GitHub would link to xmpp or a longer mailto
        self.addresses: dict[int, int] = {}  # where a bare address starts, and where it ends, at a space or a <
        self.scan()

    def scan(self) -> None:
        """Find code spans, autolinks, mask words and bare addresses, which take the text they cover before any
        bracket does, and pair each bracket with its match outside them."""
        s, i, n = self.s, 0, len(self.s)
        stack: list[int] = []
        mailish = bool(MAIL_SCHEME.search(s))
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n and s[i + 1] in ASCII_PUNCT:
                i += 2
                continue
            if c == "[" and (word := next((w for w in MASK_WORDS if s.startswith(w, i)), None)):
                self.opaque[i] = i + len(word)
                self.words.add(i)
                i += len(word)
                continue
            if c in "hHfFwW" and ADDRESS.match(s, i) and i not in self.addresses:
                end = i
                while end < n and not s[end].isspace() and s[end] != "<":
                    end += 1
                self.addresses[i] = end
                self.plain.update(k for k in range(i, end) if s[k] == "`")
            if mailish and c.isascii() and (c.isalnum() or c in ".+_-") and (m := MAILISH.match(s, i)) and \
                    (m.group(1).lower() == "xmpp" or m.start(1) > i):
                self.opaque[i] = m.end()
                self.mails.add(i)
                i = m.end()
                continue
            if c == "`" and i in self.plain:
                i += 1
                continue
            if c == "`":
                run = _run(s, i)
                close = _closing_run(s, i + run, run)
                if close is not None:
                    self.opaque[i] = close
                    i = close
                else:
                    self.plain.update(range(i, i + run))  # cmark and markdown-it pair what follows otherwise
                    i += run
                continue
            if c == "<":
                end = self.angle(i)
                if end:
                    self.opaque[i] = end
                    i = end
                    continue
            if c == "[":
                stack.append(i)
            elif c == "]" and stack:
                self.pairs[stack.pop()] = i
                if s.startswith("(", i + 1) and (tail := _destination(s, i + 1)) is not None:
                    i = tail[2]  # a link's address and title, which no renderer reads as code or a bare address
                    continue
            i += 1
        self.closers = set(self.pairs.values())

    def angle(self, i: int) -> int:
        """Where the CommonMark autolink that opens at `i` ends, or 0 for none."""
        s = self.s
        for pattern in (AUTOLINK, EMAIL_LINK):
            if m := pattern.match(s, i):
                return m.end()
        return 0

    def line_start(self, i: int) -> bool:
        """Whether `i` starts a line of a paragraph, past up to three spaces, or past a task list's box, which
        GitHub takes off an item before it reads the rest."""
        k = i
        if box := TASK.search(self.s, 0, k):
            k = box.start()
        while k > 0 and self.s[k - 1] == " " and i - k < 3:
            k -= 1
        return k == 0 or self.s[k - 1] == "\n"

    def defines(self, i: int) -> bool:
        """Whether the bracket at `i` opens what cmark could read as a link's definition: a label, then a colon."""
        s, k = self.s, i + 1
        while k < len(s) and s[k] not in "[]":
            k += 2 if s[k] == "\\" else 1
        return k < len(s) and s[k] == "]" and s.startswith(":", k + 1)

    def run(self) -> str:
        s, n = self.s, len(self.s)
        out: list[str] = []
        ends: dict[int, tuple[int, str]] = {}  # a link's closing bracket: where its tail ends, and what to write
        i = 0
        while i < n:
            if i in ends:
                end, write = ends.pop(i)
                out.append(write)
                i = end
                continue
            c = s[i]
            if c == "\\" and i + 1 < n and s[i + 1] in ASCII_PUNCT:
                if s[i + 1] == "<" and OPENS.match(s, i + 1):
                    out.append("&lt;")  # `\\<` shows `<`, but a GFM autolink before it swallows the backslash
                else:
                    out.append(s[i:i + 2])
                i += 2
                continue
            if i in self.words:
                end = self.opaque[i]
                out.append(s[i:end])
                if s.startswith(("(", "["), end) or (s.startswith(":", end) and self.defs and self.line_start(i)):
                    out.append("\\")  # the mask's words are never a link's, an image's or a definition's
                i = end
                continue
            if i in self.mails:
                end = self.opaque[i]
                before = "".join(out)
                if (len(before) - len(before.rstrip("\\"))) % 2:
                    out.append("\\")  # a backslash before it is shown, and escapes no backtick
                out.append(f"`{s[i:end]}`")
                self.p.remove(f'showed "{_quoted(s[i:end])}" as code: GitHub would link it to a scheme that can '
                              "reach a reader's machine")
                i = end
                continue
            if i in self.opaque:
                end = self.opaque[i]
                out.append(self.unit(s[i:end]))
                i = end
                continue
            if c == "`":
                out.append("\\`")  # in `plain`: it closes no code span, or a bare address takes it
                i += 1
                continue
            if i in self.addresses and (shown := self.address(i, self.addresses[i])) is not None:
                out.append(shown[0])
                i = shown[1]
                continue
            if c == "<":
                if OPENS.match(s, i):
                    out.append("&lt;")
                    self.markup(s[i:])
                else:
                    out.append("&lt;" if self.liberal(i) else "<")
                i += 1
                continue
            if c == "^" and s.startswith("[", i + 1):
                out.append("\\^")  # `^[...]` is a footnote to markdown-it's footnote plugin, which only the wiki writes
                i += 1
                continue
            if c == "[" and self.defs and self.line_start(i) and not MARKER.match(s, i + 1) and self.defines(i):
                out.append("\\[")  # cmark may read the line as a definition where markdown-it does not
                self.escaped("[" + s[i + 1:i + 21].split("]")[0] + "]:", "a link's definition")
                i += 1
                continue
            if c == "[" and s.startswith("^", i + 1) and not (i in self.pairs and MARKER.fullmatch(
                    s[i + 1:self.pairs[i]]) and not s.startswith("(", self.pairs[i] + 1)):
                out.append("\\[")  # no citation's marker: GitHub's footnotes read what it holds as bare text
                i += 1
                continue
            if c == "[" and i in self.pairs:
                taken = self.link(i, image=_bang(s, i), out=out, ends=ends)
                if taken is not None:
                    i = taken
                    continue
            if self.cell and c in "[]" and i not in self.pairs and i not in self.closers:
                out.append("\\" + c)  # pairs with nothing in its cell, so nothing past the cell's pipe either
                i += 1
                continue
            if c == "]" and s.startswith("(", i + 1) and (lax := _lax(s, i + 1)) and _kind(lax[0])[0] == "bad":
                out.append("\\]")
                self.escaped("](" + lax[0][:20])
                i += 1
                continue
            if c == "&" and (m := ENTITY.match(s, i)):
                said = html.unescape(m.group())
                if said != m.group() and INVISIBLE.fullmatch(said) and said != JOINER:
                    self.p.remove(f'removed the entity {m.group()}: it writes a character that hides or reorders '
                                  "what a reader sees")
                    i = m.end()
                    continue
            out.append(c)
            i += 1
        return "".join(out)

    def liberal(self, i: int) -> bool:
        """Whether cmark with liberal tags would read the `<` at `i`, which opens no HTML, as opening a tag that ends
        at a `>` later on its line, past a cell's pipe too, and so take what lies between out of the reading every
        other renderer gives: a backtick that pairs a code span, or a tag in a code span, an autolink or a link's
        title."""
        line = self.s.find("\n", i)
        rest = self.s[i + 1:len(self.s) if line == -1 else line] + ("|" + self.cell if self.cell else "")
        taken = rest[:max(rest.rfind(">"), 0)]
        return "`" in taken or "<" in taken

    def address(self, i: int, end: int) -> tuple[str, int] | None:
        """A bare address at `i`, up to `end`: shown as text, by escaping what starts it, when a renderer would link
        it to ftp; when a `<` ends it, which a renderer that links bare addresses would take into the address once
        it is written to show as written; and when it holds a backtick, which one renderer takes into the address
        and another reads as code, or what GitHub would link to another scheme. Gives what to write and where to go
        on, or None to read on as words."""
        s = self.s
        head = ADDRESS.match(s, i)
        assert head is not None
        start = head.group().lower()
        if start.startswith("ftp") or OPENS.match(s, end) or (s.startswith("<", end) and s[end - 1] == "\\") or \
                "`" in s[i:end] or _bare_bad(s[i:end]):
            cut = i + (len(head.group()) - 3 if "://" in start else 3)
            self.p.remove(f'showed the address "{_quoted(s[i:end])}" as text: a renderer would link it '
                          + ("to a scheme that can reach a reader's machine" if start.startswith("ftp") else
                             "to an address other than the one written"))
            return s[i:cut] + "\\" + s[cut], cut + 1
        if self.p.links is not None:
            self.outside(s[i:end])
        return None

    def escaped(self, what: str, as_: str = "a link") -> None:
        self.p.remove(f'escaped "{what}" so that it shows as written: a renderer would take it for {as_}')

    def markup(self, rest: str) -> None:
        """Say once for each piece of markup a `<` opened what was written to show as written."""
        end = rest.find(">")
        self.p.shown_as_written(rest[:end + 1] if 0 < end <= SHOWN else rest[:SHOWN])

    def outside(self, url: str) -> None:
        if self.p.links is not None and _key(url) not in self.p.links and _key("https://" + url) not in self.p.links:
            self.p.note(f"kept the link to {_quoted(url)}: no cited evidence holds that address")

    def unit(self, chunk: str) -> str:
        """A code span or an autolink, as it may stand."""
        if chunk.startswith("`"):
            return chunk
        if (m := AUTOLINK.fullmatch(chunk)) or (m := EMAIL_LINK.fullmatch(chunk)):
            url = m.group(1)
            kind, scheme = _kind(url) if "@" not in url or ":" in url else ("mailto", "")
            if kind == "bad":
                self.p.remove(f'removed the target of a link to "{_quoted(url)}": a {scheme}: address can run code '
                              "or reach a reader's machine")
                return _Inline(self.p, url).run()
            if kind == "outside":
                self.outside(url)
        return chunk

    def link(self, i: int, image: bool, out: list[str], ends: dict[int, tuple[int, str]]) -> int | None:
        """Handle the bracket at `i` as a link or an image's, and give where to go on, or None if it is no link."""
        s = self.s
        close = self.pairs[i]
        inner = s[i + 1:close]
        after = close + 1
        if not image and MARKER.fullmatch(inner) and not s.startswith("(", after):
            out.append(f"[^{_marker(inner[1:], self.p.cited)}]")  # a citation's marker, as its footnote is labelled
            return after
        target = title = None
        tail_end = after
        if s.startswith("(", after):
            parsed = _destination(s, after)
            if parsed is not None:
                target, title, tail_end = parsed
            else:
                lax = _lax(s, after)
                if lax is not None and _kind(lax[0])[0] == "bad":
                    target, title, tail_end = lax[0], None, lax[1]
        if target is None:
            name = None
            if s.startswith("[]", after):
                name, tail_end = inner, after + 2
            elif s.startswith("[", after) and (end := s.find("]", after + 1)) != -1 and "[" not in s[after + 1:end]:
                name, tail_end = s[after + 1:end], end + 1
            elif not s.startswith("[", after):
                name, tail_end = inner, after
            ref = self.p.refs.get(normalizeReference(name)) if name is not None and name.strip() else None
            if ref is None:
                if image and out and out[-1].endswith("!"):  # no image here, nor in a renderer that reads it otherwise
                    out[-1] = out[-1][:-1] + "\\!"
                return None
            target, title = ref["href"], " ".join(ref["title"].split()) or None
            tail = f"]({_written(target)}" + (f' "{_title(title)}"' if title else "") + ")"
        else:
            tail = s[close:tail_end]
        kind, scheme = _kind(target)
        keep_lines = "\n" * s[close:tail_end].count("\n")
        if image:
            if _key(target) in self.p.images:
                out.append("[")
                ends[close] = (tail_end, tail)
                return i + 1
            if out and out[-1].endswith("!"):
                out[-1] = out[-1][:-1]
            self.p.remove(f'removed the image "{_quoted(target)}", keeping its words: an image loads its address as '
                          "the article is shown, and no cited evidence holds it")
            ends[close] = (tail_end, keep_lines)
            return i + 1
        if kind == "bad":
            self.p.remove(f'removed the target of the link "{_quoted(inner)}" to "{_quoted(target)}", keeping its '
                          f"words: a {scheme}: address can run code or reach a reader's machine")
            ends[close] = (tail_end, keep_lines)
            return i + 1
        if kind == "outside":
            self.outside(target)
        out.append("[")
        ends[close] = (tail_end, tail)
        return i + 1


def _flush(piece: list[str], code: bool) -> list[str]:
    """The first block of a whole part, starting at the left margin: a part stands after another part's text, where
    indented lines would go on a list it ends with. Indented code becomes a fence, and any other block moves left
    as one."""
    if not piece[0][:1].isspace():
        return piece
    if code:
        body = [_unindented(line) for line in piece]
        fence = "`" * max(3, max((len(run) for line in body for run in re.findall("`+", line)), default=0) + 1)
        return [fence, *body, fence]
    lead = len(piece[0]) - len(piece[0].lstrip(" "))
    return [line[min(lead, len(line) - len(line.lstrip(" "))):] for line in piece]


def _unindented(line: str) -> str:
    """A line of indented code without the four columns of indent that make it code, a tab reaching column four."""
    col = 0
    for k, c in enumerate(line):
        if c == " ":
            col += 1
        elif c == "\t":
            col = col // 4 * 4 + 4
        else:
            return line[k:]
        if col >= 4:
            return line[k + 1:]
    return ""


def _bang(s: str, i: int) -> bool:
    """Whether the bracket at `i` opens an image: an exclamation mark before it that no backslash escapes."""
    if not i or s[i - 1] != "!":
        return False
    k = i - 2
    while k >= 0 and s[k] == "\\":
        k -= 1
    return (i - 2 - k) % 2 == 0


def _written(href: str) -> str:
    return f"<{href}>" if not href or re.search(r"[\s()<>]", href) else href


def _title(title: str) -> str:
    return title.replace("\\", "\\\\").replace('"', '\\"')


def _run(s: str, i: int) -> int:
    k = i
    while k < len(s) and s[k] == "`":
        k += 1
    return k - i


def _closing_run(s: str, start: int, run: int) -> int | None:
    k = s.find("`" * run, start)
    while k != -1:
        length = _run(s, k)
        if length == run:
            return k + run
        k = s.find("`" * run, k + length)
    return None


def _destination(s: str, at: int) -> tuple[str, str | None, int] | None:
    """A link's destination and title from the `(` at `at`, as CommonMark reads them, with where the tail ends."""
    n, k = len(s), at + 1
    k = _space(s, k)
    if k < n and s[k] == "<":
        j = k + 1
        while j < n and s[j] not in "<>\n":
            j += 2 if s[j] == "\\" and j + 1 < n else 1
        if j >= n or s[j] != ">":
            return None
        target, k = s[k + 1:j], j + 1
    else:
        j, depth = k, 0
        while j < n:
            c = s[j]
            if c == "\\" and j + 1 < n and s[j + 1] in ASCII_PUNCT:
                j += 2
                continue
            if c.isspace() or ord(c) < 0x20 or c == "\x7f":
                break
            if c == "(":
                depth += 1
                if depth > NEST:
                    return None
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            j += 1
        if depth:
            return None
        target, k = s[k:j], j
    title = None
    spaced = _space(s, k)
    if spaced < n and spaced > k and s[spaced] in "\"'(":
        opener = s[spaced]
        closer = ")" if opener == "(" else opener
        j = spaced + 1
        while j < n and s[j] != closer:
            if opener == "(" and s[j] == "(":
                return None
            j += 2 if s[j] == "\\" and j + 1 < n else 1
        if j >= n:
            return None
        title, k = s[spaced + 1:j], j + 1
    k = _space(s, k)
    if k < n and s[k] == ")":
        return target, title, k + 1
    return None


def _lax(s: str, at: int) -> tuple[str, int] | None:
    """What a lenient renderer may take for a destination to a bad scheme from the `(` at `at`: all up to its
    balancing `)`. None when what follows opens with no bad scheme."""
    if _kind(s[at + 1:at + 1 + SHOWN])[0] != "bad":
        return None
    depth = 0
    for j in range(at, min(len(s), at + 1000)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return s[at + 1:j], j + 1
    return None


def _space(s: str, k: int) -> int:
    while k < len(s) and s[k] in " \t\n":
        k += 1
    return k


def _prefix(line: str, said: str) -> str:
    """What comes before a block's own text on its line: the marks of the quotes and list items it sits in."""
    bare, said = line.rstrip(), said.rstrip()
    if said and bare.endswith(said):
        return bare[:len(bare) - len(said)]
    return re.match(r"^[ >]*", line).group()  # type: ignore[union-attr]


TABLE_PREFIX = r"^(?:[ >]|[-+*] +|\d{1,9}[.)] +)*"  # the marks of the quotes and list items a table sits in


def _row(line: str, marks: str) -> tuple[str, str]:
    """A table's line as the marks before its row, and the row."""
    prefix = re.match(marks, line).group()  # type: ignore[union-attr]
    return prefix, line[len(prefix):]


def _cells(row: str) -> list[str]:
    row = row.strip()
    row = row.removeprefix("|")
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip() for cell in CELL_SPLIT.split(row)]


def _bullets(line: str, items: list[tuple[str, str]]) -> str:
    """`line` with each bullet list item that opens on it marked as `items` say, outermost first."""
    at = 0
    for markup, mark in items:
        m = re.compile(r"[ >]*").match(line, at)
        k = m.end() if m else at
        if markup in "-+*" and len(markup) == 1:
            if k < len(line) and line[k] == markup:
                if mark and mark != markup:
                    line = line[:k] + mark + line[k + 1:]
                at = k + 1
        else:
            m2 = re.compile(r"\d{1,9}[.)]").match(line, k)
            at = m2.end() if m2 else k
    return line


def _closing(tokens: list[Token], at: int) -> int:
    depth = 0
    for k in range(at, len(tokens)):
        depth += tokens[k].nesting
        if depth == 0:
            return k
    return len(tokens) - 1


def _parent(tokens: list[Token], at: int) -> Token | None:
    level = tokens[at].level
    for k in range(at - 1, -1, -1):
        if tokens[k].nesting == 1 and tokens[k].level == level - 1:
            return tokens[k]
    return None


def _mark_closed(tokens: list[Token], lines: list[str]) -> None:
    """Note on each fence whether its last mapped line closes it."""
    for tok in tokens:
        if tok.type != "fence" or tok.map is None:
            continue
        start, end = tok.map
        last = lines[end - 1] if end - 1 < len(lines) else ""
        m = FENCE.match(lines[start] if start < len(lines) else "")
        marks = re.sub(r"[^ >]", " ", m.group(1)) if m else ""
        body = last[len(marks):] if marks and last.startswith(marks) else last.lstrip(">") if marks else last
        mark = tok.markup
        closed = end - start >= 2 and len(body) - len(body.lstrip(" ")) <= 3 and \
            body.strip().startswith(mark[0] * len(mark)) and not body.strip().strip(mark[0])
        tok.meta["closed"] = closed  # a line four spaces in closes no fence
