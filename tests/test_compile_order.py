"""The order a pass compiles in: the articles of one collection run together, and the next collection starts once the one before is done. Which collection goes first follows the
evidence, what reads what, never a collection's name: one whose agents reach another through a link of `this`,
read from every article of the collection, compiles after it,
and collections that read each other run one after the other in path order, before any collection that reads one of
them, with one line that names each cycle. A collection named by its path in a reader's evidence orders it too, so
a digest waits for what it reads."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import pytest

from cerebro import Cerebro, Record
from cerebro.agent import Agent, Budget
from cerebro.article import Identity, Property
from cerebro.caller import Caller
from cerebro.evidence import Evidence
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.run import AgentRun, Run
from cerebro.runner import compile_groups

SOURCE = Caller("notes", "source")


def cited(prompt: str) -> dict[str, str]:
    first = re.search(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])
    return {"Summary": f"The first note. [^{first.group(1)}]"} if first else {"Summary": "x"}


class Watched(AgentRun):
    """A scripted run that keeps when each article's run began and ended, and what it read. A run of the things
    collection waits a little for any other run to begin, so runs side by side would show it."""

    def __init__(self) -> None:
        self.inner, self.lock, self.began = ScriptedRun(cited), threading.Lock(), threading.Event()
        self.order: list[str] = []
        self.read: dict[str, str] = {}

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
        article = agent.target
        with self.lock:
            self.order.append(f"begin {article}")
        if article.startswith("things/"):
            self.began.wait(0.5)  # another collection's run, side by side, would begin meanwhile
        else:
            self.began.set()
        run = self.inner.run(agent, evidence, tools, budget)
        with self.lock:
            self.read[article] = "\n".join(line for entry in evidence for line in entry.lines)
            self.order.append(f"end {article}")
        return run


def workspace(tmp_path: Path, agents: dict[str, str], run: AgentRun) -> Cerebro:
    (tmp_path / "settings.yaml").write_text("{}\n")
    for path, evidence in agents.items():
        (tmp_path / "agents" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "agents" / path).write_text(f"---\nevidence:\n{evidence}---\n\n# Output\n\n## Summary\n")
    notes = FakeConnector("notes", [Record(id="n0", source="notes", revision="1", at="2026-11-17T08:00:00Z",
                                           body="The first note.", keys={"topic": ["a"]})])
    return Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), connectors=[notes], agent_run=run)


def ends_before_begins(order: list[str], first: str, then: str) -> bool:
    """Whether every run on a `first` article ended before any run on a `then` article began."""
    ends = [n for n, e in enumerate(order) if e.startswith(f"end {first}/")]
    begins = [n for n, e in enumerate(order) if e.startswith(f"begin {then}/")]
    return bool(ends and begins) and max(ends) < min(begins)


def test_a_collection_that_reads_another_by_name_compiles_after_it_and_reads_what_it_wrote_this_pass(
        tmp_path: Path) -> None:
    watched = Watched()
    cerebro = workspace(tmp_path, {"things/article.md": "  - query: { from: facts, source: notes }\n",
                                   "digest/index.md": "  - articles: [things]\n"}, watched)
    cerebro.store.create(Identity("things/a", "things", "A", {"topic": ["a"]}))
    result = cerebro.run_once()
    assert sorted(result.compiled) == ["digest/index", "things/a"]
    assert ends_before_begins(watched.order, "things", "digest"), watched.order  # digest sorts first by name
    assert "The first note. [^" in watched.read["digest/index"]


def test_a_collection_that_reaches_another_through_a_link_of_this_compiles_after_it(tmp_path: Path) -> None:
    watched = Watched()
    cerebro = workspace(tmp_path, {"things/article.md": "  - query: { from: facts, source: notes }\n",
                                   "parts/article.md": "  - articles: [this.thing]\n"}, watched)
    cerebro.store.create(Identity("things/a", "things", "A", {"topic": ["a"]}))
    cerebro.store.create(Identity("parts/p", "parts", "P"))
    cerebro.store.set_field("parts/p", "thing", Property("things/a", SOURCE, "2026-11-17T08:00:00Z"))
    cerebro.run_once()
    assert ends_before_begins(watched.order, "things", "parts"), watched.order
    assert "The first note. [^" in watched.read["parts/p"]


def agent(tmp_path: Path, path: str, evidence: str) -> Agent:
    from cerebro.agent import load_agent

    file = tmp_path / "agents" / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(f"---\nevidence:\n{evidence}---\n\n# Output\n\n## Summary\n")
    return load_agent(file, tmp_path / "agents")


def test_collections_that_read_each_other_run_one_after_the_other_in_path_order_and_a_reader_of_either_after_both(
        tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    ones = agent(tmp_path, "ones/article.md", "  - articles: [twos]\n")
    twos = agent(tmp_path, "twos/article.md", "  - articles: [ones]\n")
    alls = agent(tmp_path, "alls/article.md", "  - articles: [twos]\n")
    free = agent(tmp_path, "zeds/article.md", "  - query: { from: facts }\n")
    store = MemoryStore()
    plan = [(alls, None, "alls/x"), (ones, None, "ones/x"), (twos, None, "twos/x"), (free, None, "zeds/x")]
    groups = compile_groups(store, [ones, twos, alls, free], plan)
    assert [[plan[n][2] for n in group] for group in groups] == [["ones/x"], ["twos/x"], ["alls/x"], ["zeds/x"]]


def test_with_no_reads_across_collections_the_collections_run_one_after_the_other_in_path_order(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    ones = agent(tmp_path, "ones/article.md", "  - query: { from: facts }\n")
    twos = agent(tmp_path, "twos/article.md", "  - query: { from: facts }\n")
    plan = [(twos, None, "twos/x"), (ones, None, "ones/x"), (ones, None, "ones/y")]
    assert compile_groups(MemoryStore(), [ones, twos], plan) == [[1, 2], [0]]  # each collection's articles together


def order(store: MemoryStore, agents: list[Agent], articles: list[str]) -> list[list[str]]:
    by = {a.target: a for a in agents}
    plan = [(by[article.rpartition("/")[0]], None, article) for article in articles]
    return [[plan[n][2] for n in group] for group in compile_groups(store, agents, plan)]


def linked(store: MemoryStore, article: str, name: str, value: Any) -> None:
    store.create(Identity(article, article.rpartition("/")[0], article))
    store.set_field(article, name, Property(value, SOURCE, "2026-11-17T08:00:00Z"))


def test_links_are_read_from_every_article_of_the_collection_not_only_those_compiled(tmp_path: Path) -> None:
    things = agent(tmp_path, "things/article.md", "  - query: { from: facts }\n")
    parts = agent(tmp_path, "parts/article.md", "  - articles: [this.thing]\n")
    store = MemoryStore()
    store.create(Identity("things/a", "things", "A"))
    linked(store, "parts/linked", "thing", "things/a")  # not compiled this pass, and its link still orders
    store.create(Identity("parts/plain", "parts", "Plain"))
    assert order(store, [things, parts], ["parts/plain", "things/a"]) == [["things/a"], ["parts/plain"]]


def test_a_link_to_an_article_that_does_not_exist_orders_nothing(tmp_path: Path) -> None:
    things = agent(tmp_path, "things/article.md", "  - query: { from: facts }\n")
    parts = agent(tmp_path, "parts/article.md", "  - articles: [this.thing]\n")
    store = MemoryStore()
    store.create(Identity("things/a", "things", "A"))
    linked(store, "parts/p", "thing", "things/gone")
    assert order(store, [things, parts], ["parts/p", "things/a"]) == [["parts/p"], ["things/a"]]


@pytest.mark.parametrize("value", ["../things/a.md", "articles/things/a.md", "things/a", ["things/a", None]])
def test_a_link_value_in_any_form_orders_the_linked_article_first(tmp_path: Path, value: Any) -> None:
    things = agent(tmp_path, "things/article.md", "  - query: { from: facts }\n")
    parts = agent(tmp_path, "parts/article.md", "  - articles: [this.thing]\n")
    store = MemoryStore()
    store.create(Identity("things/a", "things", "A"))
    linked(store, "parts/p", "thing", value)
    assert order(store, [things, parts], ["parts/p", "things/a"]) == [["things/a"], ["parts/p"]]


@pytest.mark.parametrize("value", ["Things A", "things/a b", "https://example.com/things/a", "../../things/a"])
def test_a_value_of_no_collection_and_name_shape_links_nothing(tmp_path: Path, value: str) -> None:
    things = agent(tmp_path, "things/article.md", "  - query: { from: facts }\n")
    parts = agent(tmp_path, "parts/article.md", "  - articles: [this.thing]\n")
    store = MemoryStore()
    store.create(Identity("things/a", "things", "A"))
    linked(store, "parts/p", "thing", value)
    assert order(store, [things, parts], ["parts/p", "things/a"]) == [["parts/p"], ["things/a"]]


def test_a_nested_collection_orders_by_its_whole_path(tmp_path: Path) -> None:
    from dataclasses import replace

    deep = replace(agent(tmp_path, "notes/article.md", "  - query: { from: facts }\n"), target="lab/notes")
    reader = agent(tmp_path, "aims/article.md", "  - articles: [lab/notes/x]\n")
    assert order(MemoryStore(), [deep, reader], ["aims/a", "lab/notes/x"]) == [["lab/notes/x"], ["aims/a"]]


def test_a_query_filtered_on_a_link_of_this_orders_after_what_the_link_reaches(tmp_path: Path) -> None:
    things = agent(tmp_path, "things/article.md", "  - query: { from: facts }\n")
    parts = agent(tmp_path, "parts/article.md", "  - query: { from: facts, about: this.thing }\n")
    store = MemoryStore()
    store.create(Identity("things/a", "things", "A"))
    linked(store, "parts/p", "thing", "things/a")
    assert order(store, [things, parts], ["parts/p", "things/a"]) == [["things/a"], ["parts/p"]]


def test_a_subagents_reads_order_the_collection_its_agent_writes(tmp_path: Path) -> None:
    from cerebro.agent import load_agents

    (tmp_path / "agents" / "parts" / "subagents").mkdir(parents=True)
    (tmp_path / "agents" / "parts" / "article.md").write_text("---\nsubagents: [one]\n---\n\nA part.\n")
    (tmp_path / "agents" / "parts" / "subagents" / "one.md").write_text(
        "---\nevidence:\n  - articles: [things]\n---\n\n# Instructions\n\nDraft it.\n\n# Output\n\n## Summary\n")
    things = agent(tmp_path, "things/article.md", "  - query: { from: facts }\n")
    loaded, refused = load_agents(tmp_path / "agents")
    assert not refused
    parts = next(a for a in loaded if a.id == "parts/article")
    assert order(MemoryStore(), [*loaded, things], ["parts/p", "things/a"]) == [["things/a"], ["parts/p"]]
    assert parts.role == "composer"


def test_collections_that_read_each_other_are_named_once_a_pass_and_in_status(tmp_path: Path) -> None:
    cerebro = workspace(tmp_path, {"ones/article.md": "  - articles: [twos]\n",
                                   "twos/article.md": "  - articles: [ones]\n",
                                   "alls/article.md": "  - articles: [twos]\n"}, ScriptedRun(cited))
    for article in ("ones/x", "twos/x", "alls/x"):
        cerebro.store.create(Identity(article, article.rpartition("/")[0], article))
    line = "compile order: ones <-> twos (compiled once per pass, in path order), alls"
    assert cerebro.run_once().warnings.count(line) == 1
    assert cerebro.status()["compile_order"] == line


# -- path order, a single article's links, and case


def test_collections_follow_the_order_of_their_article_paths_so_a_dash_sorts_before_a_slash(tmp_path: Path) -> None:
    """The collections come in the order of the sorted article paths, so `a-b/x` comes
    before `a/x`, where a sort of the names alone puts `a` first."""
    (tmp_path / "settings.yaml").write_text("{}\n")
    one = agent(tmp_path, "a/article.md", "  - query: { from: facts }\n")
    two = agent(tmp_path, "a-b/article.md", "  - query: { from: facts }\n")
    assert order(MemoryStore(), [one, two], ["a/x", "a-b/x"]) == [["a-b/x"], ["a/x"]]


def test_a_single_article_agents_links_order_nothing(tmp_path: Path) -> None:
    """An agent whose target is one file is skipped, so its links of `this` order nothing; what it names by path
    still orders it."""
    (tmp_path / "settings.yaml").write_text("{}\n")
    things = agent(tmp_path, "things/article.md", "  - query: { from: facts }\n")
    index = agent(tmp_path, "digest/index.md", "  - articles: [this.thing]\n")
    store = MemoryStore()
    store.create(Identity("things/a", "things", "A"))
    linked(store, "digest/index", "thing", "things/a")
    plan = [(index, None, "digest/index"), (things, None, "things/a")]
    assert [[plan[n][2] for n in g] for g in compile_groups(store, [things, index], plan)] == [
        ["digest/index"], ["things/a"]]
    named = agent(tmp_path, "digest/index.md", "  - articles: [things]\n")
    assert [[plan[n][2] for n in g] for g in compile_groups(store, [things, named], plan)] == [
        ["things/a"], ["digest/index"]]


@pytest.mark.parametrize("value", ["This.Thing", "THIS.thing", "this.THING"])
def test_this_and_a_field_name_match_in_any_case(tmp_path: Path, value: str) -> None:
    """`this` reads in any case, and `property` finds a field exactly, then in any case."""
    (tmp_path / "settings.yaml").write_text("{}\n")
    things = agent(tmp_path, "things/article.md", "  - query: { from: facts }\n")
    parts = agent(tmp_path, "parts/article.md", f"  - articles: [{value}]\n")
    store = MemoryStore()
    store.create(Identity("things/a", "things", "A"))
    linked(store, "parts/p", "thing", "things/a")
    assert order(store, [things, parts], ["parts/p", "things/a"]) == [["things/a"], ["parts/p"]]
