"""Who is on the workspace's own side is told by any spelling and follows its domains, and someone outside who shares
their name never takes a note or a fact of theirs. Neutral names only: `things` and `parts`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_held import ALL, CURATOR, PerAgent, hold
from test_own_keys import NOW, curate, lay_out_workspace, told

from cerebro import Cerebro, Record
from cerebro.agent import load_agent
from cerebro.fakes import FakeExtraction, MemoryStore
from cerebro.held import own_names, same_name
from cerebro.identity import bound_to
from cerebro.project import WorkspaceError
from cerebro.scaffold import set_identity

AT = "2026-11-16T09:00:00Z"


def marked(store: MemoryStore, *names: str, more: tuple[Record, ...] = ()) -> None:
    """A note held as a files source holds it: who on it is the workspace's own, under `ours`."""
    hold(store, Record(id="notes/kickoff.md", source="notes", revision="1", at=AT, into="things",
                       keys={"attendees": [", ".join(["Ann Ross", *names])]}, fields={"ours": list(names)}),
         *more, source="notes")


# -- names in any script, as file names keep them


def test_a_name_in_any_script_is_the_same_past_a_hyphen_or_a_stop_and_not_past_another_letter() -> None:
    assert same_name("Мария-Петрова", {"мария петрова"}) and same_name("Мария Петрова.", {"мария петрова"})
    assert same_name("Zoë Lind.", {"zoë lind"})
    assert not same_name("Anna Möller", {"anna müller"}) and not same_name("Zo Lind", {"zoë lind"})


# -- the own names follow the workspace's own domains at the next pass, with no restart


def notes_workspace(root: Path, domains: str) -> None:
    """A workspace whose files source reads one note that names Max Rho and Ann Ross by address."""
    (root / "agents" / "things").mkdir(parents=True)
    (root / "agents" / "things" / "curator.md").write_text(CURATOR.format(authority=ALL, extra="", prompt="Keep things."))
    (root / "workspace.md").write_text(f"---\nidentity:\n  domains: [{domains}]\n---\n\nWho we are.\n")
    (root / "settings.yaml").write_text("{}\n")
    (root / "sources").mkdir()
    (root / "sources" / "notes.yaml").write_text("kind: files\nselect:\n  - glob: notes/**/*.md\n")
    (root / "notes").mkdir()
    (root / "notes" / "kickoff.md").write_text(
        "With: Max Rho <max@newco.example>, Ann Ross <ann@birch.example>\n\nKickoff.\n")


@pytest.mark.parametrize("restart", [False, True])
def test_a_domain_made_the_workspaces_own_reaches_the_names_of_a_note_listed_before_it(tmp_path: Path,
                                                                                      restart: bool) -> None:
    notes_workspace(tmp_path, "ourco.example")
    run, store = PerAgent(), MemoryStore()

    def workspace() -> Cerebro:
        return Cerebro(tmp_path, store=store, extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run)

    first = workspace()
    first.run_once()
    set_identity(tmp_path / "workspace.md", "domains", ["ourco.example", "newco.example"])
    later = workspace() if restart else first  # under serve one workspace runs every pass
    later.run_once()  # the note is not changed, so its source lists nothing new
    assert "max rho" in own_names(store, ["notes"], later.ours)


# -- an outside namesake's article takes nothing of the own person's


def test_an_own_persons_note_and_its_fact_never_bind_to_an_outside_namesakes_article(tmp_path: Path) -> None:
    from cerebro.facts import Fact
    from cerebro.identity import link

    agents, store = lay_out_workspace(tmp_path), MemoryStore()
    outside = Record(id="p7", source="notes", revision="1", at=AT, into="things",
                     keys={"email": ["kim@birch.example"]}, fields={"name": "Kim Vale"})
    marked(store, "Kim Vale", more=(outside,))
    done = curate(store, load_agent(agents / "things" / "curator.md", agents), [
        ("admit", {"record": {"source": "notes", "id": "p7"}, "reason": "Someone at Birch, by her own record."})])
    assert done.admitted == ["things/kim-vale"], told(done)
    ours = Record(id="notes/kickoff.md", source="notes", revision="1", at=AT, into="things",
                  keys={"attendees": ["Ann Ross, Kim Vale"]}, fields={"ours": ["Kim Vale"]})
    fact = Fact(id="f1", text="Kim Vale sent Ann Ross the plan.", subject="Kim Vale", kind="event",
                source="notes", record=ours.id, at=AT)
    assert "things/kim-vale" not in bound_to(store, ours)
    assert "things/kim-vale" not in link(store, fact, ours)


