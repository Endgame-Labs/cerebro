"""What lands in an article is masked, then cleaned, and is safe in GitHub's renderer and in markdown-it: no element,
comment or processing instruction of its own, no link to another scheme than http, https or mailto, no image, and no
footnote or heading it made itself. And plain text reads the same after as before.

The cases are the inputs held in tests/data/clean/, and text made at random from the pieces markup is made of."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import pytest
from renderers import RENDERERS, live, shown

from cerebro.clean import land

CASES = json.loads((Path(__file__).parent / "data" / "clean" / "cases.json").read_text())
SECRET = "sk-standin-AAAA1111BBBB2222CCCC"  # made up


def mask(text: str) -> str:
    """A stand-in of the workspace's mask: one held value and the token shape, each to its own words."""
    text = re.sub(r"cbt_[A-Za-z0-9_-]{43}", "[a token, masked]", text)
    return text.replace(SECRET, "[secret]")


def made(text: str) -> list[str]:
    """What the text made of itself that only the wiki may make: a footnote, or a heading at the part's own level or
    above it (a part sits under a level-two heading)."""
    found = []
    for name, render in RENDERERS.items():
        out = render(text)
        if 'class="footnotes' in out:
            found.append(f"{name}: a footnote")
        if re.search(r"<h[12][ >]", out):
            found.append(f"{name}: a heading at level 1 or 2")
    return found


def landed(text: str) -> str:
    out = land(text, mask, top=2).text
    assert land(out, mask, top=2).text == out, "landing the landed text again changed it"
    return out


@pytest.mark.parametrize("case", range(len(CASES)))
def test_each_held_cleaning_case_lands_with_nothing_live(case: int) -> None:
    out = landed(CASES[case])
    assert live(out) == {}, (CASES[case], out)
    assert made(out) == [], (CASES[case], out)


PIECES = ["<", ">", "</", "<!--", "-->", "<?", "?>", "<!", "<![CDATA[", "]]>", "<img src=x onerror=alert(1)>",
          "<script>", "</script>", "<a href=", "javascript:", "JaVa\tScRiPt:", "&#106;", "&#x6A;", "&colon;", "&lt;",
          "&amp;", "\\", "\\<", "`", "``", "```", "~~~", "[", "]", "(", ")", "![", "](", "[^", "]:", "\"", "'", "*",
          "_", "#", "## ", "\n", "\n\n", "    ", "> ", "- ", "1. ", "|", "| --- |", "http://", "https://", "www.",
          "mailto:", "@", "x.test", "/", "{", "}", "${", "{{", "a", "word", " ", "\x01", "\u200b", "\u202e", "data:",
          "vbscript:", "//evil.test", "<https://ok.test>", "<javascript:x>", "[x]: javascript:y", SECRET, "\t", "=",
          "-->", "<p>", "<div>", "</div>", "<b>", "</b>", "<svg onload=alert(1)>", "<iframe srcdoc=x>", ":", ";"]


