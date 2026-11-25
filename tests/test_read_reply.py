"""What `read` gives a caller outside Cerebro: the article as a reader needs it, small, and its facts only when
asked for with `evidence`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp import Client
from test_mcp import YOU, over_mcp

from cerebro import Cerebro, Record
from cerebro.article import Citation, Compilation, Identity
from cerebro.fakes import FakeExtraction, MemoryStore
from cerebro.ground import quote_for
from cerebro.reply import pruned

NOTE = ("Mara Okafor said the Harbor beta moves from 2 December to 9 December, because the data export is not finished."
        "\n\nBen Ito owns the data export.\n\nBen Ito expects to finish the data export by 4 December.\n\n"
        "The beta starts with 40 teams.")


def quickstart(root: Path) -> Cerebro:
    """The quickstart's workspace after its pass: the Harbor article, its one compilation citing each of the note's four
    facts."""
    root.mkdir()
    (root / "settings.yaml").write_text("budget:\n  daily_usd: 20\n")
    extraction = FakeExtraction()
    facts = extraction.extract(Record(id="local/2026-11-16-harbor-sync.md", source="local", revision="1",
                                      at="2026-11-16T09:00:00Z", body=NOTE)).facts
    workspace = Cerebro(root, store=MemoryStore(), extraction=extraction)
    workspace.store.create(Identity("projects/harbor", "projects", "Harbor"))
    ids = [f.id for f in facts]
    text = ("The Harbor beta has moved from 2 December to 9 December, because the data export is not finished "
            f"[^{ids[0]}]. Ben Ito owns the data export [^{ids[1]}] and expects to finish it by 4 December "
            f"[^{ids[2]}]. The beta starts with 40 teams [^{ids[3]}].")
    workspace.store.write_compilation(Compilation("projects/harbor", "Where it stands", text, by="projects/harbor",
                                      at="2026-11-18T12:35:56Z", digest="sha256:" + "0" * 64,
                                      started="2026-11-18T12:35:53Z",
                                      citations=[Citation("fact", f.id, f.record, quote_for(f)) for f in facts]))
    for fact in facts:
        workspace.store.link(fact.id, ["projects/harbor"])
    return workspace


def test_the_quickstarts_article_reads_back_as_its_text_and_little_else(tmp_path: Path) -> None:
    workspace = quickstart(tmp_path / "workspace")

    async def calls(client: Client) -> Any:
        return await client.call_tool("read", {"ids": ["projects/harbor"]})

    reply = over_mcp(workspace, calls).structured_content
    [article] = reply["articles"]
    assert set(reply) == {"articles"} and set(article) == {"id", "title", "compiled_at", "text"}
    assert article["text"].startswith("## Where it stands\n\nThe Harbor beta has moved") and "[^fact_" in article["text"]
    assert len(json.dumps(reply)) <= len(json.dumps(article["text"])) + 150, json.dumps(reply)
    assert "section" not in json.dumps(reply) and "quote" not in json.dumps(reply)
    assert workspace.call(YOU, "read", {"ids": ["projects/harbor"]}) == reply  # cerebro tools --json too


def test_a_read_gives_the_facts_learned_since_the_article_was_written_and_never_the_others(tmp_path: Path) -> None:
    workspace = quickstart(tmp_path / "workspace")
    assert "facts_since_written" not in workspace.call(YOU, "read", {"ids": ["projects/harbor"]})["articles"][0]
    later = workspace.extraction.extract(Record(id="local/later.md", source="local", revision="1",
                                            at="2026-11-19T09:00:00Z", body="The beta now starts with 45 teams.")).facts
    for fact in later:
        workspace.store.link(fact.id, ["projects/harbor"], at="2026-11-19T09:00:00Z")
    [article] = workspace.call(YOU, "read", {"ids": ["projects/harbor"]})["articles"]
    assert article["facts_since_written"] == [{"id": later[0].id, "text": "The beta now starts with 45 teams.",
                                               "at": later[0].at, "record": "local/later.md"}]


def test_a_fact_asked_for_by_its_own_id_comes_back_small_with_the_words_that_hold_it(tmp_path: Path) -> None:
    workspace = quickstart(tmp_path / "workspace")
    fact = workspace.lookup.read(YOU, ["projects/harbor"]).facts[1]
    reply = workspace.call(YOU, "read", {"ids": [fact.id]})
    assert reply == {"facts": [{"id": fact.id, "text": "Ben Ito owns the data export.", "kind": "note",
                                "at": fact.at, "record": "local/2026-11-16-harbor-sync.md",
                                "quote": "Ben Ito owns the data export."}]}


def test_an_empty_field_is_left_out_at_any_depth_and_a_number_or_a_yes_or_no_stays() -> None:
    assert pruned({"a": "", "b": [], "c": {}, "d": None, "e": 0, "f": False,
                   "g": [{"h": "", "i": 1}], "j": {"k": {"l": []}}}) == {"e": 0, "f": False, "g": [{"i": 1}]}


def test_an_agents_read_it_cannot_answer_comes_back_as_a_refusal_and_the_run_goes_on(tmp_path: Path) -> None:
    from cerebro.caller import Caller
    from cerebro.compile import agent_surface

    workspace = quickstart(tmp_path / "workspace")
    from cerebro.run import answered

    _, read = agent_surface(workspace.lookup, Caller("accounts/subagents/stakeholders", "agent"), [])
    assert answered(read, [], about="Tidewater Freight") == {
        "refused": "read: ids is empty: pass at least one article id, such as notes/weekly"}


def test_an_agent_may_cite_a_fact_whose_id_the_article_it_read_names(tmp_path: Path) -> None:
    from cerebro.caller import Caller
    from cerebro.compile import agent_surface

    workspace = quickstart(tmp_path / "workspace")
    seen: list[Any] = []
    _, read = agent_surface(workspace.lookup, Caller("projects/harbor", "agent"), seen)
    shown = read(["projects/harbor"])
    assert "facts" not in shown and sorted(f.id for f in seen) == sorted(
        f.id for f in workspace.lookup.read(YOU, ["projects/harbor"]).facts)


def test_an_agent_gets_a_persons_facts_from_a_facts_query_on_their_article_and_may_cite_them(
        tmp_path: Path) -> None:
    from cerebro.caller import Caller
    from cerebro.compile import agent_surface

    workspace = quickstart(tmp_path / "workspace")
    seen: list[Any] = []
    query, _ = agent_surface(workspace.lookup, Caller("accounts/subagents/stakeholders", "agent"), seen)
    found = query("facts", {"articles": "projects/harbor"})
    assert sorted(f["text"] for f in found["items"]) == sorted(NOTE.split("\n\n"))
    assert sorted(f.id for f in seen) == sorted(f["id"] for f in found["items"])


def test_a_search_hit_on_text_written_whole_gives_the_passage_that_matched_with_or_without_a_heading() -> None:
    from cerebro.article import Article
    from cerebro.reply import _passage, article_text
    from cerebro.search import ARTICLE, Found

    article = Article(Identity("calls/kickoff", "calls", "Kickoff"), [
        Compilation("calls/kickoff", "calls/article", "They met.\n\n## Decisions\n\nA pilot.", "calls/article",
                whole=True)])
    assert article_text(article) == "They met.\n\n## Decisions\n\nA pilot."
    assert _passage(Found(ARTICLE, "calls/kickoff", "calls", "Kickoff", article, heading="Decisions")) == \
        "## Decisions\n\nA pilot."
    assert _passage(Found(ARTICLE, "calls/kickoff", "calls", "Kickoff", article)) == "They met."


def test_a_search_hit_on_the_second_of_two_parts_under_one_heading_shows_the_part_that_matched() -> None:
    from cerebro.lookup import StoreLookup
    from cerebro.reply import _passage

    store = MemoryStore()
    store.create(Identity("calls/kickoff", "calls", "Kickoff"))
    text = "## Update\n\nThe apple orchard renewed its lease.\n\n## Update\n\nThe zebrafish quokka migration halted."
    store.write_compilation(Compilation("calls/kickoff", "calls/article", text, by="calls/article", whole=True))
    found = [f for f in StoreLookup(store, FakeExtraction()).query(YOU, "", matching="zebrafish quokka").items
             if f.kind == "article"]
    assert found and found[0].place == 1
    assert _passage(found[0]) == "## Update\n\nThe zebrafish quokka migration halted."
