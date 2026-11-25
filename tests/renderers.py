"""The renderers an article file is promised to be safe in, as a reader may set them up, and a reader of what they
give that lists anything live in it.

cmark-gfm, by its Python binding `cmarkgfm` (a test dependency), with raw HTML on and GitHub's tag filter off, in five
setups: plain CommonMark, plain with liberal tags, GitHub's extensions (tables, bare addresses, strikethrough, task
lists), those with footnotes, and those with footnotes and liberal tags. markdown-it, by markdown-it-py, with raw
HTML on, in five: its default extensions, with footnotes, with bare addresses linked, with both, and with both and a
link check that lets every address through. A test asks `live(text)` for every live thing any of them shows, and
`problems(file, values)` for that and for any held value a reader can read in the file."""

from __future__ import annotations

import html.parser
import re
from collections.abc import Callable

import cmarkgfm
from cmarkgfm.cmark import Options
from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin

UNSAFE = Options.CMARK_OPT_UNSAFE
LIBERAL = Options.CMARK_OPT_LIBERAL_HTML_TAG
FOOTNOTES = Options.CMARK_OPT_FOOTNOTES
GITHUB = ["table", "autolink", "strikethrough", "tasklist"]  # never "tagfilter": a reader's setup may leave it off


def _cmark(options: int, extensions: list[str] | None) -> Callable[[str], str]:
    if extensions is None:
        return lambda text: cmarkgfm.markdown_to_html(text, options=options)
    return lambda text: cmarkgfm.markdown_to_html_with_extensions(text, options=options, extensions=extensions)


def _mdit(footnotes: bool, linkify: bool, lax: bool) -> Callable[[str], str]:
    md = MarkdownIt("js-default", {"html": True, "linkify": linkify})
    if linkify:
        md.enable("linkify")
    if footnotes:
        md.use(footnote_plugin)
    if lax:
        md.validateLink = lambda url: True  # a renderer that checks no link target, as a reader's may be
    return md.render


RENDERERS: dict[str, Callable[[str], str]] = {
    "cmark": _cmark(UNSAFE, None),
    "cmark+liberal": _cmark(UNSAFE | LIBERAL, None),
    "cmark-gfm": _cmark(UNSAFE, GITHUB),
    "cmark-gfm+footnotes": _cmark(UNSAFE | FOOTNOTES, GITHUB),
    "cmark-gfm+footnotes+liberal": _cmark(UNSAFE | FOOTNOTES | LIBERAL, GITHUB),
    "markdown-it": _mdit(False, False, False),
    "markdown-it+footnotes": _mdit(True, False, False),
    "markdown-it+linkify": _mdit(False, True, False),
    "markdown-it+linkify+footnotes": _mdit(True, True, False),
    "markdown-it+linkify+footnotes+lax": _mdit(True, True, True),
}

FORMATTING = {"p", "em", "strong", "code", "pre", "a", "ul", "ol", "li", "blockquote", "h1", "h2", "h3", "h4", "h5",
              "h6", "hr", "br", "table", "thead", "tbody", "tr", "th", "td", "del", "s", "sup", "section"}
SAFE = ("http", "https", "mailto")
ALIGN = re.compile(r"text-align: ?(?:left|right|center);?")


def _scheme(url: str) -> str:
    """The scheme a browser reads in an attribute already decoded: leading and trailing C0 and spaces stripped, and
    tabs and line ends taken out anywhere."""
    url = url.strip("".join(chr(i) for i in range(0x21))).replace("\t", "").replace("\n", "").replace("\r", "")
    m = re.match(r"([a-zA-Z][a-zA-Z0-9+.-]*):", url)
    return m.group(1).lower() if m else ""


class _Reader(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs)

    def _tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        given = dict(attrs)
        if tag == "input" and given.get("type") == "checkbox" and "disabled" in given:
            pass  # a task list's box, which GitHub's extension makes of `- [ ]`
        elif tag == "img":
            self.found.append(f"image {given.get('src')!r}")
        elif tag not in FORMATTING:
            self.found.append(f"element <{tag}>")
        for name, value in attrs:
            if name.startswith("on") or name in ("srcdoc", "formaction", "action"):
                self.found.append(f"attribute {name} on <{tag}>")
            if name == "style" and not (tag in ("th", "td") and ALIGN.fullmatch(value or "")):
                self.found.append(f"attribute style on <{tag}>")  # a table's alignment is markdown's own style
            if name in ("href", "src") and value is not None:
                scheme = _scheme(value)
                if scheme and scheme not in SAFE:  # an address with no scheme, `//host` too, takes the page's
                    self.found.append(f"{name} {value!r} on <{tag}>")

    def handle_comment(self, data: str) -> None:
        self.found.append("comment")

    def handle_decl(self, decl: str) -> None:
        self.found.append("declaration")

    def handle_pi(self, data: str) -> None:
        self.found.append("processing instruction")

    def unknown_decl(self, data: str) -> None:
        self.found.append("CDATA")


def found_in(rendered: str) -> list[str]:
    reader = _Reader()
    reader.feed(rendered)
    reader.close()
    return reader.found


def live(text: str) -> dict[str, list[str]]:
    """Each setup that shows something live in `text`, with what: an element or attribute beyond plain formatting,
    an image, a style, a comment, a declaration or a processing instruction, or a link to a scheme other than http,
    https or mailto."""
    out = {}
    for name, render in RENDERERS.items():
        if found := found_in(render(text)):
            out[name] = found
    return out


def _squeezed(text: str) -> str:
    return "".join(text.split()).casefold()


def readable(file: str, values: list[str]) -> list[str]:
    """Each held value a reader can read in `file`, as written or as any setup shows it, whole or broken by white
    space."""
    seen = [_squeezed(file)] + [_squeezed(shown(file, render)) for render in RENDERERS.values()]
    return [value for value in values if any(_squeezed(value) in text for text in seen)]


def problems(file: str, values: list[str] = ()) -> list[str]:  # type: ignore[assignment]
    """Everything wrong with an article file: what is live in each setup, and each held value a reader can read."""
    found = [f"{name}: {what}" for name, things in live(file).items() for what in things]
    return found + [f"held value readable: {value[:4]}..." for value in readable(file, list(values))]


def shown(text: str, render: Callable[[str], str] = RENDERERS["markdown-it"]) -> str:
    """The words a reader sees of `text`: its render with every element taken out, entities read, spaces folded."""
    words = re.sub(r"</?(?:a|em|strong|code|del|s|sup)\b[^>]*>", "", render(text))  # inline: no space
    words = re.sub(r"<[^>]*>", " ", words)
    return " ".join(html.unescape(words).split())
