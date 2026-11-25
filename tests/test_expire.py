"""An expire: a document flagged out of date waits for a person, a person decides it, and every reader is shown what
the decision means for the document's facts."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from conftest import GTM
from test_tokens import no_network  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro import Cerebro
from cerebro.agent import load_agent
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.cli import main
from cerebro.connector import Record
from cerebro.evidence import gather, render
from cerebro.expiry import marks, revision
from cerebro.fakes import FakeExtraction, MemoryStore
from cerebro.lookup import StoreLookup
from cerebro.mcp import render_query, render_read
from cerebro.records import (
    InputError,
    NotAllowed,
    NothingStands,
    Proposal,
    decide,
    proposals,
    propose,
    retract,
    review,
    tell,
)
from cerebro.review import lines
from cerebro.store import SQLiteStore, Store

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)
YOU = Caller("you", "person")
DESK = Caller("desk", "person")
CURATOR = Caller("knowledge/curator", "agent")
OLD = "local/knowledge/discount-policy-2025.md"
NEW = "local/knowledge/discount-policy-2026.md"
CALL = "local/calls/2026-11-03-brightwater.md"
OLD_BODY = ("Title: Discount policy 2025\n\nUnder the discount policy, reps may give up to 10% off list price without "
            "approval.\n\nUnder the discount policy, a discount above 10% needs the deal desk.")
NEW_BODY = ("Title: Discount policy 2026\n\nThis discount policy replaces the 2025 discount policy from March 1, "
            "2026.\n\nUnder the discount policy, reps may give up to 15% off list price without approval.")


@dataclass
class PolicyWorkspace:
    """A store and a facts layer holding two versions of a discount policy, as two documents, and a call."""

    store: Store
    extraction: FakeExtraction
    root: Path

    @property
    def lookup(self) -> StoreLookup:
        return StoreLookup(self.store, self.extraction, clock=lambda: NOW)

    def keep(self, id: str, body: str, revision: str, *, listed: bool = False) -> list[str]:
        """Extract a record, kept in the store as a told record is, or `listed` by a source, which the store keeps
        no copy of."""
        record = Record(id=id, source="local", revision=revision, at="2026-10-01T09:00:00Z", body=body,
                        fields={"title": body.splitlines()[0].removeprefix("Title: ")})
        if not listed:
            self.store.keep(record)
        return [f.id for f in self.extraction.extract(record).facts]

    def marked(self, ids: list[str]) -> dict[str, str]:
        found = self.extraction.facts(ids=ids)
        return {id: mark.line() for id, mark in marks(self.store, self.extraction, found).items()}


@pytest.fixture(params=["memory", "sqlite"])
def workspace(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[PolicyWorkspace]:
    store: Store = MemoryStore() if request.param == "memory" else SQLiteStore()  # in memory: none is about the file
    with store:
        yield PolicyWorkspace(store, FakeExtraction(), tmp_path)


def documents(workspace: PolicyWorkspace) -> tuple[list[str], list[str], list[str]]:
    old = workspace.keep(OLD, OLD_BODY, "r1")
    new = workspace.keep(NEW, NEW_BODY, "r1")
    call = workspace.keep(CALL, "Title: Brightwater discovery\n\nSam Okoye said the 15% limit holds since March.", "r1")
    return old, new, call


def flag(workspace: PolicyWorkspace, old: list[str], new: list[str]) -> str:
    """File an expire of the 2025 policy as a curator would, citing its first fact beside the 2026 policy's."""
    return propose(workspace.store, Proposal(
        id=f"p_{uuid4().hex[:12]}", kind="record.expire", target=OLD, by=CURATOR, at="2026-11-17T09:00:00Z",
        payload={"revision": revision(workspace.store, workspace.extraction, OLD)}, cites=[old[0], new[0]],
        reason="replaced by the 2026 policy from March 1, 2026")).id


