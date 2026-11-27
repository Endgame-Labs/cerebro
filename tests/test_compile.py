"""A compile runs an agent only when what it reads moved, and lands what the run wrote."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cerebro.agent import Agent, Budget, EvidenceLine, load_agents
from cerebro.article import Citation, Identity, body
from cerebro.caller import Caller
from cerebro.compile import DRAFTS, CompileError, CompileRefused, compile_article, targets
from cerebro.fakes import FakeExtraction, MemoryStore, ScriptedRun
from cerebro.lookup import StoreLookup
from cerebro.run import AgentRun, Run
from cerebro.workspace import Workspace

AGENT = Agent(id="reports/weekly", path=Path("agents/reports/weekly.md"), role="writer", target="reports/weekly",
              prompt="Write it.\n\n# Output\n\n## Summary\n\n## Next", output=["Summary", "Next"],
              evidence=[EvidenceLine("items", {})])


def setup(tmp_path: Path) -> tuple[MemoryStore, list[str], list[str], dict]:
    store, items, prompts = MemoryStore(), ["one"], []

    def evidence() -> list[dict[str, str]]:
        """Items."""
        return [{"id": f"r{n}", "text": text} for n, text in enumerate(items)]

    common = {"lookup": StoreLookup(store), "store": store, "root": tmp_path, "functions": {"items": evidence},
              "agent_tools": {}}
    return store, items, prompts, common


def test_a_run_lands_its_text_whole_and_is_not_due_again_on_the_same_evidence(tmp_path: Path) -> None:
    store, items, prompts, common = setup(tmp_path)
    both = ScriptedRun(lambda p: prompts.append(p) or {"Summary": "s", "Next": "n"})
    only = ScriptedRun(lambda p: prompts.append(p) or "No heading at all.")
    assert compile_article(AGENT, agent_run=both, **common).article is not None
    assert body(store.article("reports/weekly")) == "## Summary\n\ns\n\n## Next\n\nn"
    items.append("two")
    assert compile_article(AGENT, agent_run=only, **common).run is not None
    assert compile_article(AGENT, agent_run=only, **common).skipped and len(prompts) == 2
    [part] = store.article("reports/weekly").compilations
    assert part.whole and part.heading == part.by == "reports/weekly" and part.text == "No heading at all."


def test_parts_stored_one_heading_at_a_time_read_as_they_were_until_the_agents_next_run_replaces_them(
        tmp_path: Path) -> None:
    from cerebro.article import Compilation

    store, items, _, common = setup(tmp_path)
    store.create(Identity("reports/weekly", "reports", "Weekly"))
    compile_article(AGENT, agent_run=ScriptedRun(lambda p: "first"), **common)
    stamp_ = store.article("reports/weekly").compilations[0].digest
    store.delete_compilation("reports/weekly", "reports/weekly")
    for heading in ("Summary", "Next"):  # one part per heading
        store.write_compilation(Compilation("reports/weekly", heading, heading.lower(), "reports/weekly", digest=stamp_))
    assert compile_article(AGENT, agent_run=ScriptedRun(lambda p: "x"), **common).skipped  # no early run
    assert body(store.article("reports/weekly")) == "## Summary\n\nsummary\n\n## Next\n\nnext"
    items.append("two")
    compile_article(AGENT, agent_run=ScriptedRun(lambda p: "## Summary\n\nnew"), **common)
    assert [(s.heading, s.whole) for s in store.article("reports/weekly").compilations] == [("reports/weekly", True)]


def test_an_article_created_while_the_run_was_under_way_still_gets_its_text(tmp_path: Path) -> None:
    store, _, _, common = setup(tmp_path)

    def write(prompt: str) -> dict[str, str]:
        store.create(Identity("reports/weekly", "reports", "Weekly"))
        return {"Summary": "landed [^r0]"}

    compiled = compile_article(AGENT, agent_run=ScriptedRun(write), **common)
    assert compiled.article is not None and body(compiled.article) == "## Summary\n\nlanded [^r0]"


def test_what_a_failed_run_spent_reaches_the_ledger(tmp_path: Path) -> None:
    from cerebro.budget import Caps, Ledger
    from cerebro.models import ModelSpec
    from cerebro.run import Run, Usage

    class Spent(ScriptedRun):
        def run(self, *args: object) -> Run:
            return Run("run_x", "reports/weekly", "reports/weekly", "budget spent",
                       usage=Usage("glm-5p3", 3, 135_000, 2_000))

    class Raises(ScriptedRun):
        def run(self, *args: object) -> Run:
            raise RuntimeError("the loop broke")

    store, _, _, common = setup(tmp_path)
    ledger = Ledger(store, Caps())
    model = ModelSpec("fireworks", "glm-5p3", "", "compilation")
    assert compile_article(AGENT, agent_run=Spent(lambda p: {}), ledger=ledger, model=model, **common).article is None
    rows = [row.data for row in store.rows("spend")]
    assert rows[0]["run"] == "run_x" and rows[0]["usd"] > 0
    try:
        compile_article(AGENT, agent_run=Raises(lambda p: {}), ledger=ledger, model=model, **common)
    except RuntimeError:
        pass
    assert len(store.rows("spend")) == 2


def test_a_run_whose_budget_ran_out_lands_the_text_it_handed_in_and_runs_again_next_pass(tmp_path: Path) -> None:
    class Short(ScriptedRun):
        def run(self, *args: object) -> Run:
            done = super().run(*args)
            done.status, done.note = "budget spent", "the run holds 8000 tokens, so the text it handed in lands"
            return done

    _, _, _, common = setup(tmp_path)
    compiled = compile_article(AGENT, agent_run=Short(lambda p: "landed [^r0]"), **common)
    assert compiled.article is not None and body(compiled.article) == "landed [^r0]"
    assert "its run budget spent" in compiled.notes[0]
    again = compile_article(AGENT, agent_run=ScriptedRun(lambda p: "done [^r0]"), **common)
    assert not again.skipped and body(again.article) == "done [^r0]"  # not marked read: it runs again


def test_a_sentence_scored_under_min_grounding_lands_whole_with_its_mark_and_the_article_defines_it_once(
        tmp_path: Path) -> None:
    from cerebro.article import UNSUPPORTED, markdown
    from cerebro.fakes import FakeDecider

    store, _, _, common = setup(tmp_path)
    decider = FakeDecider({"s2": 2})  # the second sentence a reframe its facts do not carry, the rest backed
    text = "One shipped [^r0]. Two moved because one shipped [^r0].\n\n## Next\n\nThree waits [^r0]."
    compiled = compile_article(AGENT, agent_run=ScriptedRun(lambda p: text), decider=decider, **common)
    assert body(compiled.article) == (f"One shipped [^r0]. Two moved because one shipped [^r0].[^unsupported]\n\n"
                                      f"## Next\n\nThree waits [^r0].\n\n{UNSUPPORTED}")
    assert markdown(compiled.article).count("[^unsupported]:") == 1 and compiled.dropped == []
    assert len(decider.asked) == 2  # one request a passage
    notes = [r.data for r in store.rows("agent-runs") if r.data.get("run") == compiled.run.id][-1]["notes"]
    assert '(before the first heading): 0.50 "Two moved because one shipped [^r0].": a reframe' in notes[1]


BRIEF = """---
subagents: [risks, people]
---

A brief on one account, for its owner.

# Composing

Write a verdict.
"""
JOINED = """---
subagents: [risks, people]
---

