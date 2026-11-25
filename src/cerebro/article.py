"""The article record and its parts, and the mirror folder.

An article is assembled from what the store keeps apart: its identity, its parts, and its properties, the front
matter fields. Only a run writes a part, and only its own: an agent that writes the article hands in one part, its
whole text, and each subagent of a composed article one part under its heading. A property has two writers: the
curator that admits the article, and the article's agents when they run. Each write replaces the field's value, and
the store keeps who wrote it and when. A source's own values are not written here: they reach the article's agents as
facts, which the agents weigh. The fields the system relies on are protected: no writer's value of one ever stands.

Markdown is how a person reads an article: after each compile it is also written to `articles/` as a copy, which
nothing reads back. The copy opens with front matter: the id, the title, the status when it is not active, the keys
that bind records to it under `identity`, each property, and when it was compiled. Each part follows, one under its
heading, citing inline as `[^fact_01m7p4qga4pxpjkgvf]`, and a footnote per citation closes the article with its source
and quote, since a person reading the copy has no other way to look a fact up. An article whose body holds nothing a
reader sees carries the empty-article note in its place: what it is, where its identity came from, and how many facts
it holds.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .caller import Caller
from .clean import _bare_bad, inert, label
from .private import make_folder, write_file

if TYPE_CHECKING:
    from .facts import Fact

__all__ = [
    "NOTE_LEAD",
    "NOTE_RAN_LEAD",
    "PENDING",
    "PROTECTED",
    "Article",
    "Blocks",
    "Citation",
    "Compilation",
    "Identity",
    "Piece",
    "Property",
    "atx",
    "body",
    "compiled_at",
    "cut",
    "empty_note",
    "fence_open",
    "fields",
    "markdown",
    "mirror",
    "part_text",
    "passages",
    "pending",
    "pieces",
    "protected",
    "stands",
    "written",
]

# The fields the system relies on, each with what keeps it. The system's value always wins: a writer's value of one
# never shows as it and never changes what the engine reads, and every other field is the writers' to set.
PROTECTED = {
    "id": "it is the article's address, which links and redirects depend on",
    "title": "it is the article's name, which its curator gives and facts link by",
    "status": "it says whether the article is in the working set, which its curator decides",
    "compiled_at": "it says when the article's agents last wrote it, which compile records",
    "identity": "it holds the keys that bind records to the article, which its curator admits",
    "keys": "they are the identity's keys, which bind records to the article and which its curator admits",
    "in_source": "it says whether the article's source still holds its record, which the source says",
}

# Letters of other scripts that read as Latin ones, and two minus
# signs that read as a hyphen: a field spelled with them reads to a person or a model as the field it looks like.
LOOKALIKES = str.maketrans({
    "\u2212": "-", "\u02d7": "-",
    "і": "i", "І": "i", "ı": "i", "İ": "i", "ι": "i", "Ι": "i", "ɪ": "i", "Ӏ": "i", "ӏ": "i", "ⅰ": "i", "Ⅰ": "i",
    "ԁ": "d", "ⅾ": "d", "Ⅾ": "d",
    "а": "a", "А": "a", "В": "b", "с": "c", "С": "c", "е": "e", "Е": "e", "һ": "h", "Н": "h", "ј": "j", "Ј": "j",
    "К": "k", "М": "m", "о": "o", "О": "o", "р": "p", "Р": "p", "ԛ": "q", "Ԛ": "q", "ѕ": "s", "Ѕ": "s", "Т": "t",
    "у": "y", "У": "y", "х": "x", "Х": "x", "ԝ": "w", "Ԝ": "w",
    "α": "a", "Α": "a", "Β": "b", "ε": "e", "Ε": "e", "Ζ": "z", "Η": "h", "κ": "k", "Κ": "k", "Μ": "m", "ν": "v",
    "Ν": "n", "ο": "o", "Ο": "o", "ρ": "p", "Ρ": "p", "τ": "t", "Τ": "t", "υ": "u", "Υ": "y", "χ": "x", "Χ": "x",
})

NOTE_LEAD = "_No agent writes this article yet._"
NOTE_RAN_LEAD = "_Its agents have run and found nothing to write yet._"
CITED = re.compile(r"\[\^([^\]\s]+)\]")
ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
CLOSING = re.compile(r"(?:^|[ \t]+)#+$")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}(?=[^`]*$)|~{3,})")


@dataclass
class Citation:
    """What a line of an article rests on: a fact, a record, a row of a query, or another article.

    `source` names the record a quote came from, and `quote` is the words it rests on.
    """

    kind: str
    id: str
    source: str = ""
    quote: str = ""


@dataclass
class Property:
    """One front matter field's value, who wrote it, when, and what it rests on: the curator that admitted the
    article, or one of its agents. The store keeps `at` as UTC."""

    value: Any
    by: Caller
    at: str
    cites: list[str] = field(default_factory=list)  # the evidence ids the writer said the value rests on


@dataclass
class Compilation:
    """One part of an article, as one run of one agent, or code, wrote it.

    A part an agent wrote `whole` is its text as handed in, its own headings in it, stored under the agent's id in
    place of a heading. Any other part is text under `heading`, which the article shows above it. `digest` is the
    digest of the evidence that run read when it started, `started` when that run started, and `at` when it wrote
    the part.
    """

    article: str
    heading: str
    text: str
    by: str
    digest: str = ""
    at: str = ""
    citations: list[Citation] = field(default_factory=list)
    started: str = ""
    whole: bool = False


def protected(name: str) -> str:
    """The protected field `name` reads as, or "" for any other: read past
    case, an underscore or a hyphen at either end, spaces and invisible characters, fullwidth letters, and letters of
    another script that look like Latin ones."""
    plain = "".join(c for c in unicodedata.normalize("NFKC", name) if not c.isspace() and unicodedata.category(c) != "Cf")
    plain = plain.translate(LOOKALIKES).strip("_-").casefold()
    return plain if plain in PROTECTED else ""


def stands(name: str) -> bool:
    """Whether a value written to the field `name` stands as that field: any field but a protected one does. The
    system's value of a protected field always takes its place."""
    return not protected(name)


