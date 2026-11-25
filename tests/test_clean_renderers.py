"""What lands is safe in every setup `renderers` holds, read as the whole article file a reader gets: front matter,
headings, parts, and footnotes. The test of record is the renderers, never the cleaner's own reading of Markdown.

Each case writes through the real store (`write_compilation`, `store.article`, `markdown`) and renders the file as
written, with a mask of made-up held values."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from pathlib import Path

import hostile
import pytest
import yaml
from renderers import RENDERERS, problems, shown

from cerebro.article import Citation, Compilation, Identity, Property, markdown
from cerebro.caller import Caller
from cerebro.clean import land, land_line
from cerebro.secrets import Scrubber
from cerebro.store import SQLiteStore

IMG = "<img src=x onerror=alert(1)>"
HELD = "WrapAlpha42BetaGamma"  # made up
ANGLED = "Ab<Cd9Ef8Gh"  # made up: a held value with a `<` before a letter
MASK = Scrubber.of([HELD, ANGLED])
PERSON = Caller("you", "person")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    with SQLiteStore(tmp_path / "workspace.sqlite") as made:
        made.masks = lambda: MASK
        yield made


def file_of(store: SQLiteStore, article_id: str, title: str, parts: list[Compilation], fields: dict | None = None) -> str:
    store.create(Identity(article_id, article_id.split("/")[0], title))
    for name, value in (fields or {}).items():
        store.set_field(article_id, name, Property(value, PERSON, "2026-11-19T00:00:00Z"))
    for part in parts:
        store.write_compilation(part)
    article = store.article(article_id)
    assert article is not None
    return markdown(article)


# -- nine hostile cases, each one setup or another could show live or leak

NINE = {
    "a held value wrapped across a line in a quote": ("T", [("p", "The key was rotated [^c1].", [Citation(
        "record", "c1", "items", f"rotated key {HELD[:11]}\n{HELD[11:]} today")], True)]),
    "the title in the front matter": (IMG, [("p", "Plain words.", [], True)]),
    "a row id as a footnote's label": ("T", [("p", "The thing happened [^<script>].", [Citation(
        "record", "<script>", "items", 'he said "yes"')], True)]),
    "cmark's backtick pairing": ("T", [("p", f"Use ```` and `x` then `{IMG}` here.", [], True)]),
    "an address after a colon": ("T", [("p", f"See:https://x.test`{IMG}` here.", [], True)]),
    "indented code after a list in the part before": ("T", [("Contents", "- [Reports](./)", [], False),
                                                           ("p", f"    {IMG} a thing", [], True)]),
    "a definition cmark reads and markdown-it does not": ("T", [("p", ("See [the guide] a thing.\n\n[the guide]: "
                                                                  "javascript:alert(1)(\nA thing."), [], True)]),
    "a lazy line after a quote": ("T", [("p", f"> A thing `see\n{IMG}\n`x` here.", [], True)]),
    "a code span across a table cell's pipe": ("T", [("p", f"| a | b |\n| --- | --- |\n| `x | {IMG}` a thing |",
                                                      [], True)]),
}


@pytest.mark.parametrize("name", NINE)
def test_each_hostile_case_is_safe_in_every_setup_and_shows_no_held_value(store: SQLiteStore,
                                                                                    name: str) -> None:
    title, parts = NINE[name]
    compilations = [Compilation("things/c", heading, text, by="things/w", citations=cites, whole=whole)
                for heading, text, cites, whole in parts]
    file = file_of(store, "things/c", title, compilations)
    assert problems(file, [HELD, ANGLED]) == [], file


# -- a held value is masked whole, before anything is folded, joined or escaped

BREAKS = ["\n", "\n> ", "\u200b", "\u00a0", "\u00ad", "\u2060", "\ufeff"]


@pytest.mark.parametrize("value", [HELD, ANGLED])
@pytest.mark.parametrize("cut", BREAKS, ids=[repr(b) for b in BREAKS])
@pytest.mark.parametrize("place", ["text", "quote", "title", "field", "heading", "cell"])
def test_a_held_value_broken_anywhere_text_is_written_is_masked_whole(store: SQLiteStore, value: str, cut: str,
                                                                       place: str) -> None:
    broken = value[:4] + cut + value[4:]
    text = f"| a |\n| --- |\n| x {broken} y |" if place == "cell" else f"Key {broken} here [^c1]."
    if place in ("quote", "title", "field", "heading"):
        text = "Plain words [^c1]."
    quote = f"the key {broken} today" if place == "quote" else "said"
    part = Compilation("things/c", f"On {broken}" if place == "heading" else "Overview", text, by="things/w",
                   citations=[Citation("record", "c1", "items", quote)])
    file = file_of(store, "things/c", broken if place == "title" else "T", [part],
                   {"owner": broken} if place == "field" else None)
    assert problems(file, [value]) == [], file
    said = land(text, MASK).removed + land_line(quote, MASK).removed
    assert not [line for line in said if "".join(value.split()) in "".join(line.split())]


# -- the front matter: masked, inert as text, and read back as given

TITLES = [IMG, "R&D <draft> [x](javascript:alert(1))", "line one\nline two", 'say "hi" \\ there',
          "xmpp:ops@acme.example", "ftp://files.example/a", "`code` and ![i](https://img.example/x.png)",
          "a\u200bb\u202ec", "- a list?", "# a heading", "| --- |", "\u00dcn\u00efc\u00f6d\u00e9 \u2014 and \u65e5\u672c\u8a9e", "---", "CerebroQuoted0x"]


@pytest.mark.parametrize("title", TITLES)
def test_a_title_and_fields_in_the_front_matter_read_back_exactly_and_show_nothing_live(store: SQLiteStore,
                                                                                         title: str) -> None:
    fields = {"owner": title, title.replace("\n", " ") or "k": [title, 3, True], "map": {"k": title}}
    article = "things/" + re.sub(r"[\\:/\x00-\x1f]", "", title)[:20].strip(".") + "x"
    file = file_of(store, article, title, [Compilation(article, "Overview", "Words.", by="things/w")], fields)
    assert problems(file) == [], file
    front = yaml.safe_load(file.split("---\n")[1])
    assert front["id"] == article and front["title"] == title
    assert {k: front[k] for k in fields} == fields


def test_a_title_a_key_and_a_field_are_masked_as_they_are_written_and_the_id_never(store: SQLiteStore) -> None:
    store.create(Identity("things/c", "things", f"Key {HELD}", keys={"code": [f"x{HELD[:5]}\u200b{HELD[5:]}"]}))
    store.set_field("things/c", "owner", Property({"note": f"was {HELD}", "list": [HELD]}, PERSON, "2026-11-19T00:00:00Z"))
    article = store.article("things/c")
    assert article is not None and article.identity.id == "things/c"
    assert article.identity.title == "Key [secret]" and article.identity.keys == {"code": ["x[secret]"]}
    assert article.properties["owner"].value == {"note": "was [secret]", "list": ["[secret]"]}


def test_a_parts_heading_is_masked_and_cleaned_as_it_is_written(store: SQLiteStore) -> None:
    file = file_of(store, "things/c", "T", [Compilation("things/c", f"{IMG} {HELD}", "Words.", by="things/w")])
    article = store.article("things/c")
    assert article is not None and article.compilations[0].heading == "&lt;img src=x onerror=alert(1)> [secret]"
    assert problems(file, [HELD]) == [] and "## &lt;img" in file


# -- a row id's label

IDS = ["<script>", "List<String>", "a`b`c", "xmpp:ops@acme.example", "x.mailto:a@b.example", "ftp://h.example",
       "f_1", "rec:42", "<javascript:alert(1)>", "\u65e5\u672c"]


@pytest.mark.parametrize("id", IDS)
def test_a_row_ids_marker_and_its_footnote_carry_one_label_inert_in_every_setup(store: SQLiteStore, id: str) -> None:
    cite = Citation("record", id, "items", 'he said "yes"')
    file = file_of(store, "things/c", "T", [Compilation("things/c", "Overview", f"It happened [^{id}].", by="things/w",
                                                    citations=[cite])])
    assert problems(file) == [], file
    article = store.article("things/c")
    assert article is not None and [c.id for c in article.compilations[0].citations] == [id]  # the id is kept
    [marker] = re.findall(r"It happened \[\^([^\]\s]+)\]", file)
    assert f"\n[^{marker}]: " in file
    for name in ("cmark-gfm+footnotes", "markdown-it+footnotes"):
        assert 'class="footnote-ref"' in RENDERERS[name](file) or "data-footnote-ref" in RENDERERS[name](file), name


def test_a_landed_part_cites_by_the_label_the_store_wrote_in_place_of_the_id() -> None:
    from cerebro.compile import _cited

    cite = Citation("record", "List<String>", "items", "q")
    stored = land("A claim [^List<String>].").text.strip()
    assert stored != "A claim [^List<String>]."
    assert _cited(stored, {cite.id: cite}) == [cite]


# -- the mask's own words, fences, bare addresses

@pytest.mark.parametrize("text", [f"{HELD}(https://evil.example)", f"{HELD}(javascript:alert(1))",
                                  f"{HELD}: https://evil.example\n\nSee [secret].", f"![{HELD}](https://x.example)",
                                  f"{HELD}[x]\n\n[x]: https://evil.example"])
def test_the_masks_own_words_are_never_a_link_an_image_or_a_definition_and_never_dropped(text: str) -> None:
    out = land(text, MASK).text
    assert problems(out) == [] and "[secret]" in out, out
    for render in RENDERERS.values():
        words = shown(out, render)
        assert "[secret]" in words, (out, words)
    assert text.startswith("!") or "evil.example" in out or "javascript" in out, out  # its words stay too


@pytest.mark.parametrize("text", [">1. ~~~", "1. >- ```", "> ```yaml\n> - a", "- \n    ```\nx", "1. ~~~\n   x\n",
                                  "> - ```\n>   x", "```\nx", "~~~~\n~~~\n"])
def test_landing_any_fence_ten_times_gives_the_same_bytes(text: str) -> None:
    once = land(text).text
    now = once
    for _ in range(10):
        now = land(now).text
    assert now == once, (text, once, now)


@pytest.mark.parametrize("seed", range(3))
def test_landing_random_parts_ten_times_gives_the_same_bytes(seed: int) -> None:
    maker = hostile.Maker(1000 + seed)
    for n in range(15):
        case, _ = maker.case(n)
        for part in case.parts:
            once = land(part.text, MASK, top=1 if part.whole else 2).text
            now = once
            for _ in range(10):
                now = land(now, MASK, top=1 if part.whole else 2).text
            assert now == once, part.text


BARE = ["Chat at xmpp:ops@acme.example now", "Go x.mailto:ops@acme.example now", "Get ftp://files.example/a",
        "Go data:xmpp:ops@acme.example", "Go xmpp:x.mailto:ops@acme.example", "Go FTP://files.example",
        "Go 1ftp://files.example", "Go a\\.mailto:ops@acme.example"]


@pytest.mark.parametrize("text", BARE)
@pytest.mark.parametrize("where", ["{}", "| {} |\n| --- |\n| {} |", "> {}", "### {}", "- {}"])
def test_no_bare_address_is_linked_to_a_scheme_other_than_http_https_or_mailto(text: str, where: str) -> None:
    out = land(where.format(text, text)).text
    assert problems(out) == [], out


def test_an_address_a_tag_like_word_ends_is_shown_as_text_that_reads_right() -> None:
    out = land("Fetch https://x.test/users/<id> for one record.").text
    assert problems(out) == []
    for name, render in RENDERERS.items():
        made = render(out)
        assert "lt;id" not in "".join(re.findall(r'href="([^"]*)"', made)), (name, made)
        assert shown(out, render) == "Fetch https://x.test/users/<id> for one record.", name


# -- random articles

@pytest.mark.timeout(120)
def test_random_articles_have_nothing_live_and_no_held_value_in_their_file(tmp_path: Path) -> None:
    started = time.perf_counter()
    failed = []
    with SQLiteStore(tmp_path / "workspace.sqlite") as kept:
        for seed in (11, 12, 13):
            failed += hostile.run(hostile.masked(kept), seed, 60)
    assert failed == [], [(n, found, hostile.describe(case)[:400]) for n, _, found, case in failed[:3]]
    assert time.perf_counter() - started < 40


def test_the_cleaning_cases_hold_no_sales_word() -> None:
    cases = json.dumps(json.loads((Path(__file__).parent / "data" / "clean" / "cases.json").read_text()))
    assert not re.findall(r"(?i)\b(deals?|accounts?|opportunit\w+|contacts?|crm|leads?|pipeline|quota)\b", cases)


# -- more hostile text

DEEP = [">" * 25 + " " + IMG, "> - " * 12 + IMG, "".join(f"{'  ' * k}- a\n" for k in range(30)) + "  " * 30 + IMG,
        ">" * 22 + " [a](javascript:alert(1)) <javascript:alert(1)>"]


@pytest.mark.parametrize("text", DEEP, ids=["quotes", "quotes-and-lists", "lists", "links"])
def test_text_nested_past_the_parsers_depth_is_written_flat_with_nothing_live(store: SQLiteStore, text: str) -> None:
    file = file_of(store, "things/c", "T", [Compilation("things/c", "p", text, by="things/w", whole=True)])
    assert problems(file) == [], file
    assert land(land(text).text).text == land(text).text


@pytest.mark.parametrize("text", [f"`x\n{IMG}`\n| - |", "``\n| - |\nxmpp:a@b.test``",
                                  "1. x``\n    | - |\n    a<div>\n    ---a``"])
def test_a_head_row_with_no_pipe_never_splits_what_the_cleaner_read_as_one(text: str) -> None:
    assert problems(land(text).text) == [], land(text).text


@pytest.mark.parametrize("text", [f"a <3 `x> b\nc `{IMG}` d", f'a <3 [b](https://x.test "{IMG}")',
                                  "a <3 [b](c<img/src=x/onerror=alert(1)>)", "<>```<g>```", "<><http:>",
                                  f"| x<- id | `{IMG}`users |\n| --- | --- |\n| a | b |"])
def test_a_lone_less_than_never_lets_liberal_tags_take_what_follows(text: str) -> None:
    assert problems(land(text).text) == [], land(text).text


def test_a_lone_less_than_with_nothing_between_it_and_a_greater_than_still_reads_as_written() -> None:
    for text in ("x < y, a_b_c, $18,000, a > b and 5 <= 6.", "When 3 < 4 and a <= b.", "a -> b <- c"):
        assert land(text).text == text + "\n"


@pytest.mark.parametrize("text", ["Key Ab[](javascript:x)<Cd9Ef8Gh here.", "Key Ab&lt;Cd9Ef8Gh here.",
                                  "Key Wrap&#65;lpha42BetaGamma here.", "Key Ab\\<Cd9Ef8Gh here.",
                                  "Key WrapAlpha42&nbsp;BetaGamma here.", "Key WrapAlpha42\n\nBetaGamma here.",
                                  "Key WrapAlpha42\n- BetaGamma here."])
@pytest.mark.parametrize("place", ["text", "quote", "heading"])
def test_a_held_value_written_with_entities_escapes_or_a_link_cleaning_drops_is_masked_whole(
        store: SQLiteStore, text: str, place: str) -> None:
    part = Compilation("things/c", text if place == "heading" else "Overview",
                   text if place == "text" else "Words [^c1].", by="things/w",
                   citations=[Citation("record", "c1", "items", text if place == "quote" else "q")])
    file = file_of(store, "things/c", "T", [part])
    assert problems(file, [HELD, ANGLED]) == [], file


def test_a_held_value_as_a_fields_name_a_maps_key_or_an_identity_keys_name_is_masked(store: SQLiteStore) -> None:
    store.create(Identity("things/c", "things", "T", keys={HELD: ["x"]}))
    store.set_field("things/c", HELD, Property({HELD: "x"}, PERSON, "2026-11-19T00:00:00Z"))
    store.set_field("things/c", "owner", Property({"a": {HELD: [HELD]}}, PERSON, "2026-11-19T00:00:00Z"))
    store.write_compilation(Compilation("things/c", "Overview", "Words.", by="things/w"))
    article = store.article("things/c")
    assert article is not None
    assert problems(markdown(article), [HELD]) == []


def test_two_ids_one_the_others_escape_never_share_a_footnote(store: SQLiteStore) -> None:
    cites = [Citation("record", "a<b", "items", "first"), Citation("record", "a%3Cb", "items", "second")]
    file = file_of(store, "things/c", "T", [Compilation("things/c", "Notes", "One [^a<b].", by="things/w",
                                                    citations=cites[:1]),
                                            Compilation("things/c", "More", "Two [^a%3Cb].", by="things/x",
                                                    citations=cites[1:])])
    labels = re.findall(r"^\[\^([^\]]+)\]:", file, re.MULTILINE)
    assert len(labels) == len(set(labels)) == 2, file


@pytest.mark.parametrize(("heading", "shown_as"), [("\u200b", ""), ("Notes #", "Notes # Words."),
                                                    ("#", "# Words.")])
def test_a_heading_that_lands_empty_or_ends_in_hashes_shows_as_written(store: SQLiteStore, heading: str,
                                                                        shown_as: str) -> None:
    file = file_of(store, "things/c", "T", [Compilation("things/c", heading, "Words.", by="things/w")])
    body = file.split("---\n", 2)[2]
    assert "q" not in body.split()
    if shown_as:
        assert shown(body) == shown_as, body


@pytest.mark.parametrize("text", [f"> > 1. \t word [^<script> x] {IMG}", (">````\n    >users <javascript:x>\n> > x "
                                  "[^[a](javascript:alert(1)) x]\n>````"),
                                  f">```\n{IMG}\n> > [a](javascript:alert(1))\n>```"])
def test_a_tab_among_a_quotes_marks_and_a_fence_a_quote_ends_read_one_way(text: str) -> None:
    out = land(text).text
    assert problems(out) == [] and land(out).text == out, out


def test_a_part_that_takes_five_rounds_to_settle_lands_the_same_twice() -> None:
    text = ("- <https://ok.test> ](  the![i](https://img.test/x.png) rowx \n\n* #   [secret](javascript:x)  \n---"
            " ![  thing4 note\n- >  List<String>id a\n  >  ===\n\n    id reportx.test 3 \\` 4 users ?>\n- notehttps\\://x"
            ".test/users/&lt;id> note")
    once = land(text, top=1).text
    assert land(once, top=1).text == once


@pytest.mark.parametrize("text", ["-   > ````users\n    > ````", "1.  > ~~~\n    > ~~~",
                                  f">1. word\n>   the ```\n>   {IMG}\n>   ```", "Mail xmpp:@x.test now",
                                  "| a xmpp:@x.test |\n| --- |\n| b |"])
def test_a_quote_inside_a_list_a_quote_with_no_space_and_an_empty_xmpp_name_land_safe_and_settled(text: str) -> None:
    out = land(text).text
    assert problems(out) == [] and land(out).text == out, out


@pytest.mark.parametrize("text", ["1. [x] [x]: javascript:alert(1)(\n1. b [x]", "- [x] [x]: javascript:y\n\n[x]",
                                  "> > -  > ~~~\n> >    > > > x\n> >    > ~~~",
                                  f"> > report\n     > ```\n> > x https://x.test/users/<id> `{IMG}`"])
def test_a_task_box_before_a_definition_and_a_fence_in_a_quote_in_a_list_land_safe_and_settled(text: str) -> None:
    out = land(text).text
    assert problems(out) == [] and land(out).text == out, out


def test_a_fence_a_four_space_line_does_not_close_is_closed_before_the_next_part(store: SQLiteStore) -> None:
    parts = [Compilation("things/c", "p", "```yaml\nlist\n    ```", by="things/w", whole=True),
             Compilation("things/c", "Notes", "```\n[a](javascript:x) <style>\n```", by="things/x")]
    file = file_of(store, "things/c", "T", parts)
    assert problems(file) == [], file