A brief on one account, for its owner.
"""


def part(heading: str) -> str:
    return f"---\nevidence:\n  - items: {{}}\n---\n\n# Instructions\n\nDraft it.\n\n# Output\n\n## {heading}\n"


def workspace(tmp_path: Path, files: dict[str, str]) -> dict[str, Any]:
    for name, text in files.items():
        path = tmp_path / "agents" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    agents, refused = load_agents(tmp_path / "agents")
    assert not refused
    return {agent.id: agent for agent in agents}


class ByAgent(AgentRun):
    """Hands each run to the stand-in named for its agent, and keeps the ids of the agents run."""

    def __init__(self, runs: dict[str, AgentRun]):
        self.runs = runs
        self.ran: list[str] = []

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Budget) -> Run:
        self.ran.append(agent.id)
        return self.runs[agent.id].run(agent, evidence, tools, budget)


class Fails(AgentRun):
    def run(self, agent: Any, evidence: Any, tools: Any, budget: Budget) -> Run:
        return Run("run_f", agent.id, agent.target, "failed", note="the provider refused it")


def briefs(tmp_path: Path, composer: str = BRIEF) -> tuple[dict[str, Any], MemoryStore, list[str], dict[str, Any]]:
    agents = workspace(tmp_path, {"briefs/article.md": composer, "briefs/subagents/risks.md": part("Risks"),
                              "briefs/subagents/people.md": part("People")})
    store, items, _, common = setup(tmp_path)
    store.create(Identity("briefs/alder", "briefs", "Alder"))
    return agents, store, items, common


def test_a_part_a_composer_hands_in_as_its_subagent_returned_it_keeps_its_scores_with_no_new_judge_call(
        tmp_path: Path) -> None:
    from cerebro.fakes import FakeDecider

    agents, _store, _items, common = briefs(tmp_path)
    decider = FakeDecider({"s1": 0})  # each passage's first sentence made up
    drafts = {"briefs/subagents/risks": ScriptedRun(lambda p: "Risks one [^r0]."),
              "briefs/subagents/people": ScriptedRun(lambda p: "People one [^r0].")}

    def composes(prompt: str) -> str:  # its subagents' content as they returned it, marks and all, and a verdict
        risks = re.search(r"### Risks, from briefs/subagents/risks\n\s*\n\s*(.+)", prompt)
        return f"## Risks\n\n{risks.group(1).strip()}\n\n## Verdict\n\nAlder holds [^r0]."  # type: ignore[union-attr]

    run = ByAgent({**drafts, "briefs/article": ScriptedRun(composes)})
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", decider=decider, **common)
    asked = [[s["sentence"] for s in state["passage"]] for state, _ in decider.asked]
    assert sorted(asked) == [["Alder holds."], ["People one."], ["Risks one."]]  # the Risks passage once, as returned
    assert body(compiled.article) == ("## Risks\n\nRisks one [^r0].[^unsupported]\n\n## Verdict\n\nAlder holds [^r0]."
                                      "[^unsupported]\n\n[^unsupported]: Not fully backed by the facts it cites.")
    risks = tmp_path / "agents/briefs/subagents/risks.md"
    risks.write_text(risks.read_text() + "\nSay more.\n")  # risks alone runs again, returning what it did before
    decider.asked.clear()
    agents = workspace(tmp_path, {})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", decider=decider, **common,
                    subagents=list(agents.values()))
    assert [[s["sentence"] for s in state["passage"]] for state, _ in decider.asked] == [["Risks one."]]


def test_an_agent_with_subagents_and_no_instructions_of_its_own_joins_their_content_with_no_model(
        tmp_path: Path) -> None:
    agents, store, _items, common = briefs(tmp_path, JOINED)
    both, seen = threading.Barrier(2, timeout=5), {}

    def draft(heading: str) -> Callable[[str], dict[str, str]]:
        def write(prompt: str) -> dict[str, str]:
            seen[heading] = prompt
            both.wait()  # each runs while the other does, so they run side by side or not at all
            return {heading: f"{heading} returned [^r0]."}
        return write

    run = ByAgent({"briefs/subagents/risks": ScriptedRun(draft("Risks")),
                   "briefs/subagents/people": ScriptedRun(draft("People"))})
    composer = agents["briefs/article"]
    assert composer.own == "" and composer.context == "A brief on one account, for its owner."
    compiled = compile_article(composer, agent_run=run, this="briefs/alder", **common)
    assert sorted(run.ran) == ["briefs/subagents/people", "briefs/subagents/risks"]  # and no run of its own
    assert [(s.heading, s.by) for s in compiled.article.compilations] == [("Risks", "briefs/subagents/risks"),
                                                                      ("People", "briefs/subagents/people")]
    assert body(compiled.article) == "## Risks\n\nRisks returned [^r0].\n\n## People\n\nPeople returned [^r0]."
    assert all(s.citations and s.citations[0].id == "r0" for s in compiled.article.compilations)
    assert "A brief on one account, for its owner." in seen["Risks"] and "The wiki puts your content under the " \
        "heading Risks" in seen["Risks"]
    assert {row.data["target"] for row in store.rows("agent-runs")} == {"briefs/alder"}
    assert compile_article(composer, agent_run=run, this="briefs/alder", **common).skipped
    reordered = replace(composer, subagents=["people", "risks"],
                        subagent_ids=["briefs/subagents/people", "briefs/subagents/risks"])
    run.ran.clear()
    again = compile_article(reordered, agent_run=run, this="briefs/alder", **common,
                            subagents=list(agents.values()))
    assert run.ran == [] and [s.heading for s in again.article.compilations] == ["People", "Risks"]  # no run, no judge
    assert compile_article(reordered, agent_run=run, this="briefs/alder", **common,
                           subagents=list(agents.values())).skipped


def test_only_a_subagent_whose_evidence_moved_runs_again_and_the_others_parts_stand_as_they_were(
        tmp_path: Path) -> None:
    agents, store, _items, common = briefs(tmp_path, JOINED)
    calls = {"risks": 0, "people": 0}

    def draft(name: str, heading: str) -> ScriptedRun:
        def write(prompt: str) -> str:
            calls[name] += 1
            return f"{heading} {calls[name]} [^r0]."
        return ScriptedRun(write)

    run = ByAgent({"briefs/subagents/risks": draft("risks", "Risks"),
                   "briefs/subagents/people": draft("people", "People")})
    composer = agents["briefs/article"]
    compile_article(composer, agent_run=run, this="briefs/alder", **common)
    people = next(s for s in store.article("briefs/alder").compilations if s.heading == "People")
    risks_file = tmp_path / "agents" / "briefs" / "subagents" / "risks.md"
    risks_file.write_text(risks_file.read_text() + "\nSay more.\n")  # its file moved, so it alone is due
    run.ran.clear()
    compile_article(composer, agent_run=run, this="briefs/alder", **common)
    assert run.ran == ["briefs/subagents/risks"] and calls == {"risks": 2, "people": 1}
    assert body(store.article("briefs/alder")) == "## Risks\n\nRisks 2 [^r0].\n\n## People\n\nPeople 1 [^r0]."
    assert next(s for s in store.article("briefs/alder").compilations if s.heading == "People") == people
    run.ran.clear()
    compile_article(composer, agent_run=run, this="briefs/alder", **common, scheduled=True)  # by hand: every one
    assert sorted(run.ran) == ["briefs/subagents/people", "briefs/subagents/risks"]
    run.ran.clear()
    compile_article(composer, agent_run=run, this="briefs/alder", **common, scheduled=True, only="briefs/subagents/people")
    assert run.ran == ["briefs/subagents/people"]  # one subagent by hand: that one alone
    assert body(store.article("briefs/alder")).startswith("## Risks\n\nRisks 3 [^r0].")


def test_an_agent_with_instructions_of_its_own_runs_after_its_subagents_with_their_content_and_its_text_is_the_article(
        tmp_path: Path) -> None:
    agents, _store, items, common = briefs(tmp_path)
    seen: dict[str, str] = {}

    def compose(prompt: str) -> str:
        seen["composer"] = prompt
        return "## Verdict\n\nAlder holds [^r0]."

    drafts = {id: ScriptedRun(lambda p, h=h: {h: f"{h} returned [^r0]."})
              for id, h in (("briefs/subagents/risks", "Risks"), ("briefs/subagents/people", "People"))}
    sets = ScriptedRun(compose, calls=[("override", {"field": "level", "value": "high", "cites": ["r0"]}),
                                       ("override", {"field": "id", "value": "elsewhere"})])
    run = ByAgent({**drafts, "briefs/article": sets})
    composer = agents["briefs/article"]
    assert composer.own == "# Composing\n\nWrite a verdict."
    compiled = compile_article(composer, agent_run=run, this="briefs/alder", **common)
    assert run.ran[-1] == "briefs/article" and DRAFTS in seen["composer"]
    assert "### People, from briefs/subagents/people" in seen["composer"] and "People returned [^r0]." in seen["composer"]
    assert "Write a verdict." in seen["composer"] and "Write a verdict." not in drafts_prompt(run, agents, common)
    assert [(s.heading, s.by, s.whole) for s in compiled.article.compilations] == [
        ("briefs/article", "briefs/article", True)]
    assert body(compiled.article) == "## Verdict\n\nAlder holds [^r0]."
    assert "not yours to set" in [e.text for e in compiled.run.transcript if e.role == "tool"][1]
    assert compiled.article.properties["level"].value == "high"
    assert compiled.article.properties["level"].by.name == "briefs/article"
    run.ran.clear()
    assert compile_article(composer, agent_run=run, this="briefs/alder", **common).skipped and run.ran == []
    items.append("two")  # every subagent's evidence moved: each runs again, and then the agent itself
    stands = ByAgent({**drafts, "briefs/article": ScriptedRun(lambda p: "")})  # it returns nothing: what it composed
    compiled = compile_article(composer, agent_run=stands, this="briefs/alder", **common)  # last gives way to the
    assert stands.ran[-1] == "briefs/article" and compiled.run.status == "wrote nothing"  # drafts
    assert body(common["store"].article("briefs/alder")) == ("## Risks\n\nRisks returned [^r0].\n\n"
                                                             "## People\n\nPeople returned [^r0].")


def drafts_prompt(run: ByAgent, agents: dict[str, Any], common: dict[str, Any]) -> str:
    """The prompt the risks subagent reads inside its composer's run."""
    seen: list[str] = []
    probe = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: seen.append(p) or "x [^r0]."),
                     "briefs/subagents/people": ScriptedRun(lambda p: "y [^r0]."),
                     "briefs/article": ScriptedRun(lambda p: "")})
    compile_article(agents["briefs/article"], agent_run=probe, this="briefs/alder", **common, scheduled=True)
    return seen[0]


