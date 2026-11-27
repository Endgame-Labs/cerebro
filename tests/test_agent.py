"""Agent files: every gtm template file loads into a typed record with nothing dropped, and a bad file is refused alone
with one line that names the file, the key, and what to write."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import GTM

from cerebro.agent import AgentFileError, Authority, Budget, Render, cooldown_seconds, load_agent, load_agents

FIELDS = {  # each front matter key, and what the record must hold of it
    "evidence": lambda a, v: [line.kind for line in a.evidence] == [next(k for k in e if k not in (
        "keep", "top", "summary", "text_only")) for e in v] and all(
        line.options == {k: e[k] for k in e if k in ("keep", "top", "summary", "text_only")}
        for line, e in zip(a.evidence, v)),
    "heartbeat": lambda a, v: a.heartbeat == (v[True] if isinstance(v, dict) else v) and a.cooldown == (
        v.get("cooldown") if isinstance(v, dict) else None),
    "tools": lambda a, v: a.tools == v,
    "authority": lambda a, v: a.authority == Authority(**v),
    "subagents": lambda a, v: a.subagents == v and len(a.subagent_ids) == len(v),
    "target": lambda a, v: a.target_query == v and a.target == v["from"],
    "budget": lambda a, v: a.budget == Budget(**v),
    "heading": lambda a, v: a.heading == v and a.output == [v],
    "rendered": lambda a, v: a.rendered == {h: Render(**r) for h, r in v.items()} and not set(v) & set(a.output),
}


def test_every_gtm_template_agent_file_loads_into_a_typed_record_with_nothing_dropped() -> None:
    agents, refused = load_agents(GTM)
    assert refused == [] and len(agents) == 29
    for agent in agents:
        front = yaml.safe_load(agent.path.read_text().split("---")[1])
        front = {("on" if k is True else k): v for k, v in front.items()}
        for key, value in front.items():
            assert FIELDS[key](agent, value), (agent.id, key)
        assert agent.hash and agent.prompt and agent.warnings == []
    by = {a.id: a for a in agents}
    roles = {id: (a.role, a.target) for id, a in by.items()}
    assert roles["accounts/curator"] == ("curator", "accounts")
    assert roles["accounts/article"] == ("composer", "accounts")
    assert roles["accounts/subagents/overview"] == ("subagent", "accounts")
    assert roles["calls/article"] == ("article", "calls")
    assert roles["reports/pipeline"] == ("writer", "reports/pipeline")
    # A composer resolves to its subagents, in the article's order, and each subagent names its composer.
    accounts = by["accounts/article"]
    assert accounts.subagent_ids == ["accounts/subagents/overview", "accounts/subagents/opportunities",
                                     "accounts/subagents/stakeholders", "accounts/subagents/work-streams"]
    assert [by[s].output for s in accounts.subagent_ids] == [["Overview"], ["Opportunities"], ["Stakeholders"],
                                                             ["Work streams"]]
    assert by["accounts/subagents/work-streams"].composers == ["accounts/article"] and accounts.heartbeat == []
    assert accounts.context.startswith("An account article") and accounts.own == ""  # it joins, with no model
    assert by["risk-radar/article"].own.startswith("# Composing")  # it runs, after its subagents
    assert by["people/subagents/activity"].output == ["Recent activity"]
    # The body: instructions before # Output, each Output heading with what the file says of it.
    overview = by["accounts/subagents/overview"]
    assert overview.instructions.startswith("# Instructions") and "# Output" not in overview.instructions
    assert overview.headings["Overview"].startswith("Three to six sentences")
    assert overview.notice == "" and overview.context == "" and overview.own == ""
    index = by["digests/index"]
    assert index.output == ["Accounts", "Opportunities", "People", "Calls", "Knowledge"]
    assert next(iter(index.headings)) == "Contents" and index.cooldown is None  # the workspace's schedule.articles
    assert index.notice == ""
    assert by["reports/pipeline"].notice == ""  # its left line is built
    assert by["calls/article"].evidence[1].options == {"text_only": True}
    assert by["accounts/subagents/opportunities"].hash == "05d0b6dc"


def test_a_file_with_a_byte_order_mark_loads_and_unacted_keys_are_named(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "weekly.md"
    path.parent.mkdir()
    path.write_text("﻿---\nevidence:\n  - query: { from: notes }\n    keep: open ones"
                    "\n    top: 5\n---\nbody", encoding="utf-8")
    agent = load_agent(path, tmp_path)
    assert agent.evidence[0].options == {"keep": "open ones", "top": 5}
    assert agent.heartbeat == ["change"] and agent.context == ""
    assert agent.notice == "agents/reports/weekly.md: read but not acted on: keep, top"


BODY = """\
---
heartbeat: [change]
---

