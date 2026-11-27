"""Evals score a stand-in workspace's articles against labelled questions, and run the grounding corpus through the same
entry point, with every decision call kept under the evaluation layer and held to its cap."""

from __future__ import annotations

import copy
import functools
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from cerebro.article import Compilation, Identity, Property
from cerebro.budget import LOG, Caps, Ledger
from cerebro.caller import Caller
from cerebro.evals import LAYER, EvalError, lines, load, run, wilson
from cerebro.fakes import FakeDecider, MemoryStore
from cerebro.lookup import StoreLookup
from cerebro.models import Decision, DecisionError, ModelSpec, Question, TokenCount
from cerebro.workspace import Workspace

DATA = Path(__file__).parent / "data" / "evals"
GROUND = Path(__file__).parent / "data" / "ground" / "corpus.json"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
RUNE = ModelSpec("fireworks", "rune", job="decision")
YOU = Caller("you", "person")
SPENT = TokenCount(1000, 10)


@functools.cache
def _workspace_rows() -> list[dict[str, Any]]:
    """workspace.yaml's articles, parsed once: each stand-in gets a copy of its own."""
    return yaml.safe_load((DATA / "workspace.yaml").read_text(encoding="utf-8"))["articles"]


def stand_in(changes: dict[str, dict[str, Any]] | None = None) -> Workspace:
    """The workspace of workspace.yaml in memory, with `changes` laid over an article's properties and parts by id."""
    store = MemoryStore()
    for row in copy.deepcopy(_workspace_rows()):
        row = row | (changes or {}).get(row["id"], {})
        store.create(Identity(row["id"], row["id"].split("/")[0], row["title"], row.get("keys", {})))
        for name, value in row.get("properties", {}).items():
            store.set_field(row["id"], name, Property(value, YOU, "2026-11-01T09:00:00Z"))
        for heading, text in row["parts"].items():
            store.write_compilation(Compilation(row["id"], heading, text, by=f"{row['id'].split('/')[0]}/article"))
    return Workspace(StoreLookup(store, clock=lambda: NOW), store, [])


def ticks(start: int = 0) -> Any:
    count = iter(range(start, start + 10_000))
    return lambda: float(next(count))


def ledger(caps: Caps | None = None) -> Ledger:
    return Ledger(MemoryStore(), caps or Caps())


class Priced(FakeDecider):
    """Answers as `FakeDecider` does, and says each call spent the tokens it was told."""

    def __init__(self, answers: dict[str, Any] | None = None, usage: TokenCount = SPENT):
        super().__init__(answers)
        self.usage = usage

    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        return replace(super().decide(state, questions), usage=self.usage)


def test_a_labelled_suite_scores_a_run_on_a_stand_in_workspace_with_its_judge_calls_kept_under_their_own_layer() -> None:
    suite = load(DATA / "questions.yaml")
    decider, kept = Priced(), ledger()
    report = run(stand_in(), suite, decider=decider, model=RUNE, ledger=kept, clock=lambda: NOW, timer=ticks())
    assert report.kind == "questions" and report.layer == LAYER == "evaluation"
    assert report.score == 1.0 and [s.score for s in report.scored] == [1.0] * 6
    assert report.scored[0].read == ["projects/brightpine"] and report.scored[3].read == ["people/mara-quill"]
    assert report.scored[2].read == ["projects/kesmar"]  # a query ranks by what the articles discuss
    assert [len(s.checks) for s in report.scored] == [4, 3, 3, 2, 3, 4]
    assert report.judge == "accounts/fireworks/routers/rune" and report.calls == len(decider.asked) == 4
    state = decider.asked[0][0]
    assert state["criterion"].startswith("The answer names Mara Quill") and state["articles"][0]["id"] == \
        "projects/brightpine" and {"heading": "Overview", "text": next(
            p["text"] for p in state["articles"][0]["parts"] if p["heading"] == "Overview")} in state["articles"][0][
        "parts"] and "Mara Quill leads the move" in json.dumps(state["articles"][0]["parts"])
    rows = kept.store.rows(LOG)
    assert [r.data["layer"] for r in rows] == [LAYER] * 4 and rows[0].data["run"] == "eval brightpine-owner"
    assert report.usd == pytest.approx(sum(r.data["usd"] for r in rows)) and report.usd > 0
    assert kept.spent(NOW) == pytest.approx(report.usd)  # it counts toward the workspace's cap on all spend
    assert kept.spent(NOW, "compilation") == 0
    assert report.tokens.input_tokens == 4000 and report.tokens.output_tokens == 40 and report.seconds > 0 and report.scored[0].seconds == 1.0
    assert report.groups["owners"].scored == 2 and report.groups["all"].score == 1.0