class Ticking:
    """A clock a test moves on by hand."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def ahead(self, seconds: float) -> None:
        from datetime import timedelta

        self.now += timedelta(seconds=seconds)


def test_a_subagent_that_fails_fails_the_articles_run_and_nothing_of_it_lands(tmp_path: Path) -> None:
    agents, store, _items, common = briefs(tmp_path, JOINED)
    clock = common["clock"] = Ticking()
    people = ScriptedRun(lambda p: {"People": "Ann decides [^r0]."})
    broken = ByAgent({"briefs/subagents/risks": Fails(), "briefs/subagents/people": people})
    composer = agents["briefs/article"]
    compiled = compile_article(composer, agent_run=broken, this="briefs/alder", **common)
    assert compiled.article is None and store.article("briefs/alder").compilations == []  # nothing of it landed
    assert compiled.run.status == "failed"
    assert compiled.run.note == ("a subagent's run failed, so nothing of the article's run lands: "
                                 "briefs/subagents/risks: run run_f failed: the provider refused it")
    last = {row.data["agent"]: row.data for row in store.latest("agent-runs", key="agent")}["briefs/article"]
    assert (last["status"], last["note"]) == ("failed", compiled.run.note)
    broken.ran.clear()
    waits = compile_article(composer, agent_run=broken, this="briefs/alder", **common)
    assert waits.skipped and broken.ran == []  # the article's run waits as a whole after the failure
    fixed_ = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "None [^r0]."), "briefs/subagents/people": people})
    clock.ahead(30)  # past the wait after one failure
    compile_article(composer, agent_run=fixed_, this="briefs/alder", **common)
    assert sorted(fixed_.ran) == ["briefs/subagents/people", "briefs/subagents/risks"]  # what it returned was not kept
    assert [s.heading for s in store.article("briefs/alder").compilations] == ["Risks", "People"]


def test_an_agent_with_instructions_that_fails_lands_nothing_of_the_articles_run_and_leaves_its_last_article(
        tmp_path: Path) -> None:
    agents, store, items, common = briefs(tmp_path)
    common["clock"] = Ticking()
    drafts = {"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
              "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0].")}
    composer = agents["briefs/article"]
    compiled = compile_article(composer, agent_run=ByAgent({**drafts, "briefs/article": Fails()}), this="briefs/alder",
                               **common)
    assert compiled.article is None and compiled.run is not None and compiled.run.status == "failed"
    assert not store.article("briefs/alder").compilations  # its subagents' content waits for the run that runs again
    common["clock"].ahead(30)
    compile_article(composer, agent_run=ByAgent({**drafts, "briefs/article": ScriptedRun(lambda p: "V [^r0].")}),
                    this="briefs/alder", **common)
    items.append("two")
    compile_article(composer, agent_run=ByAgent({**drafts, "briefs/article": Fails()}), this="briefs/alder", **common)
    assert body(store.article("briefs/alder")) == "V [^r0]."


def test_an_article_agent_writes_each_article_targets_lists_one_compile_each(tmp_path: Path) -> None:
    agents = workspace(tmp_path, {"calls/article.md": part("Summary").replace("# Output", "# Notes"),
                              "meetings/prep.md": "---\ntarget: { from: meetings, status: upcoming }\n---\n\nPrep.\n"})
    store, _, _, common = setup(tmp_path)
    for id in ("calls/one", "calls/two", "meetings/soon", "meetings/done"):
        store.create(Identity(id, id.split("/")[0], id))
    store.set_identity(Identity("meetings/done", "meetings", "done", status="past"))
    store.set_identity(Identity("meetings/soon", "meetings", "soon", status="upcoming"))
    lookup = common["lookup"]
    assert targets(agents["calls/article"], lookup) == ["calls/one", "calls/two"]
    assert targets(agents["meetings/prep"], lookup) == ["meetings/soon"]
    with pytest.raises(CompileError, match="agents/calls/article.md writes each article of its collection, calls"):
        compile_article(agents["calls/article"], agent_run=ScriptedRun(lambda p: {}), **common)
    run = ScriptedRun(lambda p: {"Summary": "It went well [^r0]."})
    compiled = compile_article(agents["calls/article"], agent_run=run, this="calls/two", **common)
    assert compiled.article.identity.id == "calls/two" and compiled.run.target == "calls/two"
    assert store.article("calls/one").compilations == []
    with pytest.raises(CompileError, match="articles/calls/three.md, which does not exist"):
        compile_article(agents["calls/article"], agent_run=run, this="calls/three", **common)


def test_a_curator_and_a_subagent_are_refused_with_a_line_that_names_the_file(tmp_path: Path) -> None:
    agents = workspace(tmp_path, {"briefs/article.md": BRIEF, "briefs/subagents/risks.md": part("Risks"),
                              "briefs/subagents/people.md": part("People"),
                              "briefs/curator.md": "---\nheartbeat: [manual]\n---\n\nKeep briefs.\n"})
    _, _, _, common = setup(tmp_path)
    run = ScriptedRun(lambda p: {})
    for id, line in (("briefs/curator", "agents/briefs/curator.md is a curator"),
                     ("briefs/subagents/risks", "inside the run of agents/briefs/article.md: compile that agent")):
        with pytest.raises(CompileRefused, match=line):
            compile_article(agents[id], agent_run=run, **common)


def test_a_tool_the_file_names_reaches_the_run_bound_to_its_caller_and_the_workspace(tmp_path: Path) -> None:
    def whoami(note: str, caller: Caller, workspace: Workspace) -> str:
        """Who is asking."""
        return f"{caller.name} asks {note} of a {type(workspace).__name__}"

    agents = workspace(tmp_path, {"reports/weekly.md": "---\ntools: [whoami]\n---\n\nWrite it.\n"})
    store, _, _, common = setup(tmp_path)
    seen: list[Callable[..., Any]] = []

    class Keeps(ScriptedRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Budget) -> Run:
            seen.extend(tools)
            return super().run(agent, evidence, tools, budget)

    handle = Workspace(common["lookup"], store, ["note"], lambda: None)
    run = Keeps(lambda p: {"Summary": "s"}, calls=[("whoami", {"note": "x"})])
    common |= {"agent_tools": {"whoami": whoami}, "workspace": handle}
    compiled = compile_article(agents["reports/weekly"], agent_run=run, **common)
    import inspect
    bound = next(tool for tool in seen if tool.__name__ == "whoami")
    assert list(inspect.signature(bound).parameters) == ["note"]
    assert "reports/weekly asks x of a Workspace" in [e.text for e in compiled.run.transcript if e.role == "tool"]


def test_each_gtm_template_file_that_writes_an_article_compiles(tmp_path: Path) -> None:
    from conftest import GTM

    from cerebro.evidence import NotBuilt
    from cerebro.fakes import FakeExtraction, MemoryStore
    from cerebro.lookup import StoreLookup

    agents, refused = load_agents(GTM)
    assert not refused
    store, extraction = MemoryStore(), FakeExtraction()
    lookup = StoreLookup(store, extraction)
    store.create(Identity("accounts/acme", "accounts", "Acme"))
    for id in ("people/ann", "calls/acme-qbr", "knowledge/pilot", "open-pipeline/acme-deal",
               "risk-radar/acme", "opportunities/acme-deal"):
        store.create(Identity(id, id.split("/")[0], id))
    from cerebro.article import Property

    for id, linked in (("open-pipeline/acme-deal", ["opportunities/acme-deal", "accounts/acme"]),
                       ("risk-radar/acme", ["accounts/acme"])):
        store.set_field(id, "linked", Property(linked, Caller("t", "person"), "2026-11-01T00:00:00Z"))
    store.set_field("calls/acme-qbr", "start", Property("2099-01-01T09:00:00Z", Caller("calls/curator", "agent"),
                                                        "2026-11-01T00:00:00Z"))  # upcoming, as its curator admitted it
    store.set_field("calls/acme-qbr", "account", Property("accounts/acme", Caller("t", "person"), "2026-11-01T00:00:00Z"))
    from cerebro.connector import Record

    note = Record(id="local/acme.md", source="local", revision="1", at="2026-11-10T09:00:00Z",
                  body="Ann said Acme renewed its pilot.")
    for fact in extraction.extract(note).facts:  # one fact on each article, so each agent has something to cite
        store.link(fact.id, [identity.id for c in store.collections() for identity in
                             (a.identity for a in store.query(c))])
    run = ScriptedRun(lambda p: {"Summary": "Nothing yet."})
    wrote, not_built, waited = [], [], []
    for agent in agents:
        if agent.role not in ("writer", "article", "composer", "query"):
            continue
        for article in targets(agent, lookup)[:1]:
            try:
                compiled = compile_article(agent, lookup=lookup, store=store, agent_run=run, root=tmp_path,
                                           functions={}, agent_tools={}, this=article, subagents=agents)
            except NotBuilt as error:
                assert f"agents/{agent.id}.md" in str(error)
                not_built.append(agent.id)
                continue
            if compiled.waiting:
                waited.append(agent.id)
                continue
            assert compiled.article is not None, agent.id
            wrote.append(agent.id)
    assert {"accounts/article", "people/article", "risk-radar/article", "open-pipeline/article", "calls/article",
            "knowledge/article", "digests/index", "opportunities/article"} <= set(wrote)
    assert waited == [] and "reports/pipeline" in wrote  # no open deal in this quarter: it is run all the same
    assert not_built == []  # every evidence line the template uses is built


def test_a_persons_instruction_reaches_the_writer_at_its_next_run(tmp_path: Path) -> None:
    _, _, prompts, common = setup(tmp_path)
    run = ScriptedRun(lambda p: prompts.append(p) or {"Summary": "s", "Next": "n"})
    compile_article(AGENT, agent_run=run, **common)
    assert compile_article(AGENT, agent_run=run, **common).skipped
    common["lookup"].tell(Caller("you", "person"), "Lead with the risk.", "reports/weekly", as_="proposal")
    assert compile_article(AGENT, agent_run=run, **common).run is not None  # its arrival makes the agent due
    assert "What people asked of you" in prompts[-1] and "instruction from you (person)" in prompts[-1]
    assert "Lead with the risk." in prompts[-1]


def test_a_citation_of_a_persons_instruction_is_found_and_one_of_nothing_lands_shown_as_not_found(
        tmp_path: Path) -> None:
    from cerebro.article import markdown

    store, _, _, common = setup(tmp_path)
    store.create(Identity("reports/weekly", "reports", "Weekly"))
    common["lookup"].tell(Caller("you", "person"), "Lead with the risk.", "reports/weekly", as_="proposal")

    def write(prompt: str) -> dict[str, str]:
        asked = re.search(r"\[\^(\S+?)\] instruction from you", prompt)
        return {"Summary": f"The risk leads [^{asked.group(1)}]. It slipped [^nope]."}  # type: ignore[union-attr]

    compiled = compile_article(AGENT, agent_run=ScriptedRun(write), **common)
    [part] = compiled.article.compilations
    assert [c.kind for c in part.citations] == ["proposal"] and "It slipped [^nope]." in body(compiled.article)
    assert "[^nope]: not found" in markdown(compiled.article)


def test_a_composer_stopped_by_the_cap_leaves_the_paid_drafts_standing(tmp_path: Path) -> None:
    from cerebro.budget import BudgetSpent

    agents = workspace(tmp_path, {"briefs/article.md": BRIEF, "briefs/subagents/risks.md": part("Risks"),
                              "briefs/subagents/people.md": part("People")})
    store, _, _, common = setup(tmp_path)
    store.create(Identity("briefs/alder", "briefs", "Alder"))

    class Capped:  # the cap is reached by the drafts, so the composer's own run is refused
        def __init__(self) -> None:
            self.checks = 0

        def reserve(self, *args: Any, **kwargs: Any) -> int:
            self.checks += 1
            if self.checks > 2:
                raise BudgetSpent("compilation spent its cap")
            return self.checks

        def record(self, *args: Any) -> None:
            pass

        def release(self, ticket: int) -> None:
            pass

    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: {"Risks": "R [^r0]."}),
                   "briefs/subagents/people": ScriptedRun(lambda p: {"People": "P [^r0]."}),
                   "briefs/article": ScriptedRun(lambda p: {})})
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", ledger=Capped(),
                               model=None, **common)
    assert [s.heading for s in compiled.article.compilations] == ["Risks", "People"]
    assert compiled.run.status == "budget spent" and "did not run to its end" in compiled.notes[0]


def test_an_agents_override_replaces_the_value_its_field_held_its_curators_too(tmp_path: Path) -> None:
    from cerebro.article import Property

    store, _, _, common = setup(tmp_path)
    sets = ScriptedRun(lambda p: {"Summary": "s"}, calls=[("override", {"field": "level", "value": "high"})])
    compile_article(AGENT, agent_run=sets, **common)  # its first build, which makes the article
    store.set_field("reports/weekly", "level", Property("low", Caller("reports/curator", "agent"),
                                                        "2026-11-01T09:00:00Z"))  # as its curator admitted it again
    common["functions"]["items"] = lambda: [{"id": "r0", "text": "moved"}]
    compiled = compile_article(AGENT, agent_run=sets, **common)
    assert next(e.text for e in compiled.run.transcript if e.role == "tool") == "set level"
    level = store.article("reports/weekly").properties["level"]
    assert level.value == "high" and level.by == Caller(AGENT.id, "agent")


def test_an_agent_files_min_grounding_takes_the_place_of_the_workspaces_and_the_workspaces_of_the_default(
        tmp_path: Path) -> None:
    from cerebro.app import Cerebro
    from cerebro.fakes import FakeDecider

    (tmp_path / "agents" / "reports").mkdir(parents=True)
    text = "One shipped [^r0]."
    for floor, settings, own, marked in ((0.6, "{}\n", "", False), (0.9, "min_grounding: 0.9\n", "", True),
                                         (0.9, "min_grounding: 0.9\n", "min_grounding: 0.7\n", False)):
        (tmp_path / "settings.yaml").write_text(settings)
        (tmp_path / "agents" / "reports" / "weekly.md").write_text(
            f"---\nheartbeat: [change]\n{own}---\n\nWrite it.\n\n# Output\n\n## Summary\n")
        with Cerebro(tmp_path, store=MemoryStore(), decider=FakeDecider({"s1": 3}), trusted=True,
                     agent_run=ScriptedRun(lambda p: text)) as workspace:  # a fair reading: 0.8
            compiled = workspace.compile(workspace.agent("reports/weekly"))
        assert ("[^unsupported]" in body(compiled.article)) == marked, (floor, own)


def test_grounding_past_the_cap_asks_no_model_and_leaves_each_sentence_unscored(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from cerebro.budget import BudgetSpent
    from cerebro.compile import _Grounding
    from cerebro.fakes import FakeDecider
    from cerebro.models import DEFAULTS

    class Spent:
        def check(self, *args: Any) -> None:
            raise BudgetSpent("compilation spent its cap")

    _, _, _, common = setup(tmp_path)
    decider = FakeDecider({"s1": 0})
    grounding = _Grounding("reports/weekly", lookup=common["lookup"], root=tmp_path, ledger=Spent(),
                           clock=lambda: datetime(2026, 11, 17, tzinfo=UTC), decider=decider,
                           decision=DEFAULTS["decision"], zone=UTC, floor=0.6)
    editor = grounding.editor(AGENT, list)
    assert editor.marked("One [^r0].") == "One [^r0]." and decider.asked == []
    [line] = editor.scores
    assert line == '(before the first heading): unscored "One [^r0].": compilation spent its cap; it cites [^r0], not ' \
        'found in what was read'


def test_grounding_reads_more_than_twenty_cited_facts_in_more_than_one_call(tmp_path: Path) -> None:
    from cerebro.compile import _Grounding
    from cerebro.lookup import MAX_READ

    _, _, _, common = setup(tmp_path)
    grounding = _Grounding("reports/weekly", lookup=common["lookup"], root=tmp_path, ledger=None,
                           clock=lambda: datetime(2026, 11, 17, tzinfo=UTC), decider=None, decision=None, zone=UTC,
                           floor=0.6)
    many = [f"fact_{n:02d}" for n in range(MAX_READ + 1)]
    assert grounding.stale(dict.fromkeys(many)) == {}  # type: ignore[arg-type]


def _held_grounding(tmp_path: Path, decider: Any, cap: float) -> tuple[Any, Any]:
    from datetime import UTC, datetime

    from cerebro.budget import Caps, Ledger
    from cerebro.compile import _Grounding
    from cerebro.models import DEFAULTS

    _, _, _, common = setup(tmp_path)
    ledger = Ledger(MemoryStore(), Caps(compilation=cap))
    grounding = _Grounding("reports/weekly", lookup=common["lookup"], root=tmp_path, ledger=ledger,
                           clock=lambda: datetime(2026, 11, 17, tzinfo=UTC), decider=decider,
                           decision=DEFAULTS["decision"], zone=UTC, floor=0.6)
    return grounding, ledger


class _CostlyThenFails:
    """A decider whose first call spends a million input tokens and whose later calls raise."""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, state: Any, questions: Any) -> Any:
        from cerebro.fakes import FakeDecider
        from cerebro.models import Decision, DecisionError, TokenCount

        self.calls += 1
        if self.calls > 1:
            raise DecisionError("the model went away")
        return Decision(FakeDecider().decide(state, questions).answers, "fake", TokenCount(1_000_000, 0))


TWO_PASSAGES = "## One\n\nBeta shipped [^r0].\n\n## Two\n\nGamma slipped [^r1]."


def test_grounding_records_each_judge_call_as_it_returns_so_a_later_call_that_fails_loses_none(
        tmp_path: Path) -> None:
    grounding, ledger = _held_grounding(tmp_path, _CostlyThenFails(), cap=100.0)
    editor = grounding.editor(AGENT, list)
    assert editor.marked(TWO_PASSAGES) == TWO_PASSAGES
    assert ledger.spent(datetime(2026, 11, 17, tzinfo=UTC), "compilation") == pytest.approx(0.042)
    assert "unscored" in editor.scores[1] and "the model went away" in editor.scores[1]


def test_grounding_checks_the_cap_before_each_judge_call_and_asks_nothing_once_it_is_spent(
        tmp_path: Path) -> None:
    decider = _CostlyThenFails()  # its one call spends past the cap, so the second passage asks no model
    grounding, _ = _held_grounding(tmp_path, decider, cap=0.01)
    editor = grounding.editor(AGENT, list)
    assert editor.marked(TWO_PASSAGES) == TWO_PASSAGES and decider.calls == 1
    assert "unscored" in editor.scores[1] and "cap" in editor.scores[1]


def test_a_run_that_lands_nothing_leaves_the_text_it_wrote_before_as_it_was_though_its_evidence_went(
        tmp_path: Path) -> None:
    from cerebro.compile import KEPT

    store, items, _, common = setup(tmp_path)
    items[0] = "one item"
    compile_article(AGENT, agent_run=ScriptedRun(lambda p: {"Summary": "One item [^r0]."}), **common)
    before = store.article("reports/weekly").compilations
    common["functions"]["items"] = lambda: [{"id": "r1", "text": "another thing"}]  # r0 is no longer read
    compiled = compile_article(AGENT, agent_run=ScriptedRun(lambda p: {}), **common)
    # a body its run returned nothing for is kept: only a retracted fact's line goes
    assert store.article("reports/weekly").compilations == before and compiled.article is None
    assert compiled.notes == [KEPT] and compiled.run.status == "wrote nothing"


def test_a_subagents_override_lands_under_its_own_name(tmp_path: Path) -> None:
    agents = workspace(tmp_path, {"briefs/article.md": BRIEF, "briefs/subagents/risks.md": part("Risks"),
                              "briefs/subagents/people.md": part("People")})
    store, _, _, common = setup(tmp_path)
    store.create(Identity("briefs/alder", "briefs", "Alder"))
    risks = ScriptedRun(lambda p: {"Risks": "R [^r0]."}, calls=[("override", {"field": "level", "value": "high"})])
    run = ByAgent({"briefs/subagents/risks": risks, "briefs/subagents/people": ScriptedRun(lambda p: {}),
                   "briefs/article": ScriptedRun(lambda p: {})})
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    level = compiled.article.properties["level"]
    assert level.value == "high" and level.by.name == "briefs/subagents/risks"


def test_an_evidence_function_gets_the_workspace_handle_and_grounding_reads_the_articles_facts_whole(tmp_path: Path) -> None:
    from cerebro.article import Property
    from cerebro.facts import Fact, Quote
    from cerebro.fakes import FakeDecider, FakeExtraction

    store, facts = MemoryStore(), FakeExtraction()
    fact = Fact("f_1", "The renewal moved to May.", "Alder", "note", "2026-11-10T09:00:00Z", "notes", "n1",
                [Quote("We moved it to May.", "Ann")])
    facts.by_record["n1"] = [fact]
    store.create(Identity("reports/weekly", "reports", "Weekly"))
    store.link("f_1", ["reports/weekly"])
    store.set_field("reports/weekly", "owner", Property("Ann", Caller("you", "person"), "2026-11-01T00:00:00Z"))
    seen: list[Any] = []

    def items(workspace: Workspace) -> list[dict[str, str]]:
        """Items."""
        seen.append(workspace)
        return [{"id": "f_1", "text": "The renewal moved to May."}]

    handle, decider = Workspace(StoreLookup(store, facts), store, [], lambda: None), FakeDecider()
    compile_article(AGENT, lookup=StoreLookup(store, facts), store=store, root=tmp_path, functions={"items": items},
                    agent_tools={}, workspace=handle, decider=decider,
                    agent_run=ScriptedRun(lambda p: {"Summary": "The renewal moved to May [^f_1]."}))
    assert seen == [handle]
    state = next(state for state, _ in decider.asked if state.get("facts"))
    assert state["facts"]["f_1"]["about"] == "Alder" and state["facts"]["f_1"]["speaker"] == "Ann"


def test_grounding_reads_a_cited_fact_whole_when_it_is_not_linked_to_the_article(tmp_path: Path) -> None:
    from cerebro.facts import Fact, Quote
    from cerebro.fakes import FakeDecider, FakeExtraction

    store, facts = MemoryStore(), FakeExtraction()
    paragraph = "Ann opened the call. We moved it to May. Bo asked about seats."
    facts.by_record["n1"] = [Fact("f_1", "The renewal moved to May.", "Alder", "note", "2026-11-10T09:00:00Z",
                                  "notes", "n1", [Quote(paragraph, "Ann")])]
    store.create(Identity("reports/weekly", "reports", "Weekly"))  # f_1 is linked to no article
    decider = FakeDecider()
    compile_article(AGENT, lookup=StoreLookup(store, facts), store=store, root=tmp_path, agent_tools={},
                    functions={"items": lambda: [{"id": "f_1", "text": f'The renewal moved to May. (quote: "{paragraph}")'}]},
                    decider=decider, agent_run=ScriptedRun(lambda p: {"Summary": "The renewal moved to May [^f_1]."}))
    state = next(state for state, _ in decider.asked if state.get("facts"))
    assert state["facts"]["f_1"]["text"] == "The renewal moved to May."
    assert state["facts"]["f_1"]["stated_on"] == "Nov 10, 2026" and state["facts"]["f_1"]["about"] == "Alder"


def test_a_run_is_held_at_twice_its_agents_last_spend_and_six_side_by_side_still_cannot_pass_the_cap() -> None:
    from cerebro.budget import BudgetSpent, Caps, Ledger
    from cerebro.compile import _estimate
    from cerebro.models import DEFAULTS, TokenCount

    store = MemoryStore()
    ledger = Ledger(store, Caps(total=1.0))
    model, now = DEFAULTS["compilation"], datetime(2026, 11, 18, 9, tzinfo=UTC)
    agents = [replace(AGENT, id=f"reports/r{n}", budget=Budget(tokens_per_run=2_000_000)) for n in range(6)]
    assert _estimate(store, agents[0], [], None) is None  # with no price, a first run is held at its whole budget
    for n, agent in enumerate(agents):  # each agent's last run, yesterday, cost $0.10
        store.append("agent-runs", {"run": f"run_{n}", "agent": agent.id})
        ledger.record("compilation", datetime(2026, 11, 17, 9, tzinfo=UTC), model, TokenCount(71_428, 0), f"run_{n}")
    held = []
    for agent in agents:
        try:
            held.append(ledger.reserve("compilation", now, model, agent.budget.tokens_per_run,
                                       usd=_estimate(store, agent, [], model)))
        except BudgetSpent:
            pass
    assert len(held) == 5  # five held at $0.20 each fill the $1 cap, and the sixth waits
    with pytest.raises(BudgetSpent):  # held at its whole budget, $2.80, a run waits while any other is under way
        ledger.reserve("compilation", now, model, agents[0].budget.tokens_per_run)


def test_a_run_is_held_at_twice_what_its_agents_latest_run_spent_not_its_first() -> None:
    from cerebro.budget import Caps, Ledger
    from cerebro.compile import _estimate
    from cerebro.models import DEFAULTS, TokenCount

    store, model = MemoryStore(), DEFAULTS["compilation"]
    ledger = Ledger(store, Caps())
    for run, tokens in (("run_old", 714_286), ("run_new", 71_429)):  # $1.00, then $0.10
        store.append("agent-runs", {"run": run, "agent": AGENT.id})
        ledger.record("compilation", datetime(2026, 11, 17, 9, tzinfo=UTC), model, TokenCount(tokens, 0), run)
    assert _estimate(store, AGENT, [], model) == pytest.approx(0.2, abs=0.001)


def test_a_first_run_is_held_at_its_opening_read_a_few_times_not_its_whole_budget_so_a_small_cap_runs_many() -> None:
    from cerebro.budget import Caps, Ledger
    from cerebro.compile import FIRST_REPLY, FIRST_TURNS, _estimate
    from cerebro.evidence import Evidence
    from cerebro.models import DEFAULTS, TokenCount, cost
    from cerebro.run import tokens

    store, model = MemoryStore(), DEFAULTS["compilation"]
    evidence = [Evidence("Facts", ["- [^fact_1] " + "x" * 9_000])]  # about 3,600 tokens
    held = _estimate(store, AGENT, evidence, model)
    opening = tokens(AGENT.prompt) + tokens("Facts") + tokens(evidence[0].lines[0])
    assert held == pytest.approx(cost(model.provider, model.model, TokenCount(opening * FIRST_TURNS, FIRST_REPLY)))
    assert held < cost(model.provider, model.model, TokenCount(AGENT.budget.tokens_per_run, 0)) / 3
    ledger, now = Ledger(store, Caps(compilation=1.0)), datetime(2026, 11, 18, 9, tzinfo=UTC)
    twelve = [ledger.reserve("compilation", now, model, AGENT.budget.tokens_per_run, usd=held) for _ in range(12)]
    assert len(twelve) == 12  # twelve first runs side by side fit a $1 cap, as a composer's subagents start


def _capped(cap: float) -> tuple[MemoryStore, Any, Any, Callable[[], datetime]]:
    from cerebro.budget import Caps, Ledger
    from cerebro.models import DEFAULTS

    store = MemoryStore()
    return store, Ledger(store, Caps(compilation=cap)), DEFAULTS["compilation"], lambda: datetime(2026, 11, 18, 9,
                                                                                                    tzinfo=UTC)


def test_under_a_cap_a_run_lets_go_of_what_it_held_so_the_next_run_starts() -> None:
    from cerebro.compile import run_agent

    store, ledger, model, clock = _capped(1.0)
    big = replace(AGENT, budget=Budget(tokens_per_run=2_000_000))  # its whole budget, $2.80, is over the cap
    for _ in range(2):
        run = run_agent(big, [], [], agent_run=ScriptedRun(lambda p: {"Summary": "s"}), store=store, ledger=ledger,
                        model=model, clock=clock)
        assert run.status == "done"


def test_beside_a_run_under_way_a_cheap_run_starts_on_its_estimate_and_so_does_a_first_run_on_its_opening() -> None:
    from cerebro.budget import BudgetSpent
    from cerebro.compile import run_agent
    from cerebro.models import TokenCount

    store, ledger, model, clock = _capped(1.0)
    agents = {name: replace(AGENT, id=f"reports/{name}", budget=Budget(tokens_per_run=2_000_000))
              for name in ("outer", "cheap", "new")}
    for name in ("outer", "cheap"):  # each one's last run, yesterday, cost $0.10
        store.append("agent-runs", {"run": f"run_{name}", "agent": agents[name].id})
        ledger.record("compilation", datetime(2026, 11, 17, 9, tzinfo=UTC), model, TokenCount(71_429, 0),
                      f"run_{name}")
    beside: dict[str, str] = {}

    def outer(prompt: str) -> dict[str, str]:
        """While the outer run holds $0.20, two more runs try to start beside it."""
        for name in ("cheap", "new"):
            try:
                beside[name] = run_agent(agents[name], [], [], agent_run=ScriptedRun(lambda p: {"Summary": "s"}),
                                         store=store, ledger=ledger, model=model, clock=clock).status
            except BudgetSpent as error:
                beside[name] = str(error)
        return {"Summary": "s"}

    run_agent(agents["outer"], [], [], agent_run=ScriptedRun(outer), store=store, ledger=ledger, model=model,
              clock=clock)
    assert beside["cheap"] == "done"  # held at $0.20 beside $0.20: under the $1 cap
    assert beside["new"] == "done"  # a first run is held at its opening read a few times, not at its whole $2.80


def test_a_stop_part_way_through_landing_leaves_the_article_as_it_was_and_the_next_compile_runs(
        tmp_path: Path) -> None:
    from cerebro.fakes import FakeDecider
    from cerebro.store import SQLiteStore

    class Stopped(BaseException):
        """The process stopping, as serve's exit on SIGTERM does, while what a run handed in is grounded."""

    three = replace(AGENT, prompt="x\n\n# Output\n\n## Summary\n\n## Next\n\n## Risks", output=["Summary", "Next", "Risks"])
    two = replace(AGENT, hash="edited")

    class StopOnNext(FakeDecider):
        def decide(self, state: Any, questions: Any) -> Any:
            if state["heading"] == "Next":
                raise Stopped()
            return super().decide(state, questions)

    _, items, _, common = setup(tmp_path)
    store = SQLiteStore(tmp_path / "cerebro.db")
    common.update(store=store, lookup=StoreLookup(store))
    compile_article(three, agent_run=ScriptedRun(lambda p: {"Summary": "s1", "Next": "n1", "Risks": "r1"}), **common)
    items.append("two")
    with pytest.raises(Stopped):  # what it hands in is scored a heading at a time, and stops part way
        compile_article(two, agent_run=ScriptedRun(lambda p: {"Summary": "s2", "Next": "n2"}),
                        decider=StopOnNext(), **common)
    assert body(store.article("reports/weekly")) == "## Summary\n\ns1\n\n## Next\n\nn1\n\n## Risks\n\nr1"
    again = compile_article(two, agent_run=ScriptedRun(lambda p: {"Summary": "s3"}), **common)
    assert not again.skipped and body(store.article("reports/weekly")) == "## Summary\n\ns3"


