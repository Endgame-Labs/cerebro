"""A composed article, `agents run` and the exit code:
a subagent's run that fails fails the article's whole run and nothing of it lands; a composer whose content
grounding dropped whole says it wrote nothing and what the article holds; `agents run` runs every article, one
with nothing to cite included; and a pass exits by what settled and what landed.
Fakes and stand-ins only."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cerebro import Cerebro, Record, cli
from cerebro.agent import Agent, Budget
from cerebro.article import Identity
from cerebro.cli import main
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
from cerebro.run import AgentRun, Run, Usage

SUBAGENT_FAILED = "a subagent's run failed, so nothing of the article's run lands"
COMPOSER = ("---\nsubagents: [{subs}]\n---\n\nA brief on one thing.\n\n# Composing\n\n"
            "Write a verdict from what the subagents returned, or hand in nothing new when it says it all.\n")
SUB = ("---\nevidence:\n  - query: {{ from: facts, source: notes }}\nheartbeat: [change]\n---\n\n# Instructions\n\n"
       "Draft it.\n\n# Output\n\n## {heading}\n")
WRITER = ("---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n"
          "# Output\n\n## Summary\n")
CURATOR = "---\nheartbeat: [manual]\n---\n\nKeep them.\n"


def cited(prompt: str) -> str:
    """The first fact the prompt's evidence holds, as a sentence that cites it, or nothing when it holds none."""
    found = re.search(r"- \[\^(fact_\w+)\] (.+?) \(quote", prompt.partition("<evidence>")[2])
    return f"{found.group(2)}[^{found.group(1)}]" if found else ""


class Router(AgentRun):
    """Each agent's run by its id: `fail` ends it failed, as the real loop reports a model's error; a callable is the
    text a scripted run hands in for the prompt, and anything else hands in nothing."""

    def __init__(self, clock: Any) -> None:
        self.how: dict[str, Any] = {}
        self.ran: list[str] = []
        self.clock = clock

    def run(self, agent: Agent, evidence: Any, tools: Any, budget: Budget) -> Run:
        self.ran.append(f"{agent.id}@{agent.target}")
        how = self.how.get(f"{agent.id}@{agent.target}") or self.how.get(agent.id)
        if how == "fail":
            return Run(id=f"run_{agent.id.split('/')[-1]}", agent=agent.id, target=agent.target, status="failed",
                       usage=Usage(model="scripted", requests=1), note="a model error")
        if isinstance(how, tuple):  # an ending with no failure, as (status, note)
            return Run(id=f"run_{agent.id.split('/')[-1]}", agent=agent.id, target=agent.target, status=how[0],
                       usage=Usage(model="scripted", requests=1), note=how[1])
        return ScriptedRun(how if callable(how) else (lambda prompt: ""), clock=self.clock).run(
            agent, evidence, tools, budget)


