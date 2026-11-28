"""`cerebro archive <article>`: a caller takes an article out of the working set by hand, at any time. It is their
decision, so it is accepted as sent, and the collection's curator archives it on its next run. The rules: the article
is named by its path or id, a merged one by the article it was merged into, else by its title or an alias, ignoring
case and spacing, and the words given are one name; two articles that answer archive nothing; one out of the working
set already records nothing; a collection no curator keeps is refused, but for the article a one-article agent made,
which is archived at once; nothing else moves; its file, facts and parts stay; and an admit brings it back. A curator's
admit of what a caller archived is refused until something new arrives."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cerebro import Cerebro, cli
from cerebro.agent import load_agent
from cerebro.article import Compilation, Identity, Property, mirror
from cerebro.caller import Caller
from cerebro.cli import main
from cerebro.compile import birth
from cerebro.curate import run_curator, yes_left
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
from cerebro.records import Proposal, proposals, propose


def cited(prompt: str) -> dict[str, str]:
    """A summary that cites the first fact the prompt shows, as a run must for its line to land."""
    first = re.search(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])
    return {"Summary": f"x [^{first.group(1)}]"} if first else {"Summary": "x"}


CURATOR = "---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - query: { from: facts }\nheartbeat: [manual]\n---\n\nKeep {0}.\n"
AT = "2026-11-18T09:00:00Z"
CURATOR_OF = Caller("things/curator", "agent")
ACTS = {"article.archive": lambda p: {"article": p.target}, "article.admit": lambda p: {"name": p.target},
        "article.merge": lambda p: {"article": p.target, "other": p.payload["with"]}}


def accepted(output: str, id: str) -> bool:
    """Whether `cerebro archive` printed its one line: the caller's archive of `id`, accepted."""
    return re.fullmatch(rf"accepted  {re.escape(id)} as p_\w+: its curator archives it on its next run\. cerebro serve "
                        r"--once runs one pass now\.\n", output) is not None


def carry(workspace: Cerebro) -> list[str]:
    """Each curator carries out the accepted proposals of its collection not carried out yet, naming each by id, as
    it does on its next run. Gives its replies."""
    replies: list[str] = []
    for collection in sorted(agent.target for agent in workspace.agents if agent.role == "curator"):
        rows = [p for p in proposals(workspace.store, "accepted") if p.id in yes_left(workspace.store, collection)]
        if not rows:
            continue
        calls = [(p.kind.partition(".")[2], {**ACTS[p.kind](p), "reason": "Asked for.", "proposals": [p.id]})
                 for p in rows]
        curator = load_agent(workspace.root / "agents" / collection / "curator.md", workspace.root / "agents")
        done = run_curator(curator, lookup=workspace.lookup, store=workspace.store, root=workspace.root, functions={},
                           agent_run=ScriptedRun(lambda p: {}, calls), agent_tools={}, clock=workspace.clock)
        replies += [e.text for e in done.run.transcript if e.role == "tool"]  # type: ignore[union-attr]
    assert not [r for r in replies if "'refused'" in r], replies
    return replies