def test_an_agent_whose_evidence_lines_hold_nothing_is_run_told_so_and_settled_until_its_inputs_change(
        tmp_path: Path) -> None:
    """A writer with no evidence is still
    run, told it has none, and is settled until its inputs change, so evidence that comes an hour later runs it then,
    not at its next heartbeat."""
    _, items, prompts, common = setup(tmp_path)
    items.clear()
    run = ScriptedRun(lambda p: prompts.append(p) or "")
    first = compile_article(AGENT, agent_run=run, **common)
    assert first.run is not None and not first.skipped and len(prompts) == 1 and "Nothing matched." in prompts[0]
    settled = compile_article(AGENT, agent_run=run, **common)
    assert settled.skipped and settled.waiting == "" and len(prompts) == 1  # the same inputs: settled
    items.append("one")
    run = ScriptedRun(lambda p: prompts.append(p) or {"Summary": "s [^r0]", "Next": "n [^r0]"})
    assert compile_article(AGENT, agent_run=run, **common).article is not None and len(prompts) == 2


def test_a_summary_whose_heading_no_part_carries_loads_the_first_part_and_says_so_where_a_person_sees_it(
        tmp_path: Path) -> None:
    from cerebro.article import Compilation

    agents = workspace(tmp_path, {"calls/article.md": "---\nevidence:\n  - articles: [things/alder]\n    summary: Overview"
                                                  "\n---\n\nWrite the call.\n"})
    store, _, _, common = setup(tmp_path)
    store.create(Identity("calls/kickoff", "calls", "Kickoff"))
    store.create(Identity("things/alder", "things", "Alder"))
    store.write_compilation(Compilation("things/alder", "things/article", "Alder renews.\n\n## Verdict\n\nIt holds.",
                                "things/article", whole=True))
    seen: list[str] = []
    compiled = compile_article(agents["calls/article"], agent_run=ScriptedRun(lambda p: seen.append(p) or "x [^r0]."),
                               this="calls/kickoff", **common)
    said = "summary: articles/things/alder.md has no part headed Overview, so its first part loaded in its place"
    assert "Alder renews." in seen[0] and "It holds." not in seen[0]
    assert said in compiled.notes and said in compiled.run.note and compiled.run.status == "done"
    assert [r.data.get("note") for r in store.rows("agent-runs")] == [said]