def test_a_person_accepts_an_expire_and_every_reader_is_shown_its_facts_marked_out_of_date(workspace: PolicyWorkspace) -> None:
    old, new, _ = documents(workspace)
    id = flag(workspace, old, new)
    rows = len(workspace.store.rows("proposals"))
    decided = decide(workspace.store, DESK, accept=[id], extraction=workspace.extraction, clock=lambda: NOW)
    assert decided.answers == {id: "decided"} and "is marked out of date as it stands" in decided.notes[id]
    [row] = proposals(workspace.store, "accepted")
    assert row.decided_by == DESK and row.applied == {"record": OLD, "revision": "r1", "facts": sorted(old)}
    assert len(workspace.store.rows("proposals")) == rows + 1  # a new row: the waiting one stays in the log
    line = f"stale {id}: a person marked its document out of date on Nov 17, 2026"
    assert workspace.marked(old + new) == dict.fromkeys(old, line)
    assert all(f.status == "current" for f in workspace.extraction.facts(ids=old))  # nothing is retracted or deleted

    queried = workspace.lookup.query(YOU, "facts", {"record": OLD})
    assert queried.stale == dict.fromkeys(old, line) and line in render_query(queried)
    read = workspace.lookup.read(YOU, [OLD, NEW])
    assert read.stale == dict.fromkeys(old, line) and render_read(read).count(line) == 2

    workspace.store.create(Identity("knowledge/discount-policy", "knowledge", "Discount policy"))
    writer = load_agent(GTM / "knowledge" / "article.md", GTM)
    evidence = render(gather(writer, workspace.lookup, {}, this="knowledge/discount-policy", root=workspace.root, now=NOW))
    for fact in workspace.extraction.facts(ids=old):
        assert f"{fact.text}" in evidence and f'{OLD}) [mentions]\n  {line}' in evidence
    assert evidence.count("stale ") == 2  # the newer policy's facts carry no line


def test_a_decision_reruns_the_writers_whose_evidence_holds_the_documents_facts(workspace: PolicyWorkspace) -> None:
    from cerebro.evidence import digest

    old, new, _ = documents(workspace)
    workspace.store.create(Identity("knowledge/discount-policy", "knowledge", "Discount policy"))
    writer = load_agent(GTM / "knowledge" / "article.md", GTM)

    def read() -> str:
        return digest(writer, gather(writer, workspace.lookup, {}, this="knowledge/discount-policy", root=workspace.root,
                                     now=NOW))

    before = read()
    id = flag(workspace, old, new)
    flagged = read()
    decide(workspace.store, YOU, accept=[id], extraction=workspace.extraction, clock=lambda: NOW)
    assert len({before, flagged, read()}) == 3


def test_an_accepted_expire_holds_until_a_new_revision_lifts_the_mark_from_what_that_revision_gives(
        workspace: PolicyWorkspace) -> None:
    old, new, _ = documents(workspace)
    id = flag(workspace, old, new)
    decide(workspace.store, YOU, accept=[id], extraction=workspace.extraction, clock=lambda: NOW)
    workspace.keep(OLD, OLD_BODY + "\n\nReviewed on November 1, 2026.", "r2")  # saved again: two facts given again
    current = [f.id for f in workspace.extraction.facts(record=OLD)]
    assert set(old) < set(current)
    assert workspace.marked(current) == {}  # the document as it is now carries no line


@pytest.mark.parametrize("verdict", ["accept", "reject"])
def test_a_decision_made_after_the_document_moved_is_kept_for_the_revision_it_ruled_on(
        workspace: PolicyWorkspace, verdict: str) -> None:
    old, new, _ = documents(workspace)
    id = flag(workspace, old, new)
    workspace.keep(OLD, OLD_BODY + "\n\nReviewed on November 1, 2026.", "r2")  # still gives the fact the flag cites
    decide(workspace.store, YOU, **{verdict: [id]}, extraction=workspace.extraction, clock=lambda: NOW)
    current = [f.id for f in workspace.extraction.facts(record=OLD)]
    # the revision the person ruled on, not the one the flag was filed on
    assert set(workspace.marked(current)) == (set(current) if verdict == "accept" else set())


def test_of_two_expires_on_one_document_the_newest_decides_so_a_later_rejection_lifts_an_older_accept(
        workspace: PolicyWorkspace) -> None:
    old, new, _ = documents(workspace)
    first = flag(workspace, old, new)
    decide(workspace.store, YOU, accept=[first], extraction=workspace.extraction, clock=lambda: NOW)
    assert set(workspace.marked(old)) == set(old)
    later = propose(workspace.store, Proposal(id="p_later0000001", kind="record.expire", target=OLD, by=CURATOR,
                                          at="2026-11-18T10:00:00Z", cites=[old[0]], reason="replaced again",
                                          payload={"revision": "r1"}))
    waiting = f"stale {later.id}: its document is flagged out of date, and a person decides"
    assert set(workspace.marked(old).values()) == {waiting}
    decide(workspace.store, YOU, reject=[later.id], extraction=workspace.extraction, clock=lambda: NOW)
    assert workspace.marked(old) == {}