class Workspace:
    """A workspace on fakes, a notes source with a note on each name in `notes`, whose agents' files are `files`,
    with each article of `admitted` admitted by hand and the notes taken in as facts before any agent runs."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str],
                 admitted: tuple[str, ...] = (), notes: tuple[str, ...] = ("yard",), broken: bool = False) -> None:
        for name, text in {**files, "settings.yaml": "{}\n"}.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        self.root = root
        self.at = [datetime(2026, 11, 17, 9, tzinfo=UTC)]
        self.run = Router(lambda: self.at[0])
        self.notes = FakeConnector("notes", [self.note(name) for name in notes])
        self.sources: list[Any] = [self.notes]
        if broken:
            class Broken(FakeConnector):  # a source whose listing fails, as one that cannot be read
                def list(self, since: str | None = None) -> Any:
                    raise ConnectionError("the source did not answer")

            self.sources.append(Broken("mail", []))
        self.extraction = FakeExtraction()
        monkeypatch.setattr(cli, "open_workspace", lambda folder: self.open())
        with self.open() as opened:
            for article in admitted:
                opened.store.create(Identity(article, article.split("/")[0], article.split("/")[1].title(),
                                             {"topic": [article.split("/")[1]]}))

    @staticmethod
    def note(name: str, n: int = 0) -> Record:
        return Record(id=f"n-{name}-{n}", source="notes", revision="1", at="2026-11-10T09:00:00Z",
                      body=f"A note on {name}, number {n}.", keys={"topic": [name]})

    def open(self) -> Cerebro:
        return Cerebro(self.root, extraction=self.extraction, connectors=self.sources, clock=lambda: self.at[0],
                       agent_run=self.run, trusted=True)

    def take_in(self) -> None:
        """A pass that takes the notes in as facts and runs no agent."""
        with self.open() as opened:
            opened.run_once(agents=False)

    def move(self) -> None:
        """The notes go and a new one comes, so what cited the old notes no longer stands."""
        for id in list(self.notes.records):
            self.notes.remove(id)
        self.notes.add(self.note("yard", 9))
        self.take_in()

    def revise(self) -> None:
        """Each note is saved again with new words, so the facts the earlier text cites are an earlier save's: no
        fact is retracted, and grounding the earlier text again would drop it as history."""
        for id, record in list(self.notes.records.items()):
            self.notes.add(Record(id=id, source="notes", revision="2", at="2026-11-17T10:00:00Z",
                                  body=f"{record.body} It was saved again.", keys=record.keys))
        self.at[0] = self.at[0].replace(hour=11)
        self.take_in()

    def stored(self, article: str) -> list[Any]:
        """The article's parts as the store holds them."""
        with self.open() as opened:
            found = opened.store.article(article)
        return [] if found is None else [(s.heading, s.by, s.text, s.digest) for s in found.compilations]

    def cli(self, *args: str) -> Any:
        return CliRunner().invoke(main, [*args, "--workspace", str(self.root)])

    def article(self, article: str) -> str | None:
        path = self.root / "articles" / f"{article}.md"
        return path.read_text() if path.exists() else None


def composed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subs: tuple[str, ...] = ("overview",)) -> Workspace:
    files = {"agents/things/article.md": COMPOSER.format(subs=", ".join(subs)),
             "agents/things/curator.md": CURATOR}
    files |= {f"agents/things/subagents/{sub}.md": SUB.format(heading=sub.title()) for sub in subs}
    w = Workspace(tmp_path / "w", monkeypatch, files, admitted=("things/alpha",))
    w.take_in()
    return w


# -- by hand, a subagent's run that fails fails the article's run, over its earlier text too

@pytest.mark.parametrize("moved", [False, True])
def test_a_subagent_that_fails_by_hand_over_earlier_text_fails_the_run_exits_1_and_leaves_the_article(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moved: bool) -> None:
    workspace = composed(tmp_path, monkeypatch)
    workspace.run.how["things/subagents/overview"] = cited
    first = workspace.cli("agents", "run", "things/article")
    assert first.exit_code == 0 and first.output.startswith("wrote    things/alpha"), first.output
    before = workspace.article("things/alpha")
    assert before is not None and "A note on yard, number 0." in before
    if moved:
        workspace.move()
    workspace.run.how["things/subagents/overview"] = "fail"
    told = workspace.cli("agents", "run", "things/article")
    assert told.exit_code == 1, told.output
    assert not any(line.startswith("wrote") for line in told.output.splitlines()), told.output
    assert told.stderr.strip() == (f"Error: things/article: {SUBAGENT_FAILED}: things/subagents/overview: run run_overview "
                                   "failed: a model error"), told.stderr
    assert workspace.article("things/alpha") == before  # byte for byte: nothing of the run landed


# -- grounding dropped everything a composer and its subagents handed in