def _with_material(tmp_path: Path) -> tuple[dict[str, Any], MemoryStore, dict[str, Any], ByAgent]:
    """A composed brief whose subagents returned words the article it hands in never holds, so any place they show
    up is a leak of the material."""
    agents, store, _, common = briefs(tmp_path)
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "Zebrafish risk [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: "Quokka decides [^r0]."),
                   "briefs/article": ScriptedRun(lambda p: "## Verdict\n\nAlder holds [^r0].")})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    return agents, store, common, run


def test_what_subagents_returned_is_kept_as_material_that_no_query_read_search_log_or_evidence_lists(
        tmp_path: Path) -> None:
    import json

    from cerebro.compile import material_key
    from cerebro.evidence import gather
    from cerebro.reply import outside
    from cerebro.store import LOGS

    _, store, _, _ = _with_material(tmp_path)
    kept = store.state(material_key("briefs/alder"))
    assert set(kept) == {"briefs/subagents/risks", "briefs/subagents/people"}
    assert "Zebrafish" in kept["briefs/subagents/risks"]["text"]
    from cerebro.fakes import FakeExtraction

    lookup, you = StoreLookup(store, FakeExtraction()), Caller("you", "person")
    assert lookup.query(you, "", matching="Zebrafish Quokka").items == []
    assert lookup.query(you, "briefs", matching="Zebrafish").items == []
    shown = [outside(lookup.query(you, "briefs")), outside(lookup.read(you, ["briefs/alder"]))]
    shown += [[row.data for row in store.rows(log)] for log in LOGS]
    reader = workspace(tmp_path, {"digests/index.md": "---\nevidence:\n  - articles: [briefs]\n---\n\nList them.\n"})
    shown += [[entry.lines for entry in gather(reader["digests/index"], lookup, {}, root=tmp_path)]]
    said = json.dumps(shown, default=str)
    assert "Alder holds" in said and "Zebrafish" not in said and "Quokka" not in said


