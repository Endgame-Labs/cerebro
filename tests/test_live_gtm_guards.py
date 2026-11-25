"""The live checks' own-side and borrowed-facts guards, each shown to fail on a stand-in: an article for one of the
workspace's own people, in any collection; an account's article linking facts of a note keyed to another account, or
more than half of the workspace's facts; and one of the workspace's own people on a first run's approved list."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cerebro import Record
from cerebro.fakes import FakeExtraction, MemoryStore
from cerebro.identity import Identity

CHECKS = Path(__file__).resolve().parents[1] / "checks"


@pytest.fixture
def live_gtm(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.syspath_prepend(str(CHECKS))
    import live_gtm

    return live_gtm


NOTES = {"local/calls/a.md": "Account: Alder Works\nAttendees: Ann Ross <ann@alder.example>, Kim Vale <kim@ourco.example>"
                             "\n\nAnn Ross runs the pilot.\n\nThe pilot starts in May.",
         "local/calls/b.md": "Account: Birch Hall\nAttendees: Lee Moss <lee@birch.example>, Kim Vale <kim@ourco.example>"
                             "\n\nLee Moss signs the order.\n\nBirch Hall wants a quote."}


def stand_in(*articles: tuple[str, str, str]) -> SimpleNamespace:
    store, extraction = MemoryStore(), FakeExtraction()
    for id, collection, title in articles:
        store.create(Identity(id, collection, title))
    for rel, body in NOTES.items():
        extraction.extract(Record(id=rel, source="local", revision="1", at="2026-11-10T09:00:00Z", body=body))
    return SimpleNamespace(store=store, extraction=extraction)


def test_an_article_for_one_of_the_workspaces_own_people_in_any_collection_fails(live_gtm) -> None:  # type: ignore[no-untyped-def]
    reasons: list[str] = []
    live_gtm.own_side_articles(stand_in(("people/ann-ross", "people", "Ann Ross")), {"Kim Vale"}, [], reasons)
    assert reasons == []
    live_gtm.own_side_articles(stand_in(("people/kim-vale", "people", "Kim  vale"),
                                        ("meetings/kim-vale", "meetings", "Kim Vale")), {"Kim Vale"}, [], reasons)
    assert len(reasons) == 2 and all("on the workspace's own side" in r for r in reasons), reasons


def test_an_account_linking_facts_of_another_accounts_note_or_most_facts_fails(live_gtm) -> None:  # type: ignore[no-untyped-def]
    workspace = stand_in(("accounts/alder-works", "accounts", "Alder Works"), ("accounts/birch-hall", "accounts", "Birch Hall"))
    facts = {f.record: [] for f in workspace.extraction.facts()}
    for f in workspace.extraction.facts():
        facts[f.record].append(f.id)
    for id in facts["local/calls/a.md"]:
        workspace.store.link(id, ["accounts/alder-works"])
    reasons: list[str] = []
    live_gtm.borrowed_facts(workspace, live_gtm.accounts_of(NOTES), reasons)
    assert reasons == []  # its own note's facts, half of the workspace's
    workspace.store.link(facts["local/calls/b.md"][0], ["accounts/alder-works"])
    live_gtm.borrowed_facts(workspace, live_gtm.accounts_of(NOTES), reasons)
    assert any("a note keyed to Birch Hall" in r for r in reasons), reasons
    assert any("more than half" in r for r in reasons), reasons
