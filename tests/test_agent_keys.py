"""Agent files take no key they do not read: an evidence kind no one gives and a misspelt key at the top or inside
any block are each refused when the workspace loads, with the file and its line."""

from __future__ import annotations

from pathlib import Path

import pytest

from cerebro import Cerebro, scaffold
from cerebro.agent import AgentFileError, Budget, load_agent, load_agents, refuse_unknown_kinds
from cerebro.fakes import ScriptedRun


def lay_out_workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "w"
    scaffold.init(root, environ={})
    for name, text in files.items():
        (root / "agents" / name).parent.mkdir(parents=True, exist_ok=True)
        (root / "agents" / name).write_text(text, encoding="utf-8")
    return root


ENGINE = "articles, facts, files, held, left, query, script, sql"


def unknown(id: str, line: int, kind: str, fix: str) -> str:
    return f"agents/{id}.md: line {line}: evidence kind {kind} is not one Cerebro or an app of this workspace gives: {fix}"


WEEKLY = "---\nevidence:\n  - qurey: { from: things }\nheartbeat: [manual]\n---\n\n# Output\n\n## Summary\n"


def test_an_evidence_kind_nothing_gives_is_refused_with_its_line_and_the_kind_meant(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, {"reports/weekly.md": WEEKLY})
    with Cerebro(root, agent_run=ScriptedRun(lambda _: "")) as workspace:
        assert [agent.id for agent in workspace.agents] == []
        assert workspace.refused == [unknown("reports/weekly", 3, "qurey", "did you mean query?")]
        assert workspace.agent_list()["refused"] == workspace.refused


def test_with_no_kind_near_it_the_refusal_lists_the_kinds_there_are(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, {"reports/weekly.md": WEEKLY.replace("qurey", "zzz")})
    with Cerebro(root, agent_run=ScriptedRun(lambda _: "")) as workspace:
        assert workspace.refused == [unknown("reports/weekly", 3, "zzz", f"use one of {ENGINE}")]


def test_a_kind_an_app_of_the_workspace_gives_loads(tmp_path: Path) -> None:
    from cerebro.apps import load_app

    notes = Path(__file__).resolve().parents[1] / "apps" / "notes"
    root = lay_out_workspace(tmp_path, {"reports/weekly.md": WEEKLY.replace("qurey: { from: things }",
                                                                    "notes: { collection: notes/things }")})
    with Cerebro(root, agent_run=ScriptedRun(lambda _: ""), apps=[load_app(notes)]) as workspace:
        assert "reports/weekly" in [agent.id for agent in workspace.agents] and workspace.refused == []
    with Cerebro(root, agent_run=ScriptedRun(lambda _: "")) as workspace:
        assert workspace.refused == [unknown("reports/weekly", 3, "notes", f"use one of {ENGINE}")]


def lookup_web(question: str) -> str:
    """Look a question up."""
    return question


CURATOR = "---\nevidence:\n  - held: {}\n---\n\nKeep the things.\n"


def test_a_tool_an_app_of_the_workspace_gives_loads(tmp_path: Path) -> None:
    from cerebro.apps import App, agent_tool

    root = lay_out_workspace(tmp_path, {"reports/weekly.md": "---\ntools: [lookup_web]\n---\n\nWrite it.\n"})
    with Cerebro(root, agent_run=ScriptedRun(lambda _: ""), apps=[App("web", functions=[agent_tool(lookup_web)])]) as workspace:
        assert "reports/weekly" in [agent.id for agent in workspace.agents] and workspace.refused == []


def test_a_tool_nothing_runs_in_its_file_is_refused_with_the_tool_meant(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, {"things/curator.md": CURATOR,
                                "things/article.md": "---\ntools: [archive]\n---\n\nWrite it.\n",
                                "reports/weekly.md": WEEKLY.replace("qurey", "query").replace("---\n\n", "tools: [zzz]\n---\n\n"),
                                "reports/daily.md": "---\ntools: [override]\n---\n\nWrite it.\n",
                                "reports/hourly.md": "---\ntools: [return_content]\n---\n\nWrite it.\n"})
    with Cerebro(root, agent_run=ScriptedRun(lambda _: "")) as workspace:
        runs = "agents/{}.md: tool {} is not one Cerebro or an app of this workspace runs: {}"
        assert sorted(workspace.refused) == [
            "agents/reports/daily.md: tool override is one every run has already: take it off tools:",
            ("agents/reports/hourly.md: tool return_content is how a run hands in what it wrote, and no file lists "
             "it: take it off tools:"),
            runs.format("reports/weekly", "zzz", "use one of query, read"),
            ("agents/things/article.md: tool archive is one every curator.md is given, and no other file runs: take it "
             "off tools:")]


