"""The article as a person reads it: front matter, headings, footnotes, and the note an empty article carries."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cerebro.article import Article, Citation, Compilation, Identity, Property, body, cut, markdown, mirror, passages
from cerebro.caller import Caller
from cerebro.facts import Fact

YOU = Caller("you", "person")


def fact(n: int, source: str, at: str) -> Fact:
    return Fact(f"f_{n}", "The review moved.", "Apollo", "note", at, source, f"r_{n}")


def test_an_article_reads_as_front_matter_parts_and_a_footnote_per_citation(tmp_path: Path) -> None:
    cited = Citation("fact", "f_1", "local/kickoff.md", "The review moved to April.")
    article = Article(Identity("projects/apollo", "projects", "Apollo", status="archived"),
                      [Compilation("projects/apollo", "Overview", "It moved [^f_1].", "projects/article",
                               at="2026-11-02T09:00:00Z", citations=[cited, cited])],
                      {"stage": Property("open", YOU, "2026-11-01T09:00:00Z")})
    assert markdown(article) == (
        "---\nid: projects/apollo\ntitle: Apollo\nstatus: archived\nstage: open\ncompiled_at: '2026-11-02T09:00:00Z'"
        "\n---\n\n## Overview\n\nIt moved [^f_1].\n\n"
        '[^f_1]: local/kickoff.md "The review moved to April."\n')
    path = mirror(article, tmp_path)
    assert path == tmp_path / "articles" / "projects" / "apollo.md" and path.read_text() == markdown(article)


def test_front_matter_indents_each_lists_items_under_its_key_and_keeps_a_risky_titles_escapes() -> None:
    risky = "R&D <draft> [x](y)"
    article = Article(Identity("things/a", "things", risky, keys={"call_id": ["1234"]}),
                      properties={"attendees": Property(["ana@ourco.example", risky], YOU, "2026-11-01T09:00:00Z"),
                                  "kind": Property("Beagle (dog)", YOU, "2026-11-01T09:00:00Z")})
    text = markdown(article)
    front = text.split("---\n")[1]
    escaped = '"R\\x26D \\x3Cdraft\\x3E \\x5Bx\\x5D(y)"'
    assert front == (f"id: things/a\ntitle: {escaped}\nidentity:\n  call_id:\n    - '1234'\n"
                     f"attendees:\n  - ana@ourco.example\n  - {escaped}\nkind: Beagle (dog)\n")
    assert yaml.safe_load(front) == {"id": "things/a", "title": risky, "identity": {"call_id": ["1234"]},
                                     "attendees": ["ana@ourco.example", risky], "kind": "Beagle (dog)"}


def test_an_empty_article_carries_the_note_by_its_state_until_a_part_holds_text() -> None:
    article = Article(Identity("projects/apollo", "projects", "Apollo", admitted_by=Caller("crm", "source")))
    assert "_No agent writes this article yet._\n\nIdentity from crm. No facts yet.\n" in markdown(article)
    facts = [fact(1, "notes", "2026-10-06T09:00:00Z"), fact(2, "calls", "2026-10-07T23:30:00Z"),
             fact(3, "notes", "2026-09-01T09:00:00Z")]
    article.compilations = [Compilation("projects/apollo", "Overview", "", "projects/subagents/overview")]
    assert ("_Its agents have run and found nothing to write yet._\n\nIdentity from crm. 3 facts from notes and "
            "calls, the last Oct 7, 2026. Reads serve them now.\n\n## Overview\n") in markdown(article, facts)
    article.compilations[0].text = "It moved."
    assert "_" not in markdown(article, facts)


def test_a_copy_whose_path_would_leave_the_mirror_folder_is_not_written(tmp_path: Path) -> None:
    escaped = Article(Identity("accounts/../../escaped", "accounts", "Escaped"))
    with pytest.raises(OSError, match="resolves outside articles/"):
        mirror(escaped, tmp_path)
    assert not (tmp_path.parent / "escaped.md").exists()


def test_a_footnotes_quote_is_one_line_in_the_copy() -> None:
    cited = Citation("fact", "f_1", "notes/a.md", "Fine.\n\n## Forged heading\nmore")
    article = Article(Identity("notes/a", "notes", "A"), [Compilation("notes/a", "Summary", "Fine [^f_1].", by="w",
                                                                citations=[cited])])
    text = markdown(article)
    assert '[^f_1]: notes/a.md "Fine. ## Forged heading more"' in text
    assert not any(line.startswith("## Forged") for line in text.splitlines())


def test_text_is_cut_at_its_headings_outside_fences_and_text_before_any_heading_is_a_part_of_its_own() -> None:
    assert cut("") == []
    assert cut("No heading at all.\nTwo lines.") == [("", "No heading at all.\nTwo lines.")]
    assert cut("Lead.\n\n# One\n\nA.\n\n~~~\n# inside\n~~~\n\n## Two ##\n\n### Three\n\nB.\n\n## Empty") == [
        ("", "Lead."), ("One", "A.\n\n~~~\n# inside\n~~~"), ("Two", "### Three\n\nB."), ("Empty", "")]


def test_a_part_written_whole_reads_as_written_beside_parts_under_their_headings_and_cuts_into_passages() -> None:
    cited = [Citation("fact", "f_1", "local/a.md"), Citation("fact", "f_2", "local/b.md")]
    article = Article(Identity("calls/kickoff", "calls", "Kickoff"), [
        Compilation("calls/kickoff", "Contents", "- [Calls](./)", "rendered:calls/article"),
        Compilation("calls/kickoff", "calls/article", "They met [^f_1].\n\n## Decisions\n\nA pilot [^f_2].",
                "calls/article", digest="d", citations=cited, whole=True)])
    assert body(article) == "## Contents\n\n- [Calls](./)\n\nThey met [^f_1].\n\n## Decisions\n\nA pilot [^f_2]."
    assert "\n---\n\n## Contents\n\n- [Calls](./)\n\nThey met [^f_1].\n\n## Decisions\n\nA pilot [^f_2].\n\n" \
        in markdown(article)
    shown = passages(article)
    assert [(p.heading, p.text, p.by, p.digest, [c.id for c in p.citations]) for p in shown] == [
        ("Contents", "- [Calls](./)", "rendered:calls/article", "", []),
        ("", "They met [^f_1].", "calls/article", "d", ["f_1"]),
        ("Decisions", "A pilot [^f_2].", "calls/article", "d", ["f_2"])]


def test_a_heading_is_read_as_commonmark_reads_one_and_none_is_read_in_a_fence_or_a_comment() -> None:
    from cerebro.article import atx

    assert [atx(line) for line in ("## A", "##\tA", "   ## A", "    ## A", "##A", "## A ##", "## A#", "#", "####### A")] \
        == [(2, "A"), (2, "A"), (2, "A"), None, None, (2, "A"), (2, "A#"), (1, ""), None]
    text = ("Lead.\n<!--\n## Hidden\n-->\n## Seen\n\n````\n```\n## In a fence\n````\n\n   ~~~\n## Also in\n~~~ not a "
            "close\n~~~\n\n## Last")
    assert [h for h, _ in cut(text)] == ["", "Seen", "Last"]
    assert cut("## A\n\n    ```\n\n## B\n\nText.") == [("A", "    ```"), ("B", "Text.")]  # indented: no fence
