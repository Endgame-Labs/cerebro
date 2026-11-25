"""An item of a data block its writer marks presumed: the value of the one key the mark names is the writer's guess,
which no fact need state. Every guard before the judge holds for it as for any item. The judge is asked the main
question of the rest, and one question of the guess beside it, and the item lands, its mark kept, only when the rest
holds and the judge firmly finds the guess a reading of the part its subject plays. A guess is never evidence for a
later claim. An item with no mark is asked exactly what the recorded asks hold."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from cerebro import ground as grounding
from cerebro.agent import Agent
from cerebro.article import Citation
from cerebro.facts import Fact, Quote
from cerebro.fakes import FakeDecider
from cerebro.ground import JUDGE_MIN_CONFIDENCE, ground, presumed
from cerebro.models import Answer, Decision, DecisionTooLarge, Question

ASKED_BEFORE = Path(__file__).parent / "data" / "ground" / "unmarked_asks.json"
AGENT = Agent(id="things/parts", path=Path("agents/things/parts.md"), role="writer", target="things/halden",
              prompt="Name who holds each part of the work.\n")
FACTS = {
    "f_1": Fact("f_1", "Tomas Wren leads the assay team at the Halden lab.", "Halden lab", "note",
                "2026-11-03T10:00:00Z", "files", "local/halden.md",
                [Quote("Tomas leads the assay team.", "Rhea Osei", "2026-11-03T10:00:00Z")]),
    "f_2": Fact("f_2", "Rhea Osei said the assay protocol needs a sign-off before the trial starts.", "Halden lab",
                "note", "2026-11-04T10:00:00Z", "files", "local/halden-2.md",
                [Quote("We need a sign-off on the protocol first.", "Rhea Osei", "2026-11-04T10:00:00Z")]),
}
CITABLE = {id: Citation("fact", id, f.record, f.text) for id, f in FACTS.items()}
ITEM = '- owner: Tomas Wren\n  status: signs off the protocol\n  fact: "[^f_1] [^f_2]"\n'
MARKED = '- owner: Tomas Wren\n  status: signs off the protocol\n  presumed: true\n  fact: "[^f_1] [^f_2]"\n'
UNMARKED = {  # blocks whose items carry no mark: each is asked as the recorded asks hold
    "plain": ITEM,
    "false": ITEM.replace('  fact:', '  presumed: false\n  fact:'),
    "nested": ITEM.replace('  fact:', '  detail: {presumed: true}\n  fact:'),
    "map": 'Tomas Wren: {status: signs off the protocol, fact: "[^f_1] [^f_2]"}\n',
    "two": ITEM + '- owner: Rhea Osei\n  status: asks for a sign-off\n  fact: "[^f_2]"\n',
}


def block(*items: str) -> str:
    return "Tomas Wren leads the assay team [^f_1].\n\n```yaml\n" + "".join(items) + "```\n"


def normal(state: Any, questions: dict[str, Question]) -> Any:
    """A request as JSON, with grounding's own lines named in place of their text, which another change may word
    again: what is compared is everything else."""
    text = json.dumps({"state": state, "questions": {k: q.wire() for k, q in questions.items()}}, sort_keys=True,
                      default=str)
    lines = [(grounding.CONTRACT, "<contract>"),
             *((grounding.DATA_ITEM.format(f"s{i}"), f"<data_item s{i}>") for i in range(1, 9))]
    for old, new in [*lines, (grounding.DATA_WHOLE, "<data_whole>")]:
        text = text.replace(json.dumps(old)[1:-1], new)
    return json.loads(text)


def landed(text: str) -> list[Any]:
    return yaml.safe_load(text.split("```yaml\n")[1].split("```")[0])


def run(items: list[str], decider: Any) -> grounding.Grounded:
    return ground(AGENT, {"Part": block(*items)}, CITABLE, decider=decider, facts=FACTS, today="2026-11-10")


def item_questions(decider: FakeDecider) -> dict[str, Question]:
    for state, questions in decider.asked:
        if any(s.get("sentence", "").startswith("owner:") for s in state["sentences"]):
            return questions
    raise AssertionError("no request asked about an item")


# -- the mark

def test_an_item_is_marked_presumed_by_a_top_level_key_reading_presumed_and_an_unreadable_mark_marks_it_all() -> None:
    assert presumed({"presumed": True}) and presumed({" Presumed ": True}) and presumed({"PRESUMED": True})
    for value in ({"presumed": "true"}, {"presumed": 1}, {"owner": "x", "presumed": "status"}):
        assert presumed(value) is True, value
    for value in ({"presumed": False}, {"presumed": None}, {"presumed": []}, {"detail": {"presumed": True}},
                  {"presumes": True}, "presumed: true", [{"presumed": True}], None):
        assert not presumed(value), value


# -- an item with no mark is asked as recorded

def test_an_item_with_no_mark_is_asked_exactly_what_the_recorded_asks_hold() -> None:
    before = json.loads(ASKED_BEFORE.read_text(encoding="utf-8"))
    for name, item in UNMARKED.items():
        decider = FakeDecider()
        ground(AGENT, {"Part": block(item)}, CITABLE, decider=decider, facts=FACTS, today="2026-11-10")
        assert [normal(s, q) for s, q in decider.asked] == before[name], name


def test_an_item_with_no_mark_and_a_value_no_fact_states_still_goes() -> None:
    grounded = run([ITEM], FakeDecider({"s1": "unsupported", "p1": "held"}))
    assert "```yaml" not in grounded.parts.get("Part", "")


# -- a marked item, at the judge

def test_a_marked_item_whose_rest_holds_and_whose_guess_is_a_reading_lands_with_its_mark_in_the_file() -> None:
    grounded = run([MARKED, ITEM.replace("Tomas Wren", "Rhea Osei")],
                   FakeDecider({"s1": "grounded", "n1": "open", "s2": "unsupported"}))
    assert landed(grounded.parts.get("Part", "")) == [
        {"owner": "Tomas Wren", "status": "signs off the protocol", "presumed": True, "fact": "[^f_1] [^f_2]"}]
    assert "  presumed: true\n" in grounded.parts.get("Part", "")


def test_a_marked_item_goes_when_its_facts_do_not_hold_the_rest_or_say_otherwise() -> None:
    for answers, reason in (({"s1": "unsupported", "n1": "open"}, "does not hold the rest of it"),
                            ({"s1": "grounded", "n1": "denied"}, "says otherwise of status"),
                            ({"s1": "contradicted", "n1": "open"}, "says otherwise of the rest")):
        grounded = run([MARKED], FakeDecider(answers))
        assert "```yaml" not in grounded.parts.get("Part", ""), answers
        [line] = [d for d in grounded.dropped if d.startswith('Part: dropped "owner')]
        assert reason in line and "presumed" in line, (answers, line)


class Unsure(FakeDecider):
    """Answers as told, at a confidence just under the bar for one question."""

    def __init__(self, answers: dict[str, Any], unsure: str):
        super().__init__(answers)
        self.unsure = unsure

    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        made = super().decide(state, questions)
        if (a := made.answers.get(self.unsure)) is not None:
            made.answers[self.unsure] = Answer(choice=a.choice, confidence=JUDGE_MIN_CONFIDENCE - 0.01,
                                               probabilities=a.probabilities)
        return made


# -- a marked item, before the judge: every guard holds

def test_every_guard_before_the_judge_holds_for_a_marked_item() -> None:
    cases = {
        "cites nothing": MARKED.replace('  fact: "[^f_1] [^f_2]"\n', ""),
        "did not read": MARKED.replace("[^f_2]", "[^f_9]"),
        "names Ilse Brandt": MARKED.replace("Tomas Wren", "Ilse Brandt"),
        "date": MARKED.replace("signs off the protocol", "signs off the protocol by 14 March 2027"),
    }
    for why, item in cases.items():
        decider = FakeDecider({"s1": "grounded", "n1": "open"})
        grounded = run([item], decider)
        assert "```yaml" not in grounded.parts.get("Part", ""), why
        assert not any(s.get("sentence", "").startswith(("owner:", "Ilse")) for st, _ in decider.asked
                       for s in st["sentences"]), why


# -- an item that names its guess: `presumed: <key>`

NAMED = ('- owner: Tomas Wren\n  status: signs off the protocol\n  presumed: status\n'
         '  fact: "[^f_1] [^f_2]"\n')


def item_sentence(decider: FakeDecider) -> str:
    for state, _ in decider.asked:
        for s in state["sentences"]:
            if s.get("sentence", "").startswith("owner:"):
                return s["sentence"]
    raise AssertionError("no request asked about an item")


def test_a_named_value_is_set_aside_and_the_rest_is_asked_the_main_question_alone() -> None:
    decider = FakeDecider({"s1": "grounded"})
    grounded = run([NAMED], decider)
    assert item_sentence(decider) == "owner: Tomas Wren."
    assert sorted(item_questions(decider)) == ["n1", "s1", "s1c1"]
    assert landed(grounded.parts.get("Part", "")) == [
        {"owner": "Tomas Wren", "status": "signs off the protocol", "presumed": "status", "fact": "[^f_1] [^f_2]"}]


def test_a_named_item_goes_when_the_rest_is_not_held_and_says_what_it_marked() -> None:
    grounded = run([NAMED], FakeDecider({"s1": "unsupported"}))
    assert "```yaml" not in grounded.parts.get("Part", "")
    [line] = [d for d in grounded.dropped if d.startswith('Part: dropped "owner')]
    assert "it marks status presumed, and its cited evidence does not hold the rest of it" in line


def test_a_mark_that_names_no_value_of_the_item_goes_with_why_and_is_never_asked() -> None:
    cases = {
        "a key it lacks": (NAMED.replace("presumed: status", "presumed: role"), "marks role presumed, which is no key"),
        "a word": (NAMED.replace("presumed: status", 'presumed: "true"'), "marks true presumed, which is no key"),
        "its citations": (NAMED.replace("presumed: status", "presumed: fact"), "marks fact presumed, which is no key"),
        "the mark itself": (NAMED.replace("presumed: status", "presumed: presumed"), "which is no key"),
        "a number": (NAMED.replace("presumed: status", "presumed: 3"), "holds neither true nor a key of it"),
        "a map": (NAMED.replace("presumed: status", "presumed: {status: yes}"), "holds neither true nor a key"),
        "every value": ('- status: signs off the protocol\n  presumed: status\n  fact: "[^f_1]"\n', "every value"),
    }
    for name, (item, reason) in cases.items():
        decider = FakeDecider({"s1": "grounded", "n1": "open"})
        grounded = run([item], decider)
        assert "```yaml" not in grounded.parts.get("Part", ""), name
        assert reason in dropped_reason(grounded), (name, grounded.dropped)
        assert not any(s.get("sentence", "").startswith("owner:") for st, _ in decider.asked
                       for s in st["sentences"]), name


def test_a_mark_that_names_nothing_leaves_the_item_unmarked() -> None:
    for value in ("~", "[]", '""', "false"):
        decider = FakeDecider({"s1": "grounded"})
        grounded = run([NAMED.replace("presumed: status", f"presumed: {value}")], decider)
        assert sorted(item_questions(decider)) == ["s1", "s1c1"], value
        assert "```yaml" in grounded.parts.get("Part", ""), value


def test_the_name_and_figure_guards_still_read_a_named_value_they_can_read() -> None:
    # The guards read the whole item, so a capitalised full name or a written date no cited fact holds goes before the
    # judge, named presumed or not. They cannot read every name or figure: see the next test for the rest.
    for item in (NAMED.replace("owner: Tomas Wren", "owner: Ilse Brandt").replace("presumed: status",
                                                                                    "presumed: owner"),
                 NAMED.replace("signs off the protocol", "by 14 March 2027")):
        decider = FakeDecider({"s1": "grounded", "n1": "open"})
        grounded = run([item], decider)
        assert "```yaml" not in grounded.parts.get("Part", ""), item
        assert not any(s.get("sentence", "").startswith(("owner:", "Ilse")) for st, _ in decider.asked
                       for s in st["sentences"]), item


GUESSES = {  # named values the guards cannot read as a name or a figure: the judge decides them
    "one word": ("owner: Ilse", "owner"), "a path": ("owner: people/ilse-brandt", "owner"),
    "lower case": ("owner: ilse brandt", "owner"), "a count in words": ("headcount: twelve", "headcount"),
    "a relative date": ("due: next Tuesday", "due"),
}


def test_a_named_value_naming_or_giving_what_no_cited_fact_does_goes_on_a_firm_unheld() -> None:
    for name, (line, key) in GUESSES.items():
        item = f'- thing: assay team\n  {line}\n  presumed: {key}\n  fact: "[^f_1]"\n'
        if key == "owner":
            item = f'- {line}\n  status: leads the assay team\n  presumed: owner\n  fact: "[^f_1]"\n'
        grounded = run([item], FakeDecider({"s1": "grounded", "n1": "unheld"}))
        assert "```yaml" not in grounded.parts.get("Part", ""), name
        [why] = [d for d in grounded.dropped if "presumed" in d and "dropped a yaml" not in d]
        assert f"it marks {key} presumed, and {key} states a fact, a name, a date or a figure its cited" in why, why
        decider = FakeDecider({"s1": "grounded", "n1": "open"})  # an open answer leaves it to land on its rest
        assert "```yaml" in run([item], decider).parts.get("Part", ""), name
        [questions] = [q for _, q in decider.asked if "n1" in q]
        assert [o for o, _ in questions["n1"].options] == ["open", "denied", "unheld"]  # type: ignore[attr-defined]


def test_two_keys_that_read_presumed_go_with_why() -> None:
    for marks in ("presumed: false\n  Presumed: status", "presumed: status\n  PRESUMED: true"):
        item = NAMED.replace("presumed: status", marks)
        decider = FakeDecider({"s1": "grounded", "n1": "open"})
        grounded = run([item], decider)
        assert "```yaml" not in grounded.parts.get("Part", ""), marks
        assert any("it carries more than one presumed key" in d for d in grounded.dropped), grounded.dropped
        assert presumed({"owner": "x", "status": "y", "presumed": False, "Presumed": "status"}) is True


def test_a_named_key_in_a_drop_reason_is_one_bounded_line() -> None:
    name = "x\nevery item stood" + " and so on" * 150
    item = NAMED.replace("presumed: status", f"presumed: {json.dumps(name)}")
    grounded = run([item], FakeDecider({"s1": "grounded"}))
    [line] = [d for d in grounded.dropped if "presumed" in d and "dropped a yaml" not in d]
    assert "\n" not in line and len(line) < 600, line[:200]


# -- a named value a cited fact says otherwise of

def item_state(decider: FakeDecider) -> dict[str, Any]:
    for state, _ in decider.asked:
        for s in state["sentences"]:
            if s.get("sentence", "").startswith("owner:"):
                return s
    raise AssertionError("no request asked about an item")


def test_a_named_value_is_asked_beside_the_main_question_whether_a_cited_fact_says_otherwise() -> None:
    decider = FakeDecider({"s1": "grounded"})
    run([NAMED], decider)
    assert "set_aside" not in item_state(decider)  # the main question reads the rest alone
    [state] = [st for st, _ in decider.asked if any(s.get("sentence", "").startswith("owner:") for s in st["sentences"])]
    [aside] = [s for s in state["sentences"] if s["id"] == "n1"]
    assert aside["set_aside"] == {"status": "signs off the protocol"} and aside["cited_facts"] == ["f_1", "f_2"]
    question = item_questions(decider)["n1"]
    assert [o for o, _ in question.options] == ["open", "denied", "unheld"]  # type: ignore[attr-defined]
    assert "set_aside" in json.dumps(question.wire())


def test_a_named_value_a_cited_fact_denies_goes_with_the_key_named() -> None:
    grounded = run([NAMED], FakeDecider({"s1": "grounded", "n1": "denied"}))
    assert "```yaml" not in grounded.parts.get("Part", "")
    [line] = [d for d in grounded.dropped if d.startswith('Part: dropped "owner')]
    assert "it marks status presumed, and a cited fact says otherwise of status" in line


def test_no_other_item_is_asked_of_a_set_aside_value() -> None:
    for item in (ITEM, ITEM.replace("  fact:", "  presumed: false\n  fact:")):
        decider = FakeDecider({"s1": "grounded"})
        run([item], decider)
        assert "n1" not in item_questions(decider) and "set_aside" not in item_state(decider), item


# -- a mark buys one guess: the value of the one key it names, shown as a guess, and nothing else

def dropped_reason(grounded: grounding.Grounded) -> str:
    [line] = [d for d in grounded.dropped if "presumed" in d and "dropped a yaml" not in d]
    return line


def never_asked(decider: FakeDecider) -> bool:
    return not any(s.get("sentence", "").startswith("owner:") or "set_aside" in s
                   for st, _ in decider.asked for s in st["sentences"])


def test_a_mark_names_a_key_exactly_as_written_and_never_chooses_between_keys_that_fold_alike() -> None:
    twins = '- owner: Tomas Wren\n  status: signs off the protocol\n  Status: approves the budget\n' \
            '  presumed: status\n  fact: "[^f_1]"\n'
    decider = FakeDecider({"s1": "grounded", "n1": "open"})
    grounded = run([twins], decider)
    assert "```yaml" not in grounded.parts.get("Part", "")
    assert "status and Status" in dropped_reason(grounded) and "differ only in case or spacing" in \
        dropped_reason(grounded)
    assert never_asked(decider)
    folded = NAMED.replace("presumed: status", "presumed: STATUS")
    decider = FakeDecider({"s1": "grounded", "n1": "open"})
    grounded = run([folded], decider)
    assert "it marks STATUS presumed, which is no key of it as written" in dropped_reason(grounded)
    assert never_asked(decider)
    assert presumed({"owner": "x", "role": "a", "Role": "b", "presumed": "role"}) is True


def test_a_mark_names_one_key_and_a_list_of_more_than_one_goes_with_why() -> None:
    many = ('- owner: Tomas Wren\n  status: signs off the protocol\n  next: a review\n  presumed: [status, next]\n'
            '  fact: "[^f_1]"\n')
    decider = FakeDecider({"s1": "grounded", "n1": "open"})
    grounded = run([many], decider)
    assert "```yaml" not in grounded.parts.get("Part", "")
    assert "it marks more than one key presumed, and a mark names one" in dropped_reason(grounded)
    assert never_asked(decider)
    one = NAMED.replace("presumed: status", "presumed: [status]")
    decider = FakeDecider({"s1": "grounded", "n1": "open"})
    assert "```yaml" in run([one], decider).parts.get("Part", "")
    assert item_sentence(decider) == "owner: Tomas Wren."


def test_a_guess_lands_only_when_the_judge_answers_that_it_is_a_reading() -> None:
    class Silent(FakeDecider):
        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            made = super().decide(state, questions)
            made.answers.pop("n1", None)
            return made

    class Small(FakeDecider):
        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            if any("set_aside" in s for s in state["sentences"]):
                raise DecisionTooLarge("over")
            return super().decide(state, questions)

    for decider in (FakeDecider({"s1": "grounded", "n1": "open"}), Unsure({"s1": "grounded", "n1": "open"}, "n1")):
        assert "```yaml" in run([NAMED], decider).parts.get("Part", ""), type(decider)
    for decider in (Silent({"s1": "grounded"}), Unsure({"s1": "grounded", "n1": "unheld"}, "n1"),
                    Unsure({"s1": "grounded", "n1": "denied"}, "n1"), Small({"s1": "grounded", "n1": "open"}),
                    FakeDecider({"s1": "grounded", "n1": "unheld"}), FakeDecider({"s1": "grounded", "n1": "denied"})):
        grounded = run([NAMED], decider)
        assert "```yaml" not in grounded.parts.get("Part", ""), type(decider)
        assert "it marks status presumed" in dropped_reason(grounded), grounded.dropped


def test_the_question_on_a_guess_asks_whether_it_is_a_reading_of_a_part_someone_plays() -> None:
    decider = FakeDecider({"s1": "grounded", "n1": "open"})
    run([NAMED], decider)
    told = json.dumps(item_questions(decider)["n1"].wire())
    for words in ("the part someone plays in the item's own subject", "says that something happened",
                  "gives a cause or a condition", "a date or a figure"):
        assert words in told, words


def test_a_guess_is_shown_to_the_judge_with_what_code_found_of_its_names_and_figures() -> None:
    item = '- owner: Tomas Wren\n  backup: Ilse Brandt\n  presumed: backup\n  fact: "[^f_1]"\n'
    decider = FakeDecider({"s1": "grounded", "n1": "open"})
    grounded = run([item], decider)
    assert "```yaml" not in grounded.parts.get("Part", "")  # the names guard reads the guess too
    item = '- owner: Tomas Wren\n  backup: Rhea Osei\n  presumed: backup\n  fact: "[^f_1] [^f_2]"\n'
    decider = FakeDecider({"s1": "grounded", "n1": "open"})
    run([item], decider)
    [aside] = [s for st, _ in decider.asked for s in st["sentences"] if s["id"] == "n1"]
    assert aside["set_aside"] == {"backup": "Rhea Osei"} and "Osei" in aside["checks"]["in_facts"]


def test_a_guess_holding_a_figure_or_date_its_cited_facts_do_not_give_goes_before_the_judge() -> None:
    for guess in ("leads the 12 person team", "takes over in Q4", "runs 4 sites"):
        item = f'- owner: Tomas Wren\n  status: {guess}\n  presumed: status\n  fact: "[^f_1]"\n'
        decider = FakeDecider({"s1": "grounded", "n1": "open"})
        grounded = run([item], decider)
        assert "```yaml" not in grounded.parts.get("Part", ""), guess
        assert "it marks status presumed, and status gives a figure or a date its cited evidence does not" in \
            dropped_reason(grounded), guess
        assert never_asked(decider), guess


def test_with_no_judge_a_marked_item_is_read_by_its_words_as_the_same_item_unmarked() -> None:
    for item in (NAMED.replace("signs off the protocol", "juggles kittens"),
                 MARKED.replace("signs off the protocol", "juggles kittens"), NAMED, MARKED):
        bare = item.replace("  presumed: status\n", "").replace("  presumed: true\n", "")
        marked = ground(AGENT, {"Part": block(item)}, CITABLE, decider=None, facts=FACTS)
        plain = ground(AGENT, {"Part": block(bare)}, CITABLE, decider=None, facts=FACTS)
        assert ("```yaml" in marked.parts.get("Part", "")) is ("```yaml" in plain.parts.get("Part", "")), item


def test_presumed_true_on_an_item_with_one_value_that_could_be_a_guess_names_that_value() -> None:
    decider = FakeDecider({"s1": "grounded", "n1": "open"})
    grounded = run([MARKED], decider)
    assert item_sentence(decider) == "owner: Tomas Wren."
    [aside] = [s for st, _ in decider.asked for s in st["sentences"] if s["id"] == "n1"]
    assert aside["set_aside"] == {"status": "signs off the protocol"}
    assert landed(grounded.parts.get("Part", ""))[0]["presumed"] is True
    assert presumed({"owner": "x", "status": "y", "since": date(2026, 3, 3), "presumed": True}) == ["status"]
    assert presumed({"person": "x", "role": "y", "since": "2026-03-03", "presumed": True}) == ["role"]
    assert "p1" not in {name for _, q in decider.asked for name in q}


def test_presumed_true_on_any_other_item_buys_nothing_and_is_asked_as_the_item_unmarked() -> None:
    two = '- owner: Tomas Wren\n  status: signs off the protocol\n  next: a review\n  presumed: true\n' \
          '  fact: "[^f_1] [^f_2]"\n'
    bare = two.replace("  presumed: true\n", "")
    for answers in ({"s1": "grounded"}, {"s1": "unsupported", "p1": "held", "n1": "open"}):
        marked, plain = FakeDecider(answers), FakeDecider(answers)
        got, want = run([two], marked), run([bare], plain)
        assert [normal(s, q) for s, q in marked.asked] == [normal(s, q) for s, q in plain.asked], answers
        assert ("```yaml" in got.parts.get("Part", "")) is ("```yaml" in want.parts.get("Part", "")), answers
    landed_two = run([two], FakeDecider({"s1": "grounded"})).parts.get("Part", "")
    assert landed(landed_two)[0]["presumed"] is True  # the mark is kept, and readers show it
    assert presumed({"owner": "x", "status": "y", "next": "z", "presumed": True}) is True


def test_two_presumed_keys_spelled_the_same_go_as_two_marks() -> None:
    for first, last in (("false", "status"), ("status", "false"), ("true", "true")):
        item = NAMED.replace("presumed: status", f"presumed: {first}\n  presumed: {last}")
        decider = FakeDecider({"s1": "grounded", "n1": "open"})
        grounded = run([item], decider)
        assert "```yaml" not in grounded.parts.get("Part", ""), (first, last)
        assert "it carries more than one presumed key, so its mark cannot be read" in dropped_reason(grounded)
        assert never_asked(decider)
        [root] = [grounding.load_data(f"- owner: x\n  presumed: {first}\n  presumed: {last}\n")]
        assert presumed(root[0]) is True


# -- a guess is never evidence

ONWARD = "Omar Haddad decides for Alder [^things/alder]."
ONWARD_CITED = {"things/alder": Citation("article", "things/alder", "things/alder")}
ONWARD_TEXT = ("### Article articles/things/alder.md\n\n#### Stakeholders\n\nJane Doe runs the steering meetings "
               "[^f_1].\n\n```yaml\n- person: people/jane-doe\n  role: runs the steering meetings\n  fact: \"[^f_1]\"\n"
               "{item}```")


def onward(item: str) -> FakeDecider:
    decider = FakeDecider()
    ground(AGENT, {"Sponsor": ONWARD}, ONWARD_CITED, decider=decider, texts={"things/alder": ONWARD_TEXT.format(
        item=item)}, today="2026-11-10")
    return decider


def onward_shown(decider: FakeDecider) -> tuple[str, dict[str, Any]]:
    state = next(st for st, _ in decider.asked for s in st["sentences"] if s.get("sentence", "").startswith("Omar"))
    item = next(s for s in state["sentences"] if s.get("sentence", "").startswith("Omar"))
    return json.dumps(state["facts"]), item.get("checks", {})


def test_a_guessed_value_in_a_cited_article_is_never_shown_as_evidence() -> None:
    for item in ('- person: people/omar-haddad\n  role: decides\n  presumed: person\n  fact: "[^f_1]"\n',
                 '- person: Jane Doe\n  role: Omar Haddad decides\n  presumed: role\n  fact: "[^f_1]"\n',
                 '- person: Jane Doe\n  role: Omar Haddad decides\n  presumed: true\n  fact: "[^f_1]"\n',
                 '- person: Jane Doe\n  role: Omar Haddad decides\n  next: x\n  presumed: true\n  fact: "[^f_1]"\n',
                 ('- person: Jane Doe\n  role: Omar Haddad decides\n  presumed: false\n  presumed: role\n'
                  '  fact: "[^f_1]"\n')):
        facts, checks = onward_shown(onward(item))
        assert "Haddad" not in facts and "Omar" not in facts, item
        assert "Haddad" not in checks.get("in_facts", []), (item, checks)
        assert "runs the steering meetings" in facts, item  # what was checked stays


def test_a_cited_article_with_no_guess_is_shown_as_it_was() -> None:
    plain = '- person: people/omar-haddad\n  role: decides\n  fact: "[^f_1]"\n'
    facts, checks = onward_shown(onward(plain))
    assert "omar-haddad" in facts and "Haddad" in checks.get("in_facts", [])
    text = ONWARD_TEXT.format(item=plain)
    assert grounding._guesses_left_out(text) == text


def test_a_guess_that_states_a_cause_no_cited_fact_states_goes() -> None:
    item = ('- owner: Tomas Wren\n  status: signs off the protocol because the trial was moved up\n'
            '  presumed: status\n  fact: "[^f_1] [^f_2]"\n')

    class Unbacked(FakeDecider):
        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            self.answers |= {k: "link:none" for k, q in questions.items() if k.startswith("n1c")}
            return super().decide(state, questions)

    decider = Unbacked({"s1": "grounded", "n1": "open"})
    grounded = run([item], decider)
    [aside] = [s for st, _ in decider.asked for s in st["sentences"] if s["id"] == "n1"]
    assert [c["kind"] for c in aside["claims"]] == ["link"]
    assert "```yaml" not in grounded.parts.get("Part", "")
    assert "it marks status presumed, and status states a fact" in dropped_reason(grounded)


def test_a_cited_text_whose_fences_hold_no_mark_is_shown_as_it_was() -> None:
    for text in ("Ledger note:\n```yaml\nthe sample is presumed lost; Tomas Wren signs off the protocol\n```",
                 "Ledger:\n```yaml\n# presumed comment\n{owner: Tomas Wren}\n```",
                 "Ledger:\n```yaml\n- owner: Tomas Wren\n  fact: Tomas Wren signs off, presumed\n```",
                 "Ledger:\n```yaml\n- owner: Tomas Wren\n\nprose after an open fence, presumed"):
        assert grounding._guesses_left_out(text) == text, text


def test_a_guess_in_a_fence_with_any_info_is_left_out_of_a_cited_text() -> None:
    for opening in ("```{.yaml}", "```yaml title", "~~~yml", "```"):
        text = f"{opening}\n- person: Jane Doe\n  role: Omar Haddad decides\n  presumed: role\n  fact: \"[^f_1]\"\n" \
               f"{opening[:3]}\n"
        assert "Omar" not in grounding._guesses_left_out(text), opening


def test_with_no_judge_a_marked_map_entry_is_read_as_the_same_entry_unmarked() -> None:
    for mark in ("presumed: true", "presumed: task"):
        bare = 'sponsorship committee rota: {status: signs off the protocol, task: asks for a sign-off, fact: "[^f_2]"}\n'
        item = bare.replace("fact:", f"{mark}, fact:")
        marked = ground(AGENT, {"Part": block(item)}, CITABLE, decider=None, facts=FACTS)
        plain = ground(AGENT, {"Part": block(bare)}, CITABLE, decider=None, facts=FACTS)
        assert ("```yaml" in marked.parts.get("Part", "")) is ("```yaml" in plain.parts.get("Part", "")), mark


def test_presumed_true_on_a_map_entry_with_one_value_buys_nothing() -> None:
    item = 'Tomas Wren: {status: approves the trial budget, presumed: true, fact: "[^f_1]"}\n'
    decider = FakeDecider({"s1": "grounded", "n1": "open"})
    run([item], decider)
    assert "n1" not in {name for _, q in decider.asked for name in q}
    assert presumed({"status": "x", "presumed": True}, keyed=True) is True


def test_a_guess_that_holds_a_list_or_a_map_goes_before_the_judge() -> None:
    for value in ("[signs off the protocol, approves the budget]", "{a: signs off, b: approves}"):
        item = f'- owner: Tomas Wren\n  status: {value}\n  presumed: status\n  fact: "[^f_1]"\n'
        decider = FakeDecider({"s1": "grounded", "n1": "open"})
        grounded = run([item], decider)
        assert "```yaml" not in grounded.parts.get("Part", ""), value
        assert "it marks status presumed, and status holds more than one value" in dropped_reason(grounded)
        assert never_asked(decider)
