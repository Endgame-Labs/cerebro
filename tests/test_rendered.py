"""The digest keys: code writes a rendered part with no model, stamped so it moves only with what it lists, and
`keep` and `top` cut an articles line to the passages a judge keeps."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cerebro import rendered
from cerebro.agent import Agent, AgentFileError, EvidenceLine, Render, load_agent
from cerebro.article import Compilation, Identity, passages
from cerebro.caller import Caller
from cerebro.compile import birth, compile_article
from cerebro.curate import apply
from cerebro.evidence import (
    KEEP_BAR,
    KEEP_LINE,
    DecisionKeepJudge,
    KeepJudge,
    KeepScores,
    Passage,
    blocks,
    choose,
    digest,
    gather,
    render,
)
from cerebro.fakes import FakeDecider, MemoryStore, ScriptedRun
from cerebro.ground import Grounded
from cerebro.lookup import StoreLookup
from cerebro.models import DEFAULTS, ModelSpec
from cerebro.records import Proposal

TEMPLATE = Path(rendered.__file__).parent / "templates" / "gtm" / "agents"
CURATOR = Caller("things/curator", "agent")

RUNE = ModelSpec("fireworks", "rune", "", "decision")


def keep_all(agent: object, parts: dict[str, str], citable: dict, *_: object) -> Grounded:
    return Grounded(dict(parts))


class Clock:
    """A clock that moves a minute each time it is read."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


def workspace(tmp_path: Path, written: bool = False) -> tuple[MemoryStore, dict]:
    """A store of three articles, each with a compilation a run wrote when `written`, as a rendered list counts them."""
    store = MemoryStore()
    for id, title in (("accounts/alpha-co", "Alpha Co"), ("accounts/beta-co", "Beta Co"), ("people/dana-lee", "Dana Lee")):
        (made if written else MemoryStore.create)(store, Identity(id, id.split("/")[0], title, status="active"))
    common = {"lookup": StoreLookup(store), "store": store, "root": tmp_path, "functions": {}, "agent_tools": {},
              "grounder": keep_all, "clock": Clock()}
    return store, common


def index() -> Agent:
    return load_agent(TEMPLATE / "digests" / "index.md", TEMPLATE)


def made(store: MemoryStore, identity: Identity) -> None:
    """An article with a compilation a run wrote: one never written is not counted in a rendered list."""
    store.create(identity)
    store.write_compilation(Compilation(identity.id, "Overview", "Written.", by=f"{identity.collection}/article"))


def admit(store: MemoryStore, id: str) -> None:
    """Admit an article, and land its first run's compilation."""
    apply(store, Proposal("p_admit", "article.admit", id, CURATOR, "2026-11-17T10:00:00Z", {"name": "Gamma Co"}),
          "2026-11-17T10:00:00Z")
    store.write_compilation(Compilation(id, "Overview", "Written.", by="accounts/article"))


def archive(store: MemoryStore, id: str) -> None:
    apply(store, Proposal("p_archive", "article.archive", id, CURATOR, "2026-11-17T11:00:00Z"), "2026-11-17T11:00:00Z")


def contents(store: MemoryStore) -> Compilation:
    return next(s for s in store.article("digests/index").compilations if s.heading == "Contents")


# -- rendered ------------------------------------------------------------------------------------------------------


def test_the_template_index_compiles_and_code_writes_its_contents_while_the_model_writes_the_rest(
        tmp_path: Path) -> None:
    store, common = workspace(tmp_path, written=True)
    agent = index()
    assert agent.notice == "" and "Contents" not in agent.output and agent.rendered["Contents"] == Render(
        "collections", "outline", ["count"])
    birth(store, agent, "digests/index", "2026-11-17T08:00:00Z")  # a pass births a fixed article before its first run
    prompts: list[str] = []
    run = ScriptedRun(lambda p: prompts.append(p) or {"Accounts": "- [Alpha Co](../accounts/alpha-co.md) - a maker",
                                                      "Contents": "the model's own list"})
    compiled = compile_article(agent, agent_run=run, **common)
    article = compiled.article
    assert [s.heading for s in passages(article)] == ["Contents", "Accounts"]
    assert "Contents: dropped the part: the wiki writes Contents itself" in compiled.dropped
    assert contents(store).text == ("- [Accounts](../accounts/) (2 articles)\n- [Digests](./) (1 article)\n"
                                    "- [People](../people/) (1 article)")
    assert contents(store).by == "rendered:digests/index" and contents(store).citations == []
    # The model reads the articles as data, by the same evidence path, with keep loading them whole until it has a
    # judge.
    assert "### Article articles/accounts/alpha-co.md" in prompts[0] and "no decision model was handed" in prompts[0]


def test_a_second_compile_with_nothing_changed_leaves_the_rendered_part_and_its_stamp_as_they_were(
        tmp_path: Path) -> None:
    store, common = workspace(tmp_path, written=True)
    agent = index()
    birth(store, agent, "digests/index", "2026-11-17T08:00:00Z")
    run = ScriptedRun(lambda p: {"Accounts": "- Alpha Co"})
    compile_article(agent, agent_run=run, **common)
    before = contents(store)
    again = compile_article(agent, agent_run=run, **common)
    assert again.skipped and contents(store) == before and before.digest == rendered.stamp_of(before.text)