COMPOSER = "---\nsubagents: [overview, plans]\n---\n\nThe things.\n"
PART = "---\nevidence:\n  - facts: {}\n---\n\nWrite it.\n"


def test_a_refused_subagent_leaves_its_composer_as_a_refused_subagent_file_does(tmp_path: Path) -> None:
    files = {"things/article.md": COMPOSER, "things/subagents/overview.md": PART,
             "things/subagents/plans.md": PART.replace("facts: {}", "fcats: {}")}
    root = lay_out_workspace(tmp_path, files)
    with Cerebro(root, agent_run=ScriptedRun(lambda _: "")) as workspace:
        assert sorted(a.id for a in workspace.agents) == ["things/article", "things/subagents/overview"]
        assert workspace.refused == [unknown("things/subagents/plans", 3, "fcats", "did you mean facts?")]
    (root / "agents/things/subagents/plans.md").write_text("---\nevidence: [\n---\n", encoding="utf-8")
    loaded, refused = load_agents(root / "agents")
    assert sorted(a.id for a in loaded) == ["things/article", "things/subagents/overview"] and len(refused) == 1


def test_a_refused_composer_leaves_its_subagents_named_as_listed_by_none(tmp_path: Path) -> None:
    files = {"things/article.md": COMPOSER.replace("---\n\n", "evidence:\n  - qurey: { from: things }\n---\n\n")
             + "\n# Composing\n\nJoin them.\n",
             "things/subagents/overview.md": PART, "things/subagents/plans.md": PART}
    root = lay_out_workspace(tmp_path, files)
    with Cerebro(root, agent_run=ScriptedRun(lambda _: "")) as workspace:
        assert sorted(a.id for a in workspace.agents) == ["things/subagents/overview", "things/subagents/plans"]
        assert workspace.refused[0].startswith("agents/things/article.md: line 4: evidence kind qurey ")
        assert all(a.composers == [] for a in workspace.agents)
        for name in ("overview", "plans"):
            assert workspace.notices.count(f"agents/things/subagents/{name}.md: agents/things/article.md lists it and is "
                                       "refused at line 4, so it does not run: fix that file") == 1
        assert not [line for line in workspace.notices if "no composer lists it" in line]


def test_a_subagent_whose_composer_file_does_not_load_is_told_the_same(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, {"things/article.md": "---\nsubagents: [overview\n---\n",
                                "things/subagents/overview.md": PART})
    agents, refused = load_agents(root / "agents")
    assert len(refused) == 1 and agents[0].warnings == [
        ("agents/things/subagents/overview.md: agents/things/article.md is refused at line 3, so it does not run: "
         "fix that file")]


def test_a_refused_agent_leaves_no_notice_that_names_it(tmp_path: Path) -> None:
    files = {"reports/weekly.md": WEEKLY,
             "things/subagents/stray.md": PART.replace("facts: {}", "fcats: {}")}
    root = lay_out_workspace(tmp_path, files)
    with Cerebro(root, agent_run=ScriptedRun(lambda _: "")) as workspace:
        assert [line.split(":")[0] for line in workspace.refused] == ["agents/reports/weekly.md",
                                                                   "agents/things/subagents/stray.md"]
        named = [line for line in workspace.notices if line.startswith(("agents/reports/weekly.md:",
                                                                     "agents/things/subagents/stray.md:"))]
        assert named == []


# -- every block of an agent file takes the keys it reads, and names the line of one it does not ------------------