@pytest.mark.parametrize("seed", range(40))
def test_text_made_at_random_from_the_pieces_of_markup_lands_with_nothing_live(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(50):
        text = "".join(rng.choice(PIECES) for _ in range(rng.randint(1, 40)))
        out = landed(text)
        assert live(out) == {}, (text, out)
        assert made(out) == [], (text, out)


# -- plain text is not harmed: a reader sees the same words after as the writer meant before


PLAIN = [
    ("The field <Title> names the article.", "The field <Title> names the article."),
    ("Fetch users/<id> for one record.", "Fetch users/<id> for one record."),
    ("Write `<b>` for bold, and `a < b` too.", "Write <b> for bold, and a < b too."),
    ("When 3 < 4 and a <= b, stop.", "When 3 < 4 and a <= b, stop."),
    ("See https://docs.example.test/start.", "See https://docs.example.test/start."),
    ("Mail ops@acme.example or <ops@acme.example>.", "Mail ops@acme.example or ops@acme.example."),
    ("| a | b |\n| --- | --- |\n| <x> | 1 |", "a b <x> 1"),
    ("import the list from the old sheet.", "import the list from the old sheet."),
    ("export it as CSV each Friday.", "export it as CSV each Friday."),
    ("Loop over <i> from zero; see <i>this</i> too.", "Loop over <i> from zero; see <i>this</i> too."),
    ("The claim <title> is here [^f_1] and </title> ends.", "The claim <title> is here [^f_1] and </title> ends."),
    ("Use {{name}} and ${id} in the template.", "Use {{name}} and ${id} in the template."),
    ("```html\n<b>kept</b>\n```", "<b>kept</b>"),
    ("Welcome2026! to the new hires.", "Welcome2026! to the new hires."),
]


@pytest.mark.parametrize(("text", "words"), PLAIN, ids=[t for t, _ in PLAIN])
@pytest.mark.parametrize("renderer", sorted(RENDERERS))
def test_plain_text_reads_the_same_after_it_lands(text: str, words: str, renderer: str) -> None:
    if text.startswith("|") and renderer.startswith("cmark") and "gfm" not in renderer:
        words = "| a | b | | --- | --- | | <x> | 1 |"  # plain CommonMark reads no table, before or after
    assert shown(landed(text), RENDERERS[renderer]) == words


# -- the order: masked, then cleaned


def test_a_masks_own_words_never_become_a_link_whatever_follows_them() -> None:
    """A token's shape masked, then `(javascript:...)` after it, and a reference line after a held value."""
    token = "cbt_" + "Q" * 43
    for text in (f"Sign in with {token}(javascript:alert(document.cookie)) today.",
                 f"See {SECRET} for the key.\n\n[secret]: javascript:alert(2)",
                 f"See [{SECRET}](javascript:alert(3))."):
        out = landed(text)
        assert live(out) == {} and "](javascript:" not in out and "]: javascript:" not in out, out
        assert "[secret]" in out or "[a token, masked]" in out, out


def test_cleaning_never_escapes_a_secret_out_of_the_masks_sight() -> None:
    """A held value holding `${`, `{{` or a tag is masked whole before cleaning touches it."""
    for held in ("Qm7${Lp2#Vx9&Rt4Kd", "Tr0ub4dor-<!Kq7>-staple", "ab{{cd}}ef-1234"):
        out = land(f"The db password {held} was rotated.", lambda t, held=held: t.replace(held, "[secret]")).text
        assert held not in out and "[secret]" in out and not any(part in out for part in re.split(r"[<>{}$!]", held)
                                                                 if len(part) > 4)


def test_a_secret_split_by_an_invisible_character_is_masked_once_cleaning_joins_it() -> None:
    out = land(f"Deploy key {SECRET[:6]}\u200b{SECRET[6:]} rotated.", mask)
    assert SECRET not in out.text and "[secret]" in out.text
    assert all(SECRET not in line for line in out.removed + out.noted)


def test_landing_cleans_at_most_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.clean

    calls: list[str] = []
    real = cerebro.clean.clean

    def counting(text: str, **options: object) -> cerebro.clean.Cleaned:
        calls.append(text)
        return real(text, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(cerebro.clean, "clean", counting)
    out = land(f"{SECRET[:4]}\u200b{SECRET[4:]} and <b>x</b>", mask)
    assert len(calls) <= 2 and SECRET not in out.text


@pytest.mark.parametrize("seed", range(10))
def test_a_whole_article_file_with_random_footnote_sources_and_quotes_has_nothing_live(seed: int) -> None:
    """The file a reader sees: front matter, the empty note or the parts, and each citation's footnote, whose source
    is a record's id shown as written and never cleaned, and whose quote landed with its part."""
    from cerebro.article import Article, Citation, Compilation, Identity, Property, markdown
    from cerebro.caller import Caller
    from cerebro.store import Store

    rng = random.Random(seed)

    def piece() -> str:
        return "".join(rng.choice(PIECES) for _ in range(rng.randint(1, 8)))

    landing = Store.landed.__get__(type("S", (), {"scrubbing": lambda self: mask})())
    for _ in range(30):
        cited = [Citation("record", f"f_{n}", piece(), piece()) for n in range(3)]
        part, _ = landing(Compilation("t/a", "Overview", piece() + " [^f_0] [^f_1] [^f_2]", by="x", citations=cited))
        claim = Property(piece(), Caller("you", "person"), "2026-11-19T00:00:00Z")
        empty = Article(Identity("t/a", "t", "A"), [], properties={"source": claim})
        for article in (Article(Identity("t/a", "t", "A"), [part]), empty):
            text = markdown(article).split("\n---\n", 1)[1]  # after the front matter, which a site reads as data
            assert live(text) == {}, text
            for name, render in RENDERERS.items():  # only the wiki's: one level-two heading for the one part
                out = render(text)
                assert len(re.findall(r"<h[12][ >]", out)) == len(article.compilations), (name, text)
                assert article.compilations or 'class="footnotes' not in out, (name, text)
