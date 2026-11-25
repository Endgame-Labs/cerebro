"""A composer with subagents, one of which hands in nothing on a run: its text that still grounds lands beside a
sibling's new draft, so its part is never left a bare heading. Fakes only."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from test_compile import BRIEF, JOINED, ByAgent, Fails, briefs, part, setup, workspace

from cerebro.article import Identity, body
from cerebro.compile import compile_article
from cerebro.fakes import ScriptedRun
from cerebro.ground import Grounded

ARTICLE = "briefs/alder"


def strict(agent: object, parts: dict[str, str], citable: dict, *_: object) -> Grounded:
    """Keeps a line only when every id it cites is one this run may cite."""
    kept, dropped = {}, []
    for heading, text in parts.items():
        lines = []
        for line in text.splitlines():
            ids = re.findall(r"\[\^([^\]]+)\]", line)
            if ids and not all(id in citable for id in ids):
                dropped.append(f"{heading}: {line}")
            else:
                lines.append(line)
        if any(line.strip() for line in lines):
            kept[heading] = "\n".join(lines).strip()
    return Grounded(kept, dropped)


def runs(**by: Any) -> ByAgent:
    return ByAgent({f"briefs/subagents/{name}" if name != "article" else "briefs/article":
                    ScriptedRun(lambda p, said=said: said) for name, said in by.items()})


def again(tmp_path: Path, *names: str) -> None:
    for name in names:
        f = tmp_path / "agents" / "briefs" / "subagents" / f"{name}.md"
        f.write_text(f.read_text() + "\nSay more.\n")


def test_a_silent_subagent_keeps_its_grounded_text_beside_its_siblings_new_draft_over_three_passes(
        tmp_path: Path) -> None:
    agents, store, _items, common = briefs(tmp_path)
    composer = agents["briefs/article"]
    compile_article(composer, agent_run=runs(risks={"Risks": "Risks one [^r0]."}, people={"People": "People one [^r0]."},
                                             article="## Verdict\n\nAlder holds [^r0]."), this=ARTICLE, **common)
    assert body(store.article(ARTICLE)) == "## Verdict\n\nAlder holds [^r0]."
    again(tmp_path, "risks", "people")
    second = compile_article(composer, agent_run=runs(risks={"Risks": "Risks two [^r0]."}, people="", article=""),
                             this=ARTICLE, **common)
    assert body(store.article(ARTICLE)) == "## Risks\n\nRisks two [^r0].\n\n## People\n\nPeople one [^r0]."
    assert "People: empty, so it kept what it wrote last" in second.notes
    third = compile_article(composer, agent_run=runs(risks={"Risks": "Risks two [^r0]."}, people="", article=""),
                            this=ARTICLE, **common)
    assert third.skipped
    assert body(store.article(ARTICLE)) == "## Risks\n\nRisks two [^r0].\n\n## People\n\nPeople one [^r0]."


def test_when_every_subagent_hands_in_nothing_what_the_composer_wrote_stands_and_each_part_says_so(
        tmp_path: Path) -> None:
    agents, store, _items, common = briefs(tmp_path)
    composer = agents["briefs/article"]
    compile_article(composer, agent_run=runs(risks={"Risks": "Risks one [^r0]."}, people={"People": "People one [^r0]."},
                                             article="## Verdict\n\nAlder holds [^r0]."), this=ARTICLE, **common)
    again(tmp_path, "risks", "people")
    second = compile_article(composer, agent_run=runs(risks="", people="", article=""), this=ARTICLE, **common)
    assert body(store.article(ARTICLE)) == "## Verdict\n\nAlder holds [^r0]."
    assert "Risks: empty, so briefs/article's text stands" in second.notes
    assert "People: empty, so briefs/article's text stands" in second.notes
    assert not any("kept what it wrote last" in note for note in second.notes)


def test_a_silent_subagent_whose_old_text_no_longer_grounds_lands_nothing_beside_its_siblings_draft(
        tmp_path: Path) -> None:
    agents, store, items, common = briefs(tmp_path)
    items.append("two")
    common["grounder"] = strict
    composer = agents["briefs/article"]
    compile_article(composer, agent_run=runs(risks={"Risks": "Risks one [^r0]."}, people={"People": "People one [^r1]."},
                                             article="## Verdict\n\nAlder holds [^r0]."), this=ARTICLE, **common)
    items.pop()  # r1 is no longer evidence
    again(tmp_path, "risks", "people")
    second = compile_article(composer, agent_run=runs(risks={"Risks": "Risks two [^r0]."}, people="", article=""),
                             this=ARTICLE, **common)
    assert body(store.article(ARTICLE)) == "## Risks\n\nRisks two [^r0]."
    assert "People: empty, and nothing it wrote grounds now, so it waits for the next run" in second.notes


def test_with_three_subagents_the_silent_middle_one_keeps_its_place(tmp_path: Path) -> None:
    agents = workspace(tmp_path, {"briefs/article.md": BRIEF.replace("[risks, people]", "[risks, people, steps]"),
                              "briefs/subagents/risks.md": part("Risks"), "briefs/subagents/people.md": part("People"),
                              "briefs/subagents/steps.md": part("Steps")})
    store, _items, _, common = setup(tmp_path)
    store.create(Identity(ARTICLE, "briefs", "Alder"))
    composer = agents["briefs/article"]
    compile_article(composer, agent_run=runs(risks={"Risks": "Risks one [^r0]."}, people={"People": "People one [^r0]."},
                                             steps={"Steps": "Steps one [^r0]."},
                                             article="## Verdict\n\nAlder holds [^r0]."), this=ARTICLE, **common)
    again(tmp_path, "risks", "people", "steps")
    second = compile_article(composer, agent_run=runs(risks={"Risks": "Risks two [^r0]."}, people="",
                                                      steps={"Steps": "Steps two [^r0]."}, article=""),
                             this=ARTICLE, **common)
    assert body(store.article(ARTICLE)) == ("## Risks\n\nRisks two [^r0].\n\n## People\n\nPeople one [^r0].\n\n"
                                         "## Steps\n\nSteps two [^r0].")
    assert "People: empty, so it kept what it wrote last" in second.notes


def test_kept_text_from_a_pass_that_failed_is_no_new_draft_so_what_the_composer_wrote_stands(tmp_path: Path) -> None:
    agents, store, items, common = briefs(tmp_path)
    items.append("two")
    common["grounder"] = strict
    composer = agents["briefs/article"]
    compile_article(composer, agent_run=runs(risks={"Risks": "Risks one [^r0].\nRisks two [^r1]."},
                                             people={"People": "People one [^r0].\nPeople two [^r1]."},
                                             article="## Verdict\n\nAlder holds [^r0]."), this=ARTICLE, **common)
    items.pop()  # r1 goes, so both subagents run, hand in nothing, and keep what grounds
    failing = runs(risks="", people="")
    failing.runs["briefs/article"] = Fails()
    compile_article(composer, agent_run=failing, this=ARTICLE, **common)
    compile_article(composer, agent_run=runs(risks={"Risks": "Not run."}, people={"People": "Not run."}, article=""),
                    this=ARTICLE, by_hand=True, **common)
    assert body(store.article(ARTICLE)) == "## Verdict\n\nAlder holds [^r0]."


def test_a_kept_part_lands_with_the_time_its_text_was_written(tmp_path: Path) -> None:
    now = [datetime(2026, 11, 1, 9, tzinfo=UTC)]
    agents, store, _items, common = briefs(tmp_path)
    common["clock"] = lambda: now[0]

    def timed(**by: Any) -> ByAgent:
        return ByAgent({f"briefs/subagents/{name}" if name != "article" else "briefs/article":
                        ScriptedRun(lambda p, said=said: said, clock=lambda: now[0]) for name, said in by.items()})

    composer = agents["briefs/article"]
    compile_article(composer, agent_run=timed(risks={"Risks": "Risks one [^r0]."}, people={"People": "People one [^r0]."},
                                              article="## Verdict\n\nAlder holds [^r0]."), this=ARTICLE, **common)
    now[0] = datetime(2026, 11, 1, 10, tzinfo=UTC)
    again(tmp_path, "people")
    compile_article(composer, agent_run=timed(people="", article=""), this=ARTICLE, **common)
    now[0] = datetime(2026, 11, 1, 11, tzinfo=UTC)
    again(tmp_path, "risks", "people")
    compile_article(composer, agent_run=timed(risks={"Risks": "Risks two [^r0]."}, people="", article=""), this=ARTICLE,
                    **common)
    people = next(s for s in store.article(ARTICLE).compilations if s.heading == "People")
    assert people.text == "People one [^r0]." and people.started.startswith("2026-11-01T09")


def test_a_kept_part_whose_id_the_store_wrote_as_a_label_grounds_again_by_that_id(tmp_path: Path) -> None:
    from cerebro.clean import label

    agents, store, _items, common = briefs(tmp_path, JOINED)  # no instructions of its own: its parts land as they are
    id = "r<0>"

    def items() -> list[dict[str, str]]:
        """Items."""
        return [{"id": id, "text": "one"}]

    common["functions"], common["grounder"] = {"items": items}, strict
    composer = agents["briefs/article"]
    compile_article(composer, agent_run=runs(risks={"Risks": f"Risks one [^{id}]."},
                                             people={"People": f"People one [^{id}]."}), this=ARTICLE, **common)
    again(tmp_path, "risks", "people")
    second = compile_article(composer, agent_run=runs(risks={"Risks": f"Risks two [^{id}]."}, people=""), this=ARTICLE,
                             **common)
    people = next(s for s in store.article(ARTICLE).compilations if s.heading == "People")
    assert label(id) != id and people.text == f"People one [^{label(id)}]."
    assert [c.id for c in people.citations] == [id] and not second.dropped