def test_a_wrong_article_lowers_the_score_and_says_what_it_read_instead() -> None:
    suite = load(DATA / "questions.yaml")
    wrong = {"projects/brightpine": {"properties": {"owner": "Tobin Reyes", "stage": "open"},
                                     "parts": {"Overview": "Brightpine Labs is moving its archive. Tobin Reyes "
                                                              "leads the move, and the cutover is set for May 2027."}}}
    right = run(stand_in(), suite, clock=lambda: NOW)
    report = run(stand_in(wrong), suite, clock=lambda: NOW)
    assert report.score is not None and right.score is not None and report.score < right.score
    owner = report.scored[0]
    assert owner.score == pytest.approx(1 / 3)  # the article, but not its owner in words or in its field
    assert [(m.name, m.why) for m in owner.misses] == [
        ("holds Mara Quill", "not in projects/brightpine"),
        ("field owner", "projects/brightpine holds owner 'Tobin Reyes'")]
    assert report.scored[1].score == pytest.approx(1 / 2)
    assert report.scored[2].score == 1.0  # a question about another article does not move
    assert any(line == "miss brightpine-owner field owner: projects/brightpine holds owner 'Tobin Reyes'"
               for line in lines(report))


def test_a_route_to_the_wrong_article_misses_every_check_and_a_word_it_must_lack_is_shown_where_it_stands() -> None:
    suite = load(DATA / "questions.yaml")
    kesmar = {"projects/kesmar": {"parts": {"Overview": "Kesmar Systems closed its search rebuild in October."}}}
    report = run(stand_in(kesmar), suite, clock=lambda: NOW)
    state = report.scored[2]
    assert [(m.name, m.why) for m in state.misses] == [
        ("lacks closed", "projects/kesmar holds it: 'aused Overview Kesmar Systems closed its search rebuild in October'")]
    gone = run(stand_in(), replace(suite, probes=[replace(suite.probes[0], target="Nobody Here")]), clock=lambda: NOW)
    assert gone.score == 0.0 and gone.scored[0].read == []
    assert {m.why for m in gone.scored[0].misses} == {"find 'Nobody Here' resolved to no article"}
    other = run(stand_in(), replace(suite, probes=[replace(suite.probes[0], route="read",
                                                           target=["projects/lumenhart"])]), clock=lambda: NOW)
    assert other.scored[0].misses[0].why == "read projects/lumenhart" and other.score == 0.0


def test_an_article_as_a_run_writes_it_with_inline_citations_and_a_list_property_is_read_as_its_words() -> None:
    suite = load(DATA / "questions.yaml")
    cited = {"projects/brightpine": {
        "properties": {"owner": ["Ines Varga", "Mara Quill"], "stage": "open"},
        "parts": {"Overview": "Brightpine Labs is moving its archive to the new store. Mara Quill [^f_8c1e] leads "
                                 "the move, and the cutover is set for March 15, [^f_8c1f] 2027 [^f_8c20]."}}}
    decider = FakeDecider()
    report = run(stand_in(cited), suite, decider=decider, model=RUNE, ledger=ledger(), clock=lambda: NOW)
    assert report.scored[0].score == 1.0 and report.scored[1].score == 1.0
    assert json.loads(json.dumps(decider.asked[0][0]))["articles"][0]["properties"]["owner"] == ["Ines Varga",
                                                                                                "Mara Quill"]
    shown = run(stand_in(cited), replace(suite, probes=[replace(suite.probes[0], expect={"lacks": ["cutover is"]})]))
    assert shown.scored[0].misses[0].why == ("projects/brightpine holds it: 'Quill leads the move, and the cutover is set "
                                             "for March 15, 2027.'")


def test_the_judge_decides_where_no_rule_is_enough_and_a_no_lowers_the_score() -> None:
    suite = load(DATA / "questions.yaml")
    report = run(stand_in(), suite, decider=FakeDecider({"right": 0.2}), model=RUNE, ledger=ledger(),
                 clock=lambda: NOW)
    owner = report.scored[0]
    assert owner.score == pytest.approx(3 / 4)
    assert owner.misses[0].name == "judge" and owner.misses[0].why.startswith("the judge gave yes 0.20: The answer "
                                                                              "names Mara Quill")
    assert report.scored[3].score == 1.0  # a question with no judge is scored by its rules alone


def test_with_no_decision_model_a_judge_check_counts_for_nothing_and_says_why() -> None:
    report = run(stand_in(), load(DATA / "questions.yaml"), clock=lambda: NOW)
    owner = report.scored[0]
    assert report.score == 1.0 and owner.score == 1.0 and report.calls == 0 and report.usd == 0
    assert owner.checks[-1].name == "judge" and owner.checks[-1].passed is None
    assert owner.checks[-1].why == "no decision model: run with one to judge"
    assert report.judge == "none: rules alone"


def test_the_evaluation_layer_stops_at_its_cap_and_the_rest_of_the_run_says_why() -> None:
    suite = load(DATA / "questions.yaml")
    kept = ledger()
    kept.store.append(LOG, {"day": "2026-11-17", "layer": LAYER, "usd": 0.5})
    decider = Priced()
    report = run(stand_in(), suite, decider=decider, model=RUNE, ledger=kept, cap_usd=0.5, clock=lambda: NOW)
    assert decider.asked == [] and report.calls == 0 and len(kept.store.rows(LOG)) == 1
    assert report.stopped == ("evaluation spent $0.50 of its $0.5 cap on 2026-11-17 (UTC): its model calls wait "
                              "for 00:00 UTC, or raise budget.evaluation_daily_usd")  # the ledger's own line
    assert report.scored[0].checks[-1].passed is None and report.scored[0].checks[-1].why == report.stopped
    assert report.scored[0].checks[-1].name == "judge" and all((s.calls, s.usd) == (0, 0.0) for s in report.scored)
    assert report.score == 1.0  # the rules still score
    other_day = run(stand_in(), suite, decider=decider, model=RUNE, ledger=kept, cap_usd=0.5,
                    clock=lambda: datetime(2026, 11, 18, 1, tzinfo=UTC))
    assert other_day.calls == 4 and other_day.stopped == ""