def test_material_goes_with_its_article_when_it_is_archived_merged_or_split_or_its_subagent_is_dropped(
        tmp_path: Path) -> None:
    from cerebro.compile import material_key
    from cerebro.curate import apply
    from cerebro.records import Proposal

    agents, store, common, run = _with_material(tmp_path)
    composer = agents["briefs/article"]
    dropped = replace(composer, subagents=["risks"], subagent_ids=["briefs/subagents/risks"])
    compile_article(dropped, agent_run=run, this="briefs/alder", **common, subagents=list(agents.values()))
    assert set(store.state(material_key("briefs/alder"))) == {"briefs/subagents/risks"}
    you = Caller("you", "person")
    apply(store, Proposal("p_1", "article.archive", "briefs/alder", you, "2026-11-18T09:00:00Z"), "2026-11-18T09:00:00Z")
    assert store.state(material_key("briefs/alder")) is None
    compile_article(composer, agent_run=run, this="briefs/alder", **common, scheduled=True)
    assert store.state(material_key("briefs/alder")) is None  # an archived article keeps none
    store.set_identity(replace(store.article("briefs/alder").identity, status="active"))  # admitted again
    store.create(Identity("briefs/birch-yard", "briefs", "Birch Yard"))
    for article in ("briefs/alder", "briefs/birch-yard"):
        compile_article(composer, agent_run=run, this=article, **common, scheduled=True)
        assert store.state(material_key(article)) is not None
    merge = Proposal("p_2", "article.merge", "briefs/alder", you, "2026-11-18T10:00:00Z", {"with": "briefs/birch-yard"})
    apply(store, merge, "2026-11-18T10:00:00Z")
    assert store.state(material_key("briefs/alder")) is None and store.state(material_key("briefs/birch-yard")) is None
    compile_article(composer, agent_run=run, this="briefs/alder", **common, scheduled=True)
    gone = next(p for p in ("briefs/alder", "briefs/birch-yard") if store.article(p).identity.status == "merged")
    survivor = "briefs/birch-yard" if gone == "briefs/alder" else "briefs/alder"
    compile_article(composer, agent_run=run, this=survivor, **common, scheduled=True)
    apply(store, Proposal("p_3", "article.split", gone, you, "2026-11-18T11:00:00Z"), "2026-11-18T11:00:00Z")
    assert store.state(material_key(survivor)) is None


def test_an_underlined_heading_opens_a_part_with_its_text_under_it_and_front_matter_is_no_heading(
        tmp_path: Path) -> None:
    from cerebro.fakes import FakeDecider

    store, items, _, common = setup(tmp_path)
    items[0] = "Alder renewed its contract for two more years."
    renewed = "Alder renewed its contract for two more years [^r0]."
    text = f"---\nstatus: draft\n---\n\n{renewed}\n\n## Closed\n\n{renewed}\n\nPast due\n===\n\n{renewed}\n\nOpen\n---\n\n{renewed}"
    compile_article(AGENT, agent_run=ScriptedRun(lambda p: text), decider=FakeDecider(), **common)
    landed = body(store.article("reports/weekly"))
    assert landed.endswith(f"## Closed\n\n{renewed}\n\n## Past due\n\n{renewed}\n\n## Open\n\n{renewed}")
    assert "status" not in landed


def test_a_part_another_agent_writes_on_the_article_stays_when_a_composer_with_a_changed_list_lands(
        tmp_path: Path) -> None:
    files = {"reports/brief.md": JOINED.replace("A brief", "The brief"), "reports/notes.md": (
        "---\ntarget: { from: reports }\nevidence:\n  - items: {}\n---\n\nNotes on it.\n"),
        "reports/subagents/risks.md": part("Risks"), "reports/subagents/people.md": part("People")}
    agents = workspace(tmp_path, files)
    store, _, _, common = setup(tmp_path)
    run = ByAgent({"reports/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
                   "reports/subagents/people": ScriptedRun(lambda p: "P [^r0]."),
                   "reports/notes": ScriptedRun(lambda p: "Notes [^r0].")})
    compile_article(agents["reports/brief"], agent_run=run, **common, subagents=list(agents.values()))
    compile_article(agents["reports/notes"], agent_run=run, this="reports/brief", **common)
    (tmp_path / "agents/reports/brief.md").write_text(files["reports/brief.md"].replace("[risks, people]", "[risks]"))
    agents = workspace(tmp_path, {})
    compile_article(agents["reports/brief"], agent_run=run, **common, subagents=list(agents.values()))
    assert sorted(s.by for s in store.article("reports/brief").compilations) == ["reports/notes", "reports/subagents/risks"]


def test_a_renamed_or_removed_subagents_part_and_material_go_when_the_article_lands(tmp_path: Path) -> None:
    from cerebro.compile import material_key

    agents, store, _items, common = briefs(tmp_path, JOINED)
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "Old risks [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0]."),
                   "briefs/subagents/threats": ScriptedRun(lambda p: "New risks [^r0].")})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    folder = tmp_path / "agents" / "briefs"
    (folder / "subagents" / "risks.md").rename(folder / "subagents" / "threats.md")
    (folder / "article.md").write_text(JOINED.replace("[risks, people]", "[threats, people]"))
    agents = workspace(tmp_path, {})
    for _ in range(2):
        compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common,
                        subagents=list(agents.values()))
    assert body(store.article("briefs/alder")) == "## Threats\n\nNew risks [^r0].\n\n## People\n\nP [^r0]."
    assert set(store.state(material_key("briefs/alder"))) == {"briefs/subagents/threats", "briefs/subagents/people"}
    (folder / "article.md").write_text(JOINED.replace("[risks, people]", "[people]"))  # nothing re-runs: still due
    agents = workspace(tmp_path, {})
    run.ran.clear()
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common,
                               subagents=list(agents.values()))
    assert not compiled.skipped and run.ran == [] and body(compiled.article) == "## People\n\nP [^r0]."
    assert set(store.state(material_key("briefs/alder"))) == {"briefs/subagents/people"}


def test_an_agent_that_drops_subagents_and_writes_the_article_itself_takes_their_parts_and_material_with_it(
        tmp_path: Path) -> None:
    from cerebro.compile import material_key

    agents, store, _items, common = briefs(tmp_path, JOINED)
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "Old risks [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: "Old people [^r0]."),
                   "briefs/article": ScriptedRun(lambda p: "## Brief\n\nNew brief [^r0].")})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    (tmp_path / "agents/briefs/article.md").write_text(part("Brief"))
    for name in ("risks", "people"):
        (tmp_path / f"agents/briefs/subagents/{name}.md").unlink()
    agents = workspace(tmp_path, {})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert body(store.article("briefs/alder")) == "## Brief\n\nNew brief [^r0]."
    assert store.state(material_key("briefs/alder")) is None


def test_with_no_material_a_failed_subagent_leaves_the_article_as_it_was_and_one_that_says_nothing_keeps_its_part(
        tmp_path: Path) -> None:
    from cerebro.compile import material_key

    for people, stays in ((Fails(), True), (ScriptedRun(lambda p: ""), True)):
        root = tmp_path / str(stays)
        agents, store, _items, common = briefs(root, JOINED)
        good = {"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
                "briefs/subagents/people": ScriptedRun(lambda p: "Ann decides [^r0].")}
        composer = agents["briefs/article"]
        compile_article(composer, agent_run=ByAgent(good), this="briefs/alder", **common)
        store.set_state(material_key("briefs/alder"), None)  # as an archive and re-admit, or a merge, leaves it
        compiled = compile_article(composer, agent_run=ByAgent({**good, "briefs/subagents/people": people}),
                                   this="briefs/alder", **common)
        failed = isinstance(people, Fails)  # a failed subagent fails the article's run, which lands nothing
        assert (compiled.article is None) is failed
        article = store.article("briefs/alder")
        assert ("## People\n\nAnn decides [^r0]." in body(article)) is stays
        assert "## Risks\n\nR [^r0]." in body(article)


def test_a_subagent_held_for_room_stands_aside_and_the_others_content_lands_once(tmp_path: Path) -> None:
    from cerebro.budget import HeldForRoom

    class Held(AgentRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Budget) -> Run:
            raise HeldForRoom("runs under way hold the room left under the compilation cap")

    agents, store, _items, common = briefs(tmp_path, JOINED)
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."), "briefs/subagents/people": Held()})
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert body(compiled.article) == "## Risks\n\nR [^r0]."
    assert any("briefs/subagents/people: held for room under the cap" in note for note in compiled.notes)
    for _ in range(4):  # it alone is due, and held: nothing is paid for again
        with pytest.raises(HeldForRoom):
            compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert run.ran.count("briefs/subagents/risks") == 1 and body(store.article("briefs/alder")) == "## Risks\n\nR [^r0]."


def test_content_returned_on_a_pass_that_fails_before_it_lands_lands_next_pass_without_running_again(
        tmp_path: Path) -> None:
    agents, store, items, common = briefs(tmp_path, JOINED)
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: f"R{len(items)} [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: f"P{len(items)} [^r0].")})
    composer = agents["briefs/article"]
    compile_article(composer, agent_run=run, this="briefs/alder", **common)
    items.append("news")

    def down(*_: object) -> None:
        raise RuntimeError("the store is down")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MemoryStore, "write_compilation", down)
        with pytest.raises(RuntimeError):
            compile_article(composer, agent_run=run, this="briefs/alder", **common)
    assert body(store.article("briefs/alder")) == "## Risks\n\nR1 [^r0].\n\n## People\n\nP1 [^r0]."
    run.ran.clear()
    compiled = compile_article(composer, agent_run=run, this="briefs/alder", **common)
    assert run.ran == [] and body(compiled.article) == "## Risks\n\nR2 [^r0].\n\n## People\n\nP2 [^r0]."
    assert compile_article(composer, agent_run=run, this="briefs/alder", **common).skipped


