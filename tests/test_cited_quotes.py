"""The words a citation of a fact shows: the quotes the fact kept when it was extracted, the sentences its
extraction cited as stating it, each under the record it is in, and never a sentence code guessed, nor words the
source does not hold."""

from __future__ import annotations

from pathlib import Path

from test_facts import on_factoid

from cerebro.article import Article, Compilation, Identity, markdown
from cerebro.connector import Record
from cerebro.evidence import _item
from cerebro.facts import Fact, Quote
from cerebro.ground import QUOTE_BOUND, fields_of, quote_for

HARBOR = ("Mara Okafor said the Harbor beta moves from 2 December to 9 December, because the data export is not "
          "finished.\nBen Ito owns the data export and expects to finish it by 4 December.\nThe beta starts with "
          "40 teams.")


def fact(id: str, text: str, *quotes: str) -> Fact:
    return Fact(id, text, "Harbor", "fact", "2026-11-16T00:00:00Z", "local", "local/2026-11-16-harbor-sync.md",
                [Quote(q) for q in quotes])


def copy_of(facts: list[Fact]) -> str:
    citations = [c for f in facts for c in _item(f).citations]
    text = " ".join(f"It holds [^{f.id}]." for f in facts)
    compilation = Compilation("projects/harbor", "Where it stands", text, "projects/harbor", citations=citations)
    return markdown(Article(Identity("projects/harbor", "projects", "Harbor"), [compilation]))


def test_the_quickstart_note_quoted_sentence_by_sentence_gives_each_fact_the_sentences_it_kept() -> None:
    first, second, third = HARBOR.splitlines()
    facts = [fact("fact_a", "Mara Okafor said the Harbor beta moves from 2 December to 9 December, because the data "
                            "export is not finished.", first),
             fact("fact_b", "Ben Ito owns the data export for the Harbor beta.", second),
             fact("fact_c", "Ben Ito expects to finish the data export by 4 December.", second),
             fact("fact_d", "The Harbor beta starts with 40 teams.", first, third)]
    notes = [line for line in copy_of(facts).splitlines() if line.startswith("[^")]
    assert notes == [f'[^fact_a]: local/2026-11-16-harbor-sync.md "{first}"',
                     f'[^fact_b]: local/2026-11-16-harbor-sync.md "{second}"',
                     f'[^fact_c]: local/2026-11-16-harbor-sync.md "{second}"',
                     f'[^fact_d]: local/2026-11-16-harbor-sync.md "{first} … {third}"']  # both it kept, never one
    item = _item(facts[2])
    assert item.text == (f'- [^fact_c] Ben Ito expects to finish the data export by 4 December. (quote: "{second}", '
                         "local/2026-11-16-harbor-sync.md)")


def test_a_fact_another_record_states_again_shows_each_quote_under_its_own_record(tmp_path: Path) -> None:
    handler, models = on_factoid(tmp_path)
    models.pairs = "duplicate"  # the second record states the first one's fact again, so Factoid adds its quote
    handler.extract(Record(id="local/a.md", source="local", revision="1",
                           body="Orion signs the plan.\n\nVega leads the review."))
    handler.extract(Record(id="local/b.md", source="local", revision="1", body="Vega leads the review, as agreed."))
    told = next(f for f in handler.facts() if f.record == "local/a.md" and "Vega" in f.text)
    handler.close()
    own, again = '"Vega leads the review."', '"Vega leads the review, as agreed."'
    assert [line for line in copy_of([told]).splitlines() if line.startswith("[^")] == [
        f"[^{told.id}]: local/a.md {own} · local/b.md {again}"]  # passages 2 and 1, of two records: its own first
    assert _item(told).text == f"- [^{told.id}] {told.text} (quote: local/a.md {own} · local/b.md {again})"
    assert fields_of(told.id, None, told)["more_sources"] == [{"quote": again[1:-1], "record": "local/b.md"}]


def test_a_fact_that_kept_a_whole_passage_shows_it_and_never_a_sentence_guessed_from_shared_words() -> None:
    plan = ("Ben Ito sent Mara Okafor the plan for the Harbor export, priced at $18,000 for eight weeks.\n"
            "Mara Okafor replied that the plan looks right and that legal needs a review first.")
    told = fact("f", "Mara Okafor said the plan for the Harbor export looks right.", plan)
    assert quote_for(told) == " ".join(plan.split())


def test_quotes_that_stand_next_to_each_other_join_as_the_record_has_them_and_a_gap_is_marked() -> None:
    first, second, third = HARBOR.splitlines()
    def kept(*numbered: tuple[str, int]) -> Fact:
        return Fact("f", "Ben Ito owns the export.", "Harbor", "fact", "", "local", "local/harbor.md",
                    [Quote(text, passage=n) for text, n in numbered])
    assert quote_for(kept((first, 1), (second, 2))) == f"{first} {second}"
    assert quote_for(kept((second, 2), (first, 1))) == f"{first} {second}"  # in the record's order
    assert quote_for(kept((first, 1), (third, 3))) == f"{first} … {third}"  # the sentence between is left out
    assert quote_for(kept((first, 1), (second, 2), (third, 3))) == f"{first} {second} {third}"
    assert quote_for(kept((first, 0), (second, 0))) == f"{first} … {second}"  # not known to stand together


def test_a_quote_kept_twice_shows_once() -> None:
    told = fact("f", "The beta starts with 40 teams.", "The beta starts with 40 teams.",
                "The beta starts with  40 teams.")
    assert quote_for(told) == "The beta starts with 40 teams."


def test_quotes_longer_than_the_bound_are_cut_at_a_word() -> None:
    long = " ".join(f"Sentence number {n} talks about lunch." for n in range(40))
    shown = quote_for(fact("f", "Rain fell on the harbour.", long))
    assert shown.endswith("…") and len(shown) <= QUOTE_BOUND + 1 and long.startswith(shown[:-1])
    def own_and(n: int) -> Fact:  # its own words, n of them, then another record's
        return Fact("f", "Rain fell.", "", "fact", "", "local", "local/a.md",
                    [Quote(" ".join(["lunch"] * n)), Quote("Another record says this.", record="local/b.md")])
    assert quote_for(own_and(48)).endswith('" · local/b.md "Another…')  # the bound cuts words, never a name
    assert quote_for(own_and(50)) == " ".join(["lunch"] * 50)  # none of its words fit: the record is left out
    assert quote_for(fact("f", "Nothing was said.")) == ""


def test_a_hostile_quote_lands_on_one_line_and_forges_no_heading_or_footnote() -> None:
    hostile = "The beta starts with 40 teams.\n## Forged\n[^fact_x]: forged \"quote\" ] here."
    named = Fact("fact_c", "It rained.", "", "fact", "", "local", "local/a.md",
                 [Quote("It rained."), Quote("Rain.", record="local/[x](http://e.vil).md")])
    copy = copy_of([fact("fact_a", "The beta starts with 40 teams.", hostile),
                    fact("fact_b", "Rain fell on the harbour.", "Rain\n# Heading\n[^fact_x]: fell ]"), named])
    lines = copy.splitlines()
    assert [line.split("]:")[0] for line in lines if line.startswith("[^")] == ["[^fact_a", "[^fact_b", "[^fact_c"]
    assert '" · `local/[x](http://e.vil).md` "Rain."' in copy  # another record's name is no link
    assert not [line for line in lines if line.startswith("#") and line != "## Where it stands"]