def test_an_admit_changes_the_rendered_part_and_its_stamp(tmp_path: Path) -> None:
    store, common = workspace(tmp_path, written=True)
    agent = index()
    birth(store, agent, "digests/index", "2026-11-17T08:00:00Z")
    run = ScriptedRun(lambda p: {"Accounts": "- Alpha Co"})
    compile_article(agent, agent_run=run, **common)
    before = contents(store)
    admit(store, "accounts/gamma-co")
    compile_article(agent, agent_run=run, **common)
    after = contents(store)
    assert after.text.startswith("- [Accounts](../accounts/) (3 articles)\n") and after.digest != before.digest
    assert after.at != before.at


def test_an_archive_changes_the_rendered_part_and_its_stamp(tmp_path: Path) -> None:
    store, common = workspace(tmp_path, written=True)
    agent = index()
    birth(store, agent, "digests/index", "2026-11-17T08:00:00Z")
    run = ScriptedRun(lambda p: {"Accounts": "- Alpha Co"})
    compile_article(agent, agent_run=run, **common)
    before = contents(store)
    archive(store, "people/dana-lee")
    compile_article(agent, agent_run=run, **common)
    after = contents(store)
    assert after.text.endswith("- [People](../people/) (0 articles)") and after.digest != before.digest


def test_a_rendered_part_is_written_with_no_model_call_even_when_the_rest_of_the_article_skips(tmp_path: Path) -> None:
    store, common = workspace(tmp_path, written=True)
    decider = FakeDecider()
    article = Agent(id="digests/home", path=tmp_path / "agents" / "digests" / "home.md", role="writer",
                 target="digests/home", prompt="Write it.\n\n# Output\n\n## Contents\n\n## Notes",
                 output=["Notes"], rendered={"Contents": Render("collections", show=[])})
    calls: list[str] = []
    run = ScriptedRun(lambda p: calls.append(p) or {"Notes": "n"})
    compile_article(article, agent_run=run, decider=decider, **common)
    assert len(calls) == 1
    admit(store, "accounts/gamma-co")
    skipped = compile_article(article, agent_run=run, decider=decider, **common)
    # Nothing the model reads moved, so no run and no decision; the compilation still follows what it lists.
    assert skipped.skipped and len(calls) == 1 and decider.asked == []
    text = next(s.text for s in store.article("digests/home").compilations if s.heading == "Contents")
    assert text == "- [Accounts](../accounts/)\n- [Digests](./)\n- [People](../people/)"


def test_the_rendered_module_imports_no_model_decider_or_extraction() -> None:
    tree = ast.parse(Path(rendered.__file__).read_text(encoding="utf-8"))
    names = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    banned = ("models", "run", "facts", "ground", "identity", "evidence", "compile", "curate", "pydantic_ai", "factoid")
    assert not [n for n in names if n.split(".")[-1] in banned or n.split(".")[0] in banned]


def test_headings_and_show_lay_out_each_collection() -> None:
    store = MemoryStore()
    for id in ("accounts/alpha-co", "work-streams/rollout", "work-streams/pilot"):
        made(store, Identity(id, id.split("/")[0], id))
    listed = rendered.collections(store, "work-streams/overview", {"accounts": "Who the team works with."})
    assert [(i.title, i.link, i.count) for i in listed] == [("Accounts", "../accounts/", 1),
                                                            ("Work streams", "./", 2)]
    both = Render("collections", "headings", ["count", "description"])
    assert rendered.render(both, listed) == ("### [Accounts](../accounts/)\n\nWho the team works with. 1 article.\n\n"
                                             "### [Work streams](./)\n\n2 articles.")
    assert rendered.render(Render("collections", "outline", ["description", "count"]), listed[:1]) == (
        "- [Accounts](../accounts/) (1 article): Who the team works with.")
    assert rendered.render(Render("collections", "headings", []), listed[:1]) == "### [Accounts](../accounts/)"
    assert rendered.collections(store, "home")[0].link == "accounts/"
    assert rendered.collections(store, "a/b/c")[0].link == "../../accounts/"


def test_an_article_not_born_yet_or_an_agent_with_nothing_rendered_writes_nothing() -> None:
    store = MemoryStore()
    agent = Agent(id="digests/index", path=Path("x.md"), role="writer", target="digests/index", prompt="",
                  rendered={"Contents": Render("collections")})
    assert rendered.land(store, agent, "digests/index", "2026-11-17T08:00:00Z") == []
    store.create(Identity("digests/index", "digests", "Index"))
    assert rendered.land(store, replace(agent, rendered={}), "digests/index", "2026-11-17T08:00:00Z") == []
    assert rendered.land(store, agent, "digests/index", "2026-11-17T08:00:00Z") == ["Contents"]
    assert rendered.land(store, agent, "digests/index", "2026-11-17T09:00:00Z") == []


# -- keep and top ---------------------------------------------------------------------------------------------------


