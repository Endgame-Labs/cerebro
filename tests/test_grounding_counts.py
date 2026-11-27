"""What grounding marked or left unscored is counted from what grounding did and never from the words of a line: the
counts go under the article's row and on its run's line of the `agent-runs` log, which also keeps each sentence's
score and why, and the run's end says no line of it again, on a terminal or off one."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_cli import NOTE

from cerebro import cli
from cerebro.app import Cerebro
from cerebro.article import body
from cerebro.fakes import FakeDecider, FakeExtraction, ScriptedRun
from cerebro.ground import COUNTED, TALLY, counted
from cerebro.run import AgentRun
from cerebro.runner import Progress

AGENT = ("---\nevidence:\n  - query: { from: facts, source: local }\nheartbeat: [change]\n---\n\nSum up the notes.\n\n"
         "# Output\n\n## Summary\n\n## Detail\n")


class Uncited(AgentRun):
    """A stand-in writer that cites nothing under Detail, which the judge scores made up, as it does each first
    sentence."""

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        import re

        def write(prompt: str) -> dict[str, str]:
            facts = re.findall(r"\[\^(fact_\w+)\] ([^(\n]+)", prompt)
            return {"Summary": " ".join(f"{t.strip()} [^{i}]" for i, t in facts), "Detail": "Nothing yet."}

        return ScriptedRun(write).run(agent, evidence, tools, budget)


def lay_out_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    extraction = FakeExtraction()
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, agent_run=Uncited(), extraction=extraction,
                                                                clock=lambda: datetime(2026, 11, 18, 9, tzinfo=UTC),
                                                                decider=FakeDecider({"s1": 0})))
    root = tmp_path / "team"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    (root / "local" / "kickoff.md").write_text(NOTE)
    (root / "agents" / "digests").mkdir(parents=True)
    (root / "agents" / "digests" / "weekly.md").write_text(AGENT)
    return root


def test_the_counts_are_said_one_kind_after_another() -> None:
    assert counted({}) == "" and counted({"marked": 0}) == ""
    assert counted({"unscored": 1, "marked": 3}) == "marked 3 sentences not fully backed, left 1 sentence unscored"
    assert all(counted({kind: 1}) for kind, _, _ in COUNTED)


def test_a_pass_counts_what_grounding_marked_keeps_each_score_on_the_runs_line_and_says_none_of_them_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        events: list[Progress] = []
        result = workspace.run_once(on=events.append)
        rows = [row.data for row in workspace.store.rows("agent-runs", {"agent": "digests/weekly"})]
        written = body(workspace.store.article("digests/weekly"))
    assert result.compiled == ["digests/weekly"]
    tally = result.grounding["digests/weekly"]
    assert tally == {"marked": 2} and not result.warnings
    note = next(e for e in events if e.kind == "note")
    assert (note.stage, note.key, note.text) == ("compile", "digests/weekly", "marked 2 sentences not fully backed")
    kept = rows[-1]  # the run's line, as it stood, with what grounding did on it
    assert kept["grounding"] == tally and 'Detail: 0.00 "Nothing yet.": made up' in kept["notes"][-1]
    assert rows[0]["run"] == kept["run"] and rows[0]["status"] == kept["status"]
    assert "Nothing yet.[^unsupported]" in written
    passed = CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)])  # nothing moved: no run
    assert "Nothing yet." not in passed.output


def test_off_a_terminal_a_pass_says_what_grounding_marked_as_counts_alone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    passed = CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)])
    assert passed.exit_code == 0, passed.output
    assert "wrote     digests/weekly" in passed.output
    assert "Nothing yet." not in passed.output, passed.output  # no score, only its count
    assert "grounding digests/weekly: marked 2 sentences not fully backed" in passed.output.splitlines()


@pytest.mark.parametrize("command", [["agents", "run"], ["compile", "--article"]])
def test_a_run_by_hand_says_what_grounding_marked_and_keeps_the_count_on_its_runs_line_as_a_pass_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    ran = CliRunner().invoke(cli.main, [*command, "digests/weekly", "--workspace", str(root)])
    assert ran.exit_code == 0, ran.output
    assert "grounding digests/weekly: marked 1 sentence not fully backed" in ran.output.splitlines(), ran.output
    with cli.open_workspace(root) as workspace:  # no pass took in the note, so only the uncited line is marked
        rows = [row.data for row in workspace.store.rows("agent-runs", {"agent": "digests/weekly"})]
    assert rows[-1]["grounding"] == {"marked": 1}


def test_a_tally_is_kept_only_while_a_compile_sets_one() -> None:
    from cerebro.ground import tallied

    tallied({"marked": 1})  # none set: counted nowhere
    kept: Counter[str] = Counter()
    token = TALLY.set(kept)
    try:
        tallied({"marked": 1, "unscored": 2})
    finally:
        TALLY.reset(token)
    assert kept == Counter({"marked": 1, "unscored": 2})


class Saying(AgentRun):
    """A stand-in writer that hands in what `say(facts)` gives, as heading to text."""

    def __init__(self, say: Any) -> None:
        self.say = say

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        import re

        return ScriptedRun(lambda prompt: self.say(re.findall(r"\[\^(fact_\w+)\] [^(\n]+", prompt))).run(
            agent, evidence, tools, budget)


def saying(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, say: Any) -> Path:
    root = lay_out_workspace(tmp_path, monkeypatch)
    extraction = FakeExtraction()
    monkeypatch.setattr(cli, "open_workspace", lambda path: Cerebro(path, agent_run=Saying(say), extraction=extraction,
                                                                clock=lambda: datetime(2026, 11, 18, 9, tzinfo=UTC)))
    return root


def test_a_note_is_kept_whole_and_one_runs_notes_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.compile import NOTES_MOST, keep_grounding
    from cerebro.run import Run

    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        workspace.run_once()
        run_id = next(r.data["run"] for r in workspace.store.rows("agent-runs") if r.data.get("run"))
        run = Run(run_id, "digests/weekly", "digests/weekly", "done")
        before = workspace.store.rows("agent-runs")[-1].data["notes"]
        long = 'Summary: 0.20 "' + "a long sentence " * 40 + '": it misrepresents what its facts say'
        keep_grounding(workspace.store, run, {"marked": 1}, [long])
        assert workspace.store.rows("agent-runs")[-1].data["notes"] == [*before, long]  # whole, past 300 characters
        many = [long] * 200
        keep_grounding(workspace.store, run, {"marked": 200}, many)
        kept = workspace.store.rows("agent-runs")[-1].data
    assert sum(len(n) for n in kept["notes"][:-1]) <= NOTES_MOST and kept["grounding"] == {"marked": 200}
    assert kept["notes"][-1] == f"and {200 - (len(kept['notes']) - len(before) - 2)} more, not kept"


def test_what_landing_made_safe_is_counted_per_article_and_each_line_kept_on_its_runs_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    runs = count(1)  # each run hands in new text, so each lands and is cleaned
    root = saying(tmp_path, monkeypatch, lambda ids: {  # no decision model: each sentence is unscored
        "Summary": f"The review moved <script>alert({next(runs)})</script> to March 3 [^{ids[0]}].",
        "Detail": f"The review moved to March 3 [^{ids[0]}].\n\n```yaml\nmoved: March 3\u200b [^{ids[0]}]\n```"})
    events: list[Progress] = []
    with cli.open_workspace(root) as workspace:
        result = workspace.run_once(on=events.append)
        passed = workspace.store.rows("agent-runs", {"agent": "digests/weekly"})[-1].data
    assert result.grounding == {"digests/weekly": {"unscored": 3}}
    assert [line for line in passed["notes"] if "escaped raw HTML" in line], passed
    assert [e.text for e in events if e.kind == "note"] == ["left 3 sentences unscored, made 2 safety edits"]
    cli._print(result)
    printed = capsys.readouterr().out
    assert "cleaned   digests/weekly: made 2 safety edits" in printed.splitlines(), printed
    assert "escaped raw HTML" not in printed  # no line of it, only its count
    assert "kept invisible characters" in printed  # the yaml block's: it changed nothing, so it is its own line
    by_hand = CliRunner().invoke(cli.main, ["agents", "run", "digests/weekly", "--workspace", str(root)])
    with cli.open_workspace(root) as workspace:
        kept = workspace.store.rows("agent-runs", {"agent": "digests/weekly"})[-1].data  # the run by hand's line
    assert "cleaned  digests/weekly: made 2 safety edits" in by_hand.output.splitlines(), by_hand.output
    def edits(row: dict[str, Any]) -> list[str]:  # what landing made safe: each run's scores name its own text
        return [line for line in row["notes"] if " unscored " not in line]

    assert "escaped raw HTML" not in by_hand.output and edits(kept) == edits(passed), kept
    assert "kept invisible characters" in by_hand.output
    by_article = CliRunner().invoke(cli.main, ["compile", "--article", "digests/weekly", "--workspace", str(root)])
    with cli.open_workspace(root) as workspace:
        kept = workspace.store.rows("agent-runs", {"agent": "digests/weekly"})[-1].data
    assert "cleaned  digests/weekly: made 2 safety edits" in by_article.output.splitlines(), by_article.output
    assert edits(kept) == edits(passed), kept


def test_off_a_terminal_and_in_status_the_latest_runs_counts_are_said(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    root = lay_out_workspace(tmp_path, monkeypatch)
    passed = CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)])
    with cli.open_workspace(root) as workspace:
        said = counted(next(iter(workspace.run_once().grounding.values()), {})) or None
    assert "grounding digests/weekly: marked " in passed.output, passed.output
    status = CliRunner().invoke(cli.main, ["status", "--workspace", str(root)])
    assert "grounding 1 article: marked " in status.output and "digests/weekly: marked " in status.output
    data = json.loads(CliRunner().invoke(cli.main, ["status", "--json", "--workspace", str(root)]).output)
    assert data["grounding"]["digests/weekly"]["marked"] == 2
    assert said is None or "marked" in said
    with cli.open_workspace(root) as workspace:
        extraction = workspace.extraction.handler  # the stand-in facts layer the pass filled
    labels = Saying(lambda ids: {"Summary": "Risks:", "Detail": "Next steps:"})  # no sentence to score
    monkeypatch.setattr(cli, "open_workspace", lambda path: Cerebro(path, agent_run=labels, extraction=extraction,
                                                                clock=lambda: datetime(2026, 11, 18, 9, tzinfo=UTC),
                                                                decider=FakeDecider()))
    clean = CliRunner().invoke(cli.main, ["compile", "--article", "digests/weekly", "--workspace", str(root)])
    assert clean.exit_code == 0 and "wrote    digests/weekly" in clean.output, clean.output
    data = json.loads(CliRunner().invoke(cli.main, ["status", "--json", "--workspace", str(root)]).output)
    assert data["grounding"] == {}, data["grounding"]  # the run by hand's text, not the pass's before it


PARTS = "---\nsubagents: [alpha, beta]\n---\n\nA brief on one thing.\n"
PART = ("---\nevidence:\n  - query: {{ from: facts, source: {source} }}\nheartbeat: [change]\n---\n\n"
        "# Instructions\n\nDraft it.\n\n# Output\n\n## {heading}\n")
OTHER = "kind: files\nwindow: { behind: all }\nselect:\n  - glob: \"other/**/*.md\"\n    extract: facts\n"


class Parts(AgentRun):
    """Stand-in subagents that each cite their first fact, beta then adding a line that cites nothing while `marks`
    holds."""

    marks = True

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        import re

        def write(prompt: str) -> dict[str, str]:
            fact, text = re.findall(r"\[\^(fact_\w+)\] ([^(\n]+)", prompt)[0]
            if agent.id.endswith("alpha"):
                return {"Alpha": f"{text.strip()} [^{fact}]"}
            return {"Beta": f"{text.strip()} [^{fact}]" + ("\n\nNothing yet." if self.marks else "")}

        return ScriptedRun(write).run(agent, evidence, tools, budget)


class Picky(FakeDecider):
    """The stand-in judge, which scores made up each sentence that says "Nothing yet"."""

    def decide(self, state: Any, questions: Any) -> Any:
        self.answers = {u["id"]: 0 for u in state.get("passage", []) if "Nothing yet" in u["sentence"]}
        return super().decide(state, questions)


def test_status_counts_what_the_latest_run_of_each_part_left_on_the_article(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    parts, extraction = Parts(), FakeExtraction()
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, agent_run=parts, extraction=extraction,
                                                                clock=lambda: datetime(2026, 11, 18, 9, tzinfo=UTC),
                                                                decider=Picky()))
    root = tmp_path / "team"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    (root / "sources" / "other.yaml").write_text(OTHER)
    (root / "other").mkdir()
    (root / "agents" / "digests" / "subagents").mkdir(parents=True)
    (root / "agents" / "digests" / "weekly.md").write_text(PARTS)
    for name, source in (("alpha", "local"), ("beta", "other")):
        (root / "agents" / "digests" / "subagents" / f"{name}.md").write_text(
            PART.format(source=source, heading=name.title()))

    def counts_after(*notes: tuple[str, str]) -> Any:  # a pass that takes in each note, then what status counts
        for path, text in notes:
            (root / path).write_text(f"Date: 2026-11-10\n\n{text}\n")
        assert CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)]).exit_code == 0
        return json.loads(CliRunner().invoke(cli.main, ["status", "--json", "--workspace", str(root)]).output)[
            "grounding"]

    marked = {"digests/weekly": {"marked": 1}}  # beta's line that cites nothing
    assert counts_after(("local/kickoff.md", "The review moved to March 3."),
                        ("other/plan.md", "The plan names four steps.")) == marked
    assert counts_after(("local/later.md", "The review moved to March 4.")) == marked  # alpha alone ran again
    parts.marks = False
    assert counts_after(("other/later.md", "The plan names five steps.")) == {}  # beta ran again, clean


