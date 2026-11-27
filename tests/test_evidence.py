"""Evidence: each line is a thin call onto `Lookup` as the agent, preload keeps the file's order and half the budget,
an agent never reads its own text, and the digest moves with what the lines return and nothing else."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cerebro.agent import Agent, AgentFileError, Budget, EvidenceLine, load_agent
from cerebro.article import Article, Citation, Compilation, Identity, Property
from cerebro.caller import Caller
from cerebro.evidence import (
    LEFT_OUT,
    SCRIPT_BYTES,
    SCRIPT_SECONDS,
    WORKSPACE_ENV,
    Evidence,
    EvidenceError,
    Finished,
    Item,
    LastSets,
    Launch,
    NotBuilt,
    ScriptError,
    digest,
    estimate_tokens,
    gather,
    left_key,
    preload,
    render,
    start,
)
from cerebro.facts import Fact, Quote
from cerebro.file import File
from cerebro.lookup import Lookup, QueryResult, ReadResult

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
SOURCE = Caller("sync", "source")


def landed(text: str) -> list[Citation]:
    """The citations a compile lands with a compilation: each fact its text cites, with its quote."""
    return [Citation("fact", id, "notes:1", f"quote {id}") for id in dict.fromkeys(re.findall(r"\[\^([^\]\s]+)\]", text))]


def article(id: str, front: dict[str, Any], parts: list[tuple[str, str, str]] = ()) -> Article:
    collection = id.split("/")[0]
    return Article(Identity(id, collection, title=id.split("/")[1].replace("-", " ").title()),
                   [Compilation(id, heading, text, by=by, citations=landed(text)) for heading, text, by in parts],
                   {k: Property(v, SOURCE, "2026-11-01T00:00:00Z") for k, v in front.items()})


def fact(id: str, at: str, record: str = "notes:1") -> Fact:
    return Fact(id, f"Fact {id}.", "Alder", "timeline", at, "notes", record, [Quote(f"quote {id}")])


class StandIn(Lookup):
    """A lookup over a few articles and facts. It records each call as the caller made it, and applies equal filters
    alone: a window word is recorded and matches everything, since the lookup resolves windows."""

    def __init__(self, articles: list[Article], facts: dict[str, list[Fact]]):
        self.articles = {a.identity.id: a for a in articles}
        self.facts = facts
        self.calls: list[tuple[Any, ...]] = []
        self.orders: list[tuple[str | None, bool]] = []

    def query(self, caller: Caller, source: str, filters: dict[str, Any] | None = None, *, limit: int | None = None,
              order_by: str | None = None, descending: bool = False, **_: Any) -> QueryResult:
        filters = dict(filters or {})
        self.calls.append(("query", caller, source, filters, limit))
        self.orders.append((order_by, descending))
        if source == "facts":
            ids = filters.get("articles")
            items: list[Any] = sorted({f.id: f for key, fs in self.facts.items() for f in fs
                                       if ids in (None, key) or (isinstance(ids, list) and key in ids)}.values(),
                                      key=lambda f: f.at, reverse=True)
        else:
            items = [a for a in self.articles.values() if a.identity.collection == source and all(
                str(v).endswith("_days") or (a.properties.get(k) and a.properties[k].value == v)
                for k, v in filters.items())]
        return QueryResult(source, items[:limit], len(items))

    def read(self, caller: Caller, ids: list[str] | str, about: str | None = None) -> ReadResult:
        ids = [ids] if isinstance(ids, str) else list(ids)
        self.calls.append(("read", caller, ids))
        return ReadResult([self.articles[i] for i in ids if i in self.articles],
                          missing=[i for i in ids if i not in self.articles])


def agent(tmp_path: Path, rel: str, front: str) -> Agent:
    path = tmp_path / "agents" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n\n# Instructions\n\nWrite it.\n")
    return load_agent(path, tmp_path / "agents")


ALDER = article("things/alder", {"name": "Alder", "close_date": "2026-12-01", "compiled_at": "2026-11-15T00:00:00Z"},
            [("Overview", "Alder renews in December. [^f_old]", "things/subagents/overview"),
             ("Stakeholders", "Jane Doe signs.", "things/subagents/stakeholders")])
MEETING = article("meetings/qbr", {"status": "upcoming", "account": "things/alder",
                                "attendees": ["people/nobody", "people/jane"]})
JANE = article("people/jane", {"name": "Jane Doe", "email": "jane@example.com"})
CALL = article("calls/kickoff", {"account": "things/alder", "starts": "2026-11-02"})
FACTS = {"things/alder": [fact("f_1", "2026-10-01T00:00:00Z"), fact("f_3", "2026-11-03T00:00:00Z", "notes:2"),
                           fact("f_2", "2026-10-20T00:00:00Z")],
         "people/jane": [fact("f_9", "2026-11-05T00:00:00Z")], "meetings/qbr": [fact("f_m", "2026-11-10T00:00:00Z")]}


def test_each_line_is_a_thin_call_onto_the_lookup_as_the_agent_with_windows_passed_through(tmp_path: Path) -> None:
    prep = agent(tmp_path, "meetings/prep.md", "target: { from: meetings, status: upcoming }\nevidence:\n"
                 "  - articles: [this.account, this.attendees, this.sponsor]\n"
                 "  - query: { from: calls, account: this.account, starts: last_90_days, order_by: starts }\n"
                 "  - facts: { window: last_365_days, classes: [timeline] }\n"
                 "  - query: { from: people, owner: this.sponsor }\n")
    lookup = StandIn([ALDER, MEETING, JANE, CALL], FACTS)
    evidence = gather(prep, lookup, {}, this="meetings/qbr", now=NOW)
    me = Caller("meetings/prep", "agent")
    assert {call[1] for call in lookup.calls} == {me}
    assert [c for c in lookup.calls if c[0] == "query"] == [
        ("query", me, "facts", {"articles": "things/alder"}, None),
        ("query", me, "facts", {"articles": "people/jane"}, None),
        ("query", me, "calls", {"account": "things/alder", "starts": "last_90_days"}, None),
        ("query", me, "facts", {"articles": "meetings/qbr", "window": "last_365_days", "classes": ["timeline"]}, None)]
    assert lookup.orders[2:4] == [("starts", True), (None, False)]  # order_by orders the line, newest first
    assert [e.heading for e in evidence] == [
        "Articles this.account, this.attendees, this.sponsor",
        "Query from calls, account this.account, starts last_90_days, order_by starts, as of Mon Nov 16, 2026",
        "Facts, window last_365_days, classes timeline, as of Mon Nov 16, 2026",
        "Query from people, owner this.sponsor"]
    assert evidence[3].lines == ["owner this.sponsor names no article, so the query matches nothing"]
    first = evidence[0]
    assert first.lines[0].startswith("### Article articles/things/alder.md\n\n" + '{"close_date": "2026-12-01"')
    assert "compiled_at" not in render(evidence)  # front matter every compile moves wakes no reader
    assert [line.split("]")[0] for line in first.lines[1:4]] == ["- [^f_3", "- [^f_2", "- [^f_1"]  # newest first
    assert "articles/people/nobody.md does not exist yet" in first.lines  # a link to no article is said, not an error
    assert "this.sponsor names no article, so this line reads nothing from it" in first.lines
    assert {c.id for c in first.citations} >= {"things/alder", "f_1", "f_old", "people/jane", "f_9"}
    assert evidence[1].lines == ['- [^calls/kickoff] Kickoff: {"account": "things/alder", "starts": "2026-11-02"}']


def test_an_agent_never_reads_text_it_or_the_other_writers_of_its_run_wrote(tmp_path: Path) -> None:
    overview = agent(tmp_path, "things/subagents/overview.md", "evidence:\n  - articles: [this, things/birch-yard]\n")
    birch = article("things/birch-yard", {"name": "Birch Yard"},
                 [("Overview", "Birch Yard is mine.", "things/subagents/overview"),
                  ("Stakeholders", "Birch Yard has a sponsor.", "things/subagents/stakeholders")])
    lookup = StandIn([ALDER, birch], {})
    writers = ["things/article", "things/subagents/overview", "things/subagents/stakeholders"]
    text = render(gather(overview, lookup, {}, this="things/alder", writers=writers))
    assert "the one you write, without your own text" in text and "Alder renews" not in text
    assert "Jane Doe signs" not in text  # a sibling's part of the article this run rewrites
    # another article, less its own part
    assert "Birch Yard has a sponsor." in text and "Birch Yard is mine." not in text


def test_a_fact_of_a_query_on_several_linked_articles_is_labelled_by_the_one_it_is_tied_to(tmp_path: Path) -> None:
    from cerebro.connector import Record
    from cerebro.fakes import FakeExtraction, MemoryStore
    from cerebro.identity import RECORD, SUBJECT
    from cerebro.lookup import StoreLookup

    store, extraction = MemoryStore(), FakeExtraction(names=["Ann Lee"])
    for id, title in (("things/alder", "Alder"), ("things/birch", "Birch"), ("people/ann", "Ann Lee")):
        store.create(Identity(id, id.split("/")[0], title))
    store.create(Identity("parts/one", "parts", "One"))
    store.set_field("parts/one", "linked", Property(["things/alder", "things/birch"], Caller("t", "person"), "2026-11-01T00:00:00Z"))
    [fact] = extraction.extract(Record(id="local/a.md", source="local", revision="1", body="Ann Lee signs.")).facts
    store.link(fact.id, ["things/alder"], how=RECORD)
    store.link(fact.id, ["people/ann"], how=SUBJECT)
    reader = agent(tmp_path, "parts/article.md", "evidence:\n  - query: { from: facts, articles: this.linked }\n")
    [entry] = gather(reader, StoreLookup(store, extraction), {}, this="parts/one")
    assert entry.items[0].text.endswith(' [from a record of "Alder"; subject: "Ann Lee"]'), entry.items[0].text


def test_a_query_line_on_facts_takes_a_list_of_classes_or_kinds_as_a_facts_line_does(tmp_path: Path) -> None:
    from cerebro.connector import Record
    from cerebro.fakes import FakeExtraction, MemoryStore
    from cerebro.lookup import StoreLookup

    extraction = FakeExtraction(kinds=["alpha", "beta", "gamma"])
    for kind in ("alpha", "beta", "gamma"):
        extraction.extract(Record(id=kind, source="local", revision="1", body=f"A {kind} fact.", fields={"kind": kind}))
    reader = agent(tmp_path, "parts/article.md", "evidence:\n  - query: { from: facts, classes: [alpha, beta] }\n"
                   "  - query: { from: facts, kind: [gamma] }\n")
    both, one = gather(reader, StoreLookup(MemoryStore(), extraction), {}, this="parts/one")
    assert sorted(c.source for c in both.citations) == ["alpha", "beta"] and [c.source for c in one.citations] == [
        "gamma"]
    with pytest.raises(AgentFileError, match=r"evidence 1, query kind is not a class or a list of them"):
        agent(tmp_path, "parts/article.md", "evidence:\n  - query: { from: facts, kind: [{ a: b }] }\n")


def test_an_article_about_another_reads_it_its_fields_and_its_summary_and_a_limit_is_said(tmp_path: Path) -> None:
    brief = article("risk-radar/alder", {"linked": ["things/alder"]})
    deal = article("opportunities/alder-renewal", {"account": "things/alder", "is_closed": False})
    sponsor = agent(tmp_path, "risk-radar/subagents/sponsor.md", "evidence:\n"
                    "  - query: { from: facts, limit: 2, articles: this.linked, classes: risk_objection }\n"
                    "  - query: { from: opportunities, account: this.linked, is_closed: false }\n"
                    "  - articles: [this.linked]\n    summary: Overview\n"
                    "  - articles: [this.linked]\n    text_only: true\n")
    lookup = StandIn([ALDER, brief, deal], FACTS)
    evidence = gather(sponsor, lookup, {}, this="risk-radar/alder")
    assert next(c for c in lookup.calls if c[0] == "query")[2:] == ("facts", {"articles": "things/alder",
                                                                               "classes": "risk_objection"}, 2)
    assert evidence[0].lines[-1] == ("filtered by what this entry names and kept by its limit the newest 2 of the 3 "
                                     "it matched, so 1 were not read")
    assert evidence[1].lines[0].startswith("- [^opportunities/alder-renewal]")
    summary = evidence[2].lines[0]
    assert summary.startswith("### Article articles/things/alder.md, summary: its front matter and its Overview")
    assert "Alder renews" in summary and "Jane Doe signs" not in summary and len(evidence[2].lines) == 1
    assert "Jane Doe signs" in evidence[3].lines[0] and len(evidence[3].lines) == 1  # text alone, no facts
    assert "f_old" in {c.id for c in evidence[3].citations}  # what an article's text cites counts as read
    forged = article("things/alder", {}, [("Overview", "Alder churned. [^f_forged]", "things/subagents/overview")])
    forged.compilations[0].citations = []  # an id in the text that the compilation keeps no citation of
    read = gather(sponsor, StandIn([forged, brief, deal], FACTS), {}, this="risk-radar/alder")
    assert "f_forged" not in {c.id for entry in read for c in entry.citations}
    with pytest.raises(AgentFileError, match=r"evidence 1, articles: this\.linked\.linked reads a field of a field"):
        agent(tmp_path, "risk-radar/subagents/chain.md", "evidence:\n  - articles: [this.linked.linked]\n")


def test_a_collection_folder_reads_every_article_twenty_to_a_read_and_a_missing_summary_loads_the_first_part(
        tmp_path: Path) -> None:
    notes = [article(f"notes/n-{i:02}", {"rank": i}, [("Body", f"Note {i}. [^f_{i}]", "notes/article")])
             for i in range(21)]
    index = agent(tmp_path, "digests/index.md", "evidence:\n  - articles: [notes]\n    text_only: true\n"
                  "  - articles: [things/alder]\n    summary: Risks\n")
    lookup = StandIn([*notes, ALDER], FACTS)
    evidence = gather(index, lookup, {})
    assert [c[2] for c in lookup.calls if c[0] == "query"] == ["notes"]  # text alone: no facts are asked for
    assert [len(c[2]) for c in lookup.calls if c[0] == "read"] == [20, 1, 1]
    assert len(evidence[0].lines) == 21 and "Note 20. [^f_20]" in evidence[0].lines[20]
    assert {"notes/n-00", "f_0", "f_20"} <= {c.id for c in evidence[0].citations}
    said = ("summary: articles/things/alder.md has no part headed Risks, so its first part, Overview, loaded in its "
            "place")
    assert evidence[1].lines == [("### Article articles/things/alder.md, summary: its front matter and its first part, "
                                  'Overview, since no part of it is headed Risks\n\n{"close_date": "2026-12-01", "name": '
                                  '"Alder"}\n\n#### Overview\n\nAlder renews in December. [^f_old]'), said]
    assert evidence[1].notes == [said]


def test_preload_stops_at_the_first_item_that_does_not_fit_and_fills_the_cap_exactly() -> None:
    def entries() -> list[Evidence]:
        return [Evidence("A", items=[Item("a1", "x" * 98), Item("big", "y" * 900), Item("a3", "z")]),
                Evidence("B", items=[Item("b1", "w" * 8)])]

    room = sum(estimate_tokens(t) for t in ("## A\n\n", "x" * 98 + "\n", "y" * 900 + "\n", "z\n", "## B\n\n",
                                             "w" * 8 + "\n"))
    whole = preload(entries(), Budget(tokens_per_run=2 * room))
    assert [e.heading for e in whole] == ["A", "B"] and len(whole[0].lines) == 3
    cut = preload(entries(), Budget(tokens_per_run=2 * room - 2))
    assert [e.heading for e in cut] == [LEFT_OUT, "A", "B"]
    assert cut[1].lines == ["x" * 98] and cut[2].lines == ["None of this entry loaded: see What was left out above."]
    assert cut[0].lines[2:4] == ["- entry 1, A: 2 of 3 not loaded", "- entry 2, B: 1 of 1 not loaded"]


def test_preload_keeps_the_file_order_loads_each_fact_once_and_says_what_it_left_out(tmp_path: Path) -> None:
    many = {"things/alder": [fact(f"f_{n:02}", f"2026-10-{n:02}T00:00:00Z", f"notes:{n % 3}") for n in range(1, 31)]}
    writer = agent(tmp_path, "things/subagents/work-streams.md", "evidence:\n"
                   "  - query: { from: facts, articles: this, classes: timeline, limit: 5 }\n"
                   "  - facts: { window: last_365_days }\n  - query: { from: things }\n")
    lookup = StandIn([ALDER, article("things/birch-yard", {"name": "Birch Yard"})], many)
    roomy = gather(writer, lookup, {}, this="things/alder")
    assert [e.heading for e in roomy][:2] == ["Query from facts, articles this, classes timeline, limit 5",
                                              "Facts, window last_365_days"]
    assert [line[4:8] for line in roomy[0].lines[:5]] == ["f_30", "f_29", "f_28", "f_27", "f_26"]
    assert "f_30" not in "".join(roomy[1].lines) and len(roomy[1].lines) == 25  # loaded once, in the first entry
    writer.budget = Budget(tokens_per_run=1400)
    tight = gather(writer, lookup, {}, this="things/alder")
    told = tight[0]
    assert told.heading == LEFT_OUT and told.lines[0].startswith("Your evidence loads in the order your agent file")
    assert told.lines[2].startswith("- entry 2, facts, window last_365_days: ") and ", from notes:" in told.lines[2]
    assert told.lines[3].startswith("- entry 3, query from things: 2 of 2 not loaded: articles/things/alder.md")
    loaded = [line[4:8] for entry in tight[1:3] for line in entry.lines if line.startswith("- [^")]
    assert loaded == sorted(loaded, reverse=True) and loaded[:5] == ["f_30", "f_29", "f_28", "f_27", "f_26"]
    assert told.lines[2].startswith(f"- entry 2, facts, window last_365_days: {25 - len(tight[2].lines)} of 25 not")
    assert tight[3].lines == ["None of this entry loaded: see What was left out above."] and not tight[3].citations
    assert estimate_tokens(render(tight)) <= 700  # half the budget, the lines that say what was left out included
    cited = {c.id for entry in tight for c in entry.citations}
    assert cited == {line[4:8] for entry in tight for line in entry.lines if line.startswith("- [^f_")}


def test_the_digest_moves_with_what_the_lines_return_and_not_with_the_budget_or_the_day(tmp_path: Path) -> None:
    writer = agent(tmp_path, "reports/weekly.md", "evidence:\n  - query: { from: things, close_date: this_quarter }"
                   "\n  - facts: {}\n")
    facts = {"reports/weekly": [fact("f_1", "2026-11-01T00:00:00Z")]}
    lookup = StandIn([ALDER], facts)
    first = digest(writer, gather(writer, lookup, {}, now=NOW))
    writer.budget = Budget(tokens_per_run=60)
    cut = gather(writer, lookup, {}, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert cut[0].heading == LEFT_OUT and digest(writer, cut) == first
    ALDER.properties["compiled_at"].value = "2026-11-16T00:00:00Z"
    assert digest(writer, gather(writer, lookup, {})) == first
    facts["reports/weekly"].append(fact("f_2", "2026-11-02T00:00:00Z"))
    second = digest(writer, gather(writer, lookup, {}))
    assert second != first
    facts["reports/weekly"][0].text = "Fact f_1, restated."
    third = digest(writer, gather(writer, lookup, {}))
    assert third not in (first, second)
    writer.path.write_text(writer.path.read_text() + "Say it in fewer words.\n")
    assert digest(writer, gather(writer, lookup, {})) != third  # an edit of the file is a change too


def test_the_digest_moves_when_a_file_a_function_returned_changes_in_place(tmp_path: Path) -> None:
    writer = agent(tmp_path, "reports/weekly.md", "evidence:\n  - facts: {}\n")
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"first picture")
    entry = Evidence("Charts", spelled="charts", files=[File("chart.png", "image/png", path=chart)])
    first = digest(writer, [entry])
    chart.write_bytes(b"second picture")  # the same path, new bytes: the run reads these
    assert digest(writer, [entry]) != first


def test_left_and_script_refuse_a_caller_that_hands_in_no_kept_sets_or_no_starter(tmp_path: Path) -> None:
    usage = script_file(tmp_path, "scripts/usage.sh")
    writer = agent(tmp_path, "reports/pipeline.md", "evidence:\n  - left: { from: opportunities, stage: open }\n"
                   f"  - script: {{ run: {usage} }}\n")
    with pytest.raises(NotBuilt, match=r"^agents/reports/pipeline.md: evidence 1, left cannot run here: .*"
                                       r"gather was given none: pass last$"):
        gather(writer, StandIn([], {}), {}, root=tmp_path)
    with pytest.raises(NotBuilt, match=r"^agents/reports/pipeline.md: evidence 2, script cannot run here: .*"
                                       r"gather was given none: pass starter$"):
        gather(writer, StandIn([], {}), {}, root=tmp_path, last=LastSets())


def test_files_load_whole_and_a_line_with_no_article_to_run_on_is_refused(tmp_path: Path) -> None:
    (tmp_path / "skills" / "prep").mkdir(parents=True)
    (tmp_path / "skills" / "prep" / "SKILL.md").write_text("Prepare a call.")
    (tmp_path / "workspace.md").write_text("Who this workspace is for.")
    reader = agent(tmp_path, "reports/weekly.md", "evidence:\n  - files: [workspace.md, skills]\n")
    entry = gather(reader, StandIn([], {}), {}, root=tmp_path)[0]
    assert entry.lines == ["### File workspace.md\n\nWho this workspace is for.",
                           "### File skills/prep/SKILL.md\n\nPrepare a call."]
    writer = agent(tmp_path, "things/article.md", "evidence:\n  - facts: {}\n")
    with pytest.raises(EvidenceError, match="was given none: run it on an article"):
        gather(writer, StandIn([], {}), {})
    odd = agent(tmp_path, "notes/weekly.md", "evidence:\n  - usage: { account: this }\n")
    with pytest.raises(EvidenceError, match="evidence kind 'usage' is not one Cerebro or an app gives"):
        gather(odd, StandIn([], {}), {})
    assert Citation("record", "u1", "usage") in gather(odd, StandIn([], {}), {"usage": lambda account: [
        {"id": "u1", "text": f"{account} uses 4 seats"}]})[0].citations


def test_real_gtm_template_agent_files_gather_through_the_real_lookup_and_store(tmp_path: Path) -> None:
    from conftest import GTM

    from cerebro.connector import Record
    from cerebro.fakes import FakeExtraction
    from cerebro.lookup import StoreLookup
    from cerebro.store import SQLiteStore

    recent = "2026-11-06T09:00:00Z"
    notes = [Record(id="calls/1", source="local", revision="1", at=recent, fields={"kind": "risk_objection"},
                    body="Acme worries about the security review."),
             Record(id="calls/2", source="local", revision="1", at="2024-01-01T09:00:00Z", body="Acme was a lead."),
             Record(id="docs/3", source="local", revision="1", at=recent, body="The pilot playbook says start small.")]
    extraction = FakeExtraction()
    with SQLiteStore(tmp_path / "cerebro.sqlite") as store:
        store.create(Identity("accounts/acme", "accounts", "Acme"))
        store.create(Identity("knowledge/pilot-playbook", "knowledge", "Pilot playbook"))
        for record in notes[:2]:
            store.link(extraction.extract(record).facts[0].id, ["accounts/acme"], at=recent)
        extraction.extract(notes[2])
        lookup = StoreLookup(store, extraction, clock=lambda: NOW)
        overview = load_agent(GTM / "accounts" / "subagents" / "overview.md", GTM)
        concerns, recent_facts, deals = gather(overview, lookup, {}, this="accounts/acme", root=tmp_path, now=NOW)[-3:]
        assert deals.items == []  # the account's open deals, of which it has none
        assert [i.text for i in concerns.items if "security" in i.text] and len(concerns.items) == 1
        assert [i.left for i in recent_facts.items] == ["calls/1"]  # the 2024 fact is out of the window
        knowledge = load_agent(GTM / "knowledge" / "article.md", GTM)
        named = gather(knowledge, lookup, {}, this="knowledge/pilot-playbook", root=tmp_path, now=NOW)
        assert any("playbook says start small" in i.text for entry in named for i in entry.items)


def test_a_files_line_reads_only_visible_files_inside_the_workspace(tmp_path: Path) -> None:
    from cerebro.fakes import MemoryStore
    from cerebro.lookup import StoreLookup

    workspace, outside = tmp_path / "workspace", tmp_path / "secret.txt"
    (workspace / "skills").mkdir(parents=True)
    outside.write_text("outside the workspace")
    (workspace / "skills" / "plain.md").write_text("A plain skill.")
    (workspace / "skills" / ".env").write_text("TOKEN=hidden")
    (workspace / "skills" / "link.md").symlink_to(outside)
    (workspace / ".cerebro").mkdir()
    (workspace / ".cerebro" / "state.md").write_text("state")
    agent = Agent(id="reports/weekly", path=workspace / "agents" / "reports" / "weekly.md", role="writer",
                  target="reports/weekly", prompt="x", evidence=[EvidenceLine("files", ["skills", ".cerebro/state.md"])])
    entry = gather(agent, StoreLookup(MemoryStore()), {}, root=workspace, now=NOW)[0]
    text = " ".join(i.text for i in entry.items)
    assert "A plain skill." in text and "hidden" not in text and "outside" not in text and "state" not in text
    assert any("leads outside" in note for note in entry.notes)


def test_a_summary_leaves_out_the_part_the_reading_agent_wrote() -> None:
    other = article("things/alder", {}, [("Overview", "Alder renews in March.", "things/subagents/overview"),
                                       ("Overview2", "x", "someone")])
    other.compilations[0].heading = "Overview"
    mine = article("things/alder", {}, [("Overview", "Written by the reader.", "reports/weekly")])
    agent = Agent(id="reports/weekly", path=Path("agents/reports/weekly.md"), role="writer", target="reports/weekly",
                  prompt="x", evidence=[EvidenceLine("articles", ["things/alder"], {"summary": "Overview"})])
    kept = gather(agent, StandIn([other], {}), {}, now=NOW)[0].lines[0]
    own = gather(agent, StandIn([mine], {}), {}, now=NOW)[0].lines[0]
    assert "Alder renews in March." in kept and "Written by the reader." not in own


# -- left ---------------------------------------------------------------------------------------------------------

QUARTER = "query from opportunities, close_date this_quarter"


def deals(rows: dict[str, dict[str, Any]], now: datetime, zone: Any = UTC, archived: tuple[str, ...] = ()) -> Any:
    """A real lookup over a store fake that holds these opportunities, read at `now` in `zone`."""
    from cerebro.fakes import MemoryStore
    from cerebro.lookup import StoreLookup

    store = MemoryStore()
    for id, front in rows.items():
        store.create(Identity(id, id.split("/")[0], id.split("/")[1],
                              status="archived" if id in archived else "active"))
        for key, value in front.items():
            store.set_field(id, key, Property(value, SOURCE, "2026-10-01T00:00:00Z"))
    return StoreLookup(store, clock=lambda: now, zone=zone)


def left_of(entries: list[Evidence]) -> Evidence:
    return next(e for e in entries if e.returns_now is not None)


def test_left_holds_what_the_last_run_counted_that_left_by_its_own_fields_with_why(tmp_path: Path) -> None:
    writer = agent(tmp_path, "reports/pipeline.md", "evidence:\n  - left: { from: opportunities, close_date: this_quarter }\n")
    assert left_key(writer.evidence[0]) == QUARTER
    lookup = deals({"opportunities/slipped": {"close_date": "2027-02-10"},
                    "opportunities/still-in": {"close_date": "2026-11-20"},
                    "opportunities/pulled-in": {"close_date": "2026-09-15T17:00:00Z"},
                    "opportunities/new": {"close_date": "2026-12-05"}}, NOW)
    last = LastSets({QUARTER: ["opportunities/slipped", "opportunities/still-in", "opportunities/gone",
                               "articles/opportunities/pulled-in.md"]}, datetime(2026, 11, 9, 9, tzinfo=UTC))
    entry = gather(writer, lookup, {}, last=last, now=NOW)[0]
    assert entry.heading == ("Left since the last run on Mon Nov 9, 2026, query from opportunities, close_date "
                             "this_quarter: 2 articles it returned then that it no longer returns, read as of that "
                             "run, each as it stands now with why it left")
    assert entry.lines == [
        ("### Article articles/opportunities/slipped.md\n\nWhy it left: close_date is now 2027-02-10, after this_quarter"
        ' as of the last run, Oct 1 to Dec 31, 2026.\n\n{"close_date": "2027-02-10"}'),
        ("### Article articles/opportunities/pulled-in.md\n\nWhy it left: close_date is now 2026-09-15T17:00:00Z, "
        'before this_quarter as of the last run, Oct 1 to Dec 31, 2026.\n\n{"close_date": "2026-09-15T17:00:00Z"}')]
    assert entry.citations == [Citation("article", "opportunities/slipped", "opportunities/slipped"),
                               Citation("article", "opportunities/pulled-in", "opportunities/pulled-in")]
    assert sorted(entry.returns_now) == ["opportunities/new", "opportunities/still-in"]  # what compile keeps next
    as_of_now = gather(writer, lookup, {}, last=LastSets(last.sets), now=NOW)[0]
    assert as_of_now.heading == (f"Left since the last run, {QUARTER}: 2 articles it returned then that it no longer "
                                 "returns, read as of now, each as it stands now with why it left")
    assert "Why it left: close_date is now 2027-02-10, after this_quarter, the query read as of now." in \
        as_of_now.lines[0]
    first = gather(writer, lookup, {}, last=LastSets(), now=NOW)[0]
    assert first.lines == [("the agent keeps no set of this query on this article yet, so nothing has left: the first "
                           "write that lands keeps one")]


def test_left_reads_the_query_as_of_the_last_run_so_the_calendar_moving_on_leaves_nothing(tmp_path: Path) -> None:
    writer = agent(tmp_path, "reports/pipeline.md",
                   "evidence:\n  - left: { from: opportunities, close_date: this_quarter, stage: open }\n")
    january = datetime(2027, 1, 4, 9, tzinfo=UTC)
    lookup = deals({"opportunities/closes-dec": {"close_date": "2026-12-30", "stage": "open"},
                    "opportunities/moved-jan": {"close_date": "2027-01-15", "stage": "open"},
                    "opportunities/won": {"close_date": "2026-12-01", "stage": "won"}}, january)
    key = left_key(writer.evidence[0])
    last = LastSets({key: ["opportunities/closes-dec", "opportunities/moved-jan", "opportunities/won"]},
                    datetime(2026, 12, 28, 9, tzinfo=UTC))
    entry = gather(writer, lookup, {}, last=last, now=january)[0]
    assert [line.split("\n")[0] for line in entry.lines] == ["### Article articles/opportunities/moved-jan.md",
                                                             "### Article articles/opportunities/won.md"]
    assert "close_date is now 2027-01-15, after this_quarter as of the last run, Oct 1 to Dec 31, 2026." \
        in entry.lines[0]
    assert "Why it left: stage is now won, where the query asks for open." in entry.lines[1]
    assert entry.returns_now == ["opportunities/moved-jan"]  # this quarter, read now, is January's


def test_left_reads_its_windows_and_its_day_in_the_lookups_zone_and_says_an_archived_one(tmp_path: Path) -> None:
    from zoneinfo import ZoneInfo

    writer = agent(tmp_path, "reports/pipeline.md", "evidence:\n  - left: { from: opportunities, close_date: this_quarter }\n")
    lookup = deals({"opportunities/edge": {"close_date": "2026-06-30"}, "opportunities/late": {"close_date": "2026-10-01"},
                    "opportunities/held": {"close_date": "2026-09-30"}, "opportunities/shelved": {"close_date": "2026-08-01"}},
                   NOW, ZoneInfo("America/Los_Angeles"), archived=("opportunities/shelved",))
    last = LastSets({QUARTER: ["opportunities/edge", "opportunities/late", "opportunities/held", "opportunities/shelved"]},
                    # Sep 30 in the workspace's zone, the third quarter's last day
                    datetime(2026, 10, 1, 3, tzinfo=UTC))
    entry = gather(writer, lookup, {}, last=last, now=NOW)[0]
    assert entry.heading.startswith("Left since the last run on Wed Sep 30, 2026, query from opportunities")
    assert [line.split("\n\n")[1] for line in entry.lines] == [
        "Why it left: close_date is now 2026-06-30, before this_quarter as of the last run, Jul 1 to Sep 30, 2026.",
        "Why it left: close_date is now 2026-10-01, after this_quarter as of the last run, Jul 1 to Sep 30, 2026.",
        "Why it left: it is archived now."]
    assert entry.returns_now == ["opportunities/late"]


def test_the_starter_pipeline_report_loads_and_gathers_its_left_line_which_moves_no_digest() -> None:
    from conftest import GTM as STARTER

    pipeline = load_agent(STARTER / "reports" / "pipeline.md", STARTER)
    assert pipeline.role == "writer" and [line.kind for line in pipeline.evidence].count("left") == 1
    lookup = deals({"opportunities/slipped": {"close_date": "2027-02-10", "is_closed": False},
                    "opportunities/overdue": {"close_date": "2026-09-15", "is_closed": False},
                    "opportunities/due": {"close_date": "2026-12-15", "is_closed": False}}, NOW)
    quarter = f"{QUARTER}, order_by close_date"  # the starter's lines name their order
    last = LastSets({quarter: ["opportunities/slipped", "opportunities/overdue", "opportunities/due"]},
                    datetime(2026, 11, 9, 9, tzinfo=UTC))
    entries = gather(pipeline, lookup, {}, last=last, now=NOW)
    left = left_of(entries)
    assert [e.spelled for e in entries][3] == f"left {quarter.removeprefix('query ')}"
    assert left.lines[0].startswith("### Article articles/opportunities/slipped.md\n\nWhy it left: close_date is now "
                                    "2027-02-10, after this_quarter as of the last run, Oct 1 to Dec 31, 2026.\n\n{")
    assert left.lines[1] == ("### Article articles/opportunities/overdue.md, shown above\n\nWhy it left: close_date is "
                             "now 2026-09-15, before this_quarter as of the last run, Oct 1 to Dec 31, 2026.")
    assert left.returns_now == ["opportunities/due"]
    assert entries[5].lines[0].startswith("- [^opportunities/slipped] ")  # open, closing next quarter
    assert digest(pipeline, entries) == digest(pipeline, gather(pipeline, lookup, {}, last=LastSets(), now=NOW))


# -- script -------------------------------------------------------------------------------------------------------


def script_file(root: Path, rel: str, body: str = "#!/bin/sh\necho hi\n", mode: int = 0o755) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return rel


class FakeStarter:
    """Starts nothing: records each launch and answers with what the test set."""

    def __init__(self, finished: Finished | Exception):
        self.finished = finished
        self.launches: list[Launch] = []

    def __call__(self, launch: Launch) -> Finished:
        self.launches.append(launch)
        if isinstance(self.finished, Exception):
            raise self.finished
        return self.finished


def usage_agent(root: Path, rel: str = "scripts/usage.py") -> Agent:
    return agent(root, "reports/usage.md", f"evidence:\n  - script: {{ run: {rel} }}\n")


def test_a_script_runs_in_the_workspace_with_a_fixed_environment_and_what_it_prints_is_cited_data(tmp_path: Path,
                                                                                            monkeypatch) -> None:
    monkeypatch.setenv("SOME_KEY", "secret")
    script_file(tmp_path, "scripts/usage.py")
    (tmp_path / "bin").symlink_to(tmp_path / "scripts")  # a link inside the workspace is found by its real path
    starter = FakeStarter(Finished(0, b"Alder uses 4 seats.\nBirch Yard uses 2.\n\n\nInitech uses none.\n",
                                   b"a warning"))
    entry = gather(usage_agent(tmp_path, "bin/usage.py"), StandIn([], {}), {}, root=tmp_path, starter=starter)[0]
    [launch] = starter.launches
    real = (tmp_path / "scripts" / "usage.py").resolve()
    assert launch.argv == [str(real)] and launch.cwd == tmp_path.resolve()
    assert set(launch.env) == {"PATH", WORKSPACE_ENV}
    assert launch.env[WORKSPACE_ENV] == str(tmp_path.resolve())
    assert (launch.seconds, launch.limit) == (SCRIPT_SECONDS, SCRIPT_BYTES)
    assert entry.heading == "Script bin/usage.py, what it printed"
    assert entry.lines == ["- [^script:bin/usage.py:1] Alder uses 4 seats.\nBirch Yard uses 2.",
                           "- [^script:bin/usage.py:2] Initech uses none."]
    assert entry.citations == [Citation("record", "script:bin/usage.py:1", "script:bin/usage.py"),
                               Citation("record", "script:bin/usage.py:2", "script:bin/usage.py")]
    assert launch.env["PATH"] == os.environ["PATH"]  # the one variable it takes from the parent
    assert "a warning" not in render([entry])


def test_a_scripts_changed_output_moves_the_digest_and_its_stderr_does_not(tmp_path: Path) -> None:
    script_file(tmp_path, "scripts/usage.py")
    writer = usage_agent(tmp_path)

    def stamp(out: bytes, err: bytes = b"") -> str:
        return digest(writer, gather(writer, StandIn([], {}), {}, root=tmp_path, starter=FakeStarter(
            Finished(0, out, err))))

    first = stamp(b"Alder uses 4 seats.\n")
    assert stamp(b"Alder uses 4 seats.\n", b"took 2s") == first
    assert stamp(b"Alder uses 5 seats.\n") != first  # the agent is due


@pytest.mark.parametrize(("rel", "problem"), [
    ("/bin/sh", "is outside the workspace's folder: name a path inside it"),
    ("../usage.py", "is outside the workspace's folder"),
    (".venv/bin/usage.py", "is in a hidden folder or file (.venv)"),
    ("node_modules/tool/usage.py", "is in a vendored folder (node_modules)"),
    ("scripts/missing.py", "does not exist: write it, or fix the path"),
    ("scripts/escape.py", "leads outside the workspace's folder through a link"),
    ("scripts/state.py", "resolves to a file in a hidden folder or file (.cerebro)"),
    ("scripts/plain.py", "is not executable: run chmod +x scripts/plain.py"),
    ("scripts", "is not a regular file"),
])
def test_a_script_must_be_a_regular_executable_file_inside_the_workspace_by_its_real_path(tmp_path: Path, rel: str,
                                                                                      problem: str) -> None:
    workspace = tmp_path / "workspace"
    script_file(workspace, ".venv/bin/usage.py")
    script_file(workspace, "node_modules/tool/usage.py")
    script_file(workspace, "scripts/plain.py", mode=0o644)
    script_file(tmp_path, "outside.py")
    script_file(workspace, ".cerebro/state.py")
    (workspace / "scripts" / "escape.py").symlink_to(tmp_path / "outside.py")
    (workspace / "scripts" / "state.py").symlink_to(workspace / ".cerebro" / "state.py")
    starter = FakeStarter(Finished(0, b"never"))
    with pytest.raises(ScriptError) as caught:
        gather(usage_agent(workspace, rel), StandIn([], {}), {}, root=workspace, starter=starter)
    assert str(caught.value).startswith(f"agents/reports/usage.md: evidence 1, script {rel}: {problem}")
    assert "\n" not in str(caught.value) and not starter.launches


@pytest.mark.parametrize(("finished", "problem"), [
    (Finished(3, b"half", b"\n  no such account\nTraceback"), ("exited 3, saying no such account: run it by hand in "
                                                              "the workspace's folder to see why, and fix it")),
    (Finished(-9), "was stopped by signal 9, and wrote nothing to stderr: run it by hand"),
    (Finished(None), "was stopped before it exited"),
    (Finished(None, b"partial", timed_out=True), (f"ran past {SCRIPT_SECONDS:g} seconds and was stopped: make it "
                                                 "finish sooner")),
    (Finished(0, b"\xff\xfe"), "printed text that is not UTF-8: have it print UTF-8 text"),
    (OSError(8, "Exec format error"), "could not start (Exec format error): give it a #! line"),
])
def test_a_failing_script_stops_the_gather_with_one_line_that_names_it(tmp_path: Path, finished: Any,
                                                                      problem: str) -> None:
    script_file(tmp_path, "scripts/usage.py")
    with pytest.raises(ScriptError) as caught:
        gather(usage_agent(tmp_path), StandIn([], {}), {}, root=tmp_path, starter=FakeStarter(finished))
    assert str(caught.value).startswith(f"agents/reports/usage.md: evidence 1, script scripts/usage.py: {problem}")
    assert "\n" not in str(caught.value)


def test_output_cut_at_the_limit_is_kept_and_said_and_a_bad_line_is_refused(tmp_path: Path) -> None:
    script_file(tmp_path, "scripts/usage.py")
    cut = Finished(None, "Seats: 4 €".encode()[:-1], cut=True)  # ends inside a character
    entry = gather(usage_agent(tmp_path), StandIn([], {}), {}, root=tmp_path, starter=FakeStarter(cut))[0]
    assert entry.lines == ["- [^script:scripts/usage.py:1] Seats: 4",
                           (f"it printed more than {SCRIPT_BYTES} bytes, so it was stopped and what came after is not "
                           "here: have it print less")]
    quiet = gather(usage_agent(tmp_path), StandIn([], {}), {}, root=tmp_path, starter=FakeStarter(Finished(0)))[0]
    assert quiet.lines == ["it printed nothing"]
    with pytest.raises(AgentFileError, match=r"odd.md: line 3: evidence 1, script takes run alone, not args"):
        agent(tmp_path, "reports/odd.md", "evidence:\n  - script: { run: scripts/usage.py, args: [x] }\n")
    odd = usage_agent(tmp_path)
    odd.evidence = [EvidenceLine("script", {"run": "scripts/usage.py", "args": ["x"]})]  # built in code, not read
    with pytest.raises(EvidenceError, match=r"evidence 1, script: it takes run alone"):
        gather(odd, StandIn([], {}), {}, root=tmp_path, starter=FakeStarter(Finished(0)))
    with pytest.raises(EvidenceError, match=r"gather was given none: pass root"):
        gather(usage_agent(tmp_path), StandIn([], {}), {}, starter=FakeStarter(Finished(0)))


def test_the_real_starter_runs_only_a_script_this_test_wrote_with_no_inherited_env_and_its_limits(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    import tempfile

    workspace = Path(tempfile.mkdtemp(prefix="evidence-workspace-"))  # a temp folder, so no checkout is needed or written
    try:
        monkeypatch.setenv("SOME_KEY", "secret")
        script_file(workspace, "scripts/usage.sh", '#!/bin/sh\necho "home ${HOME-unset} key ${SOME_KEY-unset}"\n'
                                               f'echo "workspace ${WORKSPACE_ENV}"\n'
                                               'echo "Alder uses 4 seats." >&2\nexit 0\n')
        writer = usage_agent(workspace, "scripts/usage.sh")
        entry = gather(writer, StandIn([], {}), {}, root=workspace, starter=start)[0]
        assert entry.lines == [(f"- [^script:scripts/usage.sh:1] home unset key unset\n"
                               f"workspace {workspace.resolve()}")]
        env = {"PATH": "/usr/bin:/bin"}
        loud = Path(script_file(workspace, "loud.sh", "#!/bin/sh\nwhile :; do echo 0123456789; done\n"))
        done = start(Launch([str(workspace / loud)], workspace, env, seconds=5, limit=25))
        assert done.cut and done.code is None and done.stdout == b"0123456789\n0123456789\n012"
        slow = Path(script_file(workspace, "slow.sh", "#!/bin/sh\necho started\nsleep 5\n"))
        done = start(Launch([str(workspace / slow)], workspace, env, seconds=0.3))
        assert done.timed_out and done.code is None and b"started\n".startswith(done.stdout)  # a loaded laptop may
        # not reach the echo in time
        exact = Path(script_file(workspace, "exact.sh", "#!/bin/sh\necho 0123456789\n"))
        assert start(Launch([str(workspace / exact)], workspace, env, limit=11)) == Finished(0, b"0123456789\n")
        import time

        left = Path(script_file(workspace, "left.sh", "#!/bin/sh\nsleep 30 &\necho done\n"))
        began = time.monotonic()
        assert start(Launch([str(workspace / left)], workspace, env)) == Finished(0, b"done\n")
        assert time.monotonic() - began < 3  # what it left running holding its output was stopped with its group
        failed = Path(script_file(workspace, "fail.sh", "#!/bin/sh\necho no such account >&2\nexit 3\n"))
        assert start(Launch([str(workspace / failed)], workspace, env)) == Finished(3, b"", b"no such account\n")
    finally:
        shutil.rmtree(workspace)


@pytest.mark.skipif(os.uname().sysname != "Linux", reason="only Linux has a subreaper to find such a child")
def test_a_child_that_starts_its_own_session_does_not_outlive_its_script_nor_hold_the_gather(tmp_path: Path) -> None:
    import sys
    import time

    late = tmp_path / "late"
    body = (f"#!{sys.executable}\nimport os, time\nprint('hi', flush=True)\nif os.fork() == 0:\n    os.setsid()\n"
            f"    time.sleep(0.3)\n    open({str(late)!r}, 'w').write('alive')\n    time.sleep(20)\n    os._exit(0)\n")
    script = tmp_path / script_file(tmp_path, "escape.py", body)
    began = time.monotonic()
    assert start(Launch([str(script)], tmp_path, {"PATH": "/usr/bin:/bin"}, seconds=5)) == Finished(0, b"hi\n")
    assert time.monotonic() - began < 1
    time.sleep(0.5)
    assert not late.exists()


def test_the_real_starter_runs_a_script_in_its_working_folder_and_in_a_session_of_its_own(tmp_path: Path) -> None:
    import sys

    script = tmp_path / script_file(tmp_path, "where.py", f"#!{sys.executable}\nimport os\n"
                                                          "print(os.getcwd())\nprint(os.getsid(0))\n")
    done = start(Launch([str(script)], tmp_path, {"PATH": "/usr/bin:/bin"}))
    folder, session = done.stdout.decode().split()
    assert Path(folder).resolve() == tmp_path.resolve() and int(session) != os.getsid(0)


@pytest.mark.skipif(os.uname().sysname != "Linux", reason="only Linux has a keeper to be too slow")
def test_a_cut_script_and_all_it_started_are_killed_even_when_the_keeper_is_too_slow_to_sweep(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import subprocess

    monkeypatch.setattr(importlib.import_module("cerebro.evidence"), "KEEPER_WAIT", 0.0)  # as on a loaded machine: the keeper is killed before it sweeps
    loud = tmp_path / script_file(tmp_path, "loud.sh", "#!/bin/sh\n(while :; do sleep 1; done) &\n"
                                                        "while :; do echo 0123456789; done\n")
    try:
        for _ in range(5):
            assert start(Launch([str(loud)], tmp_path, {"PATH": "/usr/bin:/bin"}, seconds=5, limit=25)).cut
        left = subprocess.run(["pgrep", "-f", str(loud)], check=False, capture_output=True, text=True).stdout.split()
        assert left == []
    finally:
        subprocess.run(["pkill", "-9", "-f", str(loud)], check=False)


# -- what an agent's own runs wrote never makes it due ------------------------------------------------------------

WEEKLY, OTHER = Caller("reports/weekly", "agent"), Caller("things/article", "agent")


def own_runs_workspace(tmp_path: Path, lines: str) -> tuple[Agent, Any, Any]:
    from cerebro.fakes import FakeExtraction, MemoryStore
    from cerebro.lookup import StoreLookup

    store = MemoryStore()
    store.create(Identity("reports/weekly", "reports", "Weekly"))
    return agent(tmp_path, "reports/weekly.md", f"evidence:\n{lines}"), store, StoreLookup(store, FakeExtraction())


def run_row(agent_id: str, run: str) -> dict[str, Any]:
    return {"run": run, "agent": agent_id, "target": agent_id, "status": "done", "ended_at": f"2026-11-1{run[-1]}"}


def proposal_row(id: str, by: Caller, status: str = "waiting", decided_by: Caller | None = None) -> dict[str, Any]:
    return {"id": id, "kind": "article.admit", "target": "reports/q4", "by": {"name": by.name, "kind": by.kind},
            "at": "2026-11-16T09:00:00Z", "status": status,
            "decided_by": {"name": decided_by.name, "kind": decided_by.kind} if decided_by else None}


@pytest.mark.parametrize(("log", "own", "another", "shown"), [
    ("agent-runs", run_row("reports/weekly", "run_2"), run_row("things/article", "run_3"), "run_2"),
    ("spend", {"at": "2026-11-16T09:00:00Z", "usd": 0.01, "run": "run_1"},
     {"at": "2026-11-16T09:00:00Z", "usd": 0.01, "run": "run_3"}, "run_1"),
    ("proposals", proposal_row("p_2", WEEKLY), proposal_row("p_3", OTHER), "p_2"),
    ("merges", {"at": "2026-11-16T09:00:00Z", "distinct_from": ["a/x", "a/y"], "by": "reports/weekly"},
     {"at": "2026-11-16T09:00:00Z", "distinct_from": ["a/x", "a/z"], "by": "things/curator"}, "a/y"),
])
def test_an_agents_own_rows_of_a_log_stay_in_what_it_reads_and_never_move_its_digest_and_anothers_do(
        tmp_path: Path, log: str, own: dict[str, Any], another: dict[str, Any], shown: str) -> None:
    writer, store, lookup = own_runs_workspace(tmp_path, f"  - query: {{ from: {log} }}\n")
    store.append("agent-runs", run_row("reports/weekly", "run_1"))
    store.append("agent-runs", run_row("things/article", "run_3"))  # so a spend row names a run of another's
    before = digest(writer, gather(writer, lookup, {}))
    store.append(log, own)
    read = gather(writer, lookup, {})
    assert digest(writer, read) == before and any(shown in line and json_has(line, own) for line in read[-1].lines)
    store.append(log, another)
    assert digest(writer, gather(writer, lookup, {})) != before


LEAD_SUB = Caller("reports/weekly-detail", "agent")  # a subagent that writes on the lead's article, as gtm's do


def _co_run(store: Any, by: Caller) -> None:
    store.append("agent-runs", run_row(by.name, "run_7" if by == LEAD_SUB else "run_8"))


def _co_spend(store: Any, by: Caller) -> None:
    store.append("spend", {"at": "2026-11-16T09:00:00Z", "usd": 0.01, "run": "run_5" if by == LEAD_SUB else "run_6"})


def _co_field(store: Any, by: Caller) -> None:
    store.set_field("reports/weekly", "level", Property("high" if by == LEAD_SUB else "low", by, "2026-11-16T09:00:00Z"))


def _co_rendered(store: Any, by: Caller) -> None:
    store.write_compilation(Compilation("reports/weekly", "Contents", f"- [Reports](./) ({by.name})", by="rendered:" + by.name))


def _co_rendered_there(store: Any, by: Caller) -> None:
    """A compilation rendered again on the article the co-writer writes, which the lead reads by its summary: the
    co-writer rendered it once before, so the summary shows a Contents heading either way."""
    store.write_compilation(Compilation("reports/detail", "Contents", f"- [Reports](./) ({by.name})", by="rendered:" + by.name))


@pytest.mark.parametrize(("line", "change"), [
    ("  - query: { from: agent-runs }\n", _co_run),
    ("  - query: { from: spend }\n", _co_spend),
    ("  - articles: [reports/weekly]\n", _co_field),
    ("  - articles: [reports/weekly]\n", _co_rendered),
    ("  - articles: [reports/detail]\n    summary: Contents\n", _co_rendered_there),
])
def test_what_a_co_writers_runs_leave_never_moves_the_leads_digest_and_what_another_agents_leave_does(
        tmp_path: Path, line: str, change: Any) -> None:
    lead, store, lookup = own_runs_workspace(tmp_path, line)
    store.create(Identity("reports/detail", "reports", "Detail"))
    store.write_compilation(Compilation("reports/detail", "Contents", "- [Reports](./)", by="rendered:" + LEAD_SUB.name))
    store.append("agent-runs", run_row(LEAD_SUB.name, "run_5"))
    store.append("agent-runs", run_row(OTHER.name, "run_6"))
    writers = [LEAD_SUB.name]
    before = digest(lead, gather(lead, lookup, {}, this="reports/weekly", writers=writers))
    change(store, LEAD_SUB)
    assert digest(lead, gather(lead, lookup, {}, this="reports/weekly", writers=writers)) == before
    change(store, OTHER)
    assert digest(lead, gather(lead, lookup, {}, this="reports/weekly", writers=writers)) != before


def json_has(line: str, row: dict[str, Any]) -> bool:
    return all(str(value) in line for value in row.values() if isinstance(value, str))


def test_a_person_deciding_an_agents_own_proposal_moves_its_digest_and_its_own_decision_does_not(
        tmp_path: Path) -> None:
    writer, store, lookup = own_runs_workspace(tmp_path, "  - query: { from: proposals }\n")
    store.append("proposals", proposal_row("p_1", OTHER))
    before = digest(writer, gather(writer, lookup, {}))
    store.append("proposals", proposal_row("p_1", OTHER, "accepted", WEEKLY))  # a proposal to it, which it decides
    assert digest(writer, gather(writer, lookup, {})) == before
    store.append("proposals", proposal_row("p_2", WEEKLY))
    assert digest(writer, gather(writer, lookup, {})) == before
    store.append("proposals", proposal_row("p_2", WEEKLY, "accepted", Caller("you", "person")))
    assert digest(writer, gather(writer, lookup, {})) != before


@pytest.mark.parametrize("line", ["  - articles: [reports/weekly]\n", "  - query: { from: reports }\n",
                                  "  - articles: [reports/weekly]\n    summary: Summary\n"])
def test_a_field_an_agent_set_on_its_own_article_never_moves_its_digest_and_its_curators_does(
        tmp_path: Path, line: str) -> None:
    writer, store, lookup = own_runs_workspace(tmp_path, line)
    before = digest(writer, gather(writer, lookup, {}))
    store.set_field("reports/weekly", "level", Property("high", WEEKLY, "2026-11-16T09:00:00Z"))
    store.set_field("reports/weekly", "tone", Property("brisk", WEEKLY, "2026-11-16T09:00:00Z"))
    read = gather(writer, lookup, {})
    assert digest(writer, read) == before and '"level": "high"' in read[0].lines[0]  # it reads what it set
    store.set_field("reports/weekly", "level", Property("medium", Caller("reports/curator", "agent"),
                                                        "2026-11-17T09:00:00Z"))
    assert digest(writer, gather(writer, lookup, {})) != before


def test_a_part_rendered_in_an_agents_name_stays_in_what_it_reads_and_never_moves_its_digest(
        tmp_path: Path) -> None:
    writer, store, lookup = own_runs_workspace(tmp_path, "  - articles: [reports/weekly]\n")
    before = digest(writer, gather(writer, lookup, {}))
    store.write_compilation(Compilation("reports/weekly", "Contents", "- [Reports](./) (1 article)",
                                by="rendered:reports/weekly"))
    read = gather(writer, lookup, {})
    assert digest(writer, read) == before and "#### Contents" in read[0].lines[0]
    store.write_compilation(Compilation("reports/weekly", "Notes", "Ana wrote this.", by="you"))
    assert digest(writer, gather(writer, lookup, {})) != before


def test_another_agents_article_still_moves_the_digest_of_an_agent_that_reads_it(tmp_path: Path) -> None:
    writer, store, lookup = own_runs_workspace(tmp_path, "  - articles: [things/alder]\n")
    store.create(Identity("things/alder", "things", "Alder"))
    before = digest(writer, gather(writer, lookup, {}))
    store.set_field("things/alder", "level", Property("high", OTHER, "2026-11-16T09:00:00Z"))
    after = digest(writer, gather(writer, lookup, {}))
    store.write_compilation(Compilation("things/alder", "Overview", "Alder renews.", by=OTHER.name))
    assert len({before, after, digest(writer, gather(writer, lookup, {}))}) == 3


def test_a_passage_kept_from_the_second_of_two_parts_under_one_heading_cites_what_that_part_cites() -> None:
    from cerebro.evidence import KeepJudge, KeepScores
    from cerebro.fakes import MemoryStore
    from cerebro.lookup import StoreLookup

    class Zebra(KeepJudge):
        def score(self, instruction: str, passages: list[Any], bar: float = 0.5) -> KeepScores:
            return KeepScores([1.0 if "zebrafish" in p.text else 0.0 for p in passages], scored=len(passages))

    store = MemoryStore()
    store.create(Identity("calls/kickoff", "calls", "Kickoff"))
    text = "## Update\n\nThe apple orchard renewed [^f_apple].\n\n## Update\n\nThe zebrafish migration halted [^f_zebra]."
    store.write_compilation(Compilation("calls/kickoff", "calls/article", text, by="calls/article", whole=True, citations=[
        Citation("fact", "f_apple", quote="apple"), Citation("fact", "f_zebra", quote="zebra")]))
    reader = Agent(id="reports/weekly", path=Path("agents/reports/weekly.md"), role="writer", target="reports/weekly",
                   prompt="x", evidence=[EvidenceLine("articles", ["calls"], {"keep": "zebrafish", "text_only": True})])
    entries = gather(reader, StoreLookup(store), {}, this="reports/weekly", judge=Zebra())
    item = next(i for e in entries for i in e.items if i.key.startswith("kept:"))
    assert [c.id for c in item.citations] == ["calls/kickoff", "f_zebra"] and "apple" not in item.text


def test_a_summary_that_falls_back_takes_the_first_part_an_agent_wrote_not_one_code_wrote_before_it() -> None:
    from cerebro.evidence import _summary_of

    article = Article(Identity("digests/index", "digests", "Index"), [
        Compilation("digests/index", "Contents", "- [Things](../things/)", "rendered:digests/index"),
        Compilation("digests/index", "digests/index", "## Things\n\nAlder [^f_1].", "digests/index", whole=True)])
    taken, fell = _summary_of(article, "Overview", "reports/weekly")
    assert fell and [(s.heading, s.by) for s in taken] == [("Things", "digests/index")]
