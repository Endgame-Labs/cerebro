"""One cooldown for every writer: the workspace's `schedule: { articles: }`, 4 hours unless it says otherwise, which an
agent's own `heartbeat: { cooldown: }` overrides, `0` for every pass. It holds under `serve` only, a pass run with
`scheduled`, and only a rebuild that new evidence alone asks for: it counts from the agent's last run on that article.
Every other cause runs at once, and a run it starts moves the
article's clock, so an instruction counts once."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cerebro import Cerebro, Record
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.clock import stamp
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.project import WorkspaceError, read_schedule
from cerebro.records import Proposal, propose

YOU = Caller("you", "person")
AGENT = "things/article"


class Hands:
    """A clock moved by hand."""

    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def move(self, **delta: float) -> None:
        self.at += timedelta(**delta)


def cited(prompt: str) -> dict[str, str]:
    """A summary that cites the first fact the prompt shows, as a run must for its line to land."""
    first = re.search(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])
    return {"Summary": f"x [^{first.group(1)}]"} if first else {"Summary": "x"}


def note(id: str, body: str) -> Record:
    return Record(id=id, source="notes", revision="1", at="2026-11-17T08:00:00Z", body=body, keys={"topic": ["first"]})


def opened(tmp_path: Path, hands: Hands, store: MemoryStore, extraction: FakeExtraction,
           notes: FakeConnector) -> Cerebro:
    return Cerebro(tmp_path, store=store, extraction=extraction, clock=hands, connectors=[notes],
                   agent_run=ScriptedRun(cited, clock=hands))


def workspace(tmp_path: Path, hands: Hands, front: str = "", settings: str = "{}\n") -> tuple[Cerebro, FakeConnector]:
    """A workspace with one writer for the things collection, which holds things/first, over every note."""
    notes = FakeConnector("notes", [note("n0", "The first note.")])
    (tmp_path / "agents" / "things").mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings.yaml").write_text(settings)
    (tmp_path / "agents" / "things" / "article.md").write_text(
        f"---\nevidence:\n  - query: {{ from: facts, source: notes }}\n{front}---\n\n# Output\n\n## Summary\n")
    cerebro = opened(tmp_path, hands, MemoryStore(), FakeExtraction(), notes)
    cerebro.store.create(Identity("things/first", "things", "First", {"topic": ["first"]}))
    return cerebro, notes


def started(tmp_path: Path, **options: str) -> tuple[Cerebro, FakeConnector, Hands]:
    """A workspace whose first pass wrote things/first at 09:00, and a note that came an hour later."""
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    cerebro, notes = workspace(tmp_path, hands, **options)
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]
    notes.add(note("n1", "A later note."))
    hands.move(hours=1)
    return cerebro, notes, hands


def test_with_no_setting_new_evidence_waits_four_hours_after_a_writers_last_run(tmp_path: Path) -> None:
    cerebro, _, hands = started(tmp_path)
    held = cerebro.run_once(scheduled=True)
    assert held.compiled == [] and held.extracted == 1
    assert held.skipped[AGENT] == "cooling down: new evidence waits until 2026-11-17 13:00, cooldown 4h"
    hands.move(hours=3)
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def test_a_read_says_the_article_is_behind_its_facts_while_its_writer_cools_down(tmp_path: Path) -> None:
    cerebro, _, _ = started(tmp_path)
    assert cerebro.run_once(scheduled=True).compiled == []
    [article] = cerebro.call(YOU, "read", {"ids": ["things/first"]})["articles"]
    assert [f["text"] for f in article["facts_since_written"]] == ["A later note."]


def test_the_workspace_sets_the_cooldown_and_0_rewrites_on_every_pass(tmp_path: Path) -> None:
    cerebro, _, _ = started(tmp_path, settings="schedule: { articles: 0 }\n")
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def test_the_workspace_cooldown_is_any_duration(tmp_path: Path) -> None:
    cerebro, _, hands = started(tmp_path, settings="schedule: { articles: 90m }\n")
    held = cerebro.run_once(scheduled=True)
    assert held.skipped[AGENT] == "cooling down: new evidence waits until 2026-11-17 10:30, cooldown 90m"
    hands.move(minutes=30)
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def test_an_agents_own_cooldown_overrides_the_workspaces_and_its_0_rewrites_on_every_pass(tmp_path: Path) -> None:
    cerebro, _, _ = started(tmp_path, front="heartbeat: { on: [change], cooldown: 0 }\n")
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def test_an_agents_longer_cooldown_overrides_the_workspaces(tmp_path: Path) -> None:
    cerebro, _, hands = started(tmp_path, front="heartbeat: { on: [change], cooldown: 6h }\n",
                                settings="schedule: { articles: 0 }\n")
    assert cerebro.run_once(scheduled=True).skipped[AGENT] == "cooling down: new evidence waits until 2026-11-17 15:00, cooldown 6h"
    hands.move(hours=5)
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def test_an_articles_first_build_runs_at_once_and_starts_no_new_wait_for_the_others(tmp_path: Path) -> None:
    cerebro, _, hands = started(tmp_path)
    cerebro.store.create(Identity("things/second", "things", "Second"))
    assert cerebro.run_once(scheduled=True).compiled == ["things/second"]  # things/first still waits
    hands.move(hours=3)  # four hours after the agent's last run that was not a first build
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def test_a_persons_instruction_runs_at_once(tmp_path: Path) -> None:
    cerebro, _, hands = started(tmp_path)
    instruct(cerebro, hands, "things/first")
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def instruct(cerebro: Cerebro, hands: Hands, target: str, by: Caller = YOU, id: str = "p_lead") -> None:
    at = stamp(hands())
    propose(cerebro.store, Proposal(id=id, kind="article.instruct", target=target, by=by, at=at,
                                    payload={"text": "Lead with the later note."}, status="accepted",
                                    decided_by=by, decided_at=at))


def test_a_curators_instruction_on_the_article_runs_at_once_as_a_persons_does(tmp_path: Path) -> None:
    cerebro, _, hands = started(tmp_path)
    instruct(cerebro, hands, "things/first", Caller("things/curator", "agent"))
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def test_the_writers_own_instruction_waits_out_the_cooldown(tmp_path: Path) -> None:
    cerebro, _, hands = started(tmp_path)
    instruct(cerebro, hands, "things/first", Caller(AGENT, "agent"))
    assert cerebro.run_once(scheduled=True).compiled == []


@pytest.mark.parametrize("target", ["things", AGENT])
def test_an_instruction_to_the_collection_or_the_agent_is_on_no_article_so_new_evidence_waits(
        tmp_path: Path, target: str) -> None:
    cerebro, _, hands = started(tmp_path)
    instruct(cerebro, hands, target)
    assert cerebro.run_once(scheduled=True).compiled == []


def test_one_instruction_runs_the_article_once_and_then_new_evidence_waits_again(tmp_path: Path) -> None:
    cerebro, notes, hands = started(tmp_path)
    instruct(cerebro, hands, "things/first")
    runs = []
    for n in range(4):
        runs.append(cerebro.run_once(scheduled=True).compiled)
        hands.move(minutes=20)
        notes.add(note(f"m{n}", f"Another note {n}."))
    assert runs == [["things/first"], [], [], []]


def test_a_run_on_one_article_holds_back_none_of_the_agents_others(tmp_path: Path) -> None:
    cerebro, notes, hands = started(tmp_path)  # things/first ran at 09:00
    cerebro.store.create(Identity("things/second", "things", "Second", {"topic": ["first"]}))
    assert cerebro.run_once(scheduled=True).compiled == ["things/second"]  # its first build, at 10:00
    hands.move(hours=3)
    notes.add(note("n2", "A note at one."))
    held = cerebro.run_once(scheduled=True)  # 13:00: four hours after things/first's run, three after the second's
    assert held.compiled == ["things/first"]


def test_a_pass_by_hand_never_waits(tmp_path: Path) -> None:
    cerebro, _, _ = started(tmp_path)
    assert cerebro.run_once().compiled == ["things/first"]


def test_a_change_to_workspace_md_runs_at_once(tmp_path: Path) -> None:
    cerebro, _, _ = started(tmp_path)
    (tmp_path / "workspace.md").write_text("---\n---\n\nA wiki of things.\n")
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def test_a_change_to_an_ejected_prompt_runs_at_once(tmp_path: Path) -> None:
    cerebro, _, _ = started(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "rules.md").write_text("Write it.\n")
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]


def composed(tmp_path: Path, hands: Hands, sub: str = "") -> tuple[Cerebro, FakeConnector]:
    """A writer for things that lists two subagents over every note: `sub` is the first one's front matter."""
    notes = FakeConnector("notes", [note("n0", "The first note.")])
    folder = tmp_path / "agents" / "things"
    (folder / "subagents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (folder / "article.md").write_text("---\nsubagents: [early, late]\n---\n\nOne thing.\n")
    for name, front in (("early", sub), ("late", "")):
        (folder / "subagents" / f"{name}.md").write_text(
            f"---\nevidence:\n  - query: {{ from: facts, source: notes }}\n{front}---"
            f"\n\n# Instructions\n\nDraft it.\n\n# Output\n\n## Summary\n")
    cerebro = opened(tmp_path, hands, MemoryStore(), FakeExtraction(), notes)
    cerebro.store.create(Identity("things/first", "things", "First", {"topic": ["first"]}))
    return cerebro, notes


def test_a_subagents_own_cooldown_holds_back_its_part_and_the_run_waits_for_the_first_part_due(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    cerebro, notes = composed(tmp_path, hands, "heartbeat: { on: [change], cooldown: 1h }\n")
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]
    notes.add(note("n1", "A later note."))
    hands.move(minutes=30)
    assert cerebro.run_once(scheduled=True).skipped[AGENT] == (
        "cooling down: new evidence waits until 2026-11-17 10:00, cooldown 1h")
    hands.move(minutes=30)
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]  # not the composer's 4h