def test_the_workspaces_cap_on_all_spend_and_an_unpriced_model_stop_the_judge_too() -> None:
    suite = load(DATA / "questions.yaml")
    full = ledger(Caps(total=1.0))
    full.store.append(LOG, {"day": "2026-11-17", "layer": "compilation", "usd": 1.0})
    report = run(stand_in(), suite, decider=Priced(), model=RUNE, ledger=full, cap_usd=None, clock=lambda: NOW)
    assert report.stopped.startswith("model calls spent $1.00 of the $1 daily cap on 2026-11-17") and report.calls == 0
    odd = ModelSpec("fireworks", "no-such-model", job="decision")
    unpriced = run(stand_in(), suite, decider=Priced(), model=odd, ledger=ledger(), clock=lambda: NOW)
    assert unpriced.stopped.startswith("fireworks model no-such-model has no known price") and unpriced.calls == 0
    free = run(stand_in(), suite, decider=Priced(), model=odd, ledger=(kept := ledger()), cap_usd=None,
               clock=lambda: NOW)
    assert free.calls == 4 and all(r.data["unpriced"] for r in kept.store.rows(LOG))


class Failing(FakeDecider):
    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        raise DecisionError("the decision model gave no answer: ask again")


def test_a_failed_decision_call_leaves_the_judge_unjudged_and_the_rules_scored() -> None:
    report = run(stand_in(), load(DATA / "questions.yaml"), decider=Failing(), model=RUNE, ledger=ledger(),
                 clock=lambda: NOW)
    assert report.score == 1.0 and report.scored[0].checks[-1].passed is None
    assert report.scored[0].checks[-1].why == "the decision call failed: the decision model gave no answer: ask again"


def corpus_ids() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in json.loads(GROUND.read_text(encoding="utf-8"))["cases"]}


class Labelled(FakeDecider):
    """A judge that agrees with each case's label, found by the facts its sentence cites."""

    def __init__(self, by_facts: dict[frozenset[str], bool]):
        import threading

        super().__init__()
        self.by_facts = by_facts
        self.lock = threading.Lock()  # workers ask side by side, and `answers` is the one each call reads

    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        supported = self.by_facts[frozenset(state["facts"])]
        with self.lock:
            self.answers = {"s1": 4} if supported else {"s1": 0}  # the facts say it, or made up
            return replace(super().decide(state, questions), usage=TokenCount(500, 5))


SLICE = ["r01", "r02", "r05", "r09", "y01"]  # each cites facts of its own, so a judge can tell them apart


def test_the_grounding_cases_run_through_the_same_entry_point_and_score_as_labelled() -> None:
    suite = load(GROUND)
    assert suite.kind == "ground" and len(suite.cases) == 232
    cases = corpus_ids()
    cited = {id: frozenset(re.findall(r"\[\^([^\]\s]+)\]", cases[id]["sentence"])) for id in SLICE}
    assert len(set(cited.values())) == len(SLICE)
    assert {cases[id]["label"] for id in SLICE} == {"supported-in-other-words", "truly-unsupported"}
    judge = Labelled({cited[id]: cases[id]["label"] == "supported-in-other-words" for id in SLICE})
    sliced = replace(suite, cases=[c for c in suite.cases if c.id in SLICE])
    kept = ledger()
    report = run(None, sliced, decider=judge, model=RUNE, ledger=kept, clock=lambda: NOW, workers=3)
    assert report.score == 1.0 and [s.id for s in report.scored] == [c.id for c in sliced.cases]
    assert {s.outcome for s in report.scored if s.label == "supported-in-other-words"} == {"1.00"}
    assert report.calls == len(kept.store.rows(LOG)) and report.tokens.input_tokens == 500 * report.calls
    assert {r.data["layer"] for r in kept.store.rows(LOG)} == {LAYER}
    all_ = report.groups["all"]
    assert all_.supported + all_.unsupported == 5 and all_.held == all_.supported and \
        all_.marked == all_.unsupported


def test_a_grounding_case_against_its_label_lowers_the_score_and_says_how_it_scored() -> None:
    suite = load(GROUND)
    sliced = replace(suite, cases=[c for c in suite.cases if c.id in SLICE])
    report = run(None, sliced, decider=FakeDecider({"s1": 0}), model=RUNE, ledger=ledger(), clock=lambda: NOW)
    supported = [s for s in report.scored if s.label == "supported-in-other-words"]
    assert supported and all(s.score == 0.0 and s.outcome == "0.00" for s in supported)
    miss = supported[0].misses[0]
    assert miss.name == "scores as labelled"
    assert miss.why == "labelled supported-in-other-words, and it scored 0.00: made up: no cited fact holds what it says"
    assert report.score is not None and report.score < 1.0
    assert report.groups["all"].held == 0