def test_an_article_archived_while_it_compiles_keeps_no_material(tmp_path: Path) -> None:
    from cerebro.compile import drop_material, material_key

    agents, store, _items, common = briefs(tmp_path, JOINED)

    def archive_then(prompt: str) -> str:
        identity = store.article("briefs/alder").identity
        store.set_identity(replace(identity, status="archived"))
        drop_material(store, "briefs/alder")
        return "R [^r0]."

    run = ByAgent({"briefs/subagents/risks": ScriptedRun(archive_then),
                   "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0].")})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert store.state(material_key("briefs/alder")) is None


def test_an_instructed_agent_may_cite_what_a_subagent_that_did_not_run_this_pass_read_by_its_tools(
        tmp_path: Path) -> None:
    from cerebro.connector import Record
    from cerebro.fakes import FakeExtraction

    agents, store, _items, common = briefs(tmp_path)
    extraction = FakeExtraction()
    extraction.extract(Record(id="local/a.md", source="local", revision="1", at="2026-11-10T09:00:00Z",
                              body="Priya Raman leads the Alder renewal."))
    fact = extraction.facts()[0]
    common |= {"lookup": StoreLookup(store, extraction)}
    said: list[str] = []
    runs = {"briefs/subagents/risks": ScriptedRun(lambda p: said.append(p) or f"R{len(said)} [^r0]."),
            "briefs/subagents/people": ScriptedRun(lambda p: f"{fact.text} [^{fact.id}]",
                                                   calls=[("query", {"source": "facts"})]),
            "briefs/article": ScriptedRun(lambda p: f"## Verdict\n\nR [^r0]. {fact.text} [^{fact.id}]")}
    first = compile_article(agents["briefs/article"], agent_run=ByAgent(runs), this="briefs/alder", **common)
    risks = tmp_path / "agents/briefs/subagents/risks.md"
    risks.write_text(risks.read_text() + "\nSay more.\n")  # risks alone runs again
    agents = workspace(tmp_path, {})
    run = ByAgent(runs)
    again = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common,
                            subagents=list(agents.values()))
    assert run.ran == ["briefs/subagents/risks", "briefs/article"]
    for compiled in (first, again):
        assert fact.id in [c.id for c in compiled.article.compilations[-1].citations]


def test_an_instructed_agent_does_not_run_when_its_subagents_ran_again_and_returned_the_same(tmp_path: Path) -> None:
    agents, _store, items, common = briefs(tmp_path)
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0]."),
                   "briefs/article": ScriptedRun(lambda p: "V [^r0].")})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    items.append("two")  # each subagent's evidence moved, and each returns what it returned before
    run.ran.clear()
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert sorted(run.ran) == ["briefs/subagents/people", "briefs/subagents/risks"]
    assert body(compiled.article) == "V [^r0]."


def test_an_instructed_agent_does_not_run_when_a_subagent_fails_and_its_article_stays_as_it_was(
        tmp_path: Path) -> None:
    agents, store, items, common = briefs(tmp_path)
    seen: list[str] = []
    composer = agents["briefs/article"]
    compile_article(composer, agent_run=ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R1 [^r0]."),
                                                 "briefs/subagents/people": ScriptedRun(lambda p: "P1 [^r0]."),
                                                 "briefs/article": ScriptedRun(lambda p: "V1 [^r0].")}),
                    this="briefs/alder", **common)
    items.append("Alder signed")
    run = ByAgent({"briefs/subagents/risks": Fails(), "briefs/subagents/people": ScriptedRun(lambda p: "P2 [^r1]."),
                   "briefs/article": ScriptedRun(lambda p: seen.append(p) or "V2 signed [^r1].")})
    compiled = compile_article(composer, agent_run=run, this="briefs/alder", **common)
    assert "briefs/article" not in run.ran and seen == []  # the subagent's failure fails the run before it
    assert compiled.article is None and body(store.article("briefs/alder")) == "V1 [^r0]."


def test_instructions_taken_out_and_put_back_word_for_word_run_again(tmp_path: Path) -> None:
    agents, _store, _items, common = briefs(tmp_path)
    runs = {"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
            "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0]."),
            "briefs/article": ScriptedRun(lambda p: "Verdict [^r0].")}
    for text in (BRIEF, JOINED, BRIEF):
        (tmp_path / "agents/briefs/article.md").write_text(text)
        agents = workspace(tmp_path, {})
        run = ByAgent(runs)
        compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common,
                                   subagents=list(agents.values()))
    assert run.ran == ["briefs/article"] and body(compiled.article) == "Verdict [^r0]."


def test_a_join_that_lands_records_a_run_row_that_says_it_joined_with_no_model(tmp_path: Path) -> None:
    agents, store, _items, common = briefs(tmp_path, JOINED)
    store.append("agent-runs", {"run": "run_old", "agent": "briefs/article", "target": "briefs/alder",
                                "status": "failed", "note": "the provider refused it", "ended_at": "2026-11-01T00:00:00Z"})
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0].")})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    last = {row.data["agent"]: row.data for row in store.latest("agent-runs", key="agent")}["briefs/article"]
    assert (last["status"], last["model"], last["note"]) == ("joined", "no model",
                                                            "joined 2 parts, with no model call and no cost")
    assert "section" not in str(last)
    assert compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common).skipped
    assert len(store.rows("agent-runs", {"agent": "briefs/article"})) == 2  # a pass that skips adds none


def test_a_subagent_that_keeps_failing_waits_a_doubling_while_before_it_is_paid_again_until_its_evidence_moves(
        tmp_path: Path) -> None:
    agents, store, items, common = briefs(tmp_path, JOINED)
    clock = common["clock"] = Ticking()
    run = ByAgent({"briefs/subagents/risks": Fails(), "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0].")})
    composer = agents["briefs/article"]

    def tries(seconds: float) -> int:
        clock.ahead(seconds)
        run.ran.clear()
        compile_article(composer, agent_run=run, this="briefs/alder", **common)
        return run.ran.count("briefs/subagents/risks")

    assert tries(0) == 1
    assert [tries(s) for s in (29, 1, 59, 1, 119, 1)] == [0, 1, 0, 1, 0, 1]  # 30 s, then 60, then 120
    for _ in range(10):
        tries(3600)
    assert tries(3599) == 0 and tries(1) == 1  # never more than an hour
    items.append("two")  # new evidence: it runs at once
    assert tries(0) == 1
    run.runs["briefs/subagents/risks"] = ScriptedRun(lambda p: "R [^r0].")
    assert tries(30) == 1 and tries(0) == 0  # a success forgets the failures
    assert store.state("failing:briefs/subagents/risks:briefs/alder") is None


def test_an_agent_whose_run_keeps_failing_waits_a_doubling_while_before_it_is_paid_again(tmp_path: Path) -> None:
    _, items, _, common = setup(tmp_path)
    clock = common["clock"] = Ticking()
    fails = ByAgent({"reports/weekly": Fails()})

    def tries(seconds: float) -> int:
        clock.ahead(seconds)
        fails.ran.clear()
        compiled = compile_article(AGENT, agent_run=fails, **common)
        assert not fails.ran or not compiled.skipped
        return len(fails.ran)

    assert [tries(s) for s in (0, 0, 30, 30, 30)] == [1, 0, 1, 0, 1]
    items.append("two")
    assert tries(0) == 1


def test_an_instructed_agent_whose_run_keeps_failing_waits_before_it_is_paid_again(tmp_path: Path) -> None:
    agents, _store, _items, common = briefs(tmp_path)
    clock = common["clock"] = Ticking()
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0]."), "briefs/article": Fails()})
    ran = []
    for seconds in (0, 0, 30, 30):
        clock.ahead(seconds)
        run.ran.clear()
        compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
        ran.append(run.ran.count("briefs/article"))
    assert ran == [1, 0, 1, 0]


def _nothing_part(heading: str) -> str:
    return part(heading).replace("items: {}", "nothing: {}")


def test_a_subagent_whose_evidence_holds_nothing_is_run_told_so_and_then_settled(tmp_path: Path) -> None:
    """An agent with no evidence is run, its empty lines
    reading `Nothing matched.`, and settled until what it reads changes."""
    agents = workspace(tmp_path, {"briefs/article.md": JOINED, "briefs/subagents/risks.md": part("Risks"),
                              "briefs/subagents/people.md": _nothing_part("People")})
    store, _, _, common = setup(tmp_path)
    common["functions"]["nothing"] = list
    store.create(Identity("briefs/alder", "briefs", "Alder"))
    told: list[str] = []
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: told.append(p) or "")})
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert sorted(run.ran) == ["briefs/subagents/people", "briefs/subagents/risks"]
    assert told and "Nothing matched." in told[0] and body(compiled.article) == "## Risks\n\nR [^r0]."
    again = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert again.skipped and again.waiting == "" and len(told) == 1  # settled: what it reads has not changed


def test_an_article_none_of_whose_parts_has_anything_to_cite_is_run_and_never_said_to_wait(tmp_path: Path) -> None:
    agents = workspace(tmp_path, {"briefs/article.md": JOINED, "briefs/subagents/risks.md": _nothing_part("Risks"),
                              "briefs/subagents/people.md": _nothing_part("People")})
    store, _, _, common = setup(tmp_path)
    common["functions"]["nothing"] = list
    store.create(Identity("briefs/birch", "briefs", "Birch"))
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: ""), "briefs/subagents/people": ScriptedRun(
        lambda p: "")})
    none_yet = compile_article(agents["briefs/article"], agent_run=run, this="briefs/birch", **common)
    assert sorted(run.ran) == ["briefs/subagents/people", "briefs/subagents/risks"] and none_yet.waiting == ""
    settled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/birch", **common)
    assert settled.skipped and settled.waiting == "" and len(run.ran) == 2


def test_an_instructed_agents_own_tool_reads_count_in_its_freshness(tmp_path: Path) -> None:
    from cerebro.connector import Record
    from cerebro.fakes import FakeExtraction

    agents, store, _items, common = briefs(tmp_path)
    extraction = FakeExtraction()
    extraction.extract(Record(id="local/a.md", source="local", revision="1", at="2026-11-10T09:00:00Z",
                              body="Priya Raman leads the Alder renewal."))
    common["lookup"] = StoreLookup(store, extraction)
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0]."),
                   "briefs/article": ScriptedRun(lambda p: "V [^r0].", calls=[("query", {"source": "facts"})])})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common).skipped
    extraction.retract(extraction.facts()[0].id)  # a fact it read by its tools moved, and nothing else did
    run.ran.clear()
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert run.ran == ["briefs/article"]