def test_a_change_to_a_subagents_file_runs_at_once(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    cerebro, notes = composed(tmp_path, hands)
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]
    notes.add(note("n1", "A later note."))
    hands.move(hours=1)
    assert cerebro.run_once(scheduled=True).compiled == []
    path = tmp_path / "agents" / "things" / "subagents" / "late.md"
    path.write_text(path.read_text().replace("Draft it.", "Draft it short."))
    again = opened(tmp_path, hands, cerebro.store, cerebro.extraction, notes)  # type: ignore[arg-type]
    assert again.run_once(scheduled=True).compiled == ["things/first"]


def test_a_change_to_the_agents_file_runs_at_once(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    extraction = FakeExtraction()
    notes = FakeConnector("notes", [note("n0", "The first note.")])
    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    path = tmp_path / "agents" / "things" / "article.md"
    path.write_text("---\nevidence:\n  - query: { from: facts, source: notes }\n---\n\n# Output\n\n## Summary\n")
    cerebro = opened(tmp_path, hands, MemoryStore(), extraction, notes)
    cerebro.store.create(Identity("things/first", "things", "First"))
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]
    hands.move(hours=1)
    path.write_text(path.read_text().replace("## Summary", "## Summary\n\nKeep it short."))
    assert opened(tmp_path, hands, cerebro.store, extraction, notes).run_once(scheduled=True).compiled == ["things/first"]


