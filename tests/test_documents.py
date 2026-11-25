"""`record: document` in a query of facts: the kind of record, never a record's name."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from test_mcp import over_mcp
from test_tokens import no_network  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro import Caller, Cerebro, Record
from cerebro.facts import Revisions
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.lookup import Documents, StoreLookup
from cerebro.store import SQLiteStore, Store

AGENT = Caller("knowledge/curator", "agent")
NOW = datetime(2026, 11, 18, 9, tzinfo=UTC)
FILES = {
    "local/knowledge/discounts.md": "Topic: Discounts\nDate: 2026-09-01\n\nReps may give up to 10% without approval.",
    "local/Knowledge/Attendees-guide.md": "Attendees: Ana Ruiz\n\nA guide filed as knowledge, whatever its top.",
    "local/onboarding.md": "# Onboarding\n\nA new rep shadows three calls in the first week.",
    "local/calls/alder-sync.md": "Account: Alder\nDate: 2026-11-02\n\nAlder asked for a pilot.",
    "local/meetings/standup.md": "Attendees: Ana Ruiz, Ben Ito\nDate: 2026-11-03\n\nBen owns the export.",
    "local/emails/renewal.md": "Subject: Renewal\n\nThe renewal is due in March.",
    "local/forwarded.md": "From: ana@alder.example\nSubject: Pricing\n\nAlder wants a quote.",
    "local/calls/knowledge/playbook.md": "# Playbook\n\nOpen with the customer's goal.",  # the nearest folder decides
    "local/knowledge/calls/alder-review.md": "# Alder review\n\nAlder wants the export first.",
}
DOCUMENTS = {"local/knowledge/discounts.md", "local/Knowledge/Attendees-guide.md", "local/onboarding.md",
             "local/calls/knowledge/playbook.md"}
EXPORT = "exports/4120.md"  # a connector's record whose id is a file on disk, which is still no document


def write(root: Path) -> None:
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    (root / "sources").mkdir(exist_ok=True)
    (root / "sources" / "local.yaml").write_text('kind: files\nselect:\n  - glob: "local/**/*.md"\n')
    (root / "sources" / "gong.yaml").write_text("kind: gong\n")
    (root / EXPORT).parent.mkdir(parents=True, exist_ok=True)
    (root / EXPORT).write_text("# Call 4120\n\nAlder signed.", encoding="utf-8")
    outside = root.parent / f"{root.name}-outside.md"
    outside.write_text("# Outside\n\nA file beside the workspace, not in it.", encoding="utf-8")


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    made = MemoryStore() if request.param == "memory" else SQLiteStore()
    yield made
    made.close()


def lookup_on(root: Path, store: Store) -> tuple[StoreLookup, Revisions]:
    write(root)
    layer = Revisions(FakeExtraction(), store, clock=lambda: NOW)
    for rel, text in FILES.items():
        layer.extract(Record(id=rel, source="local", revision="1", at="2026-11-01", body=text.split("\n\n", 1)[1]))
    layer.extract(Record(id="calls/4120", source="gong", revision="1", at="2026-11-04", body="Alder signed."))
    layer.extract(Record(id=EXPORT, source="gong", revision="1", at="2026-11-04", body="Alder signed the export."))
    layer.extract(Record(id=f"../{root.name}-outside.md", source="local", revision="1", at="2026-11-04",
                         body="A file beside the workspace, not in it."))
    layer.extract(Record(id="document", source="note", revision="1", at="2026-11-05", body="A note named document."))
    return StoreLookup(store, layer, root=root), layer


def records(result: Any) -> set[str]:
    return {f.record for f in result.items}


def test_record_document_gives_the_facts_of_documents_alone_whatever_their_date(tmp_path: Path, store: Store) -> None:
    lookup, _ = lookup_on(tmp_path, store)
    assert records(lookup.query(AGENT, "facts", {"record": "document"})) == DOCUMENTS
    assert records(lookup.query(AGENT, "facts", {"record": "Document"})) == DOCUMENTS  # in any case


def test_a_calls_notes_an_email_and_a_connectors_call_are_no_document(tmp_path: Path, store: Store) -> None:
    lookup, _ = lookup_on(tmp_path, store)
    found = records(lookup.query(AGENT, "facts", {"record": "document"}))
    assert not found & {"local/calls/alder-sync.md", "local/meetings/standup.md", "local/emails/renewal.md",
                        "local/forwarded.md", "calls/4120", EXPORT, "local/knowledge/calls/alder-review.md"}


def test_a_record_whose_path_leads_out_of_the_workspace_folder_is_no_document(tmp_path: Path, store: Store) -> None:
    lookup, _ = lookup_on(tmp_path, store)
    assert (tmp_path / f"../{tmp_path.name}-outside.md").is_file()
    assert not any(r.startswith("..") for r in records(lookup.query(AGENT, "facts", {"record": "document"})))


def test_a_record_named_document_is_not_the_kind_and_another_name_gives_its_own_record(tmp_path: Path,
                                                                                       store: Store) -> None:
    lookup, _ = lookup_on(tmp_path, store)
    assert "document" not in records(lookup.query(AGENT, "facts", {"record": "document"}))
    assert records(lookup.query(AGENT, "facts", {"record": "local/calls/alder-sync.md"})) == {
        "local/calls/alder-sync.md"}


def test_a_file_that_is_gone_gives_none(tmp_path: Path, store: Store) -> None:
    lookup, _ = lookup_on(tmp_path, store)
    (tmp_path / "local" / "knowledge" / "discounts.md").unlink()  # in a kind folder, so its top is never read
    assert "local/knowledge/discounts.md" not in records(lookup.query(AGENT, "facts", {"record": "document"}))


def test_record_document_with_latest_false_gives_every_save_of_every_document(tmp_path: Path, store: Store) -> None:
    lookup, layer = lookup_on(tmp_path, store)
    layer.extract(Record(id="local/knowledge/discounts.md", source="local", revision="2", at="2026-11-10",
                         body="Reps may give up to 15% without approval."))
    latest = lookup.query(AGENT, "facts", {"record": "document"}).items
    every = lookup.query(AGENT, "facts", {"record": "document", "latest": False}).items
    assert [f.text for f in every if f.status == "superseded"] == ["Reps may give up to 10% without approval."]
    assert {f.id for f in every} - {f.id for f in latest} == {f.id for f in every if f.status == "superseded"}


def test_a_workspace_with_no_folder_tells_no_document() -> None:
    assert not Documents(None).holds("local", "local/knowledge/discounts.md")


def test_record_document_reaches_an_outside_agent_over_mcp_and_the_json_api_alike(tmp_path: Path) -> None:
    from test_api import call
    from test_tokens import serving

    write(tmp_path)
    (tmp_path / "settings.yaml").write_text("{}\n")
    notes = FakeConnector("local", [Record(id=rel, source="local", revision="1", at="2026-11-01",
                                           body=text.split("\n\n", 1)[1]) for rel, text in FILES.items()])
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), connectors=[notes],
                    clock=lambda: NOW, agent_run=ScriptedRun(lambda prompt: {}, clock=lambda: NOW))
    workspace.run_once()

    async def calls(client: Client) -> Any:
        return await client.call_tool("query", {"from": "facts", "record": "document"})

    got = over_mcp(workspace, calls)
    assert {f["record"] for f in got.structured_content["items"]} == DOCUMENTS
    token = workspace.make_token("desk-agent").token
    with serving(workspace) as client:
        answered = call(client, "query", {"from": "facts", "record": "document"}, token).json()
    assert answered["items"] == got.structured_content["items"] and answered["count"] == got.structured_content["count"]


def test_a_document_a_person_marked_out_of_date_still_gives_its_facts_with_their_mark(tmp_path: Path) -> None:
    from test_expire import CALL, NEW, NEW_BODY, OLD, OLD_BODY, PolicyWorkspace, flag

    from cerebro.records import decide

    store = MemoryStore()
    for rel, body in ((OLD, OLD_BODY), (NEW, NEW_BODY), (CALL, "Title: Brightwater\n\nSam said 15% holds.")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, encoding="utf-8")
    workspace = PolicyWorkspace(store, FakeExtraction(), tmp_path)
    old = workspace.keep(OLD, OLD_BODY, "r1")
    new = workspace.keep(NEW, NEW_BODY, "r1")
    workspace.keep(CALL, "Title: Brightwater\n\nSam said 15% holds.", "r1")
    decide(store, Caller("you", "person"), accept=[flag(workspace, old, new)], extraction=workspace.extraction,
           clock=lambda: NOW)
    queried = StoreLookup(store, workspace.extraction, root=tmp_path, clock=lambda: NOW).query(
        AGENT, "facts", {"record": "document"})
    assert records(queried) == {OLD, NEW} and set(queried.stale) == set(old)