class Short(ScriptedRun):
    def run(self, *args: Any) -> Run:
        done = super().run(*args)
        done.status, done.note = "budget spent", "the run holds 8000 tokens, so the text it handed in lands"
        return done


def test_a_writer_cut_short_by_its_budget_lands_the_fields_it_set_with_its_text(tmp_path: Path) -> None:
    store, _items, _prompts, common = setup(tmp_path)
    compile_article(AGENT, agent_run=Short(lambda p: "S [^r0].", calls=[("override", {"field": "level", "value": "high"})]),
                    **common)
    assert store.article("reports/weekly").properties["level"].value == "high"


def test_a_subagent_cut_short_by_its_budget_lands_its_fields_only_with_text_that_stands_and_runs_again_next_pass(
        tmp_path: Path) -> None:
    agents, store, _items, common = briefs(tmp_path, JOINED)
    run = ByAgent({"briefs/subagents/risks": Short(lambda p: "R [^r0].", calls=[
        ("override", {"field": "level", "value": "high"})]),
                   "briefs/subagents/people": Short(lambda p: "P [^r0].", calls=[
                       ("override", {"field": "tone", "value": "calm"})])})
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert body(compiled.article) == "## Risks\n\nR [^r0].\n\n## People\n\nP [^r0]."
    assert store.article("briefs/alder").properties["level"].value == "high"
    assert store.article("briefs/alder").properties["tone"].value == "calm"
    run.ran.clear()
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert sorted(run.ran) == ["briefs/subagents/people", "briefs/subagents/risks"]  # not done reading: not fresh
    agents, store, _items, common = briefs(tmp_path / "instructed")  # a draft its composer wrote over never stands
    run.runs["briefs/article"] = ScriptedRun(lambda p: "V [^r0].")
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert body(compiled.article) == "V [^r0]." and "level" not in store.article("briefs/alder").properties


def test_a_composer_cut_short_by_its_budget_lands_its_fields_only_with_text_that_stands_and_runs_again_next_pass(
        tmp_path: Path) -> None:
    agents, store, _items, common = briefs(tmp_path)
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0]."),
                   "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0]."),
                   "briefs/article": Short(lambda p: "", calls=[
                       ("override", {"field": "level", "value": "high"})])})
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert compiled.run.status == "budget spent"
    assert body(compiled.article) == "## Risks\n\nR [^r0].\n\n## People\n\nP [^r0]."
    assert "level" not in store.article("briefs/alder").properties  # it handed in no text
    run.ran.clear()
    run.runs["briefs/article"] = Short(lambda p: "V [^r0].", calls=[("override", {"field": "level", "value": "high"})])
    compiled = compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert run.ran == ["briefs/article"] and body(compiled.article) == "V [^r0]."  # not done reading: not fresh
    assert store.article("briefs/alder").properties["level"].value == "high"


@pytest.mark.parametrize("shape", ["joined", "instructed", "instructed, adds nothing",
                                   "joined, a subagent returns nothing",
                                   "joined, a subagent waits", "instructed, a subagent waits",
                                   "instructed, a subagent fails"])
def test_ten_unchanged_passes_make_no_model_call_for_any_shape_of_composed_article(tmp_path: Path, shape: str) -> None:
    def returns(text: str) -> ScriptedRun:
        return ScriptedRun(lambda p: text)

    runs: dict[str, AgentRun] = {"briefs/subagents/risks": returns("R [^r0]."),
                                 "briefs/subagents/people": returns("P [^r0]."), "briefs/article": returns("V [^r0].")}
    if shape == "instructed, adds nothing":
        runs["briefs/article"] = returns("")
    elif shape == "joined, a subagent returns nothing":
        runs["briefs/subagents/people"] = returns("")
    elif shape == "instructed, a subagent fails":
        runs["briefs/subagents/people"] = Fails()
    agents = workspace(tmp_path, {"briefs/article.md": JOINED if shape.startswith("joined") else BRIEF,
                              "briefs/subagents/risks.md": part("Risks"),
                              "briefs/subagents/people.md": _nothing_part("People") if "waits" in shape
                              else part("People")})
    store, _, _, common = setup(tmp_path)
    store.create(Identity("briefs/alder", "briefs", "Alder"))
    common["functions"]["nothing"] = list
    common |= {"clock": Ticking()}
    run = ByAgent(runs)
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    run.ran.clear()
    for _ in range(10):
        common["clock"].ahead(60)  # a minute between passes, so a wait after a failure ends partway
        compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    # a subagent that fails fails the article's run, so what the other returned is not kept and both run again,
    # each time the wait after the failure ends
    assert sorted(run.ran) == ([] if shape != "instructed, a subagent fails" else
                               sorted(["briefs/subagents/people", "briefs/subagents/risks"] * 4))


def test_a_retracted_fact_whose_id_is_not_label_safe_is_withdrawn_from_the_text_the_store_wrote() -> None:
    from types import SimpleNamespace

    from cerebro.article import Compilation, Identity
    from cerebro.clean import label
    from cerebro.compile import _Grounding, _withdrawn
    from cerebro.facts import Fact
    from cerebro.fakes import MemoryStore

    gone, kept = "f 1<x>", "f_2"
    asked: list[str] = []

    class Facts:
        def facts(self, ids: list[str]) -> list[Fact]:
            asked.extend(ids)
            return [Fact(id, "x", "Halden", "note", "2026-11-03", "files", "local/halden.md",
                         status="retracted" if id == gone else "current") for id in ids]

    assert label(gone) != gone
    store = MemoryStore()
    store.create(Identity("things/halden", "things", "Halden"))
    store.write_compilation(Compilation("things/halden", "Part", f"The lab moved [^{label(gone)}].\nIt has a dock [^{kept}].\n",
                                by="things/parts", citations=[Citation("fact", gone), Citation("fact", kept)]))
    grounding = _Grounding("things/halden", lookup=SimpleNamespace(extraction=Facts()),  # type: ignore[arg-type]
                           root=Path("."), ledger=None, clock=lambda: datetime.now(UTC), decider=None, decision=None,
                           zone=UTC, floor=0.6)
    said = _withdrawn(grounding, store, "things/halden", ["things/parts"])
    part = next(s for s in store.article("things/halden").compilations if s.by == "things/parts")
    assert sorted(asked) == sorted([gone, kept]) and len(said) == 1
    assert "moved" not in part.text and "dock" in part.text and [c.id for c in part.citations] == [kept]


LINKS = "---\nevidence:\n  - items: {}\n  - articles: [this.linked]\n---\n\n# Instructions\n\nDraft it.\n"


def test_a_writer_is_fresh_after_a_run_that_set_a_field_its_own_evidence_reads(tmp_path: Path) -> None:
    agents = workspace(tmp_path, {"briefs/article.md": LINKS})
    store, _items, _prompts, common = setup(tmp_path)
    store.create(Identity("briefs/alder", "briefs", "Alder"))
    store.create(Identity("things/beam", "things", "Beam"))
    common["lookup"] = StoreLookup(store, FakeExtraction())
    run = ScriptedRun(lambda p: "B [^r0].", calls=[("override", {"field": "linked", "value": ["things/beam"]})])
    assert compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common).run is not None
    assert compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common).skipped
    store.drop_field("briefs/alder", "linked")  # another agent puts back what the first run read: news
    assert compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common).run is not None


def test_a_subagent_is_fresh_after_a_run_that_set_a_field_its_own_evidence_reads(tmp_path: Path) -> None:
    agents = workspace(tmp_path, {"briefs/article.md": JOINED, "briefs/subagents/risks.md": LINKS + "\n# Output\n\n## Risks\n",
                                  "briefs/subagents/people.md": part("People")})
    store, _items, _prompts, common = setup(tmp_path)
    store.create(Identity("briefs/alder", "briefs", "Alder"))
    store.create(Identity("things/beam", "things", "Beam"))
    common["lookup"] = StoreLookup(store, FakeExtraction())
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0].", calls=[
        ("override", {"field": "linked", "value": ["things/beam"]})]),
                   "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0].")})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    run.ran.clear()
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert run.ran == []


def test_a_writer_that_sets_a_field_to_the_value_it_holds_keeps_its_first_credit_and_adds_its_cites(
        tmp_path: Path) -> None:
    from cerebro.article import Property

    agents = workspace(tmp_path, {"briefs/article.md": LINKS})
    store, _items, _prompts, common = setup(tmp_path)
    store.create(Identity("briefs/alder", "briefs", "Alder"))
    common["lookup"] = StoreLookup(store, FakeExtraction())
    curator = Caller("briefs/curator", "agent")
    store.set_field("briefs/alder", "level", Property("high", curator, "2026-11-01T00:00:00Z", ["c1"]))
    store.set_field("briefs/alder", "rank", Property(1, curator, "2026-11-01T00:00:00Z"))
    first = store.article("briefs/alder").properties["level"]
    sets = [("override", {"field": "level", "value": "high", "cites": ["r0"]}),
            ("override", {"field": "level", "value": "high", "cites": ["r1"]}),
            ("override", {"field": "rank", "value": True})]  # equal to 1 in Python, and another value
    compile_article(agents["briefs/article"], agent_run=ScriptedRun(lambda p: "B [^r0].", calls=sets),
                    this="briefs/alder", **common)
    level, rank = (store.article("briefs/alder").properties[k] for k in ("level", "rank"))
    assert (level.by, level.at, level.cites) == (curator, first.at, ["c1", "r0", "r1"])
    assert rank.value is True and rank.by == Caller("briefs/article", "agent")


def test_a_subagent_is_due_when_another_agent_of_its_article_set_a_field_its_evidence_reads(tmp_path: Path) -> None:
    agents = workspace(tmp_path, {"briefs/article.md": JOINED, "briefs/subagents/risks.md": part("Risks"),
                                  "briefs/subagents/people.md": LINKS + "\n# Output\n\n## People\n"})
    store, _items, _prompts, common = setup(tmp_path)
    store.create(Identity("briefs/alder", "briefs", "Alder"))
    store.create(Identity("things/beam", "things", "Beam"))
    common["lookup"] = StoreLookup(store, FakeExtraction())
    run = ByAgent({"briefs/subagents/risks": ScriptedRun(lambda p: "R [^r0].", calls=[
        ("override", {"field": "linked", "value": ["things/beam"]})]),
                   "briefs/subagents/people": ScriptedRun(lambda p: "P [^r0].")})
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    run.ran.clear()
    compile_article(agents["briefs/article"], agent_run=run, this="briefs/alder", **common)
    assert run.ran == ["briefs/subagents/people"]