def test_a_document_a_source_lists_binds_its_expire_to_the_facts_it_gives_and_is_named_by_its_file(
        workspace: PolicyWorkspace) -> None:
    old = workspace.keep(OLD, OLD_BODY, "r1", listed=True)
    new = workspace.keep(NEW, NEW_BODY, "r1", listed=True)
    id = flag(workspace, old, new)
    [item] = review(workspace.store, YOU, extraction=workspace.extraction)
    assert item.title == "discount policy-2025"  # the store keeps no copy, so its file names it, as facts.title does
    decide(workspace.store, YOU, accept=[id], extraction=workspace.extraction, clock=lambda: NOW)
    assert set(workspace.marked(old + new)) == set(old)
    workspace.keep(OLD, OLD_BODY + "\n\nReviewed on November 1, 2026.", "r2", listed=True)
    assert workspace.marked([f.id for f in workspace.extraction.facts(record=OLD)]) == {}


def test_a_caller_takes_an_expire_back_and_the_document_counts_again_with_nothing_deleted(workspace: PolicyWorkspace) -> None:
    old, new, _ = documents(workspace)
    id = flag(workspace, old, new)
    with pytest.raises(InputError, match="waits for a caller: reject it"):
        retract(workspace.store, workspace.extraction, YOU, id, clock=lambda: NOW)
    decide(workspace.store, YOU, accept=[id], extraction=workspace.extraction, clock=lambda: NOW)
    for who in (CURATOR, Caller("local", "source")):
        with pytest.raises(NotAllowed, match="which a caller decides"):
            retract(workspace.store, workspace.extraction, who, id, clock=lambda: NOW)
    rows = len(workspace.store.rows("proposals"))
    taken = retract(workspace.store, workspace.extraction, DESK, id, "renewals keep it", clock=lambda: NOW)
    assert (taken.withdrew, taken.kind, taken.proposal) == ([id], "record.expire", id)
    [row] = proposals(workspace.store)
    assert row.status == "withdrawn" and row.decided_by == DESK and row.decision_reason == "renewals keep it"
    assert row.applied["facts"] == sorted(old) and row.applied["taken_back"]
    assert len(workspace.store.rows("proposals")) == rows + 1
    assert workspace.marked(old) == {} and workspace.lookup.read(YOU, [OLD]).stale == {}
    with pytest.raises(NothingStands, match="is withdrawn"):
        retract(workspace.store, workspace.extraction, YOU, id, clock=lambda: NOW)


def test_a_waiting_expire_follows_its_document_and_stands_down_once_its_facts_are_gone(workspace: PolicyWorkspace) -> None:
    old, new, _ = documents(workspace)
    id = flag(workspace, old, new)
    workspace.keep(OLD, OLD_BODY + "\n\nReviewed on November 1, 2026.", "r2")  # still gives the fact it cites
    current = [f.id for f in workspace.extraction.facts(record=OLD)]
    assert set(workspace.marked(current)) == set(current)
    workspace.keep(OLD, "Title: Discount policy 2025\n\nThis policy is retired.", "r3")
    retired = [f.id for f in workspace.extraction.facts(record=OLD)]
    assert workspace.marked(retired + old) == {}
    [item] = review(workspace.store, YOU, extraction=workspace.extraction)
    assert item.change.startswith("nothing: " + OLD + " changed after this expire")
    decided = decide(workspace.store, YOU, accept=[id], extraction=workspace.extraction, clock=lambda: NOW)
    assert "accepting it changes nothing" in decided.notes[id]
    assert proposals(workspace.store)[0].applied == {"stood_down": True} and workspace.marked(retired) == {}


def test_a_callers_told_expire_is_accepted_as_filed_and_an_agents_waits(
        workspace: PolicyWorkspace) -> None:
    old, _, _ = documents(workspace)
    told = tell(workspace.store, workspace.extraction, YOU, "The 2025 policy is out of date.", OLD, kind="expire",
                clock=lambda: NOW)
    [row] = proposals(workspace.store)
    assert row.status == "accepted" and row.applied["facts"] == sorted(old) and told.proposals == [row.id]
    with pytest.raises(InputError, match="no fact names local/nowhere.md"):
        tell(workspace.store, workspace.extraction, YOU, "It is old.", "local/nowhere.md", kind="expire", clock=lambda: NOW)
    tell(workspace.store, workspace.extraction, CURATOR, "It is old.", NEW, kind="expire", clock=lambda: NOW)
    assert [p.status for p in proposals(workspace.store)] == ["accepted", "waiting"]


def test_a_curator_decides_no_expire_and_review_lists_it_for_a_caller_with_its_title_and_reason(
        workspace: PolicyWorkspace) -> None:
    old, new, _ = documents(workspace)
    id = flag(workspace, old, new)
    assert decide(workspace.store, CURATOR, accept=[id], clock=lambda: NOW).answers == {id: "not yours to decide"}
    assert review(workspace.store, CURATOR) == []
    [item] = review(workspace.store, YOU, extraction=workspace.extraction)
    assert item.title == "Discount policy 2025"
    shown = lines(item, 1, 1)
    assert shown[1] == f"  expire   {OLD}" and shown[2] == "  title    Discount policy 2025"
    assert "  why      replaced by the 2026 policy from March 1, 2026" in shown
    assert f'"Discount policy 2025" ({OLD}) is marked out of date' in item.change


