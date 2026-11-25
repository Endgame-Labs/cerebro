"""A curator's admit reads keys before a path, and reads a path as an article's link names it; a path of another
collection is refused; and an article out of the working set is left to no agent however a run starts. A curator's
admit of what a caller archived, by any name the article answers to, is refused until something new arrives. Fakes
only: scripted runs, a fake connector, a fake extraction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_archive_command import Workspace, carry
from test_archived_articles import Weekly
from test_runner import cited

from cerebro.agent import load_agent
from cerebro.caller import Caller
from cerebro.cli import main
from cerebro.curate import run_curator
from cerebro.fakes import ScriptedRun
from cerebro.records import Proposal, proposals, propose

YOU = Caller("you", "person")
AT = "2026-11-10T09:00:00Z"


def refused(replies: list[str], id: str) -> bool:
    """Whether the curator's one act was refused, naming the caller's archive of `id` by its proposal's id."""
    return len(replies) == 1 and f"'refused': 'a caller archived {id} in p_" in replies[0] and \
        "an admit of it is not done until a new fact or record about it arrives" in replies[0]


def _settled(w: Workspace, collection: str = "things") -> None:
    """A caller accepted an earlier admit of this curator, carried out, so its run is no first run."""
    with w.open() as workspace:
        first = next(a.identity.id for a in workspace.store.query(collection))
        propose(workspace.store, Proposal(id=f"p_in_{collection}", kind="article.admit", target=first,
                                      by=Caller(f"{collection}/curator", "agent"), at=AT, status="accepted",
                                      decided_by=YOU, decided_at=AT, applied={"at": AT, "article": first}))


def _curate(w: Workspace, calls: list[tuple[str, dict]], collection: str = "things"):  # type: ignore[no-untyped-def]
    curator = load_agent(w.root / "agents" / collection / "curator.md", w.root / "agents")
    with w.open() as workspace:
        done = run_curator(curator, lookup=workspace.lookup, store=workspace.store,
                           agent_run=ScriptedRun(lambda p: {}, calls), root=w.root, functions={},
                           agent_tools={}, clock=workspace.clock)
        return done, [e.text for e in done.run.transcript if e.role == "tool"]  # type: ignore[union-attr]


def _runs(workspace: Any, agent: str) -> list[Any]:
    return [r for r in workspace.store.rows("agent-runs") if r.data["agent"] == agent]


def _articles(w: Workspace, collection: str = "things") -> dict[str, str]:
    with w.open() as workspace:
        return {a.identity.id: a.identity.status for a in workspace.store.query(collection)}


def _each_key_binds_one(w: Workspace, collection: str = "things") -> list[str]:
    """The key values bound to more than one article of the collection."""
    with w.open() as workspace:
        held = [(k, v.lower(), a.identity.id) for a in workspace.store.query(collection)
                for k, vs in a.identity.keys.items() for v in vs]
    return [f"{k} {v}: {sorted({id for kk, vv, id in held if (kk, vv) == (k, v)})}" for k, v, _ in held
            if len({id for kk, vv, id in held if (kk, vv) == (k, v)}) > 1]


# -- one key never binds two articles, whichever way an admit names them

ALPHA = {"title": "Alpha Widget", "alias": "Alfa", "path": "things/alpha", "domain": "alpha.example"}
BETA = {"title": "Beta Widget", "alias": "Bet", "path": "things/beta", "domain": "beta.example"}


@pytest.mark.parametrize("named, keyed, archived", [("title", "domain", ""), ("alias", "alias", "things/alpha"),
                                                     ("path", "domain", "things/beta"), ("file", "alias", "")])
def test_an_admit_that_names_one_article_and_gives_another_ones_key_never_binds_one_key_to_two(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: str, keyed: str, archived: str) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    for one in (ALPHA, BETA):
        w.make(one["path"], one["title"], {"domain": [one["domain"]], "alias": [one["alias"]]})
    _settled(w)
    if archived:
        w.archive(archived)
    name = f"articles/{ALPHA['path']}.md" if named == "file" else ALPHA[named]
    _, replies = _curate(w, [("admit", {"name": name, "keys": {keyed: [BETA[keyed]]}, "reason": "seen"})])
    assert _each_key_binds_one(w) == [], replies
    assert sorted(_articles(w)) == ["things/alpha", "things/beta"], replies  # no third article
    if archived:  # what a caller archived stays archived: the admit, if it is of that article, is refused
        assert _articles(w)[archived] == "archived", replies