class ByText(KeepJudge):
    """A judge that scores a passage by the first word of `marks` it holds, and 0 when it holds none."""

    def __init__(self, marks: dict[str, float]):
        self.marks, self.asked = marks, 0

    def score(self, instruction: str, passages: list[Passage], bar: float = KEEP_BAR) -> KeepScores:
        self.asked += 1
        return KeepScores([next((v for k, v in self.marks.items() if k in p.text), 0.0) for p in passages],
                          scored=len(passages))


def articles(store: MemoryStore) -> None:
    store.create(Identity("projects/borealis", "projects", "Borealis"))
    store.create(Identity("projects/cirrus", "projects", "Cirrus"))
    store.write_compilation(Compilation("projects/borealis", "Status", "**Health**\nOn track for March, led by Ola.\n\n"
                                "- PR 412 merged\n- PR 413 opened", by="projects/article"))
    store.write_compilation(Compilation("projects/borealis", "Work", "Refactor of the build tool.", by="projects/article"))
    store.write_compilation(Compilation("projects/cirrus", "Status", "- PR 9 merged", by="projects/article"))


def reader(**options: object) -> Agent:
    line = EvidenceLine("articles", ["projects"], {"text_only": True, "keep": "How each project stands.", **options})
    return Agent(id="digests/pulse", path=Path("agents/digests/pulse.md"), role="writer", target="digests/pulse",
                 prompt="Write it.", evidence=[line])


def test_keep_loads_only_the_passages_the_judge_keeps_each_article_with_its_front_matter() -> None:
    store = MemoryStore()
    articles(store)
    judge = ByText({"On track": 0.9, '"title"': 0.6})
    entry = gather(reader(), StoreLookup(store), {}, judge=judge)[0]
    text = render([entry])
    assert entry.lines[0] == KEEP_LINE
    assert ("### Article articles/projects/borealis.md, 2 of its 5 passages, kept for this entry's instruction\n\n"
            '{"title": "Borealis"}\n\n#### Status\n\n**Health**\nOn track for March, led by Ola.') in text
    assert "PR 412" not in text and "Refactor" not in text and "#### Work" not in text
    assert "### Article articles/projects/cirrus.md, 1 of its 2 passages" in text and "PR 9" not in text
    assert "keep: kept 3 of 7 passages and dropped 4, 7 scored now and 0 from the cache" in text


def test_an_article_with_no_passage_kept_does_not_load() -> None:
    store = MemoryStore()
    articles(store)
    entry = gather(reader(), StoreLookup(store), {}, judge=ByText({"On track": 0.9}))[0]
    assert [line.split("\n")[0] for line in entry.lines[1:2]] == [
        "### Article articles/projects/borealis.md, 1 of its 5 passages, kept for this entry's instruction"]
    assert "cirrus" not in render([entry]).replace("Cirrus", "")


def test_top_keeps_exactly_the_n_best_passages_the_best_first_and_ties_in_their_order() -> None:
    passages = [Passage("p", "s", str(n)) for n in range(6)]
    for passage, score in zip(passages, (0.6, 0.9, 0.7, 0.9, 0.4, 0.8)):
        passage.score = score
    choose(passages, 3, 0.5)
    assert [p.text for p in passages if p.kept] == ["1", "3", "5"]
    assert [p.why for p in passages] == ["past top 3", "rank 1", "past top 3", "rank 2", "below the bar of 0.5",
                                         "rank 3"]
    choose(passages, None, 0.5)
    assert [p.text for p in passages if p.kept] == ["0", "1", "2", "3", "5"]
    passages[0].score = 0.5
    choose(passages, 10, 0.5)
    assert passages[0].kept and passages[0].why == "rank 5"
    passages[4].score = 0.5  # the bar of a model nobody measured, unless a line sets its own
    choose(passages, None)
    assert passages[4].kept and KEEP_BAR == 0.5


def test_a_top_line_gathers_exactly_n_passages() -> None:
    store = MemoryStore()
    articles(store)
    judge = ByText({"PR 412": 0.95, "PR 413": 0.9, "PR 9": 0.85, "On track": 0.8, "Refactor": 0.7, '"title"': 0.6})
    entry = gather(reader(top=3), StoreLookup(store), {}, judge=judge)[0]
    text = render([entry])
    assert "PR 412" in text and "PR 413" in text and "PR 9" in text
    assert "On track" not in text and "Refactor" not in text and "kept 3 of 7 passages" in text


def test_keep_with_no_judge_or_a_passage_left_unscored_loads_the_entry_whole_and_says_why() -> None:
    store = MemoryStore()
    articles(store)
    whole = render([gather(reader(), StoreLookup(store), {})[0]])
    assert "PR 412" in whole and "Refactor" in whole and "keep: the judge left passages unscored" in whole
    assert "no decision model was handed to this run's evidence" in whole

    class Unsure(KeepJudge):
        def score(self, instruction: str, passages: list[Passage], bar: float = KEEP_BAR) -> KeepScores:
            return KeepScores([None] * len(passages), "the day's budget is spent")

    unsure = render([gather(reader(), StoreLookup(store), {}, judge=Unsure())[0]])
    assert "PR 412" in unsure and unsure.endswith("so this entry loaded as it would without keep: the day's budget "
                                                  "is spent")