# -- the workspace's own surfaces: status, review, MCP and the JSON API ------------------------------------------------

def workspace_on_disk(root: Path) -> Cerebro:
    root.mkdir()
    (root / "settings.yaml").write_text("budget:\n  daily_usd: 20\n")
    extraction = FakeExtraction()
    workspace = Cerebro(root, extraction=extraction, trusted=True, clock=lambda: NOW)
    record = Record(id=OLD, source="local", revision="r1", at="2026-10-01T09:00:00Z", body=OLD_BODY,
                    fields={"title": "Discount policy 2025\x1b[2J"})  # a title that would clear a terminal
    workspace.store.keep(record)
    facts = extraction.extract(record).facts
    propose(workspace.store, Proposal("p_5a10c0ffee00", "record.expire", OLD, CURATOR, "2026-11-16T09:00:00Z",
                                  {"revision": "r1"}, [facts[0].id], "replaced by the 2026 policy"))
    return workspace


def test_status_and_review_show_a_waiting_expire_plainly_with_its_title_and_reason_escaped(tmp_path: Path) -> None:
    workspace_on_disk(tmp_path / "workspace").close()
    runner = CliRunner()
    status = runner.invoke(main, ["status", "--workspace", str(tmp_path / "workspace")])
    assert status.exit_code == 0, status.output
    assert ('expire    p_5a10c0ffee00 flags "Discount policy 2025\\x1b[2J" (local/knowledge/discount-policy-2025.md) '
            "as out of date, awaiting review: replaced by the 2026 policy") in status.output
    assert "\x1b" not in status.output
    listed = runner.invoke(main, ["review", "--json", "--workspace", str(tmp_path / "workspace")])
    [row] = json.loads(listed.output)
    assert (row["kind"], row["title"], row["reason"]) == ("record.expire", "Discount policy 2025\x1b[2J",
                                                          "replaced by the 2026 policy")
    decided = runner.invoke(main, ["review", "--accept", "p_5a10c0ffee00", "--workspace", str(tmp_path / "workspace")])
    assert decided.exit_code == 0 and "Accepted p_5a10c0ffee00" in decided.output


@pytest.mark.parametrize("surface", ["mcp", "api"])
def test_any_token_decides_an_expire_and_takes_it_back(tmp_path: Path, surface: str) -> None:
    from test_api import Through
    from test_tokens import serving

    workspace = workspace_on_disk(tmp_path / "workspace")
    desk = workspace.make_token("desk-agent").token
    with serving(workspace) as client:
        run = Through(client, surface, (desk,))
        accepted = run(desk, "decide", {"accept": [{"id": "p_5a10c0ffee00", "reason": "the 2026 policy is out"}]})
        assert accepted["answers"] == {"p_5a10c0ffee00": "decided"}
        read = run(desk, "read", {"ids": [OLD]})
        marked = "stale p_5a10c0ffee00: a person marked its document out of date on Nov 17, 2026"
        assert {f.get("out_of_date") for f in read["facts"]} == {marked}
        assert run(desk, "retract", {"id": "p_5a10c0ffee00"})["withdrew"] == ["p_5a10c0ffee00"]
        assert not any("out_of_date" in f for f in run(desk, "read", {"ids": [OLD]})["facts"])
    workspace.close()



def test_an_articles_line_shows_the_facts_of_an_expired_document_marked(workspace: PolicyWorkspace) -> None:
    from cerebro.agent import Agent

    old, new, _ = documents(workspace)
    workspace.store.create(Identity("accounts/acme", "accounts", "Acme"))
    for id in old + new:
        workspace.store.link(id, ["accounts/acme"])
    id = flag(workspace, old, new)
    decide(workspace.store, YOU, accept=[id], extraction=workspace.extraction, clock=lambda: NOW)
    path = workspace.root / "agents" / "risk-radar" / "subagents" / "x.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nevidence:\n  - articles: [accounts/acme]\n---\n\n# Instructions\n\nWrite it.\n")
    reader: Agent = load_agent(path, workspace.root / "agents")
    text = render(gather(reader, workspace.lookup, {}, this="risk-radar/acme", root=workspace.root, now=NOW))
    assert text.count(f"stale {id}: a person marked its document out of date") == len(old)
