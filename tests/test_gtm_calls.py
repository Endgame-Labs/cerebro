"""The gtm template keeps one article for each call: the meeting on the calendar, its recording and the notes of it.
Before the call it holds the prep, and once the call has happened the write-up, never both."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from conftest import GTM

from cerebro.agent import load_agents
from cerebro.article import Identity, Property, body
from cerebro.caller import Caller
from cerebro.compile import compile_article, targets
from cerebro.connector import Record
from cerebro.evidence import gather
from cerebro.fakes import FakeExtraction, MemoryStore, ScriptedRun
from cerebro.lookup import StoreLookup

ARTICLE = "calls/acme-qbr"


def _facts(extraction: FakeExtraction, store: MemoryStore, record: Record, fields: bool = False) -> None:
    made = extraction.extract_fields(record) if fields else extraction.extract(record)
    for fact in made.facts:
        store.link(fact.id, [ARTICLE])


def test_a_call_holds_the_prep_until_the_calendar_says_it_happened_then_only_the_write_up(tmp_path: Path) -> None:
    agents = {a.id: a for a in load_agents(GTM)[0]}
    writer = agents["calls/article"]
    store, extraction = MemoryStore(), FakeExtraction()
    lookup = StoreLookup(store, extraction)
    store.create(Identity("accounts/acme", "accounts", "Acme"))
    store.create(Identity(ARTICLE, "calls", "Acme QBR"))
    _facts(extraction, store, Record(id="local/acme.md", source="local", revision="1", at="2026-11-10T09:00:00Z",
                                     body="Ann said Acme renewed."))
    seen: list[str] = []
    ahead = {"now": True}

    def model(prompt: str) -> str:  # it does as the prompt says: the prep while ahead, the write-up after
        seen.append(prompt)
        return "PREP." if ahead["now"] else "WRITE-UP."

    def event(status: str, start: str) -> None:  # the calendar's record as its connector lists it, a fact a field
        store.set_field(ARTICLE, "start", Property(start, Caller("calls/curator", "agent"), "2026-11-01T00:00:00Z"))
        _facts(extraction, store, Record(id="evt-qbr", source="calendar", revision=status, at=start, into="calls",
                                         fields={"title": "Acme QBR", "start": start, "status": status}), fields=True)

    def run() -> None:
        assert targets(writer, lookup) == [ARTICLE]
        compile_article(writer, lookup=lookup, store=store, agent_run=ScriptedRun(model), root=tmp_path,
                        functions={}, agent_tools={}, this=ARTICLE, subagents=list(agents.values()))

    event("upcoming", "2026-12-01T09:00:00Z")
    run()
    assert body(store.article(ARTICLE)) == "PREP."
    ahead["now"] = False
    event("past", "2026-11-19T09:00:00Z")  # the meeting ended: its status fact moves, and wakes the writer
    run()
    assert len(seen) == 2 and "past" in seen[-1].partition("<evidence>")[2]
    assert body(store.article(ARTICLE)) == "WRITE-UP."  # the prep went with it


def test_the_calls_curator_sees_each_recent_call_with_its_start_and_attendees(tmp_path: Path) -> None:
    curator = {a.id: a for a in load_agents(GTM)[0]}["calls/curator"]
    store, extraction = MemoryStore(), FakeExtraction()
    store.create(Identity(ARTICLE, "calls", "Acme QBR", {}, "active", Caller("calls/curator", "agent"),
                          "2026-11-10T00:00:00Z", "you took part in this call"))
    store.set_field(ARTICLE, "start", Property("2026-11-18T15:00:00Z", Caller("calls/curator", "agent"),
                                            "2026-11-10T00:00:00Z"))
    store.set_field(ARTICLE, "attendees", Property(["ann@acme.example", "me"], Caller("calls/article", "agent"),
                                                "2026-11-11T00:00:00Z"))
    evidence = gather(curator, StoreLookup(store, extraction), {}, now=datetime(2026, 11, 20, tzinfo=UTC),
                      root=tmp_path, sources={})
    row = next(line for entry in evidence for line in entry.lines if ARTICLE in line)
    assert "2026-11-18T15:00:00Z" in row and "ann@acme.example" in row