def test_a_change_to_a_dropped_passage_still_moves_the_digest() -> None:
    store = MemoryStore()
    articles(store)
    agent, judge = reader(), ByText({"On track": 0.9})
    before = digest(agent, gather(agent, StoreLookup(store), {}, judge=judge))
    store.write_compilation(Compilation("projects/borealis", "Work", "Refactor of the deploy tool.", by="projects/article"))
    after = gather(agent, StoreLookup(store), {}, judge=judge)
    assert "Refactor" not in render(after) and digest(agent, after) != before


def test_keep_on_a_summary_cuts_only_that_part_and_the_article_the_run_writes_loads_whole() -> None:
    store = MemoryStore()
    articles(store)
    store.create(Identity("digests/pulse", "digests", "Pulse"))
    store.write_compilation(Compilation("digests/pulse", "Notes", "A person's note.", by="someone"))
    summary = EvidenceLine("articles", ["projects/borealis"], {"summary": "Work", "keep": "The work."})
    text = render(gather(replace(reader(), evidence=[summary]), StoreLookup(store), {},
                         judge=ByText({"Refactor": 0.9}))[0:1])
    assert "#### Work\n\nRefactor of the build tool." in text and "On track" not in text
    own = EvidenceLine("articles", ["projects/borealis", "digests/pulse"], {"text_only": True, "keep": "The work."})
    text = render(gather(replace(reader(), evidence=[own]), StoreLookup(store), {},
                         judge=ByText({"Refactor": 0.9}))[0:1])
    assert "### Article articles/digests/pulse.md, the one you write" in text and "A person's note." in text
    assert "On track" not in text and "Refactor of the build tool." in text


def test_keep_and_top_on_an_articles_line_are_acted_on_and_elsewhere_still_named_as_not_yet(tmp_path: Path) -> None:
    path = tmp_path / "digests" / "pulse.md"
    path.parent.mkdir()
    path.write_text("---\nevidence:\n  - articles: [projects]\n    text_only: true\n    keep: status\n    top: 4\n"
                    "  - query: { from: notes }\n    keep: open ones\n---\nbody", encoding="utf-8")
    assert load_agent(path, tmp_path).notice == "agents/digests/pulse.md: read but not acted on: keep"
    path.write_text("---\nevidence:\n  - articles: [projects]\n    text_only: true\n    keep: status\n    top: 4\n---\n"
                    "body", encoding="utf-8")
    assert load_agent(path, tmp_path).notice == ""


def test_a_body_is_cut_into_paragraphs_list_items_and_fences_with_a_label_on_what_follows() -> None:
    body = ("In flight:\n- one\n  more of one\n- two\n\n<!-- a note -->\nA paragraph\nover two lines.\n\n```\ncode\n\n"
            "more\n```\n### A heading\n**Stuck**")
    assert blocks(body) == ["In flight:\n- one\n  more of one", "- two", "A paragraph\nover two lines.",
                            "```\ncode\n\nmore\n```", "### A heading\n**Stuck**"]
    assert blocks("1. first\n2) second\n3.x") == ["1. first", "2) second\n3.x"]


def test_the_decision_judge_asks_once_per_passage_and_keeps_its_scores() -> None:
    store, clock = MemoryStore(), Clock()
    decider = FakeDecider({"p1": 0.8, "p2": 0.2})
    judge = DecisionKeepJudge(decider, store, clock, decision=RUNE)
    two = [Passage("a", "s", "first"), Passage("a", "s", "second"), Passage("a", "s", "first")]
    got = judge.score("the status", two)
    assert got.scores == [0.8, 0.2, 0.8] and (got.scored, got.cached, got.wait) == (3, 0, "")
    assert len(decider.asked) == 1 and [p["text"] for p in decider.asked[0][0]["passages"]] == ["first", "second"]
    assert judge.score("the status", two[:2]).cached == 2 and len(decider.asked) == 1
    judge.score("another instruction", two[:1])
    assert len(decider.asked) == 2 and judge.calls == 2
    clock.now += timedelta(days=31)
    judge.score("the status", [Passage("c", "s", "third")])  # a score is dropped 30 days after it was scored
    assert judge.score("the status", two[:1]).cached == 0 and len(decider.asked) == 4


class Silent(FakeDecider):
    """Gives no number for a passage whose text holds `word`, however often it is asked."""

    def __init__(self, word: str, answers: dict[str, Any] | None = None):
        super().__init__(answers)
        self.word = word

    def decide(self, state, questions):  # type: ignore[no-untyped-def]
        decision = super().decide(state, questions)
        silent = {p["id"] for p in state["passages"] if self.word in p["text"]}
        return replace(decision, answers={k: v for k, v in decision.answers.items() if k not in silent})


