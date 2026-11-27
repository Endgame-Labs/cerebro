"""`cerebro admit <collection>/<name>`: a caller adds an article to a curated collection by hand, at any time, and it
is their decision, so it is accepted as sent, and the collection's curator makes it on its next run. The rules: an
article's id or path names it; a collection is matched without regard to case, the longest first, and what follows it
is the name, slashes and all; an `articles/` before it is left out; text whose every part is lower case, or whose
first part is a folder, is a likely typo; a bare name goes to the one curated collection. A name finds the article
whose title it is, or whose title holds every word of it, and only then is the caller's own ask by name. The file name
is a slug of letters of any script, at most 80. Each name is admitted on its own, and the failures are joined."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from cerebro import Cerebro, Record, cli
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.cli import _cut_collection, main
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
from cerebro.records import Proposal, proposals, propose

CURATOR = "---\nevidence:\n  - query: { from: facts }\nheartbeat: [manual]\n---\n\nKeep {0}.\n"


class Workspace:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, *curated: str, folders: tuple[str, ...] = ()):
        self.root = root
        (root / "agents").mkdir(parents=True)
        (root / "settings.yaml").write_text("{}\n")
        for collection in curated:
            (root / "agents" / collection).mkdir()
            (root / "agents" / collection / "curator.md").write_text(CURATOR.replace("{0}", collection))
        for folder in folders:  # a collection with a writer and no curator
            (root / "agents" / folder).mkdir()
            (root / "agents" / folder / "article.md").write_text("---\nevidence:\n  - query: { from: facts }\n---\n\n"
                                                                 "# Output\n\n## Summary\n")
        self.extraction = FakeExtraction()
        monkeypatch.setattr(cli, "open_workspace", lambda folder: self.open())

    def open(self) -> Cerebro:
        return Cerebro(self.root, extraction=self.extraction, connectors=[FakeConnector("src", [])])

    def run(self, *args: str) -> Result:
        return CliRunner().invoke(main, ["admit", *args, "--workspace", str(self.root)])

    def article(self, id: str):  # type: ignore[no-untyped-def]
        with self.open() as workspace:
            return workspace.store.article(id)

    def admits(self) -> list[tuple[str, str, str]]:
        with self.open() as workspace:
            return [(p.target, p.status, p.by.kind) for p in proposals(workspace.store) if p.kind == "article.admit"]

    def asked(self) -> dict[str, str]:
        """Each accepted ask not carried out yet, by its target: the name it carries."""
        with self.open() as workspace:
            return {p.target: p.payload.get("title", "") for p in proposals(workspace.store, "accepted")
                    if p.kind == "article.admit" and not p.applied}

    def carry(self, curator: str, **admit: Any) -> None:
        """The curator `curator`, a stand-in, runs once and makes the admit `admit`."""
        run = ScriptedRun(lambda prompt: {}, [("admit", {"reason": "Asked for by name.", **admit})])
        with Cerebro(self.root, extraction=self.extraction, connectors=[FakeConnector("src", [])],
                     agent_run=run) as workspace:
            workspace.curate(next(a for a in workspace.agents if a.id == curator))


def asked_as(done: Result) -> str:
    """The id of the ask `cerebro admit` filed, as its line gives it."""
    return done.output.split(" as ", 1)[1].split(":", 1)[0]


def test_a_person_admits_by_collection_and_name_and_its_curator_makes_it_as_their_decision(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects", "places")
    held = Record(id="r-77", source="src", revision="1", at="2026-11-18T09:00:00Z", into="projects",
                  keys={"project_id": ["PRJ-77"]}, fields={"name": "Apollo Launch"})
    with w.open() as workspace:
        workspace.store.set_state("held:src", [asdict(held)])
    done = w.run("projects/Apollo Launch")
    assert done.exit_code == 0, done.output
    id = asked_as(done)
    assert done.output == (f"accepted  projects/Apollo Launch as {id}: its curator carries it out on its next run. "
                           "cerebro serve --once runs one pass now.\n")
    assert w.admits() == [("projects/apollo-launch", "accepted", "person")]
    assert w.article("projects/apollo-launch") is None  # accepted, not carried out yet
    w.carry("projects/curator", record={"source": "src", "id": "r-77"}, proposals=[id])
    article = w.article("projects/apollo-launch").identity
    assert (article.title, article.keys, article.admitted_by) == ("Apollo Launch", {"project_id": ["PRJ-77"]},
                                                                  Caller("you", "person"))


def test_what_follows_a_curated_collection_is_the_name_slashes_and_all(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "places", "projects")
    assert w.run("places/North / South depot").exit_code == 0
    assert w.run("places/east/west").exit_code == 0
    assert w.asked() == {"places/north-south-depot": "North / South depot", "places/east-west": "east/west"}


def test_a_collection_matches_whatever_its_case_and_an_articles_folder_before_it_is_left_out(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "places", "projects")
    assert w.run("Projects/Apollo").exit_code == 0
    assert w.run("articles/projects/Lumen").exit_code == 0
    assert w.asked() == {"projects/apollo": "Apollo", "projects/lumen": "Lumen"}


def test_the_longest_collection_matches_first() -> None:
    assert _cut_collection("lab/notes/Ring buffer", ["lab", "lab/notes"]) == ("lab/notes", "Ring buffer")
    assert _cut_collection("LAB/Ring buffer", ["lab", "lab/notes"]) == ("lab", "Ring buffer")
    assert _cut_collection("lab/ Ring", ["lab"]) is None  # a space after the slash: a name


def test_a_bare_name_lands_in_the_one_curated_collection_and_with_several_names_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    one = Workspace(tmp_path / "one", monkeypatch, "projects")
    assert one.run("Harbor/Quarry weekly").exit_code == 0  # Harbor is no collection: the whole is a name
    assert one.run("dock/Lantern").exit_code == 0  # a part in capitals is no likely typo
    assert one.run("places / Ada").exit_code == 0  # a space beside the slash: a name
    assert one.asked() == {"projects/harbor-quarry-weekly": "Harbor/Quarry weekly",
                           "projects/dock-lantern": "dock/Lantern", "projects/places-ada": "places / Ada"}
    two = Workspace(tmp_path / "two", monkeypatch, "projects", "places")
    refused = two.run("Ada Park")
    assert refused.exit_code == 1
    assert "name its collection, as places/Ada Park: this workspace curates places and projects" in refused.output


def test_a_folder_with_no_curator_a_likely_typo_and_no_curator_at_all_are_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "places", folders=("reports",))
    no_curator = w.run("reports/Weekly")
    assert no_curator.exit_code == 1, no_curator.output
    assert "articles/reports has no curator, so nothing is admitted there: the curated collections are places" \
        in no_curator.output and "cerebro agents new reports/curator" in no_curator.output
    typo = w.run("plaecs/north-depot")
    assert typo.exit_code == 1 and "as places/plaecs/north-depot" in typo.output
    assert "cerebro agents new plaecs/curator" in typo.output
    assert w.run("/").exit_code == 1
    none = Workspace(tmp_path / "none", monkeypatch, folders=("reports",))
    refused = none.run("reports/Weekly")
    assert refused.exit_code == 1
    assert "no collection has a curator: add one with cerebro agents new <collection>/curator" in refused.output
    assert w.admits() == [] and none.admits() == []


def test_the_file_name_keeps_letters_of_any_script_and_is_at_most_80_long(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "places")
    assert w.run("places/Café Zürich").exit_code == 0
    assert w.run("places/東京").exit_code == 0
    assert {"places/café-zürich": "Café Zürich", "places/東京": "東京"}.items() <= w.asked().items()
    long = " ".join(["depot"] * 24)
    assert w.run(f"places/{long}").exit_code == 0
    [(target, _, _)] = [row for row in w.admits() if "depot" in row[0]]
    assert len(target.removeprefix("places/")) <= 80 and not target.endswith("-")


def test_an_article_in_the_working_set_already_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    with w.open() as workspace:
        workspace.store.create(Identity("projects/apollo-launch-plan", "projects", "Apollo Launch Plan"))
    for again in ("projects/Apollo Launch Plan", "projects/apollo launch  plan", "projects/apollo-launch-plan",
                  "projects/Apollo", "articles/projects/apollo-launch-plan.md"):
        said = w.run(again)
        assert said.exit_code == 0, (again, said.output)
        assert said.output == "projects/apollo-launch-plan is already in the working set.\n", (again, said.output)
    assert w.admits() == []


def test_a_name_that_two_articles_hold_admits_nothing_and_names_each(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    with w.open() as workspace:
        workspace.store.create(Identity("projects/apollo-launch", "projects", "Apollo Launch"))
        workspace.store.create(Identity("projects/apollo-rollout", "projects", "Apollo Rollout"))
    refused = w.run("projects/Apollo")
    assert refused.exit_code == 1
    assert "projects/apollo-launch Apollo Launch" in refused.output and "projects/apollo-rollout Apollo Rollout" \
        in refused.output
    assert w.admits() == []


def test_an_article_out_of_the_working_set_is_admitted_again_by_its_id_or_path_where_a_curator_makes_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects", folders=("reports",))
    with w.open() as workspace:
        workspace.store.create(Identity("projects/weekly", "projects", "Weekly", status="archived"))
        workspace.store.create(Identity("reports/weekly", "reports", "Weekly", status="archived"))
    done = w.run("articles/projects/weekly.md")
    assert done.exit_code == 0, done.output
    assert w.admits() == [("projects/weekly", "accepted", "person")]
    refused = w.run("reports/weekly")  # no curator makes it
    assert refused.exit_code == 1 and "articles/reports has no curator, so nothing is admitted there" in refused.output
    w.carry("projects/curator", name="Weekly", proposals=[asked_as(done)])
    assert w.article("projects/weekly").identity.status == "active"


def test_a_name_whose_thing_has_an_article_already_renames_that_article(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "reports")
    with w.open() as workspace:
        workspace.store.create(Identity("reports/week", "reports", "Week"))
    done = w.run("reports/Weekly briefing")
    assert done.exit_code == 0, done.output
    w.carry("reports/curator", name="reports/week", proposals=[asked_as(done)])  # its curator finds it is that one
    assert w.article("reports/week").identity.title == "Weekly briefing"
    assert w.article("reports/weekly-briefing") is None


def test_a_curators_admit_awaiting_review_stays_waiting_beside_the_callers_own_ask(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    curator = Caller("projects/curator", "agent")
    with w.open() as workspace:
        propose(workspace.store, Proposal(id="p_lumen", kind="article.admit", target="projects/lumen", by=curator,
                                          at="2026-11-18T09:00:00Z", payload={"title": "Lumen"}))
    assert w.run("projects/Lumen").exit_code == 0
    assert w.admits() == [("projects/lumen", "waiting", "agent"), ("projects/lumen", "accepted", "person")]


def test_each_name_is_admitted_on_its_own_and_the_failures_are_joined(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    names = tmp_path / "names.txt"
    names.write_text("projects/Quarry\n\nOrion\nprjects/x-ray\n")
    done = w.run("projects/Lumen", "prjects/ab", "projects/Harbor", "--from", str(names))
    assert done.exit_code == 1
    assert "prjects/ab" in done.output and "prjects/x-ray" in done.output
    assert [target for target, _, _ in w.admits()] == [
        "projects/lumen", "projects/harbor", "projects/quarry", "projects/orion"]


def test_no_name_is_a_usage_error_that_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    refused = w.run()
    assert refused.exit_code == 2 and "cerebro admit <collection>/<name>" in refused.output


def test_a_tells_admit_takes_its_title_from_its_target_alone_so_the_commands_title_rule_is_its_own(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    with w.open() as workspace:
        workspace.call(Caller("you", "person"), "tell", {"text": "HARBOR!", "target": "projects/harbor",
                                                     "kind": "article.admit"})
    assert w.asked() == {"projects/harbor": ""}  # never the tell's own words


def test_a_title_is_found_before_a_file_name_on_another_article(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    with w.open() as workspace:
        workspace.store.create(Identity("things/pp", "things", "Pied Piper", status="archived"))
        workspace.store.create(Identity("things/pied-piper", "things", "Something Else"))
    assert w.run("things/Pied Piper").exit_code == 0
    assert w.admits() == [("things/pp", "accepted", "person")]


def test_the_slug_lowers_each_letter_on_its_own() -> None:
    """The slug lowers letter by letter, so a capital I with a dot is an i, never an i and a mark."""
    from cerebro.records import _slug

    assert _slug("İstanbul") == "istanbul" and _slug("Café Zürich") == "café-zürich"
