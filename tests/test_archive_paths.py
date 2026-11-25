"""Every spelling of an article's path reads as its path. The words of an
article's title find it in the working set, and a key given as the name, or a trailing `.md`, is a new subject beside
it. The title of an article a merge folded away names the article it was merged into, and a curator's admit of it is
refused when a caller archived that article. Fakes only."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from test_archive_admit import YOU, _articles, _curate, _settled, refused
from test_archive_command import Workspace, carry

X = ("things/x", "X Thing", {"domain": ["x.example"]})
CAFE = ("things/cafe", "Café Zoë", {})
NEW = "is a new article in the working set"


# -- every spelling of an article's path reads as the path


def _path_spellings() -> list[str]:
    """Spellings of the path things/x: leading parts, the collection in any case, separators of every kind, the name
    in any case, the file suffix in any case and repeated, and what trails it."""
    leads = ["", "/", "//", "./", "../", "../../", "articles/", "ARTICLES/", "./articles/", "articles/articles/"]
    collections = ["things", "Things", "THINGS"]
    separators = ["/", "//", "\\", "\\\\", "/./", " / ", "/ ", " /"]
    names = ["x", "X"]
    suffixes = ["", ".md", ".MD", ".Md", ".md.md", ".md.MD", " .md", "."]
    trails = ["", "/", "//", "\\", " ", "/ ", "./", "/.", "\t"]
    return [f"{lead}{c}{sep}{n}{suffix}{trail}" for lead, c, sep, n, suffix, trail in
            itertools.product(leads, collections, separators, names, suffixes, trails)]


def test_every_spelling_of_a_path_reads_as_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.curate import _spelled_path

    for spelling in _path_spellings():
        assert _spelled_path(spelling).casefold() == "things/x", spelling


# -- a different subject spelled like an article in the working set gets its own


def test_an_accent_or_md_spelling_finds_no_article_in_the_working_set_so_a_different_subject_gets_its_own(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The accents, a trailing `.md` and a key given as the name make a new article beside one in the working set,
    since `Cafe` may be another subject than `Café Zoë`: its keys never land on the article it is spelled like."""
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make(*X)
    w.make(*CAFE)
    _settled(w)
    for name, made in [("X Thing.md", "things/x-thing-md"), ("Cafe Zoe", "things/cafe-zoe"),
                       ("www.x.example", "things/www-x-example")]:
        _, replies = _curate(w, [("admit", {"name": name, "reason": "a new subject",
                                                    "keys": {"domain": [f"{made.rpartition('/')[2]}.test"]}})])
        assert f"'admitted': '{made}'" in replies[0] and NEW in replies[0], (name, replies)
    with w.open() as workspace:
        assert workspace.store.article("things/x").identity.keys == {"domain": ["x.example"]}
        assert workspace.store.article("things/cafe").identity.keys == {}


@pytest.mark.parametrize("name", ["x thing", "X  THING", "X-Thing", "x_thing", "Ｘ Ｔｈｉｎｇ", "X Thing."])
def test_the_words_of_an_active_articles_title_find_it_in_the_working_set(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make(*X)
    _settled(w)
    _, replies = _curate(w, [("admit", {"name": name, "reason": "seen again"})])
    assert "admit: things/x is in the working set already" in replies[0], (name, replies)
    assert _articles(w) == {"things/x": "active"}


# -- the title of an article a merge folded away names the article it was merged into


def _merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_survivor: bool) -> Workspace:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/orion-rollout", "Orion Rollout", {"domain": ["orion.example"]})
    w.make("things/orion-program", "Orion Program", {"domain": ["program.example"], "alias": ["Hunter", "Op"]})
    _settled(w)
    with w.open() as workspace:  # the first article named stays, and the other leads to it
        workspace.tell(YOU, "merge with things/orion-rollout", target="things/orion-program", kind="article.merge")
        carry(workspace)
        assert workspace.store.article("things/orion-rollout").identity.status == "merged"
    if archive_survivor:
        w.archive("things/orion-program")
    return w