def test_the_decision_judge_asks_once_more_for_a_passage_left_unscored_and_caches_no_score_for_it() -> None:
    store = MemoryStore()
    decider = Silent("two")
    judge = DecisionKeepJudge(decider, store, Clock(), decision=RUNE)
    got = judge.score("x", [Passage("a", "s", "one"), Passage("a", "s", "two")])
    assert got.scores == [1.0, None] and got.wait == "the decision model left the passage p1 unscored"
    assert [[p["text"] for p in state["passages"]] for state, _ in decider.asked] == [["one", "two"], ["two"]]
    assert judge.score("x", [Passage("a", "s", "two")]).cached == 0 and len(decider.asked) == 4


def test_a_passage_left_unscored_beside_scored_ones_counts_as_under_the_bar_and_a_note_says_how_many() -> None:
    class Partial(ByText):
        def score(self, instruction: str, passages: list[Passage], bar: float = KEEP_BAR) -> KeepScores:
            got = super().score(instruction, passages, bar)
            got.scores = [None if "Refactor" in p.text else s for p, s in zip(passages, got.scores)]
            return replace(got, wait="the decision model left the passage p4 unscored")

    store = MemoryStore()
    articles(store)
    entry = gather(reader(), StoreLookup(store), {}, judge=Partial({"On track": 0.9, '"title"': 0.6,
                                                                   "Refactor": 0.9}))[0]
    text = render([entry])
    assert "On track" in text and "Refactor" not in text and "kept 3 of 7 passages and dropped 4" in text
    assert text.endswith("keep: the judge left 1 of the 7 passages unscored, so they counted as under the bar: the "
                         "decision model left the passage p4 unscored")


def test_a_bar_the_line_sets_is_the_one_the_judge_asks_again_under() -> None:
    store = MemoryStore()
    articles(store)
    decider = FakeDecider({f"p{n}": 0.7 for n in range(1, 41)})
    for bar, calls in ((0.5, 1), (0.9, 2)):
        decider.asked.clear()
        judge = DecisionKeepJudge(decider, decision=RUNE)
        text = render([gather(reader(keep_bar=bar), StoreLookup(store), {}, judge=judge)[0]])
        assert len(decider.asked) == calls and ("Refactor" in text) is (bar == 0.5), bar


def test_each_rendered_heading_is_written_on_its_own_when_only_it_moved() -> None:
    store = MemoryStore()
    store.create(Identity("digests/index", "digests", "Index"))
    made(store, Identity("accounts/alpha-co", "accounts", "Alpha Co"))
    agent = Agent(id="digests/index", path=Path("x.md"), role="writer", target="digests/index", prompt="",
                  rendered={"Links": Render("collections", show=[]),
                            "Contents": Render("collections", show=["description", "count"])})
    lines = {"accounts": "Who the team works with."}
    assert rendered.land(store, agent, "digests/index", "2026-11-17T08:00:00Z", lines) == ["Links", "Contents"]
    made(store, Identity("accounts/beta-co", "accounts", "Beta Co"))
    assert rendered.land(store, agent, "digests/index", "2026-11-17T09:00:00Z", lines) == ["Contents"]
    assert contents(store).text.startswith("- [Accounts](../accounts/) (2 articles): Who the team works with.\n")


def test_the_decision_judge_records_each_call_in_the_ledger_under_compilation() -> None:
    from cerebro.budget import LOG, Caps, Ledger
    from cerebro.models import DEFAULTS

    store = MemoryStore()
    ledger = Ledger(store, Caps(compilation=5.0))
    judge = DecisionKeepJudge(FakeDecider({"p1": 0.9}), clock=Clock(), ledger=ledger, decision=DEFAULTS["decision"])
    assert judge.score("the status", [Passage("a", "s", "one")]).scores == [0.9]
    rows = [row.data for row in store.rows(LOG)]
    assert [(r["layer"], r["provider"], r["model"], r["run"]) for r in rows] == [
        ("compilation", DEFAULTS["decision"].provider, DEFAULTS["decision"].model, "keep judge")]


def test_with_the_compilation_cap_spent_the_judge_asks_nothing_and_the_entry_loads_whole_saying_why() -> None:
    from cerebro.budget import Caps, Ledger
    from cerebro.models import DEFAULTS

    store = MemoryStore()
    articles(store)
    decider = FakeDecider()
    judge = DecisionKeepJudge(decider, store, Clock(), Ledger(store, Caps(compilation=0.0)), DEFAULTS["decision"])
    text = render([gather(reader(), StoreLookup(store), {}, judge=judge)[0]])
    assert decider.asked == [] and judge.calls == 0
    assert "PR 412" in text and "Refactor" in text and "so this entry loaded as it would without keep: compilation " \
        "spent $0.00 of its $0 cap" in text