REFUSED = [
    ("reports/a.md", "heartbeat: [change]\nrewrite: true\n",
     "line 3: front matter key rewrite is not one an agent file takes"),
    ("reports/a.md", "heartbeat: { on: [change], cooldwn: 3h }\n", "line 2: heartbeat: cooldwn: is no key of a heartbeat"),
    ("reports/a.md", "budget:\n  tool_calls: 4\n  tokens: 9\n",
     "line 4: budget: takes tokens_per_run and tool_calls, not tokens"),
    ("reports/a.md", "model:\n  provider: fireworks\n  model: glm-5p3\n  temp: 1\n",
     "line 5: model: has 'temp', which is not a key"),
    ("things/curator.md", "authority:\n  admit: false\n  admitt: true\n",
     "line 4: authority: takes admit, archive, merge and split, not admitt"),
    ("reports/a.md", "rendered:\n  Contents: { kind: collections, stile: headings }\n",
     "line 3: rendered: Contents takes kind, style, show and place, not stile"),
    ("reports/a.md", "evidence:\n  - facts:\n      window: last_90_days\n      clases: [plans]\n",
     "line 5: evidence 1, facts takes classes, window, mentions, latest, relation, not clases"),
    ("reports/a.md", "evidence:\n  - sql:\n      source: warehouse\n      query: select 1\n      host: db.example\n",
     "line 6: evidence 1, sql: it names host"),
    ("reports/a.md", "evidence:\n  - script:\n      run: scripts/a.sh\n      args: [1]\n",
     "line 5: evidence 1, script takes run alone, not args"),
    ("things/curator.md", "evidence:\n  - held:\n      limt: 20\n",
     "line 4: evidence 1, held: it takes limit alone"),
    ("reports/a.md", "evidence:\n  - query: { from: things }\n    keeep: open ones\n",
     "line 3: evidence 1 names 2 kinds, query and keeep"),
    ("reports/a.md", "heartbeat:\n  on: [change]\n  off: [x]\n", "line 4: heartbeat: off: is no key of a heartbeat"),
    ("reports/a.md", "heartbeat: { yes: [change] }\n", "line 2: heartbeat: yes: is no key of a heartbeat"),
    ("reports/a.md", "heartbeat: { on: [change], 1: x }\n", "line 2: heartbeat: 1: is no key of a heartbeat"),
    ("reports/a.md", "budget:\n  on: 5\n", "line 3: budget: takes tokens_per_run and tool_calls, not on"),
    ("reports/a.md", "heartbeat: [change]\nyes: 1\n", "line 3: front matter key yes is not one an agent file takes"),
    ("reports/a.md", "rendered:\n  0x10: { kind: collections, stile: x }\n",
     "line 3: rendered: 0x10 takes kind, style, show and place, not stile"),
    ("reports/a.md", "model: { provider: fireworks, model: x, 1: a, z: b }\n", "line 2: model: has '1', which is not a key"),
    ("reports/a.md", "evidence:\n  - script: [scripts/a.sh]\n", "line 3: evidence 1, script takes run, the script's path"),
    ("reports/a.md", "evidence:\n  - facts: {}\n  - script:\n", "line 4: evidence 2, script takes run, the script's path"),
    ("reports/a.md", "min_grounding: high\n", "line 2: min_grounding is 'high': write a number from 0 to 1"),
]


@pytest.mark.parametrize(("name", "front", "said"), REFUSED)
def test_a_key_no_block_reads_is_refused_with_its_file_line(tmp_path: Path, name: str, front: str, said: str) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n\nWrite it.\n", encoding="utf-8")
    with pytest.raises(AgentFileError) as refused:
        load_agent(path, tmp_path)
    assert str(refused.value).startswith(f"agents/{name}: {said}"), str(refused.value)