@pytest.fixture(scope="module")
def corpus_run() -> Any:
    """The whole grounding corpus run once on a stand-in judge that finds every case backed: the same for every test
    that reads it, so the module runs it once and hands each test a deep copy of its own."""
    return run(None, load(GROUND), decider=FakeDecider(), model=RUNE, ledger=ledger(), clock=lambda: NOW)


@pytest.fixture
def corpus_report(corpus_run: Any) -> Any:
    return copy.deepcopy(corpus_run)


def test_what_a_test_does_to_its_corpus_report_leaves_the_run_the_next_test_is_handed(
        corpus_report: Any, corpus_run: Any) -> None:
    corpus_report.scored.clear()
    corpus_report.groups["all"].supported = -1
    assert len(corpus_run.scored) == 232 and corpus_run.groups["all"].supported == 102


def test_the_whole_corpus_runs_case_by_case_and_with_no_decision_model_each_case_is_unscored(
        corpus_report: Any) -> None:
    report = corpus_report
    assert len(report.scored) == 232 and not report.unscored and report.calls == 232
    groups = report.groups
    assert set(groups) == {"all", "rows", "sense", "claims", "held_out", "structure", "steering", "lead_value", "joins",
                           "saves", "expired", "roles", "voice"}
    assert groups["all"].supported == 102 and groups["all"].unsupported == 130
    assert groups["all"].scored == sum(g.scored for name, g in groups.items() if name != "all")
    printed = lines(report)
    assert printed[0].startswith("corpus: ground, 232 cases, judge ") and ", score " in printed[0]
    assert printed[1].startswith("all: supported at or above 0.6 102 of 102 ")
    quiet = run(None, load(GROUND), group="steering")
    assert len(quiet.unscored) == 4 and quiet.score is None and quiet.calls == 0


def test_a_grounding_case_past_the_cap_is_unscored_and_names_the_cap() -> None:
    kept = ledger()
    kept.store.append(LOG, {"day": "2026-11-17", "layer": LAYER, "usd": 3.0})
    report = run(None, load(GROUND), decider=FakeDecider(), model=RUNE, ledger=kept, clock=lambda: NOW,
                 group="steering")
    assert report.score is None and len(report.unscored) == 4
    assert report.unscored[0].checks[0].why.startswith("evaluation spent $3.00 of its $2 cap")


@pytest.mark.parametrize(("question", "line"), [
    ({"ask": "x", "find": "y", "expect": {"holds": ["z"]}}, "question 1: has no id"),
    ({"id": "a", "ask": "x", "find": "y", "expect": {"holds": ["z"]}, "asks": 1}, "asks is not a question key"),
    ({"id": "a", "ask": "x", "expect": {"holds": ["z"]}}, "gives 0 routes: give one of find, query or read"),
    ({"id": "a", "ask": "x", "find": "y", "read": "z", "expect": {"holds": ["z"]}}, "gives 2 routes"),
    ({"id": "a", "ask": "x", "find": "y"}, "expects nothing"),
    ({"id": "a", "ask": "x", "find": "y", "expect": {"hold": ["z"]}}, "expect.hold is not a check"),
    ({"id": "a", "ask": "x", "find": "y", "expect": {"holds": []}}, "expect.holds is not a list of words"),
    ({"id": "a", "ask": "x", "find": "y", "expect": {"fields": "z"}}, "expect.fields is not a map"),
    ({"id": "a", "ask": "x", "find": "y", "expect": {"judge": ""}}, "expect.judge is empty"),
    ({"id": "a", "ask": "x", "query": "y", "top": 0, "expect": {"holds": "z"}}, "top is 0"),
    ({"id": "a", "ask": "x", "find": "y", "matching": "w", "expect": {"holds": "z"}}, "matching goes with a query"),
    ({"id": "a", "ask": "", "find": "y", "expect": {"holds": "z"}}, "has no ask"),
])
def test_a_bad_question_is_refused_with_one_line_that_names_the_file_and_the_question(
        tmp_path: Path, question: dict[str, Any], line: str) -> None:
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump({"questions": [question]}), encoding="utf-8")
    with pytest.raises(EvalError, match=re.escape(line)) as raised:
        load(path)
    assert str(raised.value).startswith(f"eval suite {path}: question 1")


def test_a_suite_file_that_is_neither_kind_or_does_not_parse_is_refused(tmp_path: Path) -> None:
    (bad := tmp_path / "bad.yaml").write_text("questions: [", encoding="utf-8")
    with pytest.raises(EvalError, match="does not parse"):
        load(bad)
    (other := tmp_path / "other.json").write_text('{"cases": []}', encoding="utf-8")
    with pytest.raises(EvalError, match="holds neither questions nor cases and facts"):
        load(other)
    with pytest.raises(EvalError, match="cannot be read"):
        load(tmp_path / "missing.yaml")