@pytest.mark.parametrize("again", ["the same sentence", "nothing"])
def test_a_composer_whose_content_grounding_dropped_whole_by_hand_says_it_wrote_nothing_and_what_the_article_holds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, again: str) -> None:
    workspace = composed(tmp_path, monkeypatch)
    said: list[str] = []
    workspace.run.how["things/subagents/overview"] = lambda prompt: said.append(cited(prompt)) or said[-1]
    assert workspace.cli("agents", "run", "things/article").exit_code == 0
    workspace.move()  # the note it cited is gone, so its sentence no longer stands
    workspace.run.how["things/subagents/overview"] = (lambda prompt: said[0]) if again == "the same sentence" else ""
    told = workspace.cli("agents", "run", "things/article")
    assert told.exit_code == 0, told.output
    lines = told.output.splitlines()
    assert lines[0].startswith("ran      things/article on things/alpha: wrote nothing: "), lines
    assert not any(line.startswith("wrote") for line in lines), lines
    assert "A note on yard, number 0." not in (workspace.article("things/alpha") or "")
    assert not any("so its subagents' content stands" in line for line in lines), lines  # it holds none of it
    assert any("Overview: " in line and "nothing it wrote grounds now" in line for line in lines), lines


# -- in a pass, a subagent's run that fails fails the article's run, and nothing of it lands

def test_a_pass_where_one_subagent_fails_and_another_returns_lands_nothing_and_counts_the_article_failed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = composed(tmp_path, monkeypatch, subs=("risks", "next"))
    workspace.run.how["things/subagents/risks"] = "fail"
    workspace.run.how["things/subagents/next"] = cited
    told = workspace.cli("serve", "--once", "--plain")
    lines = told.output.splitlines()
    assert (f"failed    things/article on things/alpha: {SUBAGENT_FAILED}: things/subagents/risks: run run_risks failed: "
            "a model error") in lines, lines
    assert lines[-1] == "Done: 1 failed.", lines
    assert told.exit_code == 1, told.output  # nothing else landed: the facts were taken in before this pass
    assert workspace.article("things/alpha") is None or "A note on yard, number 0." not in workspace.article("things/alpha")
    workspace.at[0] = workspace.at[0].replace(hour=11)  # past the wait after a failure, nothing else moved
    again = workspace.cli("serve", "--once", "--plain")
    assert again.output.splitlines()[-1] == "Done: 1 failed.", again.output
    byhand = workspace.cli("agents", "run", "things/article")
    assert byhand.exit_code == 1 and SUBAGENT_FAILED in byhand.stderr, byhand.output


# -- agents run runs every article, one with nothing to cite included

@pytest.mark.parametrize("alpha", ["fail", "write"])
def test_agents_run_goes_past_an_article_with_nothing_to_cite_and_counts_every_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alpha: str) -> None:
    writer = ("---\nevidence:\n  - facts: { window: last_365_days }\nheartbeat: [change]\n---\n\nWrite the thing.\n\n"
              "# Output\n\n## Summary\n")
    w = Workspace(tmp_path / "w", monkeypatch, {"agents/things/article.md": writer,
                                                       "agents/things/curator.md": CURATOR},
                          admitted=("things/alpha", "things/beta", "things/gamma"), notes=("alpha", "gamma"))
    w.take_in()
    w.run.how["things/article"] = cited
    if alpha == "fail":
        w.run.how["things/article@things/alpha"] = "fail"
    told = w.cli("agents", "run", "things/article")
    lines = told.stdout.splitlines()
    assert "wrote    things/gamma  articles/things/gamma.md" in lines, told.output  # the article after it ran
    assert any(line.startswith("ran      things/article on things/beta: wrote nothing: ") for line in lines), lines
    if alpha == "fail":
        # beta ran, told it has nothing to cite, and wrote nothing: no failure
        assert told.exit_code == 1 and told.stderr.strip() == "Error: things/article: 1 of 3 runs failed", told.output
    else:
        assert told.exit_code == 0, told.output


# -- the exit code follows what the pass did