def test_a_line_counts_from_the_top_of_the_file_after_a_byte_order_mark_and_with_crlf(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "a.md"
    path.parent.mkdir()
    path.write_bytes("﻿---\r\nheartbeat: [change]\r\n\r\nrewrite: true\r\n---\r\nbody".encode())
    with pytest.raises(AgentFileError, match=r"^agents/reports/a\.md: line 4: front matter key rewrite "):
        load_agent(path, tmp_path)


def test_a_front_matter_that_does_not_parse_names_the_line_of_the_file(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "a.md"
    path.parent.mkdir()
    path.write_text("---\nheartbeat: [change]\nevidence: [\n---\nbody", encoding="utf-8")
    with pytest.raises(AgentFileError, match=r"^agents/reports/a\.md: front matter is not YAML at line 4: "):
        load_agent(path, tmp_path)


def test_on_keeps_waking_an_agent_and_on_is_compared_as_written(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "a.md"
    path.parent.mkdir()
    path.write_text("---\nheartbeat:\n  on: [change]\n  cooldown: 3h\nbudget: { tool_calls: 4 }\n---\nbody\n")
    agent = load_agent(path, tmp_path)
    assert agent.heartbeat == ["change"] and agent.cooldown == "3h" and agent.budget.tool_calls == 4


# -- a value of the wrong shape is refused with its line, never loaded and then ignored ----------------------------

@pytest.mark.parametrize("value", ["[]", "''", "0", "false"])
def test_a_budget_that_is_no_map_is_refused_with_its_line(tmp_path: Path, value: str) -> None:
    path = tmp_path / "reports" / "a.md"
    path.parent.mkdir()
    path.write_text(f"---\nheartbeat: [change]\nbudget: {value}\n---\nbody\n", encoding="utf-8")
    with pytest.raises(AgentFileError, match=r"^agents/reports/a\.md: line 3: budget: takes tokens_per_run and "):
        load_agent(path, tmp_path)


@pytest.mark.parametrize("value", ["{}", "null", ""])
def test_an_empty_budget_loads_on_the_default_budget(tmp_path: Path, value: str) -> None:
    path = tmp_path / "reports" / "a.md"
    path.parent.mkdir()
    path.write_text(f"---\nheartbeat: [change]\nbudget: {value}\n---\nbody\n", encoding="utf-8")
    assert load_agent(path, tmp_path).budget == Budget()


SHAPES = {"a word": "projects", "a list": "[a, b]", "a number": "3", "null": "null", "a nested list": "[[a]]",
          "a map with an unknown key": "{ zzz: 1 }", "true": "true"}
# The shapes a kind takes: facts, held and an app's kind read null as {}, and articles and files take a list of paths.
TAKEN = {("facts", "null"), ("held", "null"), ("articles", "a list"), ("files", "a list"), ("usage", "null")}


def usage(collection: str = "everything") -> list[dict[str, str]]:
    """The usage of a collection."""
    return []


def refusal(path: Path, root: Path, given: object = None) -> str:
    """The line refusing the agent file at `path`, at load or at the check of its kinds, or empty when it loads."""
    try:
        agent = load_agent(path, root)
    except AgentFileError as error:
        return str(error)
    kinds = given if given is not None else {"query": None, "left": None, "facts": None, "articles": None,
                                             "files": None, "sql": None, "script": None, "usage": usage}
    _, refused, _ = refuse_unknown_kinds([agent], kinds, [])  # type: ignore[arg-type]
    return refused[0] if refused else ""


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("kind", ["query", "left", "facts", "articles", "files", "sql", "held", "script", "usage"])
def test_an_evidence_line_of_the_wrong_shape_is_refused_with_its_line(tmp_path: Path, kind: str, shape: str) -> None:
    name, tools = ("curator", "") if kind == "held" else ("writer", "")
    path = tmp_path / "things" / f"{name}.md"
    path.parent.mkdir()
    path.write_text(f"---\n{tools}evidence:\n  - {kind}: {SHAPES[shape]}\n---\nWrite.\n", encoding="utf-8")
    said = refusal(path, tmp_path)
    if (kind, shape) in TAKEN:
        assert said == ""
    else:
        assert said.startswith(f"agents/things/{name}.md: line {3 + bool(tools)}: evidence 1"), said


def test_an_app_kind_takes_a_map_of_the_arguments_its_function_takes(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "weekly.md"
    path.parent.mkdir()
    path.write_text("---\nheartbeat: [change]\nevidence:\n  - facts: {}\n  - usage: projects\n---\nReport.\n",
                    encoding="utf-8")
    assert refusal(path, tmp_path) == refusal(path, tmp_path, ["facts", "usage"]) == (
        "agents/reports/weekly.md: line 5: evidence 2, usage takes a map of the arguments its function takes, not "
        "'projects': write - usage: {} for none, or - usage: { <argument>: <value> }")
    path.write_text("---\nevidence:\n  - usage:\n      collection: things\n      colection: x\n---\nReport.\n",
                    encoding="utf-8")
    assert refusal(path, tmp_path) == ("agents/reports/weekly.md: line 5: evidence 1, usage takes collection, not "
                                       "colection: write - usage: { collection: <value> }")
    assert refusal(path, tmp_path, ["usage"]) == ""  # with no function to read, its arguments are its app's to check


def test_an_app_kind_names_an_argument_its_function_needs(tmp_path: Path) -> None:
    def needs(collection: str, since: str = "") -> list[dict[str, str]]:
        """Rows."""
        return []

    path = tmp_path / "reports" / "weekly.md"
    path.parent.mkdir()
    path.write_text("---\nevidence:\n  - needs: {}\n---\nReport.\n", encoding="utf-8")
    assert refusal(path, tmp_path, {"needs": needs}) == (
        "agents/reports/weekly.md: line 3: evidence 1, needs names no collection, which its function needs: write - "
        "needs: { collection: <value> }")


# -- a subagent whose composer is refused is told so, whichever file composes it -----------------------------------

SUMMARY = "---\nevidence:\n  - query: { from: notes }\n---\nSummarise the week.\n"
WEEKLY_COMPOSER = "---\nsubagents: [summary]\nevidence:\n  - qurey: { from: notes }\n---\nThe report.\n\n# Own\n\nJoin.\n"


def test_a_subagent_of_an_article_composer_that_does_not_load_is_told_the_same(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, {"reports/weekly.md": WEEKLY_COMPOSER.replace("evidence:\n  - qurey: { from: notes }",
                                                                             "heartbeat: [sometimes]"),
                                "reports/subagents/summary.md": SUMMARY})
    agents, refused = load_agents(root / "agents")
    assert len(refused) == 1 and agents[0].warnings == [
        ("agents/reports/subagents/summary.md: agents/reports/weekly.md lists it and is refused at line 3, so it does "
        "not run: fix that file")]


def test_a_subagent_beside_a_composer_that_does_not_read_is_told_it_may_list_it(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, {"reports/weekly.md": "---\nsubagents: [summary\n---\nThe report.\n",
                                "reports/subagents/summary.md": SUMMARY})
    agents, _ = load_agents(root / "agents")
    assert agents[0].warnings == [
        ("agents/reports/subagents/summary.md: no composer that loads lists it, so it does not run: "
        "agents/reports/weekly.md is refused at line 3 and may list it: fix that file")]


# -- a refused agent's own commands say it is refused, and why ------------------------------------------------------

BAD = "---\nheartbeat: [manual]\nevidence:\n  - qurey: { from: reports }\n---\n\nWrite a short report.\n"
WHY = "line 4: evidence kind qurey is not one Cerebro or an app of this workspace gives: did you mean query?"


def test_running_or_compiling_a_refused_agent_says_it_is_refused_and_why(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from cerebro.cli import main

    root = lay_out_workspace(tmp_path, {"reports/bad.md": BAD, "things/article.md": BAD.replace("[manual]", "[change]")})
    for args, said in ((["agents", "run", "reports/bad"], f"agents/reports/bad.md is refused, so it does not run: {WHY}"),
                       (["compile", "--article", "reports/bad"],
                        f"agents/reports/bad.md is refused, so nothing writes reports/bad: {WHY}"),
                       (["compile", "--article", "things/alpha"],
                        f"agents/things/article.md is refused, so nothing writes things/alpha: {WHY}")):
        result = CliRunner().invoke(main, [*args, "--workspace", str(root)])
        assert result.exit_code == 1 and result.output == f"Error: {said}\n", result.output
    result = CliRunner().invoke(main, ["agents", "run", "reports/none", "--workspace", str(root)])
    assert result.output == "Error: there is no agent reports/none in agents/: run cerebro agents list\n"


def test_the_refused_lines_name_each_refused_agent_apart_from_one_that_is_absent(tmp_path: Path) -> None:
    from cerebro.agent import refused_ids

    root = lay_out_workspace(tmp_path, {"reports/weekly.md": WEEKLY, "things/curator.md": "---\ntools: [admit\n---\n",
                                "things/subagents/plans.md": PART})
    with Cerebro(root, agent_run=ScriptedRun(lambda _: "")) as workspace:
        assert refused_ids(workspace.refused) == {"reports/weekly", "things/curator"}
    assert refused_ids(["sources/local.yaml: kind: files is wrong", "agents/a/b.md: line 2: x"]) == {"a/b"}


# -- a key or value YAML cannot build, and the line a refusal names --------------------------------------------------

@pytest.mark.parametrize("key", ["2020-13-01", "!!int abc"])
def test_a_key_yaml_cannot_build_refuses_the_file_and_stops_no_other(tmp_path: Path, key: str) -> None:
    root = lay_out_workspace(tmp_path, {"things/writer.md": f"---\nheartbeat: [change]\n{key}: x\n---\nWrite.\n",
                                "things/other.md": f"---\nheartbeat: [change]\nx: {key.split()[-1]}\n---\nWrite.\n"})
    agents, refused = load_agents(root / "agents")
    assert agents == [] and len(refused) == 2, refused


def test_a_value_yaml_cannot_build_is_refused_with_a_line() -> None:
    from cerebro.agent import read_yaml

    value, problem = read_yaml("a: 1\nb: 2020-13-01\n")
    assert value is None and problem == ("it holds a value YAML cannot use here (month must be in 1..12): write plain "
                                         "keys and values")


def test_a_subagent_its_loaded_article_does_not_list_is_told_to_list_it_there(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, {"things/article.md": "---\nsubagents: [other]\n---\n\nThe things.\n",
                                "things/subagents/other.md": PART, "things/subagents/overview.md": PART,
                                "things/curator.md": "---\n# \x07\n---\nAdmit.\n"})
    agents, _ = load_agents(root / "agents")
    overview = next(a for a in agents if a.id == "things/subagents/overview")
    assert overview.warnings == [("agents/things/subagents/overview.md: no composer lists it, so it does not run: "
                                  "list overview under subagents: in agents/things/article.md, where its heading goes")]


def test_a_refused_article_whose_list_does_not_read_is_named_as_refused(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, {"things/article.md": "---\nsubagents: overview\n---\n\nThe things.\n",
                                "things/subagents/overview.md": PART})
    agents, _ = load_agents(root / "agents")
    assert agents[0].warnings == [("agents/things/subagents/overview.md: agents/things/article.md is refused at line 2, "
                                   "so it does not run: fix that file")]


@pytest.mark.parametrize(("name", "front", "said"), [
    ("things/curator.md", "target: { from: things }\n", "line 2: a curator keeps the articles"),
    ("things/curator.md", "heartbeat: [daily]\nsweep: x\n", "line 3: front matter key sweep is not one"),
    ("reports/a.md", "evidence:\n  - query: { from: things }\n    top: 3\n", "line 4: evidence 1, query: top"),
    ("reports/a.md", "evidence:\n  - articles: [notes]\n    keep: x\n", "line 4: evidence 1, articles: keep"),
    ("things/article.md", "target: { from: things, order_by: due }\n", "line 2: target: order_by orders"),
])
def test_a_refusal_names_the_line_of_the_key_it_is_about(tmp_path: Path, name: str, front: str, said: str) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n\nWrite it.\n", encoding="utf-8")
    with pytest.raises(AgentFileError) as refused:
        load_agent(path, tmp_path)
    assert str(refused.value).startswith(f"agents/{name}: {said}"), str(refused.value)


def test_a_character_yaml_refuses_that_is_no_control_character_is_named_as_a_character() -> None:
    from cerebro.agent import read_yaml

    assert read_yaml("a: 1\nb: ￾\n")[1] == "line 2: it holds the character \\ufffe, which YAML does not allow: delete it"
    assert read_yaml("a: 1\rb: \x1b\n")[1] == ("line 2: it holds the control character \\x1b, which YAML does not allow: "
                                              "delete it")


def test_a_workspace_refuses_an_app_kind_line_that_is_no_map_and_runs_the_rest(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, {"reports/weekly.md": "---\nheartbeat: [manual]\nevidence:\n  - usage: projects\n---\nR.\n",
                                "reports/daily.md": "---\nheartbeat: [manual]\nevidence:\n  - usage: {}\n---\nR.\n"})
    (root / "extensions.py").write_text(
        "from cerebro import evidence\n\n\n@evidence(\"usage\")\ndef usage(collection: str = \"all\") -> list[dict[str, str]]:\n"
        "    \"\"\"Usage rows.\"\"\"\n    return []\n", encoding="utf-8")
    with Cerebro(root, agent_run=ScriptedRun(lambda _: "")) as workspace:
        assert [agent.id for agent in workspace.agents] == ["reports/daily"]
        assert workspace.refused == [
            ("agents/reports/weekly.md: line 4: evidence 1, usage takes a map of the arguments its function takes, not "
            "'projects': write - usage: {} for none, or - usage: { <argument>: <value> }")]


SUMMARY = "---\nevidence:\n  - query: { from: notes }\n---\nSum up the week.\n"
WEEK = "---\nsubagents: [summary]\nevidence:\n  - {kind}: {{ from: notes }}\n---\nThe week.\n\n# Own\n\nJoin the parts.\n"


def test_a_writers_refused_file_keeps_its_heartbeat_and_material_through_the_typo_and_after_the_mend(
        tmp_path: Path) -> None:
    from cerebro.compile import material_key
    from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore

    root = lay_out_workspace(tmp_path, {"reports/weekly.md": WEEK.format(kind="query"),
                                "reports/subagents/summary.md": SUMMARY})
    store, kept = MemoryStore(), {"heartbeat:reports/weekly": {"ran": "2026-11-18T09:00:00Z"},
                                  material_key("reports/weekly"): {"reports/subagents/summary": {"text": "Last week."}}}

    def run(kind: str) -> list[str]:
        (root / "agents" / "reports" / "weekly.md").write_text(WEEK.format(kind=kind), encoding="utf-8")
        with Cerebro(root, store=store, extraction=FakeExtraction(), connectors=[FakeConnector("crm", [])],
                     agent_run=ScriptedRun(lambda _: ""), trusted=True) as workspace:
            workspace.run_once()
            return [agent.id for agent in workspace.agents]

    assert "reports/weekly" in run("query")
    for key, value in kept.items():
        store.set_state(key, value)
    assert "reports/weekly" not in run("qurey")
    assert {key: store.state(key) for key in kept} == kept
    assert "reports/weekly" in run("query")  # mended, it runs on its evidence as any writer does: both stay kept
    assert all(store.state(key) is not None for key in kept)


def test_a_curators_refused_file_keeps_what_is_held_for_it(tmp_path: Path) -> None:
    from cerebro import Record
    from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore

    root = lay_out_workspace(tmp_path, {"things/curator.md": (
        "---\nevidence:\n  - held: {}\n  - fcats: {}\nheartbeat: [change]\n---\nAdmit things.\n")})
    record = Record(id="r1", source="crm", revision="1", at="2026-11-12T09:00:00Z", keys={"name": ["Alder"]},
                    fields={}, into="things")
    store = MemoryStore()
    with Cerebro(root, store=store, extraction=FakeExtraction(), connectors=[FakeConnector("crm", [record])],
                 agent_run=ScriptedRun(lambda _: ""), trusted=True) as workspace:
        assert workspace.agents == []
        workspace.run_once()
    assert [row["id"] for row in store.state("held:crm") or []] == ["r1"]


USAGE = ("from cerebro import evidence\n\n\n@evidence(\"usage\")\ndef usage(collection: str, since: str = \"\") -> "
         "list[dict[str, str]]:\n    \"\"\"Usage rows.\"\"\"\n    return []\n")


@pytest.mark.parametrize(("usage", "said"), [
    ("\n      collection: things\n      colection: x",
     ("line 6: evidence 1, usage takes collection, since, not colection: write - usage: { collection: <value>, since: "
      "<value> }")),
    (" { since: monday }",
     "line 4: evidence 1, usage names no collection, which its function needs: write - usage: { collection: <value> }"),
])
def test_agents_list_refuses_an_app_kind_line_whose_arguments_its_function_does_not_take(
        tmp_path: Path, usage: str, said: str) -> None:
    from click.testing import CliRunner

    from cerebro.cli import main

    root = lay_out_workspace(tmp_path, {
        "reports/weekly.md": f"---\nheartbeat: [manual]\nevidence:\n  - usage:{usage}\n---\nR.\n",
        "reports/daily.md": "---\nheartbeat: [manual]\nevidence:\n  - usage: { collection: things }\n---\nR.\n"})
    (root / "extensions.py").write_text(USAGE, encoding="utf-8")
    assert CliRunner().invoke(main, ["trust", "--workspace", str(root)]).exit_code == 0
    listed = CliRunner().invoke(main, ["agents", "list", "--workspace", str(root)])
    assert listed.exit_code == 0, listed.output
    assert f"refused  agents/reports/weekly.md: {said}" in listed.output.splitlines()
    assert listed.output.splitlines()[0].startswith("reports/daily ")
    assert listed.output.splitlines()[-1] == "1 agent, 1 refused."