def test_a_run_refuses_to_start_without_what_it_needs() -> None:
    suite = load(DATA / "questions.yaml")
    with pytest.raises(EvalError, match="asks questions of the wiki, and no workspace was given"):
        run(None, suite)
    with pytest.raises(EvalError, match="pass model and ledger with the decider"):
        run(stand_in(), suite, decider=FakeDecider(), model=RUNE)
    with pytest.raises(EvalError, match="no amount above zero"):
        run(stand_in(), suite, decider=FakeDecider(), model=RUNE, ledger=ledger(), cap_usd=0)
    with pytest.raises(EvalError, match="no case is in group 'nope'"):
        run(stand_in(), suite, group="nope")


def test_wilson_gives_the_share_and_its_interval() -> None:
    assert wilson(0, 0) == (0.0, 0.0, 0.0)
    p, low, high = wilson(8, 10)
    assert p == 0.8 and low == pytest.approx(0.4902, abs=1e-4) and high == pytest.approx(0.9433, abs=1e-4)
    assert wilson(10, 10)[2] == 1.0 and wilson(0, 10)[1] == 0.0


def test_the_eval_set_is_synthetic() -> None:
    text = (DATA / "workspace.yaml").read_text(encoding="utf-8") + (DATA / "questions.yaml").read_text(encoding="utf-8")
    domains = set(re.findall(r"[\w-]+\.[a-z]{2,}\b", text)) - {"workspace.yaml"}
    assert domains and all(d.endswith(".example") for d in domains)
    assert "synthetic" in yaml.safe_load((DATA / "workspace.yaml").read_text(encoding="utf-8"))["note"].lower()
    assert (DATA / "questions.yaml").read_text(encoding="utf-8").startswith("# Synthetic.")  # a suite takes no note


def test_a_right_answer_needs_both_facts_joined_and_one_that_holds_only_one_does_not_score_as_right() -> None:
    suite = load(DATA / "questions.yaml")
    joined = run(stand_in(), suite, group="joined", decider=FakeDecider(), model=RUNE, ledger=ledger(),
                 clock=lambda: NOW)
    assert joined.score == 1.0 and [c.name for c in joined.scored[0].checks] == [
        "article", "holds Mara Quill", "holds Ines Varga", "judge"]
    one = {"projects/brightpine": {"parts": {"Overview": "Brightpine Labs is moving its archive to the new store. "
                                                            "Mara Quill leads the move."}}}
    half = run(stand_in(one), suite, group="joined", decider=FakeDecider({"right": 0.1}), model=RUNE,
               ledger=ledger(), clock=lambda: NOW)
    assert half.score == pytest.approx(2 / 4)
    assert [m.name for m in half.scored[0].misses] == ["holds Ines Varga", "judge"]


def test_each_question_says_what_it_spent_and_a_check_that_passed_says_nothing() -> None:
    kept = ledger()
    report = run(stand_in(), load(DATA / "questions.yaml"), decider=Priced(), model=RUNE, ledger=kept,
                 clock=lambda: NOW)
    priced = report.usd / 4
    assert [(s.calls, s.usd) for s in report.scored] == [(1, pytest.approx(priced)), (1, pytest.approx(priced)),
                                                         (0, 0.0), (0, 0.0), (1, pytest.approx(priced)),
                                                         (1, pytest.approx(priced))]
    assert all(c.why == "" for s in report.scored for c in s.checks)
    assert [c.name for c in report.scored[2].checks] == ["article", "lacks closed", "field stage"]
    assert kept.store.rows(LOG)[0].data == {"at": "2026-11-17T12:00:00Z", "day": "2026-11-17", "layer": LAYER,
                                            "usd": pytest.approx(priced), "provider": "fireworks", "model": "rune",
                                            "run": "eval brightpine-owner"}


def test_the_judge_reads_the_question_and_the_workspace_and_a_yes_of_one_half_passes() -> None:
    decider = FakeDecider({"right": 0.5})
    report = run(stand_in(), load(DATA / "questions.yaml"), decider=decider, model=RUNE, ledger=ledger(),
                 clock=lambda: NOW)
    assert report.score == 1.0
    state = decider.asked[0][0]
    assert set(state) == {"question", "criterion", "context", "articles"}
    assert state["question"] == "Who leads the Brightpine archive move?" and state["context"].startswith("Synthetic")
    assert set(decider.asked[0][1]) == {"right"} and decider.asked[0][1]["right"].instructions.startswith("Do the articles")


class Silent(FakeDecider):
    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        return Decision({}, "fake", TokenCount(100, 1))


def test_an_answer_with_no_yes_is_unjudged_and_still_counts_its_spend() -> None:
    kept = ledger()
    report = run(stand_in(), load(DATA / "questions.yaml"), decider=Silent(), model=RUNE, ledger=kept,
                 clock=lambda: NOW)
    judge = report.scored[0].checks[-1]
    assert (judge.passed, judge.why) == (None, "the decision model gave no yes or no")
    assert report.calls == 4 and len(kept.store.rows(LOG)) == 4 and report.scored[0].calls == 1


def test_a_judge_on_a_route_that_read_nothing_is_a_miss_and_asks_no_model() -> None:
    suite = load(DATA / "questions.yaml")
    decider = FakeDecider()
    gone = run(stand_in(), replace(suite, probes=[replace(suite.probes[0], target="Nobody Here")]), decider=decider,
               model=RUNE, ledger=ledger(), clock=lambda: NOW)
    assert [(c.name, c.passed) for c in gone.scored[0].checks] == [
        ("article", False), ("holds Mara Quill", False), ("field owner", False), ("judge", False)]
    assert decider.asked == [] and gone.calls == 0 and gone.scored[0].usd == 0.0