def test_an_admit_with_keys_of_two_articles_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    for one in (ALPHA, BETA):
        w.make(one["path"], one["title"], {"domain": [one["domain"]], "alias": [one["alias"]]})
    _settled(w)
    _, replies = _curate(w, [("admit", {"name": "things/alpha", "reason": "seen",
                                                "keys": {"domain": ["alpha.example"], "alias": ["Bet"]}})])
    assert "bind things/alpha, things/beta" in replies[0], replies
    assert _each_key_binds_one(w) == []


# -- a fixed agent's article a real merge folded away is left alone

def _merged_weekly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Weekly:
    """reports/weekly written once, then merged by a caller into reports/hub, the first article named."""
    from cerebro.article import Identity

    weekly = Weekly(tmp_path / "w", monkeypatch)
    assert weekly.pass_().compiled == ["reports/weekly"]
    with weekly.open() as workspace:
        workspace.store.create(Identity("reports/hub", "reports", "Hub", {"alias": ["a", "b", "c"]}, "active", YOU, AT,
                                    "kept"))
        workspace.tell(YOU, "merge with reports/weekly", target="reports/hub", kind="article.merge")
        carry(workspace)
        assert workspace.store.article("reports/weekly").identity.status == "merged"
    return weekly


def test_a_pass_leaves_alone_a_fixed_agents_article_a_real_merge_folded_away(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weekly = _merged_weekly(tmp_path, monkeypatch)
    weekly.note(7)
    later = weekly.pass_()
    assert later.compiled == [] and not later.failed, later
    assert later.skipped["reports/weekly"] == "reports/weekly is out of the working set, left alone"
    with weekly.open() as workspace:
        assert len(_runs(workspace, "reports/weekly")) == 1  # only the run before the merge
        assert workspace.store.article("reports/weekly").identity.status == "merged"


@pytest.mark.parametrize("args", [("agents", "run", "reports/weekly"), ("compile", "--article", "reports/weekly")])
def test_a_run_by_hand_of_a_fixed_agent_whose_article_was_merged_away_runs_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]) -> None:
    weekly = _merged_weekly(tmp_path, monkeypatch)
    told = weekly.command(*args)
    assert told.exit_code == 0, told.output
    assert told.output == "Nothing to run. reports/weekly is out of the working set, left alone.\n"
    with weekly.open() as workspace:
        assert len(_runs(workspace, "reports/weekly")) == 1


# -- every way a run starts leaves an archived article alone