def test_a_cited_fact_retracted_runs_at_once(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    cerebro, notes = workspace(tmp_path, hands)
    notes.add(note("n1", "A second note."))
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]
    hands.move(hours=1)
    [part] = cerebro.store.article("things/first").compilations
    [fact] = cerebro.extraction.facts(ids=[c.id for c in part.citations if c.kind == "fact"])
    notes.remove(fact.record)  # the note whose fact the article cites
    result = cerebro.run_once(scheduled=True)
    assert result.retracted == 1 and result.compiled == ["things/first"]


def test_a_retracted_fact_the_article_does_not_cite_waits(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    cerebro, notes = workspace(tmp_path, hands)
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]
    notes.add(note("n1", "A later note."))
    hands.move(hours=1)
    assert cerebro.run_once(scheduled=True).compiled == []  # n1 came while it cooled down, so nothing cites it
    notes.remove("n1")
    hands.move(minutes=20)  # past the source's freshness window, so a pass under serve lists it again
    result = cerebro.run_once(scheduled=True)
    assert result.retracted == 1 and result.compiled == []


def test_an_agent_reading_a_script_line_gets_the_workspace_cooldown_and_no_warning_of_its_own(tmp_path: Path) -> None:
    from cerebro.agent import load_agent

    (tmp_path / "agents" / "things").mkdir(parents=True)
    path = tmp_path / "agents" / "things" / "article.md"
    path.write_text("---\nevidence:\n  - script: { run: count.py }\n---\n\n# Output\n\n## Summary\n")
    agent = load_agent(path, tmp_path / "agents")
    assert agent.cooldown is None and not any("cooldown" in w for w in agent.warnings)