def test_a_query_takes_its_filters_and_reads_its_top_articles_and_says_when_it_read_none() -> None:
    suite = load(DATA / "questions.yaml")
    open_only = replace(suite.probes[2], filters={"stage": "open"}, matching="", top=2,
                        expect={"holds": ["Brightpine"], "lacks": ["Kesmar"]})
    report = run(stand_in(), replace(suite, probes=[open_only]), clock=lambda: NOW)
    assert report.scored[0].read == ["projects/brightpine"] and report.score == 1.0
    both = run(stand_in(), replace(suite, probes=[replace(open_only, filters={}, top=2)]), clock=lambda: NOW)
    assert len(both.scored[0].read) == 2
    none = run(stand_in(), replace(suite, probes=[replace(open_only, matching="pricing", filters={})]),
               clock=lambda: NOW)
    assert none.scored[0].misses[0].why == ("query of projects read no article: matching 'pricing' matched none of "
                                            "the 3 articles of projects: try other words, or fewer filters")
    missing = run(stand_in(), replace(suite, probes=[replace(suite.probes[4], target=["projects/nope"])]),
                  clock=lambda: NOW)
    assert missing.scored[0].misses[0].why == "read projects/nope gave no article: missing projects/nope"


class Raising(FakeDecider):
    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        super().decide(state, questions)
        raise DecisionError("the decision model gave no answer: ask again")


def test_a_grounding_case_reads_its_facts_its_day_and_its_context_and_says_what_each_case_spent() -> None:
    suite = load(GROUND)
    fact = suite.facts["f_9b20"]
    assert (fact.record, fact.quotes[0].speaker, fact.quotes[0].at) == (
        "local:file:local/calls/2026-09-24-copperwake-freight-checkin.md", "Li Wei", "2026-09-24T16:00:00Z")
    decider = Priced()
    sliced = replace(suite, cases=[c for c in suite.cases if c.id == "r01"])
    report = run(None, sliced, decider=decider, model=RUNE, ledger=ledger(), clock=lambda: NOW)
    state = decider.asked[0][0]
    assert state["today"] == "2026-10-25" and state["operating_context"] == "Synthetic records, for an eval of grounding."
    assert state["facts"]["f_9b21"]["text"] and state["article"] == "eval/case"
    case = report.scored[0]
    assert case.calls == len(decider.asked) == report.calls and case.usd == pytest.approx(report.usd) and report.usd > 0
    assert (case.id, case.group, case.label) == ("r01", "rows", "truly-unsupported")
    failed = run(None, sliced, decider=Raising(), model=RUNE, ledger=ledger(), clock=lambda: NOW)
    assert failed.unscored[0].checks[0].why == "the decision model gave no answer: ask again"
    assert (failed.unscored[0].id, failed.unscored[0].group, failed.unscored[0].label) == ("r01", "rows",
                                                                                         "truly-unsupported")
    assert failed.calls == failed.unscored[0].calls == 1


@pytest.mark.parametrize(("question", "line"), [
    ({"id": "a", "ask": "x", "find": "y", "expect": {"holds": [3]}}, "expect.holds is not a list of words"),
    ({"id": "a", "ask": "x", "read": [""], "expect": {"holds": "z"}}, "read is not an id or a list of ids"),
    ({"id": "a", "ask": "x", "find": "", "expect": {"holds": "z"}}, "find is empty: write a name"),
    ({"id": "a", "ask": "x", "query": " ", "expect": {"holds": "z"}}, "query is empty: write a source"),
    ({"id": "a", "ask": "x", "query": "y", "top": 21, "expect": {"holds": "z"}}, "top is 21"),
    ({"id": "a", "ask": "x", "find": "y", "filters": {"a": 1}, "expect": {"holds": "z"}}, "filters go with a query"),
    ({"id": "a", "ask": "x", "find": "y", "expect": {"article": ""}}, "expect.article is empty: write an article id"),
    ({"id": "a", "ask": "x", "find": "y", "expect": {"judge": " "}}, "write what a right answer establishes"),
    ({"id": "a", "ask": "x", "find": "y", "expect": "z"}, "expects nothing"),
])
def test_a_question_written_wrong_in_its_values_is_refused_too(tmp_path: Path, question: dict[str, Any],
                                                                line: str) -> None:
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump({"questions": [question]}), encoding="utf-8")
    with pytest.raises(EvalError, match=re.escape(line)):
        load(path)


