"""Grounding scores each sentence against what it cites and marks the weak ones: it never cuts a word."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cerebro.budget import BudgetSpent
from cerebro.facts import Fact, Quote
from cerebro.fakes import FakeDecider
from cerebro.ground import Editor, Judge, fields_of, split_sentences
from cerebro.models import Decision, DecisionTooLarge, Question

DATA = Path(__file__).parent / "data" / "ground"
SENTENCES = json.loads((DATA / "sentences.json").read_text(encoding="utf-8"))
FACT = Fact("f_1", "Alder renews in March.", "Alder", "", "2026-10-08T15:00:00Z", "local", "local/calls/alder.md",
            [Quote("We renew in March.", "Ines Calder", "2026-10-08T15:00:00Z")])


def found(ids: list[str]) -> dict[str, dict[str, Any]]:
    return {id: fields_of(id, None, FACT) or {} for id in ids if id == FACT.id}


def editor(decider: Any, **kwargs: Any) -> Editor:
    return Editor(Judge(decider, "things/alder", "A workspace for one team.", "2026-10-25"), found, **kwargs)


@pytest.mark.parametrize("case", SENTENCES["cases"], ids=lambda c: c["name"])
def test_a_paragraph_splits_into_sentences_as_its_case_expects(case: dict[str, Any]) -> None:
    found_ = split_sentences(case["text"])
    assert [s.text for s in found_] == case["want"]
    assert all(case["text"][s.start:s.end] == s.text for s in found_)


TEXT = """Alder renews in March [^f_1]. Alder signs for ten years [^f_1].

## Where it stands

| Thing | State |
| --- | --- |
| Renewal | March [^f_1] |
| Term | ten years [^f_1] |

> Ines Calder: "Renovamos en marzo." [^f_1]

<!-- Alder signs for ten years [^f_1]. -->

**For ten years:**

| Term | ten years [^f_1] |

Write `[^unsupported]` as code. Run `make ten years. Then ship` now [^f_1].

## Signed for ten years [^f_1]