def fields(article: Article) -> dict[str, Property]:
    """The article's fields as everything reads them: each property that `stands`. The system's value of a protected
    field takes the place of any writer's value of it."""
    return {name: prop for name, prop in article.properties.items() if stands(name)}


def atx(line: str) -> tuple[int, str] | None:
    """A line read as an ATX heading, as CommonMark reads one: up to three spaces, one to six `#`, then a space, a tab
    or the line's end, then its words, less a closing run of `#` after a space. Its level and its words, or None."""
    m = ATX.match(line.rstrip("\r\n"))
    if m is None:
        return None
    words = m.group(2) or ""
    return len(m.group(1)), CLOSING.sub("", words).strip()


def fence_open(line: str) -> str:
    """The run of backticks or tildes a line opens a code fence with, as CommonMark reads one, or ""."""
    m = FENCE_OPEN.match(line.rstrip("\r\n"))
    return m.group(1) if m else ""


class Blocks:
    """Which lines sit inside a code fence or an HTML comment, where no heading and no prose is: one rule for every
    reader of a text, as CommonMark has it. A fence closes on a line of the same character, at least as long, with up
    to three spaces before it and nothing after it; a comment that opens a line closes on the line holding `-->`."""

    def __init__(self) -> None:
        self.fence = ""
        self.comment = False

    def skip(self, line: str) -> bool:
        """Whether `line` is a fence's or a comment's own line or inside one."""
        text = line.rstrip("\r\n")
        if self.fence:
            close = text.lstrip(" ")
            if len(text) - len(close) <= 3 and close.startswith(self.fence) and \
                    not close.rstrip(" \t").lstrip(self.fence[0]):
                self.fence = ""
            return True
        if self.comment:
            self.comment = "-->" not in text
            return True
        if opened := fence_open(text):
            self.fence = opened
            return True
        trimmed = text.lstrip(" ")
        if len(text) - len(trimmed) <= 3 and trimmed.startswith("<!--"):
            self.comment = "-->" not in trimmed[4:]
            return True
        return False


@dataclass
class Piece:
    """One piece of a text cut at its headings: the heading's level and words, the line as written, and what is
    under it, kept as written but for blank lines at its ends. Text before any heading has level 0."""

    level: int
    heading: str
    line: str
    under: str


def pieces(text: str) -> list[Piece]:
    """Text cut at each level-one or level-two heading outside a fence or a comment, each piece as `Piece` holds it.
    Text before the first heading is the first piece, with no heading. A heading with nothing under it is kept."""
    out: list[Piece] = []
    level, heading, line_, lines = 0, "", "", []
    blocks = Blocks()

    def close() -> None:
        kept = list(lines)
        while kept and not kept[0].strip():
            kept.pop(0)
        while kept and not kept[-1].strip():
            kept.pop()
        under = "\n".join(kept)
        if level or under:
            out.append(Piece(level, heading, line_, under))

    for line in text.replace("\r\n", "\n").split("\n"):
        if not blocks.skip(line) and (found := atx(line)) is not None and found[0] <= 2:
            close()
            (level, heading), line_, lines = found, line.rstrip(), []
            continue
        lines.append(line)
    close()
    return out


def cut(text: str) -> list[tuple[str, str]]:
    """Text cut at each level-one or level-two heading outside a fence or a comment, as (heading, what is under it).
    Text before the first heading is the first, under no heading. A heading with nothing under it is kept, with
    empty text."""
    return [(p.heading, p.under) for p in pieces(text)]