def test_a_question_reads_its_words_as_a_list_keeps_its_query_settings_and_refuses_a_repeated_id(
        tmp_path: Path) -> None:
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump({"questions": [
        {"id": "a", "ask": "x", "query": "projects", "filters": {"stage": "open"}, "matching": "archive", "top": 20,
         "expect": {"holds": "Mara", "lacks": "Tobin"}},
        {"id": "b", "ask": "x", "read": "projects/kesmar", "expect": {"holds": "Kesmar"}}]}), encoding="utf-8")
    first, second = load(path).probes
    assert (first.expect["holds"], first.expect["lacks"]) == (["Mara"], ["Tobin"])
    assert (first.filters, first.matching, first.top) == ({"stage": "open"}, "archive", 20)
    assert second.target == ["projects/kesmar"]
    path.write_text(yaml.safe_dump({"questions": [{"id": "a", "ask": "x", "find": "y", "expect": {"holds": "z"}}] * 2}),
                    encoding="utf-8")
    with pytest.raises(EvalError, match=re.escape("question 2 (a): repeats an id")):
        load(path)


def test_a_grounding_suite_keeps_the_corpus_day_context_and_kinds() -> None:
    raw = json.loads(GROUND.read_text(encoding="utf-8"))
    suite = load(GROUND)
    assert (suite.today, suite.context) == (raw["today"], "Synthetic records, for an eval of grounding.")
    assert (suite.cases[0].kind, suite.cases[0].group) == ("paraphrase", "rows")


def test_a_run_says_how_long_it_took_and_names_no_judge_and_no_stop_without_a_decider() -> None:
    report = run(stand_in(), load(DATA / "questions.yaml"), model=RUNE, clock=lambda: NOW, timer=ticks(100))
    assert report.judge == "none: rules alone" and report.stopped == ""
    assert report.seconds == 13.0  # the timer read once before the cases and once after: each case reads it twice
    assert all((s.calls, s.usd) == (0, 0.0) for s in report.scored)


def test_a_question_with_only_a_judge_and_no_model_is_unscored_and_a_route_that_raises_is_a_miss() -> None:
    suite = load(DATA / "questions.yaml")
    only = replace(suite.probes[0], expect={"judge": "The answer names Mara Quill."})
    report = run(stand_in(), replace(suite, probes=[only]), clock=lambda: NOW)
    assert report.score is None and [s.id for s in report.unscored] == ["brightpine-owner"]
    raising = replace(suite.probes[2], target="proposals")
    raised = run(stand_in(), replace(suite, probes=[raising]), clock=lambda: NOW)
    assert raised.score == 0.0 and raised.scored[0].misses[0].why.startswith(
        "query raised: query: matching searches a collection's articles or facts, and proposals is a log")


@pytest.mark.parametrize("cap", ["2", float("inf"), float("nan"), -1, True])
def test_a_cap_that_is_no_amount_above_zero_is_refused(cap: Any) -> None:
    with pytest.raises(EvalError, match="no amount above zero"):
        run(stand_in(), load(DATA / "questions.yaml"), decider=FakeDecider(), model=RUNE, ledger=ledger(),
            cap_usd=cap)


def test_a_check_reads_a_real_articles_numbers_maps_lists_and_cited_text() -> None:
    from cerebro.article import Article, Compilation, Identity, Property
    from cerebro.evals import Probe, _rules

    you = Caller("you", "person")
    article = Article(Identity(id="deals/kestrel", collection="deals", title="Kestrel"),
                   properties={"amount": Property(40000, you, "2026-11-01T00:00:00Z"),
                               "owners": Property(["Omar", "Lina"], you, "2026-11-01T00:00:00Z"),
                               "terms": Property({"years": 2, "seats": 50}, you, "2026-11-01T00:00:00Z")},
                   compilations=[Compilation("deals/kestrel", "Overview", "The budget stays [^fact_01m7p4qga4pxpjkgvf] at "
                                     "$40,000.", by="deals/article")])
    probe = Probe("kestrel", "What is the deal?", "read", ["deals/kestrel"], {
        "holds": ["budget stays at $40,000"],
        "fields": {"amount": "40,000", "owners": ["Lina", "Omar"], "terms": {"seats": 50, "years": 2}}})
    assert [(c.name, c.passed) for c in _rules(probe, [article], "")] == [
        ("holds budget stays at $40,000", True), ("field amount", True), ("field owners", True), ("field terms", True)]


def test_a_judge_over_many_articles_reads_as_many_as_the_decision_model_takes() -> None:
    from cerebro.evals import Probe, _judge, _Run, _Spend
    from cerebro.models import DecisionTooLarge

    class Small(Priced):
        """Takes one article a request, as a real model with a size limit takes a short list."""

        def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
            if len(state["articles"]) > 1:
                raise DecisionTooLarge("over the limit")
            return super().decide(state, questions)

    workspace = stand_in()
    you = Caller("you", "person")
    articles = [workspace.read(you, [id]).articles[0] for id in ("projects/brightpine", "projects/kesmar")]
    probe = Probe("p", "Who leads?", "find", "Brightpine", {"judge": "It names the lead."})
    check, _, calls = _judge(probe, articles, "", _Run(you, Small(), _Spend(ledger(), RUNE, None, lambda: NOW), ticks()))
    assert check.passed is True and calls >= 1  # judged on the first article, and not failed


def test_the_judge_is_told_to_judge_the_criterion_and_not_to_answer_the_question() -> None:
    from cerebro import prompts

    # A judge that answers the question, as "Was the budget cut?" with the criterion "the budget stays at $40,000",
    # fails an article that holds the criterion word for word.
    assert "The question is context only" in prompts.load("eval-judge")


