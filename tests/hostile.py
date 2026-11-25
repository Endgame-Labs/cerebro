"""Articles made at random, from a seed, with hostile pieces in every place text can enter an article's file, and a
check that writes each through the real store and renders the whole file as written in every setup `renderers`
holds.

The places: a paragraph, a whole part, a quote, the title, a footnote's source, a row id, a table cell, a list, a
block quote, a fence, indented code, a heading in the text, a part's own heading, a field, and the article's id. The
splits, where one piece starts in one place and ends in the next: across two whole parts, from a part to the next
part's heading, from a headed part to a whole one, from the text to a quote, from one quote to the next, from a
footnote's source to its quote, from the title to a field, from one field to the next, from the last part to the
first footnote, and from a list in one part to indented text in the next.

A held value goes anywhere text is written but an id or a source, which name a record and are never masked, whole or
broken by a line break, an invisible character, a no-break space or a soft hyphen."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from renderers import problems

from cerebro.article import Citation, Compilation, Identity, Property, markdown
from cerebro.caller import Caller
from cerebro.secrets import Scrubber
from cerebro.store import Store

HELD = ["WrapAlpha42BetaGamma", "Ab<Cd9Ef8Gh", "Qx`Rt7|Lm9#Zz", "Pw&Kd4^[Mn8", "Tok3nAbcDef99"]  # made up
BREAKS = ["", "\n", "\n> ", "\n  ", "\u200b", "\u00a0", "\u00ad", "\u2060", "\ufeff", "\u200e"]
IMG = "<img src=x onerror=alert(1)>"
WORDS = ["a", "thing", "the", "report", "word", "note", "x", "list", "row", "x.test", "users", "id", "3", "4"]
PIECES = [
    "<", ">", "</", "<!--", "-->", "<?", "?>", "<!", "<![CDATA[", "]]>", IMG, "<script>", "</script>", "<style>",
    "<textarea>", "<pre>", "<div>", "</div>", "<b>", "</b>", "<p", "<x-y>", "<svg/onload=1>", "<IMG SRC=x>",
    "<a href='javascript:x'>", "<!DOCTYPE x>", "<?php ?>", "<!-- c -->", "javascript:", "JaVa\tScRiPt:",
    "&#106;", "&#x6A;", "&colon;", "&lt;", "&amp;", "&#8203;", "&#x200b;", "&nbsp;", "\\", "\\<", "\\|", "\\\\|",
    "`", "``", "```", "````", "~~~", "[", "]", "(", ")", "![", "](", "[^", "]:", "^[", "\"", "'", "*", "_", "#",
    "## ", "# ", "\n", "\n\n", "    ", "\n    ", "> ", "\n> ", "- ", "\n- ", "1. ", "\n1. ", "|", "| --- |",
    "\n===", "\n---", "+++", "---", "http://", "https://", "www.", "mailto:", "xmpp:", "ftp://", "@", "x.test",
    "/", "{", "}", "${", "{{", " ", "\t", "\x01", "\u200b", "\u202e", "\u00a0", "data:", "vbscript:",
    "//evil.test", "<https://ok.test>", "<javascript:x>", "<ftp://x.test>", "<a@b.test>", "[x]: javascript:y",
    "[x]: javascript:alert(1)(", "[x]", "[secret]", "[secret](javascript:x)", "[a token, masked]",
    "[a](javascript:alert(1))", "[a](<javascript:x>)", "[a]( javascript:x )", "[a](jav&#x61;script:x)",
    "![i](https://img.test/x.png)", "![i][x]", "https://x.test/users/<id>", "xmpp:ops@acme.test",
    "x.mailto:ops@acme.test", "See:https://x.test`", "`x | " + IMG + "` y", "`" + IMG + "`", "``" + IMG + "``",
    "a < b", "x<y", "<3", "->", "<-", "List<String>", "[^f1]", "=", ":", ";", "*x*", "~~x~~",
]
SPANNING = ["`" + IMG + "`", "[a](javascript:alert(1))", "<script>alert(1)</script>", "```\n" + IMG + "\n```",
            "[x]: javascript:alert(1)\n\n[x]", "`x | " + IMG + "`", IMG, "<!-- " + IMG + " -->",
            "![i](https://img.test/x.png)", "``` `" + IMG + "` ```"]
SPLITS = ["parts", "part-heading", "headed-whole", "text-quote", "quote-quote", "source-quote", "title-field",
          "field-field", "part-footnote", "list-indent"]


@dataclass
class Case:
    """One article to write: its id, title, fields, and parts, each part with its citations."""

    id: str
    title: str
    fields: dict[str, Any] = field(default_factory=dict)
    parts: list[Compilation] = field(default_factory=list)


class Maker:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.seed = seed

    def held(self) -> str:
        value = self.rng.choice(HELD)
        cut = self.rng.choice(BREAKS)
        if not cut:
            return value
        at = self.rng.randint(1, len(value) - 1)
        return value[:at] + cut + value[at:]

    def frag(self, most: int = 6, held: bool = True) -> str:
        out = []
        for _ in range(self.rng.randint(1, most)):
            roll = self.rng.random()
            if held and roll < 0.06:
                out.append(self.held())
            elif roll < 0.55:
                out.append(self.rng.choice(PIECES))
            else:
                out.append(self.rng.choice(WORDS))
            if self.rng.random() < 0.4:
                out.append(" ")
        return "".join(out)

    def plain(self, text: str) -> str:
        """`text` as an id or a source may be: one line, never a held value."""
        return " ".join(text.split()) or "x"

    def line(self, marks: str = "") -> str:
        return " ".join(self.frag(4) for _ in range(self.rng.randint(1, 3))) + marks

    def block(self, markers: list[str]) -> str:
        kind = self.rng.choice(["paragraph", "table", "list", "quote", "fence", "indented", "heading", "raw"])
        cite = (" " + " ".join(markers)) if markers and self.rng.random() < 0.7 else ""
        if kind == "paragraph":
            return "\n".join(self.line() for _ in range(self.rng.randint(1, 3))) + cite
        if kind == "table":
            cols = self.rng.randint(1, 3)
            rows = ["| " + " | ".join(self.frag(3) for _ in range(cols)) + " |", "|" + " --- |" * cols]
            rows += ["| " + " | ".join(self.frag(4) for _ in range(cols)) + " |" for _ in range(self.rng.randint(1, 3))]
            return "\n".join(rows) + cite
        if kind == "list":
            mark = self.rng.choice(["- ", "* ", "1. ", "2) "])
            items = [mark + self.line() for _ in range(self.rng.randint(1, 3))]
            if self.rng.random() < 0.5:
                items.insert(self.rng.randint(0, len(items)), "    " + self.line())
            return "\n".join(items) + cite
        if kind == "quote":
            return "\n".join(self.rng.choice(["> ", ">", "", "> > "]) + self.line() for _ in range(3)) + cite
        if kind == "fence":
            mark = self.rng.choice(["```", "~~~", "````"])
            info = self.rng.choice(["", "js", "yaml", self.frag(2, held=False)])
            body = "\n".join(self.frag(4) for _ in range(self.rng.randint(1, 3)))
            close = self.rng.choice([mark, "", mark[0] * 2])
            return f"{self.rng.choice(['', ' ', '> ', '- '])}{mark}{info}\n{body}\n{close}"
        if kind == "indented":
            return "\n".join("    " + self.frag(4) for _ in range(self.rng.randint(1, 3)))
        if kind == "heading":
            return self.rng.choice(["# ", "## ", "### ", "#### "]) + self.line() + cite
        return self.frag(8)

    def case(self, n: int) -> tuple[Case, list[tuple[str, str]]]:
        rng = self.rng
        name = re.sub(r"[\\:/\x00-\x1f]", "", self.plain(self.frag(2, held=False))) if rng.random() < 0.3 else ""
        article = f"things/c{self.seed}-{n}-{name}".rstrip(".")
        if article.split("/")[1] in ("", ".", ".."):
            article = f"things/c{self.seed}-{n}"
        case = Case(article, self.frag(4))
        for _ in range(rng.randint(0, 3)):
            key = rng.choice(["owner", "stage", "note", self.plain(self.frag(2, held=False))])
            if key == "id":
                key = "ident"
            kind = rng.random()
            value: Any = self.frag(5) if kind < 0.6 else [self.frag(3) for _ in range(2)] if kind < 0.8 else \
                {self.plain(self.frag(1, held=False)): self.frag(3)} if kind < 0.9 else rng.randint(-5, 5)
            case.fields[key] = value
        for p in range(rng.randint(1, 4)):
            ids = [self.plain(self.frag(2, held=False)) if rng.random() < 0.4 else f"f_{p}_{k}"
                   for k in range(rng.randint(0, 3))]
            ids = list(dict.fromkeys(ids))
            cites = [Citation("record", id, self.plain(self.frag(2, held=False)) if rng.random() < 0.5 else "",
                              self.frag(5)) for id in ids]
            markers = [f"[^{id}]" for id in ids]
            text = "\n\n".join(self.block(markers) for _ in range(rng.randint(1, 4)))
            whole = rng.random() < 0.45
            heading = "" if whole else (rng.choice(["Overview", "Notes"]) + (f" {p}" if p else "")
                                         if rng.random() < 0.6 else self.frag(3))
            case.parts.append(Compilation(article, heading or f"by-{p}", text, by=f"things/w{p}", citations=cites,
                                      whole=whole))
        splits = [rng.choice(SPLITS) for _ in range(rng.randint(0, 2))]
        made = []
        for split in splits:
            piece = rng.choice(SPANNING)
            at = rng.randint(1, len(piece) - 1)
            self.split(case, split, piece[:at], piece[at:])
            made.append((split, piece))
        return case, made

    def split(self, case: Case, how: str, left: str, right: str) -> None:
        parts = case.parts
        quotes = [c for s in parts for c in s.citations]
        if how in ("parts", "part-heading", "headed-whole", "list-indent") and len(parts) > 1:
            a, b = parts[0], parts[1]
            if how == "headed-whole":
                a.whole, b.whole = False, True
            if how == "list-indent":
                a.text += "\n\n- " + left
                b.text = "    " + right + "\n" + b.text
            elif how == "part-heading" and not b.whole:
                a.text += left
                b.heading = right + b.heading
            else:
                a.text += left
                b.text = right + b.text
        elif how == "text-quote" and quotes:
            parts[-1].text += left
            quotes[0].quote = right + quotes[0].quote
        elif how == "quote-quote" and len(quotes) > 1:
            quotes[0].quote += left
            quotes[1].quote = right + quotes[1].quote
        elif how == "source-quote" and quotes:
            quotes[0].source = self.plain(quotes[0].source + left) if "\n" not in left else quotes[0].source
            quotes[0].quote = right + quotes[0].quote
        elif how == "title-field":
            case.title += left
            case.fields["note"] = right
        elif how == "field-field":
            case.fields["note"] = left
            case.fields["stage"] = right
        elif how == "part-footnote" and quotes:
            parts[-1].text += left
            quotes[0].quote = right + quotes[0].quote
        else:
            parts[0].text += left + right


def write(store: Store, case: Case) -> str:
    """The article file `case` gives once each of its pieces is written through `store`."""
    store.create(Identity(case.id, case.id.split("/")[0], case.title))
    for name, value in case.fields.items():
        store.set_field(case.id, name, Property(value, Caller("you", "person"), "2026-11-19T00:00:00Z"))
    for part in case.parts:
        store.write_compilation(part)
    article = store.article(case.id)
    assert article is not None
    return markdown(article)


def check(store: Store, case: Case, again: int = 2) -> list[str]:
    """What is wrong with `case` written through `store`: anything `renderers.problems` finds in its file; an id
    changed in the store; and a part that lands other than it stands when it is landed `again` more times."""
    file = write(store, case)
    found = problems(file, HELD)
    article = store.article(case.id)
    assert article is not None
    if article.identity.id != case.id:
        found.append("the article's id changed")
    given = {c.id for part in case.parts for c in part.citations}
    if kept := {c.id for part in article.compilations for c in part.citations} - given:
        found.append(f"a row id changed: {sorted(kept)} is none of {sorted(given)}")
    for part in article.compilations:
        now = part
        for _ in range(again):
            store._landing = {}  # land it in full, not as the store remembers landing it
            now = store.landed(now)[0]
            if (now.text, now.heading, [c.quote for c in now.citations]) != \
                    (part.text, part.heading, [c.quote for c in part.citations]):
                found.append(f"landing again changed the part {part.heading!r}")
                break
    return found


def masked(store: Store) -> Store:
    """`store` with a mask of every held value, as the workspace's secrets store gives it."""
    mask = Scrubber.of(HELD)
    store.masks = lambda: mask
    return store


