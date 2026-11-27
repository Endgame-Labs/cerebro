"""What grounding repaired or dropped is counted from what grounding did and never from
the words of a line: the counts go under the article's row and on its run's line of the `agent-runs` log, which also
keeps each line of what went, and the run's end says no line of it again, on a terminal or off one."""

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
from cerebro.fakes import FakeExtraction, ScriptedRun
from cerebro.ground import COUNTED, TALLY, counted
from cerebro.run import AgentRun
from cerebro.runner import Progress

AGENT = ("---\nevidence:\n  - query: { from: facts, source: local }\nheartbeat: [change]\n---\n\nSum up the notes.\n\n"
         "# Output\n\n## Summary\n\n## Detail\n")


class Uncited(AgentRun):
    """A stand-in writer that cites nothing under Detail, so grounding drops that sentence and the part."""

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        import re

        def write(prompt: str) -> dict[str, str]:
            facts = re.findall(r"\[\^(fact_\w+)\] ([^(\n]+)", prompt)
            return {"Summary": " ".join(f"{t.strip()} [^{i}]" for i, t in facts), "Detail": "Nothing yet."}

        return ScriptedRun(write).run(agent, evidence, tools, budget)


def lay_out_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    extraction = FakeExtraction()
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, agent_run=Uncited(), extraction=extraction,
                                                                clock=lambda: datetime(2026, 11, 18, 9, tzinfo=UTC)))
    root = tmp_path / "team"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    (root / "local" / "kickoff.md").write_text(NOTE)
    (root / "agents" / "digests").mkdir(parents=True)
    (root / "agents" / "digests" / "weekly.md").write_text(AGENT)
    return root


def test_the_counts_are_said_one_kind_after_another() -> None:
    assert counted({}) == ""
    assert counted({"repaired": 3, "dropped": 1, "leads": 2}) == ("repaired 3 sentences, dropped 1 ungrounded sentence, "
                                                                   "removed 2 leads the judge did not pass")
    assert counted({"parts": 1, "tables": 2}) == "dropped 2 tables, dropped 1 part"
    assert all(counted({kind: 1}) for kind, _, _ in COUNTED)


def test_a_pass_counts_what_grounding_did_keeps_each_line_on_the_runs_line_and_says_none_of_them_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        events: list[Progress] = []
        result = workspace.run_once(on=events.append)
        rows = [row.data for row in workspace.store.rows("agent-runs", {"agent": "digests/weekly"})]
    assert result.compiled == ["digests/weekly"]
    tally = result.grounding["digests/weekly"]
    lines = [line for line in result.warnings if line in result.grounded]
    assert lines and sum(tally.values()) == len(lines)  # each line of what went counted once, by kind
    assert tally.get("dropped", 0) >= 1 and tally.get("parts", 0) == 1
    note = next(e for e in events if e.kind == "note")
    assert (note.stage, note.key, note.text) == ("compile", "digests/weekly", counted(tally))
    kept = rows[-1]  # the run's line, as it stood, with what grounding did on it
    assert kept["grounding"] == tally and [line.split(": ", 1)[1] for line in lines] == kept["notes"]
    assert rows[0]["run"] == kept["run"] and rows[0]["status"] == kept["status"]
    passed = CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)])  # nothing moved: no run
    assert "Nothing yet." not in passed.output


def test_off_a_terminal_a_pass_says_what_grounding_dropped_as_counts_alone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    passed = CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)])
    assert passed.exit_code == 0, passed.output
    assert "wrote     digests/weekly" in passed.output
    assert "Nothing yet." not in passed.output, passed.output  # no line of what went, only its count
    assert [line for line in passed.output.splitlines() if "dropped" in line] == [
        line for line in passed.output.splitlines() if line.startswith("grounding digests/weekly: ")]


def test_a_tally_is_kept_only_while_a_compile_sets_one() -> None:
    from cerebro.ground import tallied

    tallied({"dropped": 1})  # none set: counted nowhere
    kept: Counter[str] = Counter()
    token = TALLY.set(kept)
    try:
        tallied({"dropped": 1, "repaired": 2})
    finally:
        TALLY.reset(token)
    assert kept == Counter({"dropped": 1, "repaired": 2})


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
        long = 'Summary: repaired "' + "a long sentence " * 40 + '" as "' + "the long sentence " * 40 + '"'
        keep_grounding(workspace.store, run, {"repaired": 1}, [long])
        assert workspace.store.rows("agent-runs")[-1].data["notes"] == [long]  # whole, past 300 characters
        many = [long] * 200
        keep_grounding(workspace.store, run, {"repaired": 200}, many)
        kept = workspace.store.rows("agent-runs")[-1].data
    assert sum(len(n) for n in kept["notes"][:-1]) <= NOTES_MOST and kept["grounding"] == {"repaired": 200}
    assert kept["notes"][-1].startswith(f"and {200 - len(kept['notes']) + 1} more, not kept")