def test_a_score_is_kept_per_decision_model_and_ages_from_when_it_was_scored_however_often_it_is_read() -> None:
    from cerebro.evidence import keep_bar

    store, clock, passages = MemoryStore(), Clock(), [Passage("a", "s", "Alder renewed for two years.")]
    rune = FakeDecider({"p1": 0.03})
    assert DecisionKeepJudge(rune, store, clock, decision=RUNE).score("the brief", passages).scores == [0.03]
    jev = FakeDecider({"p1": 0.95})
    got = DecisionKeepJudge(jev, store, clock, decision=DEFAULTS["decision"]).score("the brief", passages)
    assert got.scores == [0.95] and got.cached == 0 and len(jev.asked) == 1  # another model scores afresh
    for _ in range(30):  # read every day: reading does not renew it
        clock.now += timedelta(days=1)
        DecisionKeepJudge(jev, store, clock, decision=DEFAULTS["decision"]).score("the brief", passages)
    assert len(jev.asked) == 1
    clock.now += timedelta(days=1)
    assert DecisionKeepJudge(jev, store, clock, decision=DEFAULTS["decision"]).score("the brief", passages).cached == 0
    assert (keep_bar(DEFAULTS["decision"]), keep_bar(RUNE), keep_bar(None)) == (0.4, 0.1, 0.5)
    assert keep_bar(ModelSpec("typesafe", "jev-2", "", "decision")) == 0.5  # a model nobody measured


def test_a_cached_score_makes_no_decider_call() -> None:
    store, passages = MemoryStore(), [Passage("a", "s", "one"), Passage("a", "s", "two")]
    first = FakeDecider({"p1": 0.7, "p2": 0.3})
    assert DecisionKeepJudge(first, store, Clock(), decision=RUNE).score("the status", passages).scores == [0.7, 0.3]
    later = FakeDecider()
    got = DecisionKeepJudge(later, store, Clock(), decision=RUNE).score("the status", passages)
    assert got.scores == [0.7, 0.3] and (got.scored, got.cached) == (0, 2) and later.asked == []


def test_the_score_cache_keeps_at_most_its_bound_dropping_the_oldest_scored_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    evidence = importlib.import_module("cerebro.evidence")
    monkeypatch.setattr(evidence, "KEEP_CACHE_ENTRIES", 2)
    store, clock, decider = MemoryStore(), Clock(), FakeDecider()
    judge = DecisionKeepJudge(decider, store, clock)
    for text in ("one", "two", "three"):
        judge.score("the status", [Passage("a", "s", text)])
        clock.now += timedelta(days=1)
    (kept,) = [v for k, v in store.values.items() if k.startswith("cerebro:keep:")]
    assert len(kept) == 2
    assert judge.score("the status", [Passage("a", "s", "three")]).cached == 1
    assert judge.score("the status", [Passage("a", "s", "one")]).cached == 0 and len(decider.asked) == 4


def test_the_judge_reads_its_words_from_the_workspaces_own_prompt_when_it_has_one(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "keep-judge.md").write_text("Score each passage for the article.", encoding="utf-8")
    store, decider = MemoryStore(), FakeDecider()
    DecisionKeepJudge(decider, store, Clock()).score("the status", [Passage("a", "s", "one")])
    DecisionKeepJudge(decider, store, Clock(), root=tmp_path).score("the status", [Passage("a", "s", "one")])
    assert decider.asked[0][0]["contract"].startswith("An agent of a team's wiki")
    assert decider.asked[1][0]["contract"] == "Score each passage for the article."  # an edited prompt scores afresh


# -- the wiring: who hands a judge to a gather, where a collection's line comes from, and the pass that renders


class ByArticle(FakeDecider):
    """A decision model that gives each passage the probability of yes `scores` names for its article, in every call,
    or a queue of them, one each time the article is asked, and 1 for an article it does not name."""

    def __init__(self, scores: dict[str, float | list[float]]):
        super().__init__()
        self.scores = {article: list(v) if isinstance(v, list) else v for article, v in scores.items()}

    def decide(self, state: Any, questions: dict) -> Any:
        for passage in state["passages"]:
            score = self.scores.get(passage["article"], 1.0)
            self.answers[passage["id"]] = score.pop(0) if isinstance(score, list) else score
        return super().decide(state, questions)


def test_a_compile_with_a_decision_model_cuts_a_keep_line_by_its_judge(tmp_path: Path) -> None:
    store, common = workspace(tmp_path)
    agent = index()
    birth(store, agent, "digests/index", "2026-11-17T08:00:00Z")
    prompts: list[str] = []
    run = ScriptedRun(lambda p: prompts.append(p) or {"Accounts": "- Alpha Co"})
    decider = ByArticle({"accounts/beta-co": 0.05})  # beta-co scores below the bar, then again when asked once more
    compile_article(agent, agent_run=run, decider=decider, **common)
    assert len(decider.asked) == 2 and "kept 2 of 3 passages" in prompts[0]
    assert "articles/accounts/alpha-co.md, 1 of its 1 passages" in prompts[0]
    assert "articles/accounts/beta-co.md" not in prompts[0]