def test_a_pass_where_a_source_failed_beside_a_write_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, {"agents/reports/weekly.md": WRITER}, broken=True)
    w.run.how["reports/weekly"] = cited
    told = w.cli("serve", "--once", "--plain")
    lines = told.output.splitlines()
    assert "wrote     reports/weekly  articles/reports/weekly.md" in lines, lines
    assert any(line.startswith("failed    sources/mail: ") for line in lines), lines
    assert told.exit_code == 1, told.output


def test_a_pass_where_a_curator_ran_beside_a_writer_that_failed_exits_0(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    curator = ("---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n"
               "Keep the things.\n")
    w = Workspace(tmp_path / "w", monkeypatch, {"agents/reports/weekly.md": WRITER,
                                                       "agents/things/curator.md": curator},
                          admitted=("things/alpha",))
    w.take_in()  # the facts are taken in before, so the pass that matters stores none
    w.run.how["reports/weekly"] = "fail"
    told = w.cli("serve", "--once", "--plain")
    lines = told.output.splitlines()
    assert "ran       things/curator: changed nothing, $0.0000" in lines, lines
    assert any(line.startswith("failed    reports/weekly: ") for line in lines), lines
    assert told.exit_code == 0, told.output


def test_a_pass_that_stored_facts_beside_a_run_that_failed_exits_0(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, {"agents/reports/weekly.md": WRITER})
    w.run.how["reports/weekly"] = "fail"
    told = w.cli("serve", "--once", "--plain")
    lines = told.output.splitlines()
    assert lines[-1] == "Done: 1 failed.", lines
    assert any(line.startswith("facts     ") for line in lines), lines
    assert told.exit_code == 0, told.output
    w.at[0] = w.at[0].replace(minute=5)  # past the wait after a failure
    assert w.cli("serve", "--once", "--plain").exit_code == 1  # no fact this time: nothing landed


# -- a run that lands nothing leaves the article byte for byte, and is counted as a run that wrote nothing

@pytest.mark.parametrize("again", ["nothing", "a claim grounding drops"])
def test_a_writer_whose_run_lands_nothing_over_text_citing_only_an_earlier_save_takes_that_text_out(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, again: str) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, {"agents/reports/weekly.md": WRITER})
    w.run.how["reports/weekly"] = cited
    assert w.cli("serve", "--once", "--plain").exit_code == 0
    before, parts = w.article("reports/weekly"), w.stored("reports/weekly")
    assert before is not None and "A note on yard, number 0." in before
    w.revise()
    w.run.how["reports/weekly"] = "" if again == "nothing" else (lambda prompt: "The yard moved to the moon.")
    told = w.cli("serve", "--once", "--plain")
    lines = told.output.splitlines()
    assert any(line.startswith("ran       reports/weekly: wrote nothing: ") for line in lines), lines
    assert not any(line.startswith("wrote ") for line in lines), lines
    assert lines[-1] == "Done: 1 ran and wrote nothing.", lines
    # the fact it cites is an earlier save's now, which counts as withdrawn: its line goes
    assert "A note on yard, number 0." not in (w.article("reports/weekly") or "") and parts
    assert w.stored("reports/weekly") == []
    assert any("so the article holds none of its text now" in line for line in lines), lines


def test_a_run_that_lands_nothing_still_takes_out_a_line_whose_facts_were_all_retracted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, {"agents/reports/weekly.md": WRITER})
    w.take_in()
    w.run.how["reports/weekly"] = cited
    assert w.cli("agents", "run", "reports/weekly").exit_code == 0
    w.move()  # its note is gone at its source, so the fact it cites is retracted
    w.run.how["reports/weekly"] = ""
    told = w.cli("agents", "run", "reports/weekly")
    assert told.exit_code == 0 and told.output.startswith("ran      reports/weekly: wrote nothing: "), told.output
    assert "A note on yard, number 0." not in (w.article("reports/weekly") or "")
    assert any("withdrawn since" in line for line in told.output.splitlines()), told.output