def test_compile_of_one_archived_article_runs_no_agent_and_says_so(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    weekly = Weekly(tmp_path / "w", monkeypatch)
    weekly.pass_()
    weekly.carried("archive", "reports/weekly")
    weekly.note(7)
    told = weekly.command("compile", "--article", "reports/weekly")
    assert told.exit_code == 0, told.output
    assert told.output == "Nothing to run. reports/weekly is archived, left alone.\n"
    with weekly.open() as workspace:
        assert len(_runs(workspace, "reports/weekly")) == 1
        assert workspace.store.article("reports/weekly").identity.status == "archived"


def test_the_compile_of_one_article_itself_runs_no_agent_on_an_article_out_of_the_working_set(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check sits inside the compile of one article, so no caller can go around it: a collection's
    agent named on an archived article of it runs nothing either."""
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    (w.root / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    w.make("things/blue-widget", "Blue Widget")
    w.archive("things/blue-widget")
    from cerebro import Cerebro

    with Cerebro(w.root, extraction=w.extraction, agent_run=ScriptedRun(lambda p: {"Summary": "x"}),
                 connectors=[]) as workspace:
        compiled = workspace.compile(workspace.agent("things/article"), this="things/blue-widget", scheduled=True)
        assert compiled.run is None and compiled.skipped and not compiled.runs
        assert compiled.left_alone == "things/blue-widget is archived, left alone"
        assert _runs(workspace, "things/article") == []
    told = CliRunner().invoke(main, ["compile", "--article", "things/blue-widget", "--workspace",
                                     str(w.root)])
    assert told.exit_code == 0 and told.output == "Nothing to run. things/blue-widget is archived, left alone.\n"


# -- a path in a curator's admit is read as an article's path

def test_a_path_whose_rest_still_reads_as_a_path_and_finds_no_article_is_refused_to_the_curator(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget")
    _settled(w)
    w.archive("things/blue-widget")
    for name in ("things/../things/never-was", "gadgets/blue-widget"):
        done, replies = _curate(w, [("admit", {"name": name, "reason": "seen again"})])
        assert done.admitted == [] and done.proposed == [], replies
        assert "reads as an article's path, and things holds no such article" in replies[0], replies
    assert _articles(w) == {"things/blue-widget": "archived"}


def test_a_path_in_the_curators_own_collection_that_finds_no_article_admits_the_name_after_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`things/never-was` is the name `never-was`, in the curator's own collection."""
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget")
    _settled(w)
    done, replies = _curate(w, [("admit", {"name": "things/never-was", "reason": "seen"})])
    assert done.admitted == ["things/never-was"], replies
    with w.open() as workspace:
        assert workspace.store.article("things/never-was").identity.title == "never-was"


# -- a path of another collection is refused

@pytest.mark.parametrize("curator, name, refusal", [
    ("things", "parts/y", "admit: an admit of parts/y is for parts, not things"),
    ("things", "articles/parts/y.md", "admit: an admit of parts/y.md is for parts, not things"),
    ("parts", "things/x", "admit: an admit of things/x is for things, not parts")])
def test_a_curators_admit_of_a_path_in_another_collection_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, curator: str, name: str, refusal: str) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things", "parts")
    w.make("things/x", "X Thing")
    w.make("parts/y", "Y Part")
    _settled(w, curator)
    w.archive("parts/y")
    w.archive("things/x")
    done, replies = _curate(w, [("admit", {"name": name, "reason": "seen"})], curator)
    assert refusal in replies[0], replies
    assert done.admitted == [] and done.proposed == []
    assert _articles(w, "things") == {"things/x": "archived"}
    assert _articles(w, "parts") == {"parts/y": "archived"}


# -- a collection's agent whose articles are all archived is told what is true

def test_a_collection_agent_whose_articles_are_all_archived_has_nothing_to_run_and_no_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    (w.root / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts }\nheartbeat: [change]\n---\n\nWrite about the thing.\n")
    w.make("things/blue-widget", "Blue Widget")
    w.archive("things/blue-widget")
    told = CliRunner().invoke(main, ["agents", "run", "things/article", "--workspace", str(w.root)])
    assert told.exit_code == 0, told.output
    assert told.output == "Nothing to run. things/blue-widget is out of the working set, left alone.\n"
    w.make("things/red-widget", "Red Widget")
    w.archive("things/red-widget")
    told = CliRunner().invoke(main, ["agents", "run", "things/article", "--workspace", str(w.root)])
    assert told.exit_code == 0 and told.output == "Nothing to run. 2 articles out of the working set left alone.\n"
    from cerebro import Cerebro

    with Cerebro(w.root, extraction=w.extraction, agent_run=ScriptedRun(lambda p: {"Summary": "x"}),
                 connectors=[]) as workspace:
        passed = workspace.run_once()
    assert passed.skipped["things/article"] == "2 articles out of the working set left alone", passed.skipped
    assert not passed.failed


# -- a curator cannot bring an archived article's subject back by its alias, its title or its path

@pytest.mark.parametrize("name", [
    "vega", "Vega-Launch", "\uff36ega Launch", "projects/Vega", "projects/Vega Launch", "projects//vega",
    "projects\\vega", "projects/vega.md/", "ARTICLES/projects/vega.MD", "../projects/vega.md",
    "projects/../projects/vega"])
def test_a_curators_admit_by_any_name_the_archived_article_answers_to_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    w.make("projects/vega", "Vega Launch", {"alias": ["Vega"]})
    w.make("projects/orion", "Orion Rollout")
    _settled(w, "projects")
    w.archive("projects/vega")
    done, replies = _curate(w, [("admit", {"name": name, "reason": "seen again this week"})], "projects")
    assert done.admitted == [], replies
    assert refused(replies, "projects/vega"), replies
    assert _articles(w, "projects") == {"projects/orion": "active", "projects/vega": "archived"}


def test_a_curators_admit_by_the_alias_of_an_article_in_the_working_set_makes_no_second_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    w.make("projects/vega", "Vega Launch", {"alias": ["Vega"]})
    _settled(w, "projects")
    for name in ("Vega", "vega"):
        _, replies = _curate(w, [("admit", {"name": name, "reason": "seen"})], "projects")
        assert "admit: projects/vega is in the working set already" in replies[0], replies
    assert _articles(w, "projects") == {"projects/vega": "active"}


# -- a key or a binding that names the archived article finds it too

@pytest.mark.parametrize("payload", [
    {"name": "Spring Launch", "keys": {"alias": ["Vega Launch"]}},
    {"name": "Spring Launch", "keys": {"alias": ["vega-launch"]}},
    {"name": "projects/vega", "keys": {"github_repos": ["projects/vega"]}},
    {"name": "projects/vega", "keys": {"linear_projects": ["projects/vega"]}}])
def test_an_alias_or_a_binding_that_names_the_archived_article_is_refused_naming_the_archive(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    w.make("projects/vega", "Vega Launch", {"alias": ["Vega"]})
    w.make("projects/orion", "Orion Rollout")
    _settled(w, "projects")
    w.archive("projects/vega")
    with w.open() as workspace:
        [told] = [p.id for p in proposals(workspace.store) if p.kind == "article.archive" and p.by == YOU]
    done, replies = _curate(w, [("admit", {**payload, "reason": "seen again this week"})], "projects")
    assert done.admitted == [] and done.proposed == [], replies
    assert refused(replies, "projects/vega") and f"in {told}:" in replies[0], replies
    assert _articles(w, "projects") == {"projects/orion": "active", "projects/vega": "archived"}


@pytest.mark.parametrize("name, made", [("Alder / Birch", "projects/alder-birch"), ("AC/DC", "projects/ac-dc")])
def test_a_new_title_with_a_slash_that_no_article_answers_to_is_still_admitted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, made: str) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "projects")
    w.make("projects/vega", "Vega Launch", {"alias": ["Vega"]})
    _settled(w, "projects")
    w.archive("projects/vega")
    done, replies = _curate(w, [("admit", {"name": name, "reason": "seen"})], "projects")
    assert done.admitted == [made], replies
    with w.open() as workspace:
        assert workspace.store.article(made).identity.title == name


# -- an archive carried out while a writer runs: what it wrote does not land

def test_an_archive_carried_out_while_a_writer_runs_lands_nothing_of_that_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weekly = Weekly(tmp_path / "w", monkeypatch)
    weekly.pass_()
    with weekly.open() as workspace:
        before = [s.text for s in workspace.store.article("reports/weekly").compilations]
    said: list = []

    def writer(prompt: str):  # type: ignore[no-untyped-def]
        if not said:  # a caller archives the article, and the curator carries it out, while this writer runs on it
            with weekly.open() as other:
                said.append(other.tell(YOU, "archive reports/weekly", target="reports/weekly", kind="article.archive"))
                carry(other)
        return cited(prompt)

    weekly.run = ScriptedRun(writer)
    weekly.note(7)
    passed = weekly.pass_()
    assert said, "the writer never ran"
    assert passed.compiled == [] and not passed.failed, passed
    assert passed.skipped["reports/weekly"] == "reports/weekly is archived, left alone"
    with weekly.open() as workspace:
        found = workspace.store.article("reports/weekly")
        assert found.identity.status == "archived"
        assert [s.text for s in found.compilations] == before