def test_a_curator_with_a_decision_model_cuts_its_keep_line_by_the_same_judge(tmp_path: Path) -> None:
    from cerebro.curate import run_curator

    store, common = workspace(tmp_path)
    (tmp_path / "agents" / "people").mkdir(parents=True)
    (tmp_path / "agents" / "people" / "curator.md").write_text(
        "---\nevidence:\n  - articles: [accounts]\n    text_only: true\n    keep: \"The name.\"\n---\n\n"
        "Admit people.\n")
    curator = load_agent(tmp_path / "agents" / "people" / "curator.md", tmp_path / "agents")
    prompts: list[str] = []
    run = ScriptedRun(lambda p: prompts.append(p) or {})
    run_curator(curator, lookup=common["lookup"], store=store, agent_run=run, root=tmp_path, functions={},
                agent_tools={}, clock=common["clock"], decider=ByArticle({"accounts/alpha-co": 0.05}))
    assert "kept 1 of 2 passages" in prompts[0] and "articles/accounts/alpha-co.md" not in prompts[0]


def test_settings_give_a_collection_its_line_and_a_compile_renders_it(tmp_path: Path) -> None:
    store, common = workspace(tmp_path, written=True)
    lines = rendered.descriptions({"collections": {"accounts": {"description": " Who the  team works with. "},
                                                   "people": {"description": ""}}})
    assert lines == {"accounts": "Who the team works with."}
    article = Agent(id="digests/home", path=tmp_path / "home.md", role="writer", target="digests/home",
                 prompt="", output=["Notes"], rendered={"Contents": Render("collections", show=["description"])})
    birth(store, article, "digests/home", "2026-11-17T08:00:00Z")
    compile_article(article, agent_run=ScriptedRun(lambda p: {"Notes": "n"}), descriptions=lines, **common)
    text = next(s.text for s in store.article("digests/home").compilations if s.heading == "Contents")
    assert text.startswith("- [Accounts](../accounts/): Who the team works with.\n- [Digests](./)\n")


@pytest.mark.parametrize("settings, says", [
    ({"collections": ["accounts"]}, "settings.yaml collections is not a map"),
    ({"collections": {"accounts": "Who we work with."}}, "collections.accounts takes description alone"),
    ({"collections": {"accounts": {"description": "x", "title": "y"}}}, 'settings.yaml: unknown key "title"'),
    ({"collections": {"accounts": {"description": 3}}}, "collections.accounts takes description alone"),
])
def test_a_collections_setting_that_does_not_read_stops_the_workspace_with_one_line(settings: dict, says: str) -> None:
    from cerebro.project import WorkspaceError, check_settings

    with pytest.raises(WorkspaceError) as refused:
        check_settings(settings)
    assert says in str(refused.value)


@pytest.mark.parametrize("served", [True, False])
def test_a_pass_renders_an_article_after_an_admit_while_its_agent_cools_down_or_has_nothing_new(
        tmp_path: Path, served: bool) -> None:
    from cerebro import Cerebro
    from cerebro.fakes import FakeExtraction

    (tmp_path / "agents" / "digests").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("collections: { things: { description: What we keep. } }\n")
    (tmp_path / "agents" / "digests" / "index.md").write_text(
        "---\nrendered:\n  Contents: { kind: collections, show: [count, description] }\nheartbeat: { on: [change], "
        "cooldown: 1d }\n---\n\n# Output\n\n## Contents\n\n## Notes\n")
    store = MemoryStore()
    made(store, Identity("things/alpha", "things", "Alpha"))
    with Cerebro(tmp_path, store=store, extraction=FakeExtraction(), clock=Clock(),
                 agent_run=ScriptedRun(lambda p: {"Notes": "n"})) as cerebro:
        cerebro.run_once(scheduled=served)
        # its writer's text is in the article, so the article is no longer new: a new one is built at once all the same
        store.write_compilation(Compilation("digests/index", "Notes", "Two things so far.", by="digests/index"))
        admit(store, "things/gamma")
        second = cerebro.run_once(scheduled=served)
        # it cools down, or reads nothing new: code writes the compilation
        assert "digests/index" not in second.compiled
        assert "digests/index: wrote Contents with no model, as its rendered: key says" in second.notices
        text = contents(store).text
        assert text.endswith("\n- [Things](../things/) (2 articles): What we keep.")
        assert "(2 articles)" in (tmp_path / "articles" / "digests" / "index.md").read_text()
        assert "wrote Contents" not in " ".join(cerebro.run_once(scheduled=served).notices)  # nothing moved: nothing written


def test_a_passage_scored_under_the_bar_is_asked_once_more_alone_and_keeps_the_higher_score() -> None:
    store = MemoryStore()
    decider = ByArticle({"a": [0.02, 0.83], "b": [0.04, 0.03], "c": 0.9})
    judge = DecisionKeepJudge(decider, store)
    passages = [Passage("a", "s", "one"), Passage("b", "s", "two"), Passage("c", "s", "three")]
    got = judge.score("the status", passages)
    assert got.scores == [0.83, 0.04, 0.9] and judge.calls == 2
    assert [p["article"] for p in decider.asked[1][0]["passages"]] == ["b", "a"]  # the low ones alone, the other order
    assert judge.score("the status", passages).cached == 3 and judge.calls == 2  # a cached score is not asked again
    once = DecisionKeepJudge(ByArticle({"a": [0.02, 0.83]}), again=False)
    assert once.score("the status", passages[:1]).scores == [0.02] and once.calls == 1
    high = DecisionKeepJudge(ByArticle({"a": [0.3, 0.9]}))
    assert high.score("the status", passages[:1], bar=0.5).scores == [0.9]  # under a line's own bar, asked again