@pytest.mark.parametrize("value", ["30s", "soon", "-1h"])
def test_a_workspace_cooldown_that_is_no_duration_is_refused(value: str) -> None:
    with pytest.raises(WorkspaceError, match="schedule.articles"):
        read_schedule({"schedule": {"articles": value}})


def test_the_workspace_cooldown_reads_as_4h_0_or_a_duration() -> None:
    assert read_schedule({}).articles == timedelta(hours=4)
    assert read_schedule({"schedule": {"articles": 0}}).articles == timedelta(0)
    assert read_schedule({"schedule": {"articles": "90m"}}).articles == timedelta(minutes=90)


# -- the article's clock after a run by hand, a failed run and a pass by hand


class _Failing(ScriptedRun):
    """A run that fails, as a model error leaves one, while `fail` is set."""

    fail = False

    def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        run = super().run(*args, **kwargs)
        if self.fail:
            run.status, run.note, run.result = "failed", "APIError: the provider answered 529", None
        return run


def _mark(cerebro: Cerebro) -> str | None:
    from cerebro.runner import _beat

    return ((_beat(cerebro, cerebro.agent(AGENT)).articles or {}).get("things/first") or {}).get("ran")


@pytest.mark.parametrize("command", [["agents", "run", AGENT], ["compile", "--article", "things/first"]])
def test_a_run_by_hand_moves_the_articles_clock_so_the_instruction_it_took_in_does_not_run_it_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]) -> None:
    """Every compile that runs records its run, by hand too, and an instruction decided before it is no cause."""
    from contextlib import contextmanager

    from click.testing import CliRunner

    from cerebro import cli

    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    cerebro, notes = workspace(tmp_path, hands, front="heartbeat: { on: [change], cooldown: 4h }\n")
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]  # 09:00, served
    hands.move(hours=1)
    instruct(cerebro, hands, "things/first")  # 10:00
    hands.move(minutes=5)

    @contextmanager
    def same(root: object):  # type: ignore[no-untyped-def]
        yield cerebro

    monkeypatch.setattr(cli, "open_workspace", same)
    done = CliRunner().invoke(cli.main, [*command, "--workspace", str(tmp_path)])
    assert done.exit_code == 0, done.output
    assert _mark(cerebro) == "2026-11-17T10:05:00Z"
    hands.move(minutes=15)
    notes.add(note("n1", "A later note."))  # 10:20: new evidence alone, inside the cooldown from 10:05
    served = cerebro.run_once(scheduled=True)
    assert served.compiled == [] and "14:05" in served.skipped[AGENT]


def test_a_failed_run_leaves_the_clock_where_it_was_so_the_instruction_runs_on_the_next_pass(tmp_path: Path) -> None:
    """A failed run keeps the previous state, so the last run's time stays."""
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    notes = FakeConnector("notes", [note("n0", "The first note.")])
    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: { on: [change], cooldown: 4h }\n---\n"
        "\n# Output\n\n## Summary\n")
    run = _Failing(cited, clock=hands)
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands, connectors=[notes],
                      agent_run=run)
    cerebro.store.create(Identity("things/first", "things", "First", {"topic": ["first"]}))
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]  # 09:00
    hands.move(minutes=55)
    instruct(cerebro, hands, "things/first")  # 09:55
    hands.move(minutes=5)
    run.fail = True
    failed = cerebro.run_once(scheduled=True)  # 10:00: the model errors
    assert failed.compiled == [] and f"{AGENT} on things/first" in failed.failed
    assert _mark(cerebro) == "2026-11-17T09:00:00Z"
    run.fail = False
    hands.move(minutes=20)
    assert cerebro.run_once(scheduled=True).compiled == ["things/first"]  # 10:20: the instruction still runs it


def test_a_pass_by_hand_keeps_an_agents_own_cooldown_and_drops_only_the_workspace_cadence(tmp_path: Path) -> None:
    """A pass by hand turns off only the `schedule.articles` cadence, so an agent's own `cooldown:` still holds."""
    cerebro, _, hands = started(tmp_path, front="heartbeat: { on: [change], cooldown: 3h }\n")
    once = cerebro.run_once()  # 10:00, as serve --once runs it
    assert once.compiled == [] and "12:00" in once.skipped[AGENT]
    hands.move(hours=2)
    assert cerebro.run_once().compiled == ["things/first"]
