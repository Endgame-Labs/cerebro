"""A fact keeps the sentences of its record that state it: Factoid shows its extractor a record one sentence a
passage, even where the record's lines form one paragraph, so a fact cites, and keeps as its quote, the sentence it
states and not the paragraph around it. No test makes a model call."""

from __future__ import annotations

from pathlib import Path

from test_facts import on_factoid

from cerebro.connector import Record
from cerebro.facts import Fact
from cerebro.ground import quote_for

FIRST = "Ben Ito sent Mara Okafor the plan for the Harbor export, priced at $18,000 for eight weeks."
SECOND = "Mara Okafor replied that the plan looks right and that legal needs a review first."


def test_a_fact_of_a_paragraph_of_two_sentences_keeps_the_one_it_states(tmp_path: Path) -> None:
    handler, _ = on_factoid(tmp_path)
    try:
        record = Record(id="notes/plan.md", source="notes", revision="1", at="2026-11-10T15:00:00Z",
                        body=f"Topic: Harbor\n\n{FIRST}\n{SECOND}\n")
        facts = handler.extract(record).facts  # the stand-in states each passage it is shown, citing it
    finally:
        handler.close()
    assert [[q.text for q in f.quotes] for f in facts if f.text in (FIRST, SECOND)] == [[FIRST], [SECOND]]
    assert [quote_for(f) for f in facts if f.text in (FIRST, SECOND)] == [FIRST, SECOND]
    # the two sentences stand next to each other, so a fact that kept both shows them as the record has them
    both = Fact("f", "x", "Harbor", "fact", "", "notes", "notes/plan.md",
                [q for f in facts if f.text in (FIRST, SECOND) for q in f.quotes])
    assert quote_for(both) == f"{FIRST} {SECOND}"