class Workspace:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, *curated: str):
        self.root = root
        (root / "agents").mkdir(parents=True)
        (root / "settings.yaml").write_text("{}\n")
        for collection in curated:
            (root / "agents" / collection).mkdir()
            (root / "agents" / collection / "curator.md").write_text(CURATOR.replace("{0}", collection))
        self.extraction = FakeExtraction()
        monkeypatch.setattr(cli, "open_workspace", lambda folder: self.open())

    def open(self) -> Cerebro:
        return Cerebro(self.root, extraction=self.extraction, connectors=[FakeConnector("src", [])])

    def run(self, *args: str) -> Result:
        return CliRunner().invoke(main, ["archive", *args, "--workspace", str(self.root)])

    def archive(self, *args: str) -> None:
        """`cerebro archive`, then the curator's next run, which carries it out."""
        done = self.run(*args)
        assert done.exit_code == 0, done.output
        with self.open() as workspace:
            carry(workspace)

    def make(self, id: str, title: str, keys: dict[str, list[str]] | None = None, status: str = "active",
             written: bool = True) -> None:
        with self.open() as workspace:
            workspace.store.create(Identity(id, id.rpartition("/")[0], title, keys or {}, status, CURATOR_OF, AT, "kept"))
            if written:
                workspace.store.write_compilation(Compilation(id, "things/article", f"{title} is a thing.", "things/article",
                                                  whole=True))

    def article(self, id: str):  # type: ignore[no-untyped-def]
        with self.open() as workspace:
            return workspace.store.article(id)

    def status(self, id: str) -> str:
        return str(self.article(id).identity.status)

    def rows(self, kind: str = "article.archive") -> list[tuple[str, str, str, str]]:
        with self.open() as workspace:
            return [(p.target, p.status, p.by.kind, p.decided_by.kind if p.decided_by else "")
                    for p in proposals(workspace.store) if p.kind == kind]


def test_a_caller_archives_an_article_by_its_path_and_its_curator_carries_it_out_on_its_next_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget")
    done = w.run("things/blue-widget")
    assert done.exit_code == 0, done.output
    assert accepted(done.output, "things/blue-widget"), done.output
    assert w.status("things/blue-widget") == "active"
    with w.open() as workspace:  # filed as a tell is, on a record of what the caller said, and accepted
        [row] = [p for p in proposals(workspace.store) if p.kind == "article.archive"]
        assert (row.status, row.by.kind, row.decided_by, row.applied) == ("accepted", "person", row.by, {})
        assert row.id in done.output
        record = workspace.store.record(row.cites[0])
        assert record is not None and record.body == "archive things/blue-widget" and record.by.kind == "person"
        carry(workspace)
        assert workspace.store.article("things/blue-widget").identity.status == "archived"
        [row] = [p for p in proposals(workspace.store) if p.id == row.id]
        assert row.applied["article"] == "things/blue-widget"


def test_an_article_is_found_by_its_file_path_its_title_or_an_alias_whatever_the_case_and_spacing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget")
    w.make("things/red-widget", "Red Widget")
    w.make("things/green-widget", "Green Widget", {"alias": ["Verdant"]})
    assert accepted(w.run("articles/things/blue-widget.md").output, "things/blue-widget")
    assert accepted(w.run("red", "  WIDGET").output, "things/red-widget")  # the words are one name
    assert accepted(w.run("verdant").output, "things/green-widget")


def test_an_article_that_does_not_exist_is_refused_and_nothing_is_recorded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget")
    refused = w.run("things/nowhere")
    assert refused.exit_code == 1, refused.output
    assert 'there is no article "things/nowhere"' in refused.output and "give its path" in refused.output
    assert w.rows() == [] and w.status("things/blue-widget") == "active"