def run(store: Store, seed: int, count: int, again: int = 2) -> list[tuple[int, str, list[str], Case]]:
    """Make `count` cases from `seed`, check each, and give every one that failed."""
    maker = Maker(seed)
    failed = []
    for n in range(count):
        case, made = maker.case(n)
        try:
            found = check(store, case, again)
        except Exception as e:  # noqa: BLE001 - a crash is a finding too
            found = [f"crashed: {type(e).__name__}: {e}"]
        if found:
            failed.append((n, ", ".join(f"{how}: {piece!r}" for how, piece in made), found, case))
    return failed


def describe(case: Case) -> str:
    return repr({"id": case.id, "title": case.title, "fields": case.fields,
                 "parts": [(s.heading, s.whole, s.text, [(c.id, c.source, c.quote) for c in s.citations])
                           for s in case.parts]})


if __name__ == "__main__":
    import sys
    import tempfile

    from cerebro.store import SQLiteStore

    seed, count = int(sys.argv[1]), int(sys.argv[2])
    with tempfile.TemporaryDirectory() as folder, SQLiteStore(Path(folder) / "workspace.sqlite") as kept:
        for n, made, found, case in run(masked(kept), seed, count):
            print(f"seed {seed} case {n} [{made}]: {found[:3]}\n  {describe(case)[:600]}")