def test_a_file_that_is_no_suite_is_refused_with_what_a_suite_holds(tmp_path: Path) -> None:
    (listed := tmp_path / "list.yaml").write_text("- What is the Harbor beta's date?\n", encoding="utf-8")
    with pytest.raises(EvalError) as raised:
        load(listed)
    said = str(raised.value)
    assert "questions:" in said and "find, query or read" in said and "expect" in said


def test_the_suite_the_command_line_docs_show_and_the_gtm_template_ships_each_load(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    docs = (root / "docs" / "cli.md").read_text(encoding="utf-8")
    shown = [b for b in re.findall(r"```yaml\n(.*?)```", docs, re.DOTALL) if "questions:" in b]
    assert len(shown) == 1, "cli.md shows one eval suite"
    (suite_file := tmp_path / "atlas.yaml").write_text(shown[0], encoding="utf-8")
    suite = load(suite_file)
    assert suite.kind == "questions" and len(suite.probes) >= 2
    template = load(root / "src" / "cerebro" / "templates" / "gtm" / "evals" / "example.yaml")
    assert template.kind == "questions" and template.probes


def test_the_eval_judge_reads_each_part_of_an_article_in_order_two_under_one_heading_included() -> None:
    from cerebro.article import Article
    from cerebro.evals import _shown

    article = Article(Identity("calls/kickoff", "calls", "Kickoff"), [Compilation(
        "calls/kickoff", "calls/article", "## Update\n\nThe lease renewed [^f_a].\n\n## Update\n\nThe move halted [^f_z].",
        "calls/article", whole=True)])
    assert _shown(article)["parts"] == [{"heading": "Update", "text": "The lease renewed."},
                                        {"heading": "Update", "text": "The move halted."}]


def test_two_workers_recording_at_once_each_keep_their_calls_tokens_and_dollars(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from cerebro import evals

    met = threading.Barrier(2, timeout=0.5)

    def meet() -> None:
        try:
            met.wait()  # both workers hold the old total here, were they let in together
        except threading.BrokenBarrierError:
            pass

    class Meeting(float):
        def __add__(self, other: object) -> Meeting:
            meet()
            return Meeting(float(self) + float(other))  # type: ignore[arg-type]

    class Calls(int):
        def __radd__(self, other: object) -> int:
            meet()
            return int(other) + int(self)  # type: ignore[call-overload]

    class Tokens(TokenCount):
        def __init__(self, *args: Any) -> None:
            meet()
            super().__init__(*args)

    spend = evals._Spend(ledger(), RUNE, None, lambda: NOW)
    spend.usd = Meeting(0.0)
    cases = [spend.held(FakeDecider(), f"eval {n}") for n in range(2)]
    for case in cases:
        case.spent(SPENT)
        case.calls = Calls(1)
    monkeypatch.setattr(evals, "TokenCount", Tokens)
    workers = [threading.Thread(target=spend.add, args=(case,)) for case in cases]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert spend.calls == 2
    assert spend.tokens.input_tokens == 2 * SPENT.input_tokens
    assert spend.usd == pytest.approx(sum(row.data["usd"] for row in spend.ledger.store.rows(LOG)))
    assert spend.usd > 0


def test_a_top_level_key_a_question_suite_does_not_read_is_refused_with_its_line(tmp_path: Path) -> None:
    (typo := tmp_path / "typo.yaml").write_text("contxt: A wiki of things.\nquestions:\n  - id: a\n    ask: x\n"
                                                "    find: y\n    expect: { holds: [z] }\n", encoding="utf-8")
    with pytest.raises(EvalError) as raised:
        load(typo)
    assert str(raised.value) == f"eval suite {typo}: line 1: contxt is not a suite key: use context and questions"
    (typo := tmp_path / "typo.json").write_text('{"questions": [{"id": "a", "ask": "x", "find": "y",\n'
                                                '"expect": {"holds": ["z"]}}],\n "contxt": "A wiki."}',
                                                encoding="utf-8")
    with pytest.raises(EvalError, match=r"typo\.json: line 3: contxt is not a suite key"):
        load(typo)


@pytest.mark.parametrize(("name", "text", "said"), [
    ("tabs.json", '{\n\t"questions": [{"id": "a", "ask": "x", "find": "y", "expect": {"holds": ["z"]}}],\n\t"contxt": "w"\n}',
     "line 3: contxt is not a suite key"),
    ("yes.yaml", "questions:\n  - { id: a, ask: x, find: y, expect: { holds: [z] } }\nyes: 1\n", "line 3: yes is not a suite key"),
    ("null.yaml", "~: 1\nquestions:\n  - { id: a, ask: x, find: y, expect: { holds: [z] } }\n", "line 1: ~ is not a suite key"),
])
def test_a_top_level_key_is_named_as_written_with_its_line(tmp_path: Path, name: str, text: str, said: str) -> None:
    (suite := tmp_path / name).write_text(text, encoding="utf-8")
    with pytest.raises(EvalError) as raised:
        load(suite)
    assert str(raised.value).startswith(f"eval suite {suite}: {said}: use context and questions"), str(raised.value)