def test_a_fact_linked_again_when_an_article_is_born_never_links_to_an_outside_namesake_by_an_own_name(
        tmp_path: Path) -> None:
    from types import SimpleNamespace

    from cerebro.facts import Fact
    from cerebro.runner import PassResult, _link_born

    agents, store = lay_out_workspace(tmp_path), MemoryStore()
    outside = Record(id="p7", source="notes", revision="1", at=AT, into="things",
                     keys={"email": ["kim@birch.example"]}, fields={"name": "Kim Vale"})
    marked(store, "Kim Vale", more=(outside,))
    done = curate(store, load_agent(agents / "things" / "curator.md", agents), [
        ("admit", {"record": {"source": "notes", "id": "p7"}, "reason": "Someone at Birch, by her own record."})])
    assert done.admitted == ["things/kim-vale"], told(done)
    store.set_state("keys:notes", {"notes/kickoff.md": {"attendees": ["Ann Ross, Kim Vale"]}})
    store.set_state("ours:notes", {"notes/kickoff.md": ["Kim Vale"]})
    fact = Fact(id="f1", text="Kim Vale sent Ann Ross the plan.", subject="Kim Vale", kind="event",
                source="notes", record="notes/kickoff.md", at=AT)
    workspace = SimpleNamespace(store=store, extraction=SimpleNamespace(facts=lambda: [fact]), clock=lambda: NOW)
    _link_born(workspace, PassResult())  # type: ignore[arg-type]
    assert "things/kim-vale" not in store.articles_of("f1")


def test_a_domain_no_longer_the_workspaces_own_takes_its_people_off_that_side_at_the_next_pass(tmp_path: Path) -> None:
    notes_workspace(tmp_path, "ourco.example, newco.example")
    store = MemoryStore()
    workspace = Cerebro(tmp_path, store=store, extraction=FakeExtraction(), clock=lambda: NOW, agent_run=PerAgent())
    workspace.run_once(agents=False)
    assert "max rho" in own_names(store, ["notes"], workspace.ours)
    set_identity(tmp_path / "workspace.md", "domains", ["ourco.example"])
    workspace.run_once(agents=False)  # the same workspace, as under serve, and the note is not changed
    assert "max rho" not in own_names(store, ["notes"], workspace.ours)


# -- a workspace file that does not read, and a source that fails


def test_a_workspace_file_that_does_not_read_stops_the_pass_and_keeps_the_own_side_read_before(
        tmp_path: Path) -> None:
    notes_workspace(tmp_path, "ourco.example, newco.example")
    run, store = PerAgent(), MemoryStore()
    workspace = Cerebro(tmp_path, store=store, extraction=FakeExtraction(), clock=lambda: NOW, agent_run=run)
    workspace.run_once()
    (tmp_path / "profile.md").write_text("---\nidentity: [not, a, map\n---\n")
    with pytest.raises(WorkspaceError, match=r"^profile\.md"):  # refused in one line
        workspace.run_once()
    assert workspace.ours.domains == ("ourco.example", "newco.example")  # never dropped to no one


def test_a_source_that_fails_after_a_change_is_listed_again_alone(tmp_path: Path) -> None:
    from cerebro.fakes import FakeConnector

    class Down(FakeConnector):
        def list(self, since: str | None = None) -> Any:
            if self.down:
                raise RuntimeError("stand-in: unreachable")
            return super().list(since)

    notes_workspace(tmp_path, "ourco.example")
    down, up = Down("down", []), FakeConnector("up", [])
    down.down = False
    listed: list[str | None] = []
    real = up.list
    up.list = lambda since=None: listed.append(since) or real(since)  # type: ignore[method-assign]
    workspace = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW,
                    connectors=[down, up], agent_run=PerAgent())
    workspace.run_once()
    down.down = True
    set_identity(tmp_path / "workspace.md", "domains", ["ourco.example", "newco.example"])
    for _ in range(3):
        workspace.run_once()
    assert listed.count(None) == 2, listed  # its first listing, and one to read who it names again