@pytest.mark.parametrize("name", ["orion rollout", "Orion-Rollout"])
def test_the_title_of_a_merged_article_names_the_one_it_was_merged_into(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    w = _merged(tmp_path, monkeypatch, archive_survivor=False)
    _, replies = _curate(w, [("admit", {"name": name, "reason": "seen again"})])
    assert "admit: things/orion-program is in the working set already" in replies[0], replies
    assert sorted(_articles(w)) == ["things/orion-program", "things/orion-rollout"]


def test_the_title_of_an_article_merged_into_one_a_caller_archived_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _merged(tmp_path, monkeypatch, archive_survivor=True)
    _, replies = _curate(w, [("admit", {"name": "Orion Rollout", "reason": "seen again"})])
    assert refused(replies, "things/orion-program"), replies
    assert _articles(w)["things/orion-program"] == "archived"


# -- a run by hand an archive cut short says it ran, what it cost, and that nothing landed


@pytest.mark.parametrize("command", [("agents", "run", "reports/weekly"), ("compile", "--article", "reports/weekly")])
def test_a_run_by_hand_cut_short_by_a_persons_archive_says_it_ran_and_nothing_landed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...]) -> None:
    from test_archived_articles import Weekly
    from test_runner import cited

    from cerebro.fakes import ScriptedRun

    weekly = Weekly(tmp_path / "w", monkeypatch)
    weekly.pass_()
    with weekly.open() as workspace:
        before = [s.text for s in workspace.store.article("reports/weekly").compilations]
    told: list = []

    def writer(prompt: str):  # type: ignore[no-untyped-def]
        if not told:  # a caller archives the article, and the curator carries it out, while this writer runs on it
            with weekly.open() as other:
                told.append(other.tell(YOU, "archive reports/weekly", target="reports/weekly",
                                       kind="article.archive"))
                carry(other)
        return cited(prompt)

    weekly.run = ScriptedRun(writer)
    weekly.note(7)
    done = weekly.command(*command)
    assert told, "the writer never ran"
    assert done.exit_code == 0, done.output
    assert "Nothing to run" not in done.output, done.output
    [line] = done.output.splitlines()
    assert line.startswith("ran      reports/weekly: run "), line
    assert ", $" in line and "and nothing of it landed: a person archived reports/weekly while it ran" in line, line
    with weekly.open() as workspace:
        found = workspace.store.article("reports/weekly")
        assert found.identity.status == "archived" and [s.text for s in found.compilations] == before


# -- an agent's admit of a merged article is listed as what it does, and its curator carries out the yes


@pytest.mark.parametrize("archive_survivor", [False, True])
def test_an_agents_admit_of_a_merged_article_is_listed_truly_and_its_curator_carries_out_the_yes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_survivor: bool) -> None:
    from cerebro.caller import Caller
    from cerebro.curate import yes_left
    from cerebro.records import proposals, review

    w = _merged(tmp_path, monkeypatch, archive_survivor)
    with w.open() as workspace:
        workspace.tell(Caller("scout", "agent"), "Orion Rollout is back", target="things/orion-rollout",
                       kind="article.admit")
        [listed] = review(workspace.store, extraction=workspace.extraction)
        if archive_survivor:
            assert listed.change == ("things/orion-rollout was merged into things/orion-program, so "
                                     "things/orion-program joins the working set again"), listed.change
        else:
            assert listed.change == ("things/orion-rollout was merged into things/orion-program, which is in the "
                                     "working set already"), listed.change
        workspace.decide(YOU, accept=[listed.proposal.id])
        carry(workspace)
        assert proposals(workspace.store, "waiting") == [] and yes_left(workspace.store, "things") == []
    assert _articles(w) == {"things/orion-program": "active", "things/orion-rollout": "merged"}


def test_a_namesake_of_an_archived_article_with_a_key_of_its_own_is_another_thing_with_its_own_article(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/k", "K Thing", {"domain": ["k.example"]})
    w.make("things/v", "Vega Launch", {"domain": ["vega.example"]})
    _settled(w)
    w.archive("things/v")
    _, replies = _curate(w, [("admit", {"name": "Vega Launch", "keys": {"domain": ["other.example"]},
                                        "reason": "seen again"})])
    assert "'admitted': 'things/vega-launch'" in replies[0] and NEW in replies[0], replies
    with w.open() as workspace:
        found = workspace.store.article("things/v").identity
        assert (found.status, found.keys) == ("archived", {"domain": ["vega.example"]})