def part_text(compilation: Compilation) -> str:
    """A part as the article shows it: a whole part as written, any other under its heading."""
    if compilation.whole:
        return compilation.text.strip()
    return f"## {compilation.heading}\n\n{compilation.text.strip()}" if compilation.text.strip() else ""


def body(article: Article) -> str:
    """The article's text: each part as `part_text` shows it, in order."""
    return "\n\n".join(text for compilation in article.compilations if (text := part_text(compilation)))


def passages(article: Article) -> list[Compilation]:
    """The article as a reader takes it in, one heading at a time: each part not written whole as it is, and each
    whole part cut by `cut`, each piece under its heading, empty before the first, with the part's mark, digest and
    times, and the citations its own text names."""
    out: list[Compilation] = []
    for compilation in article.compilations:
        if not compilation.whole:
            out.append(compilation)
            continue
        for heading, text in cut(compilation.text):
            ids = set(CITED.findall(text))
            out.append(Compilation(compilation.article, heading, text, compilation.by, compilation.digest, compilation.at,
                               [c for c in compilation.citations if c.id in ids], compilation.started))
    return out


@dataclass
class Identity:
    """What an article is about: its id (its path under `articles/`), its collection (the id's folder), its title,
    the keys that bind records to it, its status, and who admitted it, when and why."""

    id: str
    collection: str
    title: str = ""
    keys: dict[str, list[str]] = field(default_factory=dict)
    status: str = "active"
    admitted_by: Caller | None = None
    admitted_at: str = ""
    admitted_reason: str = ""  # the one line its admit gave
    in_source: bool = True  # false once its source removed its record and a person's word kept it in the working set


@dataclass
class Article:
    """An article as read: its identity, its compilations in order, and each property as last written."""

    identity: Identity
    compilations: list[Compilation] = field(default_factory=list)
    properties: dict[str, Property] = field(default_factory=dict)


def compiled_at(article: Article) -> str:
    """When the run that wrote the oldest of an article's parts started, or when it wrote it if the part does not say
    when its run started, or "" if no part says either: the article's `compiled_at`, as its front matter, a read and a
    query give it."""
    times = [s.started or s.at for s in article.compilations if s.started or s.at]
    return min(times) if times else ""


def written(article: Article) -> bool:
    """Whether a run has written the article, as its `compiled_at` records it. The fields its curator set when it
    admitted it do not count: they are no run's writing."""
    return bool(compiled_at(article))


def pending(article: Article) -> bool:
    """Whether the article is in the working set and no run has written it yet. It is shown to a person or an outside
    agent all the same, marked with `PENDING`, so no reader takes it for one written."""
    from .identity import in_working_set

    return in_working_set(article.identity) and not written(article)


PENDING = "pending: article admitted but not yet written"  # the label a reader sees


def _joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _day(at: str, zone: tzinfo) -> str:
    """A fact's time as the one date style an article writes, such as Oct 6, 2026, in `zone`, or "" if it is no time."""
    try:
        moment = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return ""
    moment = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(zone)
    return f"{moment:%b} {moment.day}, {moment.year}"


def empty_note(article: Article, facts: list[Fact] | None = None, zone: tzinfo = UTC) -> list[str]:
    """The note an article carries while its body holds nothing a reader sees, as its lines, or none once it does.

    The lead says whether its agents have run: an article with headings and nothing under them has. The second line
    says where its identity came from, the `source` it names, else who admitted it, else you, and how many of the
    facts linked to it stand, from which sources, and the day of the newest, in `zone`.
    """
    if body(article):
        return []
    lead = NOTE_RAN_LEAD if article.compilations else NOTE_LEAD
    named = article.properties.get("source")
    admitted = article.identity.admitted_by
    source = str(named.value) if named is not None else admitted.name if admitted is not None else "you"
    current = [f for f in facts or [] if f.status == "current"]
    source = inert(source)  # a claim's value, shown as written and never as markup
    if not current:
        return [lead, f"Identity from {source}. No facts yet."]
    sources = list(dict.fromkeys(f.source for f in current if f.source))
    line = f"Identity from {source}. {len(current)} fact{'s' if len(current) != 1 else ''}"
    line += f" from {_joined(sources)}" if sources else ""
    day = _day(max(f.at for f in current), zone)
    line += f", the last {day}" if day else ""
    return [lead, line + ". Reads serve them now."]