def test_a_line_sets_its_own_keep_bar_which_goes_with_keep_and_reads_from_0_to_1(tmp_path: Path) -> None:
    store = MemoryStore()
    articles(store)
    judge = ByText({"On track": 0.9, "PR 9": 0.3})
    judge.bar = 0.1  # the bar of the judge's model, which a line's own keep_bar overrides
    assert "kept 2 of 7 passages" in render([gather(reader(), StoreLookup(store), {}, judge=judge)[0]])
    assert "kept 1 of 7 passages" in render([gather(reader(keep_bar=0.5), StoreLookup(store), {}, judge=judge)[0]])
    (tmp_path / "agents" / "digests").mkdir(parents=True)
    for bar, says in (("1.5", "keep_bar is the score from 0 to 1"), ("true", "keep_bar is the score from 0 to 1")):
        (tmp_path / "agents" / "digests" / "pulse.md").write_text(
            f"---\nevidence:\n  - articles: [projects]\n    text_only: true\n    keep: How each stands.\n"
            f"    keep_bar: {bar}\n---\n\n# Output\n\n## Pulse\n")
        with pytest.raises(AgentFileError) as refused:
            load_agent(tmp_path / "agents" / "digests" / "pulse.md", tmp_path / "agents")
        assert says in str(refused.value)


def test_a_rendered_part_stands_before_the_agents_own_text_or_after_it_in_the_order_rendered_lists(
        tmp_path: Path) -> None:
    from cerebro.agent import Render

    store, common = workspace(tmp_path, written=True)
    agent = replace(index(), rendered={"Contents": Render("collections"), "Elsewhere": Render("collections", place="after"),
                                       "Top": Render("collections", "headings")})
    birth(store, agent, "digests/index", "2026-11-17T08:00:00Z")
    compile_article(agent, agent_run=ScriptedRun(lambda p: "## Accounts\n\n- [Alpha Co](../accounts/alpha-co.md)"),
                    **common)
    assert [s.heading for s in passages(store.article("digests/index"))] == ["Contents", "Top", "Accounts", "Elsewhere"]
    moved = replace(agent, rendered={"Elsewhere": Render("collections"), "Contents": Render("collections", place="after"),
                                     "Top": agent.rendered["Top"]}, hash="moved")
    admit(store, "accounts/gamma-co")
    compile_article(moved, agent_run=ScriptedRun(lambda p: "## Accounts\n\n- [Gamma Co](../accounts/gamma-co.md)"),
                    **common)
    assert [s.heading for s in passages(store.article("digests/index"))] == ["Elsewhere", "Top", "Accounts", "Contents"]


def test_a_rendered_heading_the_agent_no_longer_names_is_removed_and_a_written_one_is_never_touched(
        tmp_path: Path) -> None:
    store, common = workspace(tmp_path)
    agent = index()
    birth(store, agent, "digests/index", "2026-11-17T08:00:00Z")
    run = ScriptedRun(lambda p: {"Accounts": "- Alpha Co"})
    compile_article(agent, agent_run=run, **common)
    store.write_compilation(Compilation("digests/index", "Notes", "Kept by hand.", by="you"))
    renamed = replace(agent, rendered={"Collections": agent.rendered["Contents"]}, hash="edited")
    admit(store, "accounts/gamma-co")
    compile_article(renamed, agent_run=run, **common)
    held = {s.heading: s.by for s in passages(store.article("digests/index"))}
    assert held == {"Collections": "rendered:digests/index", "Accounts": "digests/index", "Notes": "you"}
    rendered.refresh(store, [replace(renamed, rendered={})], "2026-11-18T08:00:00Z")
    assert "Collections" not in {s.heading for s in store.article("digests/index").compilations}


def test_a_collection_whose_articles_no_run_has_written_is_not_listed_and_an_unwritten_one_is_not_counted() -> None:
    store = MemoryStore()
    store.create(Identity("reports/pipeline", "reports", "Pipeline"))  # born before its first run, never written
    made(store, Identity("accounts/alpha-co", "accounts", "Alpha Co"))
    store.create(Identity("accounts/beta-co", "accounts", "Beta Co"))  # admitted, never written
    listed = rendered.collections(store, "digests/index")
    assert [(i.title, i.count) for i in listed] == [("Accounts", 1)]  # no link to a folder with no copy in it


def test_a_compile_whose_run_lands_nothing_still_writes_the_copy_when_code_changed_a_part(tmp_path: Path) -> None:
    store, common = workspace(tmp_path, written=True)
    agent = index()
    birth(store, agent, "digests/index", "2026-11-17T08:00:00Z")
    compiled = compile_article(agent, agent_run=ScriptedRun(lambda p: ""), **common)
    copy = tmp_path / "articles" / "digests" / "index.md"
    assert compiled.article is None and copy.is_file() and "[Accounts](../accounts/)" in copy.read_text()