def test_what_landing_made_safe_is_counted_per_article_and_each_line_kept_on_its_runs_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    runs = count(1)  # each run hands in new text, so each lands and is cleaned
    root = saying(tmp_path, monkeypatch, lambda ids: {  # every sentence grounds: only landing changes the text
        "Summary": f"The review moved <script>alert({next(runs)})</script> to March 3 [^{ids[0]}].",
        "Detail": f"The review moved to March 3 [^{ids[0]}].\n\n```yaml\nmoved: March 3\u200b [^{ids[0]}]\n```"})
    events: list[Progress] = []
    with cli.open_workspace(root) as workspace:
        result = workspace.run_once(on=events.append)
        passed = workspace.store.rows("agent-runs", {"agent": "digests/weekly"})[-1].data
    assert not result.grounding and [line for line in passed["notes"] if "escaped raw HTML" in line], passed
    assert [e.text for e in events if e.kind == "note"] == ["made 2 safety edits"]  # under the article's row
    cli._print(result)
    printed = capsys.readouterr().out
    assert "cleaned   digests/weekly: made 2 safety edits" in printed.splitlines(), printed
    assert "escaped raw HTML" not in printed  # no line of it, only its count
    assert "kept invisible characters" in printed  # the yaml block's: it changed nothing, so it is its own line
    by_hand = CliRunner().invoke(cli.main, ["agents", "run", "digests/weekly", "--workspace", str(root)])
    with cli.open_workspace(root) as workspace:
        kept = workspace.store.rows("agent-runs", {"agent": "digests/weekly"})[-1].data  # the run by hand's line
    assert "cleaned  digests/weekly: made 2 safety edits" in by_hand.output.splitlines(), by_hand.output
    assert "escaped raw HTML" not in by_hand.output and kept.get("notes") == passed["notes"], kept
    assert "kept invisible characters" in by_hand.output
    by_article = CliRunner().invoke(cli.main, ["compile", "--article", "digests/weekly", "--workspace", str(root)])
    with cli.open_workspace(root) as workspace:
        kept = workspace.store.rows("agent-runs", {"agent": "digests/weekly"})[-1].data
    assert "cleaned  digests/weekly: made 2 safety edits" in by_article.output.splitlines(), by_article.output
    assert kept.get("notes") == passed["notes"], kept


def test_off_a_terminal_and_in_status_the_counts_are_said(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    root = lay_out_workspace(tmp_path, monkeypatch)
    passed = CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)])
    with cli.open_workspace(root) as workspace:
        said = counted(next(iter(workspace.run_once().grounding.values()), {})) or None
    assert "grounding digests/weekly: dropped " in passed.output, passed.output
    status = CliRunner().invoke(cli.main, ["status", "--workspace", str(root)])
    assert "grounding 1 article: dropped " in status.output and "digests/weekly: dropped " in status.output
    data = json.loads(CliRunner().invoke(cli.main, ["status", "--json", "--workspace", str(root)]).output)
    assert data["grounding"]["digests/weekly"]["parts"] == 1
    assert said is None or "dropped" in said


def test_an_article_grounding_left_empty_or_a_citation_unread_is_still_to_check(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_first_run_screen import still

    from cerebro.runner import PassResult

    for result, name in ((PassResult(ran={"parts/writer": "grounding dropped everything it handed in"},
                                     grounding={"parts/writer": {"dropped": 2}}), "dropped 2 ungrounded sentences"),
                         (PassResult(compiled=["parts/gamma"], grounding={"parts/writer": {"citations": 1}}),
                          "removed 1 citation"),
                         (PassResult(compiled=["parts/gamma"], grounding={"parts/writer": {"repaired": 3}}), "")):
        screen, out, _ = still()
        screen(Progress("build", "start"))
        screen(Progress("build", "end"))
        cli._print_end(screen, result, fails=False)
        last = " ".join(out.getvalue().split("\n")[-3:])
        assert ("Done with warnings" in last and name in last) if name else "Done." in last, last