def test_a_run_that_lands_nothing_takes_out_a_part_left_with_only_its_heading_and_says_the_article_holds_none(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, {"agents/reports/weekly.md": WRITER})
    w.take_in()
    w.run.how["reports/weekly"] = lambda prompt: f"## Summary\n\n{cited(prompt)}"
    assert w.cli("agents", "run", "reports/weekly").exit_code == 0
    assert any(by == "reports/weekly" for _, by, _, _ in w.stored("reports/weekly"))
    w.move()  # its note is gone at its source, so the fact its one line cites is retracted
    w.run.how["reports/weekly"] = ""
    told = w.cli("agents", "run", "reports/weekly")
    lines = told.output.splitlines()
    assert told.exit_code == 0, told.output
    assert not any(by == "reports/weekly" for _, by, _, _ in w.stored("reports/weekly")), \
        w.stored("reports/weekly")  # no part of a bare heading stays
    assert "## Summary" not in (w.article("reports/weekly") or "")
    assert not any("so the article keeps what it held" in line for line in lines), lines
    assert any("so the article holds none of its text now" in line for line in lines), lines


@pytest.mark.parametrize("again", ["the same sentence", "nothing"])
def test_a_composer_whose_run_lands_nothing_over_a_part_citing_only_an_earlier_save_drops_that_part(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, again: str) -> None:
    workspace = composed(tmp_path, monkeypatch)
    said: list[str] = []
    workspace.run.how["things/subagents/overview"] = lambda prompt: said.append(cited(prompt)) or said[-1]
    assert workspace.cli("agents", "run", "things/article").exit_code == 0
    before, parts = workspace.article("things/alpha"), workspace.stored("things/alpha")
    workspace.revise()  # what it cites is an earlier save now, so its old sentence no longer grounds
    workspace.run.how["things/subagents/overview"] = (lambda prompt: said[0]) if again == "the same sentence" else ""
    told = workspace.cli("agents", "run", "things/article")
    lines = told.output.splitlines()
    assert told.exit_code == 0 and lines[0].startswith("ran      things/article on things/alpha: wrote nothing: "), lines
    assert before is not None and parts  # it wrote once; grounded again, its part grounds no more
    assert "A note on yard, number 0." not in (workspace.article("things/alpha") or "")
    assert any("Overview: " in line and "nothing it wrote grounds now" in line for line in lines), lines


# -- a curator's run that ends failed is a failure