def test_a_name_that_two_articles_answer_to_archives_neither_and_names_both(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things", "parts")
    w.make("things/bolt", "Bolt")
    w.make("parts/bolt", "Bolt")
    refused = w.run("Bolt")
    assert refused.exit_code == 1, refused.output
    assert '"Bolt" names parts/bolt and things/bolt: give one by its path' in refused.output
    assert w.rows() == [] and w.status("things/bolt") == w.status("parts/bolt") == "active"
    assert accepted(w.run("parts/bolt").output, "parts/bolt")


def test_an_article_out_of_the_working_set_already_records_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget")
    w.archive("things/blue-widget")
    rows = w.rows()
    again = w.run("Blue Widget")
    assert again.exit_code == 0 and again.output == "things/blue-widget is already out of the working set.\n"
    assert w.rows() == rows


def test_a_waiting_proposal_of_another_kind_stays_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget")
    w.make("things/blue-widgets", "Blue Widgets")
    with w.open() as workspace:
        propose(workspace.store, Proposal(id="p_merge", kind="article.merge", target="things/blue-widget", by=CURATOR_OF,
                                      at=AT, payload={"with": "things/blue-widgets"}, reason="one thing"))
    assert w.run("things/blue-widget").exit_code == 0
    assert w.rows("article.merge") == [("things/blue-widget", "waiting", "agent", "")]
    assert w.status("things/blue-widgets") == "active"


def test_a_merged_articles_path_names_the_article_it_was_merged_into(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget")
    w.make("things/blue-widgets", "Blue Widgets")
    with w.open() as workspace:  # the first article named stays, and the other leads to it
        workspace.tell(Caller("you", "person"), "merge with things/blue-widget", target="things/blue-widgets",
                       kind="article.merge")
        carry(workspace)
    assert w.status("things/blue-widget") == "merged"
    w.archive("things/blue-widget")
    assert w.status("things/blue-widgets") == "archived" and w.status("things/blue-widget") == "merged"


def test_its_file_parts_facts_claims_keys_and_runs_stay(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget", {"code": ["bw-1"]}, written=False)
    with w.open() as workspace:  # its body is empty, so its copy counts the facts linked to it
        workspace.store.write_compilation(Compilation("things/blue-widget", "things/article", "", "things/article", whole=True))
        told = workspace.tell(Caller("you", "person"), "Blue Widget ships in two sizes.", target="things/blue-widget",
                          as_="fact")
        workspace.store.set_field("things/blue-widget", "colour", Property("blue", Caller("you", "person"), AT))
        workspace.store.append("agent-runs", {"agent": "things/article", "target": "things/blue-widget", "status": "done"})
        facts = workspace.lookup.read(Caller("compile", "agent"), ["things/blue-widget"]).facts
        assert told.facts and facts
        mirror(workspace.store.article("things/blue-widget"), w.root, facts=facts, zone=workspace.zone)
    w.archive("things/blue-widget")
    copy = w.root / "articles" / "things" / "blue-widget.md"
    with w.open() as workspace:
        workspace.run_once(agents=False)  # the pass after the curator's archive writes its copy
    assert copy.is_file() and "\nstatus: archived\n" in copy.read_text()
    with w.open() as workspace:
        article = workspace.store.article("things/blue-widget")
        assert article.identity.status == "archived" and [s.by for s in article.compilations] == ["things/article"]
        assert workspace.store.facts_of("things/blue-widget") == told.facts
        assert article.properties["colour"].value == "blue"
        assert workspace.store.bound("code", "bw-1") == ["things/blue-widget"]
        assert workspace.store.rows("agent-runs")[0].data["agent"] == "things/article"
        assert workspace.store.query("things")[0].identity.id == "things/blue-widget"  # the store keeps it in its index


def test_new_facts_and_a_pass_leave_it_out_and_an_admit_brings_it_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    (w.root / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    w.make("things/blue-widget", "Blue Widget")
    w.make("things/red-widget", "Red Widget")
    w.archive("things/blue-widget")
    with Cerebro(w.root, extraction=w.extraction, connectors=[FakeConnector("src", [])],
                 agent_run=ScriptedRun(cited)) as workspace:
        for id, said in (("things/blue-widget", "Blue Widget shipped a new run."),
                         ("things/red-widget", "Red Widget shipped a new run.")):
            assert workspace.tell(Caller("you", "person"), said, target=id, as_="fact").facts
        passed = workspace.run_once(scheduled=True)
    assert "things/red-widget" in passed.compiled, passed  # the pass ran the collection's writer
    assert "things/blue-widget" not in passed.compiled
    assert w.status("things/blue-widget") == "archived"
    back = CliRunner().invoke(main, ["admit", "things/blue-widget", "--workspace", str(w.root)])
    assert back.exit_code == 0, back.output
    with w.open() as workspace:
        carry(workspace)
    assert w.status("things/blue-widget") == "active"


def test_an_article_of_a_collection_no_curator_keeps_is_refused_and_nothing_is_recorded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch)
    (w.root / "agents" / "reports").mkdir()
    (w.root / "agents" / "reports" / "weekly.md").write_text(
        "---\nevidence:\n  - query: { from: facts }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    w.make("reports/weekly", "Weekly")
    refused = w.run("reports/weekly")
    assert refused.exit_code == 1, refused.output
    assert "archive reports/weekly: articles/reports has no curator, so nothing is archived there" in refused.output
    assert w.rows() == [] and w.status("reports/weekly") == "active"


def test_the_article_a_one_article_agent_made_is_archived_at_once_though_no_curator_keeps_its_collection(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch)
    file = w.root / "agents" / "reports" / "week.md"
    file.parent.mkdir()
    file.write_text("---\nevidence:\n  - query: { from: facts }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    with w.open() as workspace:
        birth(workspace.store, load_agent(file, w.root / "agents"), "reports/week", AT)
    file.unlink()  # renamed away, so its article stays behind
    done = w.run("reports/week")
    assert done.exit_code == 0, done.output
    assert "reports/week is archived" in done.output and w.status("reports/week") == "archived", done.output
    assert w.rows() == [("reports/week", "accepted", "person", "person")]


def test_no_name_is_a_usage_error_that_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    refused = w.run()
    assert refused.exit_code == 2 and "cerebro archive <collection>/<name>" in refused.output


@pytest.mark.untrusted
def test_a_workspace_its_owner_has_not_trusted_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    root = tmp_path / "w"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    refused = CliRunner().invoke(main, ["archive", "things/blue-widget", "--workspace", str(root)])
    assert refused.exit_code == 1 and "is not a workspace you trust" in refused.output
    assert not (root / ".cerebro").exists()


def test_a_curators_admit_of_what_a_caller_archived_is_refused_naming_the_archive(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, "things")
    w.make("things/blue-widget", "Blue Widget", {"code": ["bw-1"]})
    w.archive("things/blue-widget")
    with w.open() as workspace:
        [told] = [p.id for p in proposals(workspace.store) if p.kind == "article.archive" and p.by.kind == "person"]
    curator = load_agent(w.root / "agents" / "things" / "curator.md", w.root / "agents")
    calls = [("admit", {"name": "Blue Widget", "reason": "seen again this week"}),
             ("admit", {"name": "Widget in blue", "keys": {"code": ["bw-1"]}, "reason": "its code came up"})]
    with w.open() as workspace:
        done = run_curator(curator, lookup=workspace.lookup, store=workspace.store, agent_run=ScriptedRun(lambda p: {}, calls),
                           root=w.root, functions={}, agent_tools={}, clock=workspace.clock)
        assert done.admitted == [] and done.proposed == []
        replies = [e.text for e in done.run.transcript if e.role == "tool"]  # type: ignore[union-attr]
    refusal = (f"'refused': 'a caller archived things/blue-widget in {told}: archive things/blue-widget: an admit of it "
               "is not done until a new fact or record about it arrives'")
    assert refusal in replies[0], replies
    assert refusal in replies[1], replies  # by its key
    assert w.status("things/blue-widget") == "archived"
    assert w.rows("article.admit") == []


def test_the_archive_lines_say_no_section_brain_or_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_output_words import WORDS

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    w = Workspace(tmp_path / "w", monkeypatch, "things", "parts")
    w.make("things/bolt", "Bolt")
    w.make("parts/bolt", "Bolt")
    seen = [w.run("things/bolt").output, w.run("things/bolt").output, w.run("Bolt").output,
            w.run("things/nowhere").output, w.run().output,
            CliRunner().invoke(main, ["archive", "--help"]).output]
    assert not [line for line in seen if words.search(line) or WORDS.search(line)], seen