Shared words.

# Instructions

Write it. An example of a heading you do not write:

```markdown
# Output

## Not this
```

<!--
# Output
## Nor this
-->

# Output

## First

One paragraph.

    ## Indented is code

## Second

A table.

# Notes

## Not an output
"""


def test_the_body_gives_instructions_and_output_headings_outside_code_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "notes" / "weekly.md"
    path.parent.mkdir()
    path.write_text(BODY)
    agent = load_agent(path, tmp_path)
    assert agent.output == ["First", "Second"]
    assert agent.headings == {"First": "One paragraph.\n\n    ## Indented is code", "Second": "A table."}
    assert agent.instructions.startswith("Shared words.") and agent.instructions.endswith("-->")
    assert agent.prompt.endswith("## Not an output")


def test_one_bad_file_does_not_stop_the_others_and_a_query_target_names_its_collection(tmp_path: Path) -> None:
    (tmp_path / "meetings").mkdir()
    (tmp_path / "meetings" / "bad.md").write_text("---\ntools: 5\n---\nbody")
    (tmp_path / "meetings" / "prep.md").write_text("---\ntarget: { from: meetings, status: upcoming }\n---\nbody")
    agents, refused = load_agents(tmp_path)
    assert [(a.id, a.role, a.target, a.target_query) for a in agents] == [
        ("meetings/prep", "query", "meetings", {"from": "meetings", "status": "upcoming"})]
    assert refused == ["agents/meetings/bad.md: line 2: tools: is not a list of names: write tools: [first, second]"]


def test_heartbeat_forms_and_cooldowns_read_as_the_docs_write_them(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    forms = {"[change, daily]": (["change", "daily"], None), "[weekly]": (["weekly"], None), "daily": (["daily"], None),
             "{ on: [change], cooldown: 1d }": (["change"], "1d"), "{ on: [change, manual] }": (["change", "manual"],
                                                                                           None)}
    for n, (form, read) in enumerate(forms.items()):
        path = tmp_path / "notes" / f"note-{n}.md"
        path.write_text(f"---\nheartbeat: {form}\nevidence:\n  - query: {{ from: notes }}\n---\nbody")
        agent = load_agent(path, tmp_path)
        assert (agent.heartbeat, agent.cooldown) == read, form
    assert cooldown_seconds("0") == 0  # waits for nothing, whatever the workspace's schedule.articles says
    assert [cooldown_seconds(v) for v in ("90m", "3h", "1h30m", "1d", "366d", "daily", "hourly")] == [
        5400, 10800, 5400, 86400, 366 * 86400, 86400, 3600]
    assert [cooldown_seconds(v) for v in ("30s", "0d", "367d", "3x", "", "h")] == [None] * 6
    path = tmp_path / "notes" / "clock.md"
    path.write_text("---\nheartbeat: [daily]\n---\nbody")
    assert load_agent(path, tmp_path).warnings == [(
        "agents/notes/clock.md: heartbeat [daily] rebuilds each article every day even when no new evidence came in: to "
        "rebuild only on new evidence, with a cooldown as long, write heartbeat: { on: [change], cooldown: 1d }")]


def test_an_agent_with_subagents_resolves_them_and_runs_only_with_instructions_of_its_own(tmp_path: Path) -> None:
    folder = tmp_path / "briefs"
    (folder / "subagents").mkdir(parents=True)
    (folder / "article.md").write_text("---\nsubagents: [risks]\nheartbeat: { cooldown: 3h }\n---\nShared.\n")
    (folder / "subagents" / "risks.md").write_text("---\nevidence:\n  - facts: {}\n---\nbody")
    (folder / "subagents" / "stray.md").write_text("---\nheading: Loose ends\n---\nbody")
    (tmp_path / "board" / "subagents").mkdir(parents=True)
    (tmp_path / "board" / "deck.md").write_text("---\nsubagents: [risks]\nevidence:\n  - facts: {}\n---\nShared."
                                                "\n\n# Composing\n\nWrite the deck.\n\n# Output\n\n## Lead\n")
    (tmp_path / "board" / "subagents" / "risks.md").write_text("---\nheartbeat: [change]\n---\nbody")
    agents, refused = load_agents(tmp_path)
    assert refused == []
    stray = next(a for a in agents if a.id == "briefs/subagents/stray")
    assert stray.output == ["Loose ends"] and stray.warnings == [(
        "agents/briefs/subagents/stray.md: no composer lists it, so it does not run: list stray under subagents: in "
        "agents/briefs/article.md, where its heading goes")]
    joins = next(a for a in agents if a.id == "briefs/article")
    assert (joins.own, joins.context, joins.cooldown, joins.heartbeat, joins.warnings) == ("", "Shared.", "3h", [], [])
    deck = next(a for a in agents if a.id == "board/deck")
    assert (deck.role, deck.target, deck.subagent_ids) == ("composer", "board/deck", ["board/subagents/risks"])
    assert deck.own == "# Composing\n\nWrite the deck." and deck.output == ["Lead"] and deck.context == "Shared."
    assert next(a for a in agents if a.id == "briefs/subagents/risks").composers == ["briefs/article"]


def test_two_subagents_under_one_heading_refuse_their_agent_and_name_both_files(tmp_path: Path) -> None:
    (tmp_path / "briefs" / "subagents").mkdir(parents=True)
    (tmp_path / "briefs" / "article.md").write_text("---\nsubagents: [risks, people]\n---\nShared.\n")
    (tmp_path / "briefs" / "subagents" / "risks.md").write_text("---\nheading: people\n---\nbody")
    (tmp_path / "briefs" / "subagents" / "people.md").write_text("---\nheartbeat: [change]\n---\nbody")
    agents, refused = load_agents(tmp_path)
    assert "briefs/article" not in {a.id for a in agents}
    assert refused == [("agents/briefs/article.md: its subagents agents/briefs/subagents/risks.md and "
                        "agents/briefs/subagents/people.md both write under the heading People, so one part would "
                        "replace the other: give one of them a heading: line of its own, or take one off subagents:")]


def test_a_query_target_that_lists_subagents_is_refused_with_what_to_do(tmp_path: Path) -> None:
    (tmp_path / "meetings" / "subagents").mkdir(parents=True)
    (tmp_path / "meetings" / "prep.md").write_text("---\ntarget: { from: meetings }\nsubagents: [a]\n---\n"
                                                   "Prep.\n\n# Own\n\nDo it.\n")
    (tmp_path / "meetings" / "subagents" / "a.md").write_text("---\nheartbeat: [change]\n---\nbody")
    with pytest.raises(AgentFileError, match="a query agent runs no subagents: delete subagents: and write those "
                                             "parts in its own instructions, or move the subagents to "
                                             "agents/meetings/article.md"):
        load_agent(tmp_path / "meetings" / "prep.md", tmp_path)


@pytest.mark.parametrize(("body", "own", "context"), [
    ("Shared.\n\n# Notes\n", "", "Shared."),
    ("Shared.\n\n# Notes\n\n# Composing\n", "", "Shared."),
    ("Shared.\n\n#\tComposing\n\nWrite a verdict.\n", "#\tComposing\n\nWrite a verdict.", "Shared."),
])
def test_a_level_one_heading_is_seen_after_a_tab_and_one_with_nothing_under_it_is_no_instructions(
        tmp_path: Path, body: str, own: str, context: str) -> None:
    (tmp_path / "briefs" / "subagents").mkdir(parents=True)
    (tmp_path / "briefs" / "article.md").write_text(f"---\nsubagents: [risks]\n---\n{body}")
    (tmp_path / "briefs" / "subagents" / "risks.md").write_text("---\nheartbeat: [change]\n---\nbody")
    agent = load_agent(tmp_path / "briefs" / "article.md", tmp_path)
    assert (agent.own, agent.context) == (own, context)


@pytest.mark.parametrize(("front", "rest", "says"), [
    ("evidence:\n  - facts: {}\n", "", ("so nothing reads evidence: move it to the subagent that needs it, or write "
                                         "its own instructions after a level-one heading")),
    ("model: { provider: fireworks, model: glm-5p3 }\n", "", "so nothing reads model"),
    ("budget: { tool_calls: 4 }\n", "", "so nothing reads budget"),
    ("min_grounding: 0.5\n", "", "so nothing reads min_grounding"),
    ("", "\n# Output\n\n## Lead\n", ("# Output says what a model writes, and this file joins its subagents' content "
                                     "with no model: delete # Output, or write its own instructions after a level-one "
                                     "heading")),
    ("rewrite: true\n", "", "front matter key rewrite is not one an agent file takes"),
])
def test_an_agent_with_subagents_and_no_instructions_of_its_own_refuses_what_only_a_run_would_read(
        tmp_path: Path, front: str, rest: str, says: str) -> None:
    folder = tmp_path / "briefs"
    (folder / "subagents").mkdir(parents=True)
    (folder / "subagents" / "risks.md").write_text("---\nheartbeat: [change]\n---\nbody")
    (folder / "article.md").write_text(f"---\nsubagents: [risks]\n{front}---\nShared.\n{rest}")
    with pytest.raises(AgentFileError) as refused:
        load_agent(folder / "article.md", tmp_path)
    assert says in str(refused.value)
    (folder / "subagents" / "risks.md").write_text("---\nsubagents: [x]\n---\nbody")
    with pytest.raises(AgentFileError, match="a subagent returns one part of its article, so it lists no subagents"):
        load_agent(folder / "subagents" / "risks.md", tmp_path)


OK = "evidence:\n  - query: { from: notes }\n"
BAD = [  # (path under agents/, text, words the refusal must hold)
    ("weekly.md", "---\nheartbeat: [daily]\n---\nbody", "root of agents/"),
    ("reports/old/weekly.md", "---\nheartbeat: [daily]\n---\nbody", "subagents/ is the only folder"),
    ("reports/Weekly.md", "---\nheartbeat: [daily]\n---\nbody", "lower case"),
    ("reports/bad.md", "no front matter", "front matter"),
    ("reports/bad.md", "---\n---\nbody", "front matter"),
    ("reports/bad.md", "---\nevidence: [\n---\nbody", "line"),
    ("reports/bad.md", "---\nheartbeat: [daily]\nheartbeat: [weekly]\n---\nbody", "heartbeat"),
    ("reports/bad.md", "---\nschedule: daily\n---\nbody", "schedule"),
    ("reports/bad.md", "---\non: [change]\n---\nbody", "key on"),
    ("reports/bad.md", "---\nheartbeat: [sometimes]\n---\nbody", "sometimes"),
    ("reports/bad.md", "---\nheartbeat: { on: [manual], cooldown: 3h }\n---\nbody", "add change"),
    ("reports/bad.md", "---\nheartbeat: { cooldown: 3h }\n---\nbody", "on: is missing"),
    ("reports/bad.md", "---\nheartbeat: { on: [change], cooldown: 20s }\n---\nbody", "20s"),
    ("reports/bad.md", "---\nheartbeat: { on: [change], at_most: 3h }\n---\nbody", "at_most"),
    ("reports/curator.md", "---\nheartbeat: { on: [change], cooldown: 3h }\n---\nbody", "no article"),
    ("reports/bad.md", "---\ntools: admit\n---\nbody", "tools"),
    ("reports/curator.md", "---\ntools: [admit]\n---\nbody", "delete the tools: line"),
    ("reports/bad.md", "---\nauthority: { admit: true }\n---\nbody", "authority"),
    ("reports/curator.md", "---\nauthority: { admit: maybe }\n---\nbody", "authority admit"),
    ("reports/bad.md", "---\nsubagents: overview\n---\nbody", "subagents"),
    ("reports/bad.md", "---\nsubagents: [overview]\n---\nbody", "subagents/overview.md"),
    ("reports/bad.md", "---\nheading: Weekly\n---\nbody", "heading:"),
    ("reports/bad.md", "---\nbudget: { tool_calls: forty }\n---\nbody", "tool_calls"),
    ("reports/bad.md", "---\nmodel: glm-5p3\n---\nbody", "model"),
    ("reports/bad.md", "---\ntarget: articles/reports/bad.md\n---\nbody", "target:"),
    ("reports/bad.md", "---\ntarget: { from: meetings, start: this }\n---\nbody", "target filter start"),
    ("reports/bad.md", "---\nrendered: { Contents: { kind: collections, place: middle } }\n---\nbody\n",
     "place 'middle': write before"),
    ("reports/bad.md", "---\nrendered: { Contents: table }\n---\nbody\n\n# Output\n\n## Contents\n", "table"),
    ("reports/bad.md", "---\nevidence:\n  - { query: {}, facts: {} }\n---\nbody", "evidence 1 names 2 kinds"),
    ("reports/bad.md", "---\nevidence:\n  - query: { status: open }\n---\nbody", "from"),
    ("reports/bad.md", "---\nevidence:\n  - query: { from: notes, limit: 5 }\n---\nbody", "limit"),
    ("reports/bad.md", "---\nevidence:\n  - facts: { window: lately }\n---\nbody", "lately"),
    ("reports/bad.md", "---\nevidence:\n  - articles: [../secrets]\n---\nbody", "outside"),
    ("reports/bad.md", "---\nevidence:\n  - query: { from: notes }\n    top: 5\n---\nbody", "top"),
    ("reports/bad.md", "---\nevidence:\n  - articles: [notes]\n    keep: open\n---\nbody", "text_only"),
    ("reports/bad.md", "---\nevidence:\n  - left: { from: notes, owner: this }\n---\nbody", "owner"),
    ("reports/curator.md", "---\nevidence:\n  - left: { from: notes }\n---\nbody", "no article"),
]


def test_a_bad_agent_file_is_refused_alone_with_one_line_naming_the_file_and_the_culprit(tmp_path: Path) -> None:
    for n, (rel, text, culprit) in enumerate(BAD):
        root = tmp_path / str(n)
        path = root / rel
        path.parent.mkdir(parents=True)
        path.write_text(text)
        (root / "notes").mkdir()
        (root / "notes" / "good.md").write_text(f"---\n{OK}---\nbody")
        agents, refused = load_agents(root)
        assert [a.id for a in agents] == ["notes/good"], rel
        assert len(refused) == 1 and refused[0].startswith(f"agents/{rel}: ") and culprit in refused[0], (text, refused)
        assert "\n" not in refused[0] and refused[0][len(f"agents/{rel}: "):][0].islower(), refused[0]


def test_an_agent_file_beside_an_article_agent_is_refused(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    (tmp_path / "accounts" / "article.md").write_text(f"---\n{OK}---\nbody")
    (tmp_path / "accounts" / "summary.md").write_text(f"---\n{OK}---\nbody")
    agents, refused = load_agents(tmp_path)
    assert [a.id for a in agents] == ["accounts/article"]
    assert refused[0].startswith("agents/accounts/summary.md: it sits beside agents/accounts/article.md")


def test_every_window_the_lookup_reads_loads_in_an_agent_file(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "fiscal.md"
    path.parent.mkdir()
    path.write_text("---\nevidence:\n  - query: { from: notes, due: this_fiscal_year }\n"
                    "  - query: { from: spend, window: last_quarter }\n---\nbody")
    assert [line.args["from"] for line in load_agent(path, tmp_path).evidence] == ["notes", "spend"]


def test_a_script_path_that_could_never_be_cited_is_refused_when_the_file_loads(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    for name, run in (("space", "scripts/my usage.py"), ("bracket", "scripts/a]b.py")):
        (tmp_path / "reports" / f"{name}.md").write_text(f"---\nevidence:\n  - script: {{ run: \"{run}\" }}\n---\n\n"
                                                        "# Instructions\n\nSum it up.\n\n# Output\n\n## Summary\n")
    agents, refused = load_agents(tmp_path)
    assert agents == [] and len(refused) == 2
    assert all("holds a space or a ], so what it prints could never be cited" in line for line in refused)