def test_a_curator_whose_run_ends_failed_is_counted_failed_and_the_pass_exits_1(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    curator = ("---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n"
               "Keep the things.\n")
    w = Workspace(tmp_path / "w", monkeypatch, {"agents/things/curator.md": curator}, admitted=("things/alpha",))
    w.take_in()
    w.run.how["things/curator"] = "fail"
    told = w.cli("serve", "--once", "--plain")
    lines = told.output.splitlines()
    assert "failed    things/curator: a model error" in lines, lines
    assert not any("changed nothing" in line for line in lines), lines
    assert lines[-1] == "Done: 1 failed.", lines
    assert told.exit_code == 1, told.output


# -- a composer whose own run fails: nothing of the article's run lands, and it is a failed run

OWN = ("---\nsubagents: [overview]\nevidence:\n  - query: { from: facts, source: notes }\n---\n\nA brief on one thing."
       "\n\n# Composing\n\nWrite a verdict from what the subagents returned.\n")


def test_a_composer_whose_own_run_fails_lands_nothing_is_counted_failed_and_waits_as_a_failed_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, {
        "agents/things/article.md": OWN, "agents/things/curator.md": CURATOR,
        "agents/things/subagents/overview.md": SUB.format(heading="Overview")}, admitted=("things/alpha",))
    w.take_in()
    w.run.how["things/subagents/overview"] = cited
    w.run.how["things/article"] = lambda prompt: f"## Verdict\n\n{cited(prompt)}"
    assert w.cli("agents", "run", "things/article").exit_code == 0
    before, parts = w.article("things/alpha"), w.stored("things/alpha")
    assert before is not None and "Verdict" in before
    w.move()  # the subagent returns new text, and the composer's own run fails on it
    w.run.how["things/article"] = "fail"
    told = w.cli("agents", "run", "things/article")
    assert told.exit_code == 1, told.output
    assert not any(line.startswith("wrote") for line in told.output.splitlines()), told.output
    assert told.stderr.strip() == "Error: things/article: run run_article wrote nothing: failed: a model error", \
        told.stderr
    assert w.article("things/alpha") == before and w.stored("things/alpha") == parts
    waits = w.cli("serve", "--once", "--plain")  # within the wait after a failure: no run, and no wrote
    assert not any(line.startswith("wrote") or " wrote articles/" in line for line in waits.output.splitlines()), \
        waits.output
    assert w.article("things/alpha") == before
    w.at[0] = w.at[0].replace(hour=11)  # past the wait: it runs again, and fails again
    again = w.cli("serve", "--once", "--plain")
    lines = again.output.splitlines()
    assert "failed    things/article on things/alpha: run run_article wrote nothing: failed: a model error" in lines, \
        lines
    assert lines[-1] == "Done: 1 failed.", lines
    assert again.exit_code == 1, again.output
    assert w.article("things/alpha") == before and w.stored("things/alpha") == parts


def test_a_composer_whose_own_run_failed_on_what_it_wrote_from_before_is_not_said_to_write_on_the_next_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path / "w", monkeypatch, {
        "agents/things/article.md": OWN, "agents/things/curator.md": CURATOR,
        "agents/things/subagents/overview.md": SUB.format(heading="Overview")}, admitted=("things/alpha",))
    w.take_in()
    w.run.how["things/subagents/overview"] = cited
    w.run.how["things/article"] = lambda prompt: f"## Verdict\n\n{cited(prompt)}"
    assert w.cli("agents", "run", "things/article").exit_code == 0
    before = w.article("things/alpha")
    w.run.how["things/article"] = "fail"
    assert w.cli("agents", "run", "things/article").exit_code == 1  # the subagent returned the same text
    w.at[0] = w.at[0].replace(hour=11)
    ran = len(w.run.ran)
    told = w.cli("serve", "--once", "--plain")
    lines = told.output.splitlines()
    assert len(w.run.ran) == ran, w.run.ran[ran:]  # nothing it reads moved since the article landed
    assert not any(line.startswith("wrote") or " wrote articles/" in line for line in lines), lines
    assert lines[-1] == "Done.", lines
    assert w.article("things/alpha") == before


# -- a curator run by hand that ends with no failure exits 0, with a line in a curator's words

@pytest.mark.parametrize("ending", [("wrote nothing", "it ended with an empty reply"),
                                    ("budget spent", "the run holds 3000 tokens, its budget of 3000")],
                         ids=["empty", "budget-spent"])
def test_a_curator_run_by_hand_that_ends_with_no_failure_exits_0_with_a_ran_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: tuple[str, str]) -> None:
    curator = ("---\nevidence:\n  - query: { from: facts, source: notes }\n---\n\n"
               "Keep the things.\n")
    w = Workspace(tmp_path / "w", monkeypatch, {"agents/things/curator.md": curator}, admitted=("things/alpha",))
    w.take_in()
    w.run.how["things/curator"] = ending
    told = w.cli("agents", "run", "things/curator")
    assert told.exit_code == 0, told.output
    lines = told.stdout.splitlines()
    said = ending[1] if ending[0] == "wrote nothing" else f"{ending[0]}: {ending[1]}"
    assert f"ran      things/curator: changed nothing, {said}" in lines, lines
    assert not any("wrote" in line for line in lines), lines
    assert not any("hand" in line for line in lines), lines