class _Indented(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def yaml_text(value: Any) -> str:
    """`value` as block YAML for a file a person opens: keys in their order, each value on one line, and a list's
    items indented under their key."""
    return yaml.dump(value, Dumper=_Indented, sort_keys=False, allow_unicode=True, width=1 << 20)


YAML_PLAIN = re.compile(r"[\w .,;:'/@#%+=?-]*")  # what a value holds that no renderer reads as markup
QUOTED = "CerebroQuoted"


def _quoted_scalar(text: str) -> str:
    """`text` as a YAML double-quoted scalar that any YAML reader reads back as `text`, written in letters, digits,
    spaces and `.,;:'/@#%+=?-_` alone, every other character as an escape such as `\\x3C`, so a renderer that reads
    the front matter as text finds no markup in it."""
    out = []
    linked = _bare_bad(text)  # a colon GitHub would read as a scheme's is written as an escape too
    for c in text:
        if c in "\\\"":
            out.append("\\" + c)
        elif YAML_PLAIN.fullmatch(c) and not (c.isspace() and c != " ") and unicodedata.category(c) not in ("Cf", "Co") \
                and not (linked and c == ":"):
            out.append(c)
        elif ord(c) < 0x100:
            out.append(f"\\x{ord(c):02X}")
        elif ord(c) < 0x10000:
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(f"\\U{ord(c):08X}")
    return '"' + "".join(out) + '"'


def _risky(text: str) -> bool:
    return not YAML_PLAIN.fullmatch(text) or any(c.isspace() and c != " " or unicodedata.category(c) in ("Cf", "Co")
                                                 for c in text) or _bare_bad(text) or QUOTED in text


def front_matter(front: dict[str, Any]) -> str:
    """`front` as YAML that reads back as `front`, and in which a renderer that reads it as Markdown text finds no
    markup: a key or a text value with anything but letters, digits, spaces and `.,;:'/@#%+=?-_` in it is written
    double-quoted, with escapes, and every other value as YAML writes it, on one line."""
    held: dict[str, str] = {}

    def hold(value: Any) -> Any:
        if isinstance(value, str) and _risky(value):
            held[key := f"{QUOTED}{len(held)}x"] = _quoted_scalar(value)
            return key
        if isinstance(value, dict):
            return {hold(k): hold(v) for k, v in value.items()}
        if isinstance(value, list):
            return [hold(v) for v in value]
        return value

    text = yaml_text(hold(front)).strip()
    return re.sub(QUOTED + r"\d+x", lambda m: held[m.group()], text) if held else text


def markdown(article: Article, facts: list[Fact] | None = None, zone: tzinfo = UTC) -> str:
    """The article as a person reads it: front matter, the empty-article note while there is nothing else to read,
    each compilation under its heading, then a footnote per citation with its source and quote. `facts` are the facts
    linked to the article, which the note counts."""
    who = article.identity
    front: dict[str, Any] = {"id": who.id, "title": who.title}
    if who.status != "active":
        front["status"] = who.status
    if who.keys:
        front["identity"] = who.keys
    if not who.in_source:
        front["in_source"] = False
    front |= {name: prop.value for name, prop in fields(article).items() if name not in front}  # the system's first
    if at := compiled_at(article):
        front["compiled_at"] = at
    lines = ["---", front_matter(front), "---", ""]
    for line in empty_note(article, facts, zone):
        lines += [line, ""]
    notes: dict[str, Citation] = {}
    for compilation in article.compilations:
        text = compilation.text.lstrip("\n").rstrip()  # never a leading space: it may indent a code block
        if compilation.whole:
            lines += [text, ""] if text else []
        else:
            lines += [f"## {compilation.heading}", ""] + ([text, ""] if text else [])
        notes |= {c.id: c for c in compilation.citations if c.id not in notes}
    for citation in notes.values():
        said = " ".join(citation.quote.split())  # one line: a quote's line breaks never forge a heading of the copy
        quote = f' "{said}"' if said else ""
        lines += [f"[^{label(citation.id)}]: {inert(citation.source) or inert(citation.kind) or "source"}{quote}", ""]  # never cleaned
        # each its own block: a renderer without footnotes reads lines together as one paragraph, where a backtick or
        # a bracket of one quote would pair with the next line's
    return "\n".join(lines).rstrip() + "\n"


def mirror(article: Article, root: Path, facts: list[Fact] | None = None, zone: tzinfo = UTC) -> Path:
    """Write the article's copy under `root/articles/`, as `markdown` lays it out, and return its path.

    The copy goes to a temporary name and is renamed into place, so a reader never sees half a file. An OSError
    means the copy was not written, as for an id whose path resolves outside `articles/`.
    """
    folder = (root / "articles").resolve()
    path = (folder / f"{article.identity.id}.md").resolve()
    if not path.is_relative_to(folder):
        raise OSError(f"articles/{article.identity.id}.md resolves outside articles/, so no copy is written: the id "
                      "names a path out of the mirror folder")
    make_folder(path.parent)
    partial = path.with_name(f".{path.name}.partial")
    try:
        write_file(partial, markdown(article, facts, zone))
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return path