```yaml
term: ten years
```
"""
MARKED = TEXT.replace("ten years [^f_1].", "ten years [^f_1].[^unsupported]").replace(
    "now [^f_1].", "now [^f_1].[^unsupported]").replace(
    "ten years [^f_1] |", "ten years [^f_1][^unsupported] |").replace(
    "ten years\n```\n", "ten years\n```\n[^unsupported]\n")


class Weak(FakeDecider):
    """Scores made up each sentence that holds `words`, and backs the rest."""

    def __init__(self, words: str):
        super().__init__()
        self.words = words

    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        self.answers = {s["id"]: 0 for s in state["passage"] if self.words in s["sentence"]}
        return super().decide(state, questions)


def test_each_line_a_reader_sees_under_the_floor_lands_whole_with_one_mark_after_it_and_nothing_else_moves() -> None:
    assert editor(Weak("ten years")).marked(TEXT) == MARKED  # a row, a comment and a fence, never a heading or a label
    copied = MARKED + "[^unsupported]: Not fully backed by the facts it cites.\n"
    assert editor(FakeDecider()).marked(copied) == TEXT  # a mark a later score does not call for comes off


CODE = {  # code as CommonMark reads it, and the text as it lands: no mark inside code, and none after an open fence
    "1.  Step one [^f_1].\n\n    ```yaml\n    term: ten years\n    ```\n2.  Step two [^f_1].\n":
        "1.  Step one [^f_1].[^unsupported]\n\n    ```yaml\n    term: ten years\n    ```\n    [^unsupported]\n"
        "2.  Step two [^f_1].[^unsupported]\n",
    "Run this [^f_1].\n\n    make ten years. Then ship.\n\nDone [^f_1].\n":
        "Run this [^f_1].[^unsupported]\n\n    make ten years. Then ship.\n[^unsupported]\n\nDone [^f_1].[^unsupported]\n",
    "Run this [^f_1].\n\n```yaml\nterm: ten years\n": "Run this [^f_1].[^unsupported]\n\n```yaml\nterm: ten years\n",
    "Run this [^f_1].\r\n\r\n```yaml\r\nterm: ten years\r\n```\r\nDone [^f_1].\r\n":
        "Run this [^f_1].[^unsupported]\r\n\r\n```yaml\r\nterm: ten years\r\n```\r\n[^unsupported]\r\n"
        "Done [^f_1].[^unsupported]\r\n"}


@pytest.mark.parametrize("text", CODE, ids=["a fence in a list item", "an indented block", "an open fence", "CRLF"])
def test_no_mark_lands_inside_code_and_text_marked_again_gains_none(text: str) -> None:
    once = editor(Weak("")).marked(text)
    assert once == CODE[text] and editor(Weak("")).marked(once) == once


def test_the_judge_reads_one_passage_a_request_each_cited_fact_once_and_an_id_read_nowhere_as_not_found() -> None:
    decider = FakeDecider()
    weak = editor(decider)
    weak.marked("Alder renews in March [^f_1]. It renews [^f_1] [^f_9].\n\n## Next\n\nAlder signs [^f_1].")
    (first, questions), (second, _) = decider.asked
    assert [s["sentence"] for s in first["passage"]] == ["Alder renews in March.", "It renews."]
    assert first["passage"][1]["cited"] == ["f_1", "f_9"] and first["not_found"] == ["f_9"]
    assert first["facts"] == {"f_1": {"text": "Alder renews in March.", "about": "Alder", "stated_on": "Oct 8, 2026",
                                      "speaker": "Ines Calder", "quote": "We renew in March.",
                                      "record": "local/calls/alder.md"}}
    assert first["today"] == "2026-10-25" and second["heading"] == "Next" and set(questions) == {"s1", "s2"}
    assert "not found in what was read" in weak.scores[1] and "[^f_9]" in weak.scores[1]


def test_the_first_hand_in_hears_of_each_sentence_under_the_floor_and_one_with_none_hears_nothing() -> None:
    told = editor(FakeDecider({"s1": 2}), floor=0.6).review("Alder signs for ten years [^f_1].")
    assert '"Alder signs for ten years [^f_1].": 0.50, a reframe' in told and "under 0.6" in told
    assert editor(FakeDecider()).review("Alder renews in March [^f_1].") == ""


def test_a_passage_scored_once_or_seeded_with_its_scores_is_not_asked_again_and_each_reader_marks_by_its_floor(
        ) -> None:
    """Unless a sentence of it was left unscored, or cited an id not found: then it is asked about again."""
    read, decider, known = {FACT.id}, FakeDecider(), {}
    again = Editor(Judge(decider), lambda ids: {id: {"text": "Alder renews in March."} for id in ids if id in read},
                   known=known)
    again.judged("Alder renews in March [^f_9].")
    read.add("f_9")  # told f_9 was not found, the writer reads it and hands in the same text
    assert again.judged("Alder renews in March [^f_9].")[0].reason == "the facts say it, in any words, language or style"
    editor(Spent(), known=known).judged(TEXT)
    editor(decider, known=known).judged(TEXT)
    assert len(decider.asked) == 2 + TEXT.count("\n## ") + 1  # what one editor left unscored, the next one scores
    decider, known = FakeDecider({"s2": 2}), {}  # the second sentence 0.5, a reframe
    text = "Alder renews in March [^f_1]. Alder signs for ten years [^f_1]."
    first = editor(decider, known=known)
    landed = first.marked(text)
    assert landed == text.replace("years [^f_1].", "years [^f_1].[^unsupported]")
    assert editor(decider, known=known, floor=0.3).marked(text) == text and len(decider.asked) == 1  # its own floor
    editor(decider, known=known).marked(f"## Renewal\n\n{text}")
    assert len(decider.asked) == 2  # under another heading, it is another passage
    later = editor(decider)  # a later compile, handed the text as it landed, with its scores
    later.seed(landed, first.kept(landed), "")
    assert later.marked(text) == landed and len(decider.asked) == 2
    assert later.review(text).count("0.50, a reframe") == 1


class Spent(FakeDecider):
    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        raise BudgetSpent("compilation spent its cap")


def test_with_no_decision_model_or_past_the_cap_a_sentence_is_unscored_and_lands_unmarked() -> None:
    quiet, spent = editor(None), editor(Spent())
    text = "Alder signs for ten years [^f_1].\n\n## Next\n\nAlder renews in March [^f_1]."
    assert quiet.marked(text) == spent.marked(text) == text
    assert quiet.scores == [] and quiet.tally == {}
    assert [line.split('"')[0] for line in spent.scores] == ["(before the first heading): unscored ", "Next: unscored "]
    assert spent.scores[0].endswith("compilation spent its cap") and spent.tally["unscored"] == 2


class Bounded(FakeDecider):
    """Takes at most one sentence a request, as a decision model's bound does."""

    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        if len(questions) > 1 or "long" in state["passage"][0]["sentence"]:
            raise DecisionTooLarge("over the bound")
        return super().decide(state, questions)


def test_a_request_too_large_is_split_and_a_lone_sentence_too_large_is_unscored() -> None:
    decider = Bounded({"s1": 0})
    weak = editor(decider)
    marked = weak.marked("Alder signs [^f_1]. A long one [^f_1]. Alder renews [^f_1].")
    assert marked == "Alder signs [^f_1].[^unsupported] A long one [^f_1]. Alder renews [^f_1]."
    assert decider.asked[-1][0]["before"] == ["Alder signs.", "A long one."]  # a piece is read after what came before
    assert "unscored" in weak.scores[1] and "more than the judge reads" in weak.scores[1]


def test_the_judge_reads_an_earlier_save_and_an_out_of_date_document_as_such_and_a_records_own_side() -> None:
    old = Fact("f_2", "The limit is 10%.", "", "", "2026-09-01", "local", "local/policy.md", status="superseded",
               revision="sha256:aaa1", superseded_at="2026-11-10T00:00:00Z")
    said = fields_of("f_2", None, old, stale="stale fact: a person marked its document out of date", own=["Robin"])
    assert said["earlier_save"].startswith("an earlier save of its record (revision aaa1), superseded on Nov 10, 2026")
    assert said["out_of_date"] == "a person marked its document out of date" and said["own_side"] == ["Robin"]
