"""A pass: what it lists, extracts and compiles, and what it holds back."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cerebro import Cerebro, Record
from cerebro.article import Identity
from cerebro.clock import Clock, stamp
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.records import proposals
from cerebro.run import AgentRun

NOTES = [Record(id=f"n{n}", source="notes", revision="1", at="2026-11-10T09:00:00Z", body=f"Note {n}.")
         for n in range(3)]


def workspace(tmp_path: Path, heartbeat: str = "[change]", extraction: FakeExtraction | None = None,
          notes: FakeConnector | None = None, clock: Clock | None = None, settings: str = "{}\n",
          run: AgentRun | None = None) -> Cerebro:
    (tmp_path / "agents" / "reports").mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings.yaml").write_text(settings)
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(
        f"---\nevidence:\n  - query: {{ from: facts, source: notes }}\nheartbeat: {heartbeat}\n---\n\n# Output\n\n## Summary\n")
    return Cerebro(tmp_path, store=MemoryStore(), extraction=extraction, clock=clock,
                   connectors=[notes or FakeConnector("notes", NOTES)],
                   agent_run=run or ScriptedRun(lambda prompt: cited(prompt), clock=clock))


def cited(prompt: str) -> dict[str, str]:
    """A summary that cites the first fact the prompt shows, as a run must for its line to land."""
    first = re.search(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])
    return {"Summary": f"x [^{first.group(1)}]"} if first else {"Summary": "x"}


class Hands:
    """A clock moved by hand."""

    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def move(self, **delta: float) -> None:
        self.at += timedelta(**delta)


def test_a_weekly_agent_runs_once_a_week_in_the_workspaces_zone_and_not_on_change(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 15, 20, tzinfo=UTC))  # Sunday in UTC, Monday 05:00 in Tokyo
    notes = FakeConnector("notes", NOTES)
    cerebro = workspace(tmp_path, "[weekly]", FakeExtraction(), notes, hands, "timezone: Asia/Tokyo\n")
    first = cerebro.run_once()
    assert first.compiled == ["reports/weekly"] and first.due["reports/weekly"] == "weekly, and it never ran"
    notes.add(Record(id="n7", source="notes", revision="1", at="2026-11-16T09:00:00Z", body="A new note."))
    hands.move(days=6)  # Sunday 05:00 in Tokyo: the same week
    waits = cerebro.run_once()
    assert waits.compiled == [] and waits.extracted == 1
    assert waits.skipped["reports/weekly"] == "heartbeat is weekly: it ran this period, and runs next at 2026-11-23 00:00"
    hands.move(hours=19)  # Monday 00:00 in Tokyo
    assert cerebro.run_once().compiled == ["reports/weekly"]


def test_an_agent_on_manual_alone_waits_for_a_person(tmp_path: Path) -> None:
    result = workspace(tmp_path, "[manual]", FakeExtraction()).run_once()
    assert result.compiled == [] and result.skipped["reports/weekly"] == "heartbeat is manual: run it by hand"


def test_new_evidence_waits_out_a_cooldown_and_the_pass_says_until_when(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    notes = FakeConnector("notes", NOTES)
    cerebro = workspace(tmp_path, "{ on: [change], cooldown: 3h }", FakeExtraction(), notes, hands)
    assert cerebro.run_once(scheduled=True).compiled == ["reports/weekly"]
    notes.add(Record(id="n7", source="notes", revision="1", at="2026-11-17T10:00:00Z", body="A new note."))
    hands.move(hours=1)
    held = cerebro.run_once(scheduled=True)
    assert held.compiled == [] and held.extracted == 1
    assert held.skipped["reports/weekly"] == "cooling down: new evidence waits until 2026-11-17 12:00, cooldown 3h"
    hands.move(hours=2)
    assert cerebro.run_once(scheduled=True).compiled == ["reports/weekly"]


def test_a_record_its_source_removed_has_its_facts_retracted_and_its_article_rewritten(tmp_path: Path) -> None:
    notes = FakeConnector("notes", NOTES)
    extraction = FakeExtraction()
    seen: list[str] = []
    cerebro = workspace(tmp_path, extraction=extraction, notes=notes, settings="schedule: { articles: 0 }\n",
                    run=ScriptedRun(lambda prompt: seen.append(prompt) or cited(prompt)))
    assert cerebro.run_once().compiled == ["reports/weekly"] and "Note 1." in seen[0]
    notes.remove("n1")
    result = cerebro.run_once()
    assert result.retracted == 1 and extraction.facts(record="n1") == []
    assert result.compiled == ["reports/weekly"] and "Note 1." not in seen[1]


def test_agents_run_side_by_side_and_one_failure_stops_no_other(tmp_path: Path) -> None:
    import threading

    from cerebro.agent import Agent

    for name in ("daily", "monthly"):
        (tmp_path / "agents" / "reports").mkdir(parents=True, exist_ok=True)
        (tmp_path / "agents" / "reports" / f"{name}.md").write_text(
                "---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n# Output\n\n"
                "## Summary\n")
    gate = threading.Barrier(3, timeout=2)

    class Wide(ScriptedRun):
        def run(self, agent: Agent, *args: object, **kwargs: object):  # type: ignore[override]
            gate.wait()  # all three at once, or the barrier breaks
            if agent.id == "reports/monthly":
                raise RuntimeError("the model went away")
            return super().run(agent, *args, **kwargs)  # type: ignore[arg-type]

    result = workspace(tmp_path, extraction=FakeExtraction(), run=Wide(lambda p: cited(p))).run_once()
    assert sorted(result.compiled) == ["reports/daily", "reports/weekly"]
    assert result.failed == {"reports/monthly": "RuntimeError: the model went away"}


def test_a_record_that_fails_is_reported_retried_and_holds_back_no_other(tmp_path: Path) -> None:
    class Flaky(FakeExtraction):
        broken = True

        def extract(self, record: Record) -> list:
            if record.id == "n1" and self.broken:
                raise ValueError("cannot read n1")
            return super().extract(record)

    extraction = Flaky()
    cerebro = workspace(tmp_path, extraction=extraction)
    first = cerebro.run_once()
    assert first.extracted == 2 and "cannot read n1" in first.failed["sources/notes/n1"]
    assert cerebro.run_once().failed == {"sources/notes/n1": "ValueError: cannot read n1"}
    extraction.broken = False
    third = cerebro.run_once()
    assert third.extracted == 1 and third.failed == {} and cerebro.store.state("failed:notes") == []


def test_with_no_facts_layer_a_source_keeps_its_cursor_for_the_day_one_is_plugged_in(tmp_path: Path) -> None:
    notes = FakeConnector("notes", NOTES)
    cerebro = workspace(tmp_path, extraction=None, notes=notes)
    assert "no facts layer" in cerebro.run_once().skipped["sources/notes"]
    cerebro.extraction = FakeExtraction()
    assert cerebro.store.state("cursor:notes") is None and cerebro.run_once().extracted == 3


def test_one_clock_reaches_the_window_the_run_and_the_ledger(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from cerebro.run import PydanticRun

    (tmp_path / "agents" / "reports").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("timezone: Asia/Tokyo\n")
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(
        "---\nevidence:\n  - query: { from: facts, window: last_7_days }\n---\n\n# Output\n\n## Summary\n")
    seen: list[str] = []
    at = datetime(2026, 11, 17, 20, 0, tzinfo=UTC)  # Wednesday in Tokyo, Tuesday in UTC
    note = Record(id="n", source="notes", revision="1", at="2026-11-11", body="A date-only note.")
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: at,
                      connectors=[FakeConnector("notes", [note])],
                      agent_run=PydanticRun.scripted(lambda p: seen.append(p) or cited(p)))
    cerebro.agent_run.clock, cerebro.agent_run.zone = cerebro.clock, ZoneInfo("Asia/Tokyo")
    assert cerebro.run_once().compiled == ["reports/weekly"]
    assert "Today is Wed Nov 18, 2026." in seen[0] and "A date-only note." in seen[0]
    assert cerebro.store.rows("spend")[0].data["at"] == "2026-11-17T20:00:00Z"


def test_extraction_is_held_to_its_own_cap_and_its_cost_is_kept(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    (tmp_path / "settings.yaml").write_text("budget:\n  extraction_daily_usd: 0.000000001\n")
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(),
                      connectors=[FakeConnector("notes", NOTES)])
    result = cerebro.run_once()
    rows = [row.data for row in cerebro.store.rows("spend")]
    assert result.extracted == 1 and len(rows) == 1 and rows[0]["layer"] == "extraction" and rows[0]["usd"] > 0
    assert result.failed == {}  # what the cap keeps from starting waits for the next pass, and is no failure
    assert result.stops[0].endswith("2 records wait.")
    assert cerebro.store.state("failed:notes") == ["n1", "n2"]


def test_a_workspace_past_its_compilation_cap_calls_no_model(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    called: list[str] = []
    cerebro = workspace(tmp_path, extraction=FakeExtraction())
    (tmp_path / "settings.yaml").write_text("budget:\n  compilation_daily_usd: 0.01\n")
    at = datetime(2026, 11, 17, 12, tzinfo=UTC)
    capped = Cerebro(tmp_path, store=cerebro.store, extraction=FakeExtraction(), clock=lambda: at,
                     connectors=[FakeConnector("notes", NOTES)],
                     agent_run=ScriptedRun(lambda prompt: called.append(prompt) or cited(prompt)))
    capped.store.append("spend", {"at": "2026-11-17T11:00:00Z", "day": "2026-11-17", "layer": "compilation",
                                  "usd": 1.0})
    result = capped.run_once()
    assert result.compiled == [] and called == [] and result.failed == {} and result.waiting_runs == ["reports/weekly"]


def test_a_run_held_for_room_under_the_cap_is_held_not_failed_and_runs_on_the_next_pass(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    called: list[str] = []
    cerebro = workspace(tmp_path, extraction=FakeExtraction())
    (tmp_path / "settings.yaml").write_text("budget:\n  compilation_daily_usd: 1\n")
    at = datetime(2026, 11, 17, 12, tzinfo=UTC)
    capped = Cerebro(tmp_path, store=cerebro.store, extraction=FakeExtraction(), clock=lambda: at,
                     connectors=[FakeConnector("notes", NOTES)],
                     agent_run=ScriptedRun(lambda prompt: called.append(prompt) or cited(prompt)))
    under_way = capped.ledger.reserve("compilation", at, capped.models["compilation"], 0, usd=0)
    capped.ledger._held[under_way] = ("compilation", "2026-11-17", 0.999)  # a run under way holds nearly all of it
    held = capped.run_once()
    assert called == [] and "reports/weekly" not in held.failed
    assert held.held["reports/weekly"].startswith("held for room under the cap, runs next pass")
    capped.ledger.release(under_way)  # the run under way ended
    assert capped.run_once().compiled == ["reports/weekly"] and len(called) == 1


def test_a_note_whose_header_names_an_article_links_its_facts(tmp_path: Path) -> None:
    from cerebro.article import Identity

    record = Record(id="n9", source="notes", revision="1", keys={"topic": ["Apollo"]}, body="Topic: Apollo\n\nIt moved.")
    cerebro = workspace(tmp_path, extraction=FakeExtraction(), notes=FakeConnector("notes", [record]))
    cerebro.store.create(Identity("projects/apollo", "projects", "Apollo Launch", {"topic": ["apollo"]}))
    assert cerebro.run_once().linked == 1 and cerebro.store.facts_of("projects/apollo")


def test_a_pass_reads_every_page_of_a_source(tmp_path: Path) -> None:
    cerebro = workspace(tmp_path, extraction=FakeExtraction(), notes=FakeConnector("notes", NOTES, page_size=1))
    result = cerebro.run_once()
    assert result.listed == {"notes": 3} and result.extracted == 3  # every page is extracted, not only the last


def test_an_agents_own_model_and_the_settings_zone_reach_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "agents" / "reports").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("timezone: Asia/Tokyo\n")
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(
        "---\nmodel: { provider: fireworks, model: glm-5p3-flash }\n---\n\n# Output\n\n## Summary\n")
    cerebro = Cerebro(tmp_path, store=MemoryStore())
    assert cerebro.model_of(cerebro.agents[0]).model == "glm-5p3-flash"
    assert str(cerebro.lookup.zone) == "Asia/Tokyo"


def test_a_curator_runs_before_extraction_and_only_what_it_admits_is_extracted(tmp_path: Path) -> None:
    (tmp_path / "agents" / "notes").mkdir(parents=True)
    (tmp_path / "agents" / "notes" / "curator.md").write_text("---\nauthority: { admit: true, archive: true, merge: true, split: true }\n---\nKeep notes.\n")
    notes = FakeConnector("notes", [Record(id=f"n{n}", source="notes", revision="1", at="2026-11-10T09:00:00Z",
                                           body=f"Note {n}.", keys={"topic": [topic]}, into="notes")
                                    for n, topic in enumerate(["alpha", "beta"])])
    admits = ScriptedRun(lambda prompt: cited(prompt),
                         calls=[("admit", {"name": "Alpha", "keys": {"topic": ["alpha"]}, "reason": "asked for"})])
    cerebro = workspace(tmp_path, extraction=FakeExtraction(), notes=notes, run=admits)
    result = cerebro.run_once()
    assert result.due["notes/curator"] == "change" and "notes/curator" not in result.failed
    assert cerebro.store.article("notes/alpha").identity.keys == {"topic": ["alpha"]}
    assert result.extracted == 1 and cerebro.store.state("failed:notes") == []


def test_the_loop_behind_serve_runs_pass_after_pass_and_a_pass_that_raises_does_not_end_it(tmp_path: Path) -> None:
    from cerebro.runner import PassResult, run_forever

    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    cerebro = workspace(tmp_path, extraction=FakeExtraction(), clock=hands)
    sources, slept, passes, errors = cerebro.connectors, [], [], []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        cerebro.connectors = None if len(slept) == 1 else sources  # type: ignore[assignment]  # the second pass raises
        hands.move(seconds=seconds)

    def keep(result: PassResult) -> None:
        passes.append(result)

    count = run_forever(cerebro, interval=60, stop=lambda: len(passes) == 2, sleep=sleep, on_pass=keep,
                        on_error=errors.append)
    assert count == 3 and slept == [60, 60] and len(errors) == 1
    assert passes[0].compiled == ["reports/weekly"] and passes[1].compiled == []


def test_a_cursor_that_does_not_read_is_cleared_with_a_notice_and_the_next_pass_lists_from_the_start(
        tmp_path: Path) -> None:
    cerebro = workspace(tmp_path, extraction=FakeExtraction())
    cerebro.store.set_state("cursor:notes", "999")
    first = cerebro.run_once()
    assert "sources/notes" not in first.failed and cerebro.store.state("cursor:notes") is None
    assert any(line.startswith("sources/notes: its cursor did not read") for line in first.notices)
    assert cerebro.run_once().listed == {"notes": 3}


def test_a_held_record_is_extracted_once_its_article_is_admitted(tmp_path: Path) -> None:
    from cerebro.article import Identity

    (tmp_path / "agents" / "notes").mkdir(parents=True)
    (tmp_path / "agents" / "notes" / "curator.md").write_text("---\nauthority: { admit: true, archive: true, merge: true, split: true }\n---\nKeep notes.\n")
    notes = FakeConnector("notes", [Record(id="n1", source="notes", revision="1", at="2026-11-10T09:00:00Z",
                                           body="Beta shipped.", keys={"topic": ["beta"]}, into="notes")])
    cerebro = workspace(tmp_path, extraction=FakeExtraction(), notes=notes)
    assert cerebro.run_once().extracted == 0 and [r["id"] for r in cerebro.store.state("held:notes")] == ["n1"]
    cerebro.store.create(Identity("notes/beta", "notes", "Beta", {"topic": ["beta"]}))
    again = cerebro.run_once()
    assert again.listed == {"notes": 0} and again.extracted == 1 and cerebro.store.state("held:notes") == []


def test_a_tell_whose_extraction_failed_is_kept_and_extracted_by_a_later_pass(tmp_path: Path) -> None:
    from cerebro import Caller
    from cerebro.article import Identity
    from cerebro.budget import BudgetSpent

    class CapOnce(FakeExtraction):
        stopped = False

        def extract(self, record: Record) -> Any:
            if record.source == "tell" and not self.stopped:
                self.stopped = True
                raise BudgetSpent("extraction spent its cap")
            return super().extract(record)

    cerebro = workspace(tmp_path, extraction=CapOnce(), notes=FakeConnector("notes", []))
    cerebro.store.create(Identity("reports/alder", "reports", "Alder"))
    told = cerebro.tell(Caller("you", "person"), "The renewal moved to March.", "reports/alder", as_="fact")
    assert told.facts == [] and "a later pass extracts them" in told.notes[0]
    assert cerebro.store.record(told.record) is not None
    assert cerebro.run_once().extracted == 1 and len(cerebro.store.facts_of("reports/alder")) == 1
    assert cerebro.store.state("cerebro:failed:tell") == []


def test_an_agent_another_pass_holds_waits_and_a_lapsed_lease_lets_it_run(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    cerebro = workspace(tmp_path, extraction=FakeExtraction(), clock=hands)
    cerebro.store.set_state("lease:reports/weekly", {"by": "another", "until": "2026-11-17T09:30:00+00:00"})
    held = cerebro.run_once()
    assert held.compiled == [] and "another process runs it now" in held.skipped["reports/weekly"]
    hands.move(minutes=31)
    assert cerebro.run_once().compiled == ["reports/weekly"]
    assert cerebro.store.state("lease:reports/weekly") is None


def curated_notes(tmp_path: Path, records: list[Record], **options: Any) -> Cerebro:
    """A workspace whose notes curator keeps the notes collection, so a record bound to no article of it is held."""
    (tmp_path / "agents" / "notes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents" / "notes" / "curator.md").write_text("---\nauthority: { admit: true, archive: true, merge: true, split: true }\n---\nKeep notes.\n")
    return workspace(tmp_path, notes=FakeConnector("notes", records), **options)


def note(id: str, topic: str, body: str = "A note.", **fields: Any) -> Record:
    return Record(id=id, source="notes", revision="1", at="2026-11-10T09:00:00Z", body=body,
                  keys={"topic": [topic]}, into="notes", fields=dict(fields))


def test_a_record_held_ahead_of_a_bound_one_holds_back_nothing_of_its_source(tmp_path: Path) -> None:
    from cerebro.article import Identity

    cerebro = curated_notes(tmp_path, [note("n0", "gamma"), note("n1", "beta")], extraction=FakeExtraction())
    cerebro.store.create(Identity("notes/beta", "notes", "Beta", {"topic": ["beta"]}))
    assert cerebro.run_once().extracted == 1 and [f.record for f in cerebro.extraction.facts()] == ["n1"]


def test_a_daily_agent_runs_once_each_local_day(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 14, tzinfo=UTC))  # 23:00 in Tokyo
    cerebro = workspace(tmp_path, "[daily]", FakeExtraction(), clock=hands, settings="timezone: Asia/Tokyo\n")
    assert cerebro.run_once().due["reports/weekly"].startswith("daily")
    hands.move(minutes=30)
    assert "reports/weekly" not in cerebro.run_once().due  # the same local day
    hands.move(hours=1, minutes=30)  # 01:00 the next day in Tokyo
    assert cerebro.run_once().due["reports/weekly"].startswith("daily")


def test_a_scheduled_compile_runs_with_nothing_new(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 16, 9, tzinfo=UTC))  # a Monday
    cerebro = workspace(tmp_path, "[weekly]", FakeExtraction(), clock=hands)
    assert cerebro.run_once().compiled == ["reports/weekly"]
    hands.move(days=7)
    assert cerebro.run_once().compiled == ["reports/weekly"]


def test_a_scheduled_run_that_fails_gets_one_more_try_in_its_period(tmp_path: Path) -> None:
    from cerebro.run import Run

    class Fails(AgentRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Run:
            return Run("run_f", agent.id, agent.target, "failed", note="the provider refused it")

    hands = Hands(datetime(2026, 11, 16, 9, tzinfo=UTC))
    cerebro = workspace(tmp_path, "[weekly]", FakeExtraction(), clock=hands, run=Fails())
    for tried in (True, True, False):
        assert ("reports/weekly" in cerebro.run_once().failed) is tried
        hands.move(hours=1)


def test_a_curator_whose_run_failed_waits_as_a_failed_writer_does_then_runs_again(tmp_path: Path) -> None:
    from cerebro.run import Run

    class Fails(AgentRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Run:
            return Run("run_f", agent.id, agent.target, "failed", note="the provider refused it")

    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    cerebro = curated_notes(tmp_path, [], extraction=FakeExtraction(), clock=hands, run=Fails())
    assert [r.agent for r in cerebro.run_once().runs if r.agent == "notes/curator"] == ["notes/curator"]
    waited = cerebro.run_once()
    assert not [r for r in waited.runs if r.agent == "notes/curator"]
    assert waited.skipped["notes/curator"] == ("its run failed 1 time, so it waits until 2026-11-17T09:00:30Z before it "
                                               "runs again, or until its file, workspace.md or settings.yaml changes")
    hands.move(seconds=30)
    assert [r.agent for r in cerebro.run_once().runs if r.agent == "notes/curator"] == ["notes/curator"]
    assert "waits until 2026-11-17T09:01:30Z" in cerebro.run_once().skipped["notes/curator"]  # 60 s after the second


def test_a_pass_with_concurrency_left_null_runs_at_the_default_width(tmp_path: Path) -> None:
    from cerebro.fakes import FakeDecider

    cerebro = workspace(tmp_path, extraction=FakeExtraction(), settings="compile: { concurrency: null }\n")
    cerebro.decider = FakeDecider()
    result = cerebro.run_once()
    assert result.extracted == len(NOTES) and not result.failed


def test_a_near_match_is_judged_through_the_pass_held_to_the_extraction_cap_and_booked_there(tmp_path: Path) -> None:
    from cerebro.article import Identity
    from cerebro.fakes import FakeDecider

    def judged(cap: str) -> tuple[Cerebro, FakeDecider, Any]:
        root = tmp_path / cap
        root.mkdir()
        decider = FakeDecider()
        records = [Record(id="n1", source="notes", revision="1", at="2026-11-10T09:00:00Z", body="It renewed.",
                          fields={"subject": "the Alder deal"})]
        cerebro = workspace(root, extraction=FakeExtraction(), notes=FakeConnector("notes", records),
                        settings=f"budget: {{ extraction_daily_usd: {cap} }}\n")
        cerebro.decider = decider
        cerebro.store.create(Identity("things/alder", "things", "Alder"))
        return cerebro, decider, cerebro.run_once()

    def judge_asked(decider: FakeDecider) -> list[Any]:
        return [state for state, _ in decider.asked if "candidates" in state]

    cerebro, decider, result = judged("100")
    assert judge_asked(decider) and result.linked == 1 and cerebro.store.facts_of("things/alder")
    assert {row.data["layer"] for row in cerebro.store.rows("spend") if row.data["run"].startswith("fact_")} == {
        "extraction"}
    cerebro, decider, result = judged("0.0000001")  # the extraction spends it, so the judge is not asked
    assert result.extracted == 1 and judge_asked(decider) == [] and cerebro.store.facts_of("things/alder") == []


def test_facts_kept_before_their_article_is_born_link_to_it_and_its_first_run_reads_them(tmp_path: Path) -> None:
    (tmp_path / "agents" / "projects").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "projects" / "launch.md").write_text(
        "---\nevidence:\n  - facts: { window: last_90_days }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    note = Record(id="local/sync.md", source="local", revision="1", at="2026-11-12", keys={"project": ["Launch"]},
                  body="Priya Raman moved the launch.")
    other = Record(id="local/other.md", source="local", revision="1", at="2026-11-12", keys={"project": ["Orion"]},
                   body="Orion hired a designer.")
    seen: list[str] = []
    hands = Hands(datetime(2026, 11, 18, 9, tzinfo=UTC))
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands,
                      connectors=[FakeConnector("local", [note, other])],
                      agent_run=ScriptedRun(lambda prompt: seen.append(prompt) or cited(prompt), clock=hands))
    first = cerebro.run_once()
    assert first.compiled == ["projects/launch"] and "Priya Raman moved the launch." in seen[0]
    launch = [f.id for f in cerebro.extraction.facts(record=note.id)]
    assert cerebro.store.facts_of("projects/launch") == launch
    hands.move(hours=1)
    assert cerebro.run_once().compiled == [] and len(seen) == 1  # nothing moved, so nothing runs
    # An article born later, here by hand, gets the facts that name it on the next pass, its record unchanged.
    cerebro.store.create(Identity(id="projects/orion", collection="projects", title="Orion"))
    cerebro.run_once()
    assert cerebro.store.facts_of("projects/orion") == [f.id for f in cerebro.extraction.facts(record=other.id)]


def test_a_fact_a_run_read_through_its_query_tool_may_be_cited_and_moves_its_digest(tmp_path: Path) -> None:
    (tmp_path / "agents" / "reports").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "reports" / "weekly.md").write_text("---\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    facts = FakeExtraction()

    def write(prompt: str) -> dict[str, str]:  # the model cites what its tool returned; its evidence holds nothing
        held = facts.facts(source="notes")
        return {"Summary": f"{held[0].text} [^{held[0].id}]"} if held else {}

    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=facts, connectors=[FakeConnector("notes", NOTES[:1])],
                      agent_run=ScriptedRun(write, calls=[("query", {"source": "facts"})]))
    first = cerebro.run_once()
    assert first.compiled == ["reports/weekly"] and first.warnings == []
    compilation = cerebro.store.article("reports/weekly").compilations[0]
    assert [c.id for c in compilation.citations] == [f.id for f in facts.facts()]
    assert cerebro.run_once().runs == []  # what it read by tool has not moved
    facts.retract(facts.facts()[0].id)
    again = cerebro.run_once()
    assert len(again.runs) == 1 and again.skipped == {}  # a fact it read by tool moved, so it runs again


def test_an_article_read_right_after_its_compile_counts_no_fact_as_new_and_a_later_one_counts(tmp_path: Path) -> None:
    from cerebro.caller import Caller

    hands = Hands(datetime(2026, 11, 18, 9, tzinfo=UTC))  # one instant for the whole pass, as a fast pass has
    named = [replace(note, fields={"subject": "Weekly"}) for note in NOTES[:2]]  # facts about the article link to it
    cerebro = workspace(tmp_path, extraction=FakeExtraction(), notes=FakeConnector("notes", named), clock=hands,
                    run=ScriptedRun(cited, clock=hands))
    cerebro.run_once()  # the article is born, its facts link to it, then it is written from them, all in one pass
    you = Caller("you", "person")
    assert len(cerebro.store.facts_of("reports/weekly")) == 2
    assert cerebro.lookup.read(you, ["reports/weekly"]).facts_since_compiled == {"reports/weekly": 0}
    first = cerebro.extraction.facts()[0].id
    cerebro.store.unlink(first, ["reports/weekly"])
    cerebro.store.link(first, ["reports/weekly"], "2026-11-18T10:00:00Z")  # learned an hour after the article landed
    assert cerebro.lookup.read(you, ["reports/weekly"]).facts_since_compiled == {"reports/weekly": 1}


def test_a_source_that_fails_before_it_lists_anything_is_a_failure_and_not_a_listing(tmp_path: Path) -> None:
    class Down(FakeConnector):
        def list(self, since: str | None = None) -> Any:
            raise OSError("the source is down")

    result = workspace(tmp_path, extraction=FakeExtraction(), notes=Down("notes", [])).run_once()
    assert "notes" not in result.listed and result.failed["sources/notes"] == "OSError: the source is down"


def test_a_pass_says_when_an_agent_filters_facts_on_a_kind_no_fact_carries(tmp_path: Path) -> None:
    workspace(tmp_path)  # lays the workspace out
    for name, line in (("risks", "query: { from: facts, classes: risk_objection }"),
                       ("streams", "facts: { classes: [goal, note] }")):
        (tmp_path / "agents" / "reports" / f"{name}.md").write_text(
            f"---\nevidence:\n  - {line}\nheartbeat: [manual]\n---\n\n# Output\n\n## Risks\n")
    cerebro = workspace(tmp_path, extraction=FakeExtraction())
    assert cerebro.refused == []
    cerebro.run_once()  # the first pass makes the facts, each of the stand-in's one kind
    notices = [line for line in cerebro.run_once().notices if "filters facts" in line]
    said = ", a kind no fact here carries (note), so that line reads nothing: drop the filter, or give the facts layer that kind"
    assert sorted(notices) == [f"agents/reports/risks.md filters facts on risk_objection{said}",
                               f"agents/reports/streams.md filters facts on goal{said}"]


def test_a_left_line_says_on_the_next_run_what_left_since_the_last_write_and_a_script_prints_into_it_masked(
        tmp_path: Path) -> None:
    from cerebro.article import Property
    from cerebro.caller import Caller

    (tmp_path / "agents" / "reports").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "usage.sh"
    script.write_text("#!/bin/sh\necho \"seats in use: 40, key sekret-value-0001\"\n")
    script.chmod(0o755)
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(
        "---\nevidence:\n  - query: { from: projects, stage: open }\n  - left: { from: projects, stage: open }\n"
        "  - script: { run: scripts/usage.sh }\nheartbeat: { on: [change], cooldown: 1m }\n---\n\n# Output\n\n"
        "## Summary\n")
    seen: list[str] = []
    hands = Hands(datetime(2026, 11, 18, 9, tzinfo=UTC))
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), connectors=[], clock=hands,
                      agent_run=ScriptedRun(lambda prompt: seen.append(prompt) or {"Summary": "Open [^projects/apollo]."}, clock=hands))
    cerebro.mask.add("sekret-value-0001")
    you = Caller("you", "person")
    for id in ("projects/apollo", "projects/vega"):
        cerebro.store.create(Identity(id=id, collection="projects", title=id.split("/")[1].title()))
        cerebro.store.set_field(id, "stage", Property("open", you, "2026-11-01T09:00:00Z"))
    first = cerebro.run_once()
    assert first.compiled == ["reports/weekly"], (first.failed, first.warnings)
    assert "seats in use: 40" in seen[0] and "sekret-value-0001" not in seen[0]  # its output, masked
    assert "Why it left" not in seen[0]
    cerebro.store.set_field("projects/vega", "stage", Property("closed", you, "2026-11-02T09:00:00Z"))
    hands.move(minutes=2)
    cerebro.run_once()
    assert len(seen) == 2 and "Why it left: stage is now closed" in seen[1]


class Curators(AgentRun):
    """Two curators' runs: the zones curator admits Zone A after a short wait, and the alerts curator admits an
    alert for each zone its evidence holds, so what it admits depends on whether the zones curator ran first."""

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        import time

        if agent.id == "zones/curator":
            time.sleep(0.02)  # long enough that a run side by side starts before this one admits
            calls = [("admit", {"name": "Zone A", "reason": "a zone"})]
        else:
            seen = ScriptedRun(lambda prompt: {}).run(agent, evidence, [], budget).transcript[0].text
            calls = [("admit", {"name": f"Alert {zone}", "reason": "a zone to watch"})
                     for zone in sorted(set(re.findall(r"zones/(zone-\w+)", seen)))]
        return ScriptedRun(lambda prompt: {}, calls=calls).run(agent, evidence, tools, budget)


def test_curators_run_one_at_a_time_after_what_they_read_so_a_pass_admits_the_same_every_time(tmp_path: Path) -> None:
    held = []
    for n in range(5):
        root = tmp_path / str(n)
        for collection, evidence in (("alerts", "evidence:\n  - query: { from: zones }\n"), ("zones", "")):
            (root / "agents" / collection).mkdir(parents=True)
            (root / "agents" / collection / "curator.md").write_text(f"---\nauthority: {{ admit: true, archive: true, merge: true, split: true }}\n{evidence}---\nKeep.\n")
        (root / "settings.yaml").write_text("compile: { concurrency: 8 }\n")  # the thread pool on, as wide as it goes
        cerebro = Cerebro(root, store=MemoryStore(), agent_run=Curators())
        ran = [run.agent for run in cerebro.run_once().runs]
        assert ran == ["zones/curator", "alerts/curator"]  # the one that reads zones runs after its curator
        held.append((sorted(a.identity.id for c in cerebro.store.collections() for a in cerebro.store.query(c)),
                     sorted(p.target for p in proposals(cerebro.store) if p.status == "waiting")))
    assert held == [(["zones/zone-a"], ["alerts/alert-zone-a"])] * 5  # zones left its first run: alerts is late


def test_a_tell_that_fails_while_a_pass_retries_the_told_list_stays_on_it(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from cerebro import runner
    from cerebro.records import RETRY
    from cerebro.store import SQLiteStore

    store = SQLiteStore()
    store.keep(Record(id="r_old", source="tell", revision="1", body="x"))
    store.set_state(RETRY, ["r_old"])

    def extract(store_: Any, extraction: Any, record: Record, at: str, **options: Any) -> list[str]:
        store.set_state(RETRY, [*(store.state(RETRY) or []), "r_new"])  # a tell over MCP failed meanwhile
        return ["f1"]

    monkeypatch.setattr(runner, "extract_told", extract)
    workspace = SimpleNamespace(store=store, lookup=SimpleNamespace(extraction=object()),
                            clock=lambda: datetime(2026, 11, 18, tzinfo=UTC))
    runner._retry_told(workspace, runner.PassResult())
    assert store.state(RETRY) == ["r_new"]


def test_a_lease_is_renewed_while_its_run_goes_on_and_given_back_when_it_ends(monkeypatch: Any) -> None:
    import threading
    from types import SimpleNamespace

    import pytest

    from cerebro import runner
    from cerebro.store import SQLiteStore

    now = [datetime(2026, 11, 18, 9, tzinfo=UTC)]
    workspace = SimpleNamespace(store=SQLiteStore(), clock=lambda: now[0])
    agent = SimpleNamespace(id="things/article")
    renewed = threading.Event()
    real = runner._renew
    monkeypatch.setattr(runner, "RENEW", 0.01)
    monkeypatch.setattr(runner, "_renew", lambda *a: (real(*a), renewed.set()))
    with runner.hold(workspace, agent):
        now[0] += timedelta(seconds=runner.LEASE + 1)  # the run goes on past its first lease
        renewed.clear()
        assert renewed.wait(2)
        with pytest.raises(runner.LeaseHeld, match="things/article is running in another process"), \
                runner.hold(workspace, agent):
            pass
    with runner.hold(workspace, agent):  # given back once the run ended
        pass


def test_a_pass_leaves_the_sources_to_a_pass_in_another_process_that_lists_them(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 18, 9, tzinfo=UTC))
    extraction = FakeExtraction()
    cerebro = workspace(tmp_path, "[manual]", extraction, clock=hands)
    cerebro.store.set_state("lease:#sources", {"by": "another", "until": "2026-11-18T09:30:00Z"})
    held = cerebro.run_once()
    assert held.listed == {} and extraction.facts() == [] and "lapse at 2026-11-18T09:30:00Z" in held.skipped["sources"]
    hands.move(minutes=31)
    assert cerebro.run_once().listed == {"notes": 3}


def test_a_source_named_kept_keeps_its_own_state_and_one_named_tell_is_refused(tmp_path: Path) -> None:
    import pytest

    from cerebro.fakes import notes_app
    from cerebro.project import WorkspaceError

    (tmp_path / "settings.yaml").write_text("{}\n")
    kept = [Record(id=f"k{n}", source="kept", revision="1", body=f"Note {n}.") for n in range(2)]
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), apps=[notes_app()],
                      connectors=[FakeConnector("kept", kept)], agent_run=ScriptedRun(lambda prompt: {}))
    assert cerebro.run_once().failed == {} and cerebro.store.state("cursor:kept") is not None
    assert cerebro.run_once().failed == {}
    with pytest.raises(WorkspaceError, match="a connector is named tell, the source Cerebro keeps what people tell"):
        Cerebro(tmp_path, store=MemoryStore(), connectors=[FakeConnector("tell", [])])
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "tell.yaml").write_text("kind: files\nselect:\n  - glob: \"t/*.md\"\n")
    with Cerebro(tmp_path, store=MemoryStore()) as loaded:
        assert "tell" not in loaded.connectors
        assert "sources/tell.yaml: the name tell is the source Cerebro keeps what people tell it under" in \
            loaded.refused[0]


def test_an_agent_whose_script_prints_something_new_each_time_waits_out_the_workspace_cooldown(tmp_path: Path) -> None:
    (tmp_path / "agents" / "reports").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "now.sh"
    script.write_text("#!/bin/sh\ndate +%s%N\n")
    script.chmod(0o755)
    (tmp_path / "agents" / "reports" / "clock.md").write_text(
        "---\nevidence:\n  - script: { run: scripts/now.sh }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    hands = Hands(datetime(2026, 11, 18, 9, tzinfo=UTC))
    runs: list[str] = []
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands,
                      agent_run=ScriptedRun(lambda prompt: runs.append(prompt) or {"Summary": "x [^script:scripts/now.sh:1]"}, clock=hands))
    assert cerebro.agent("reports/clock").cooldown is None  # the workspace's schedule.articles, 4h, covers it
    assert not any("cooldown" in line for line in cerebro.notices)
    for _ in range(3):
        cerebro.run_once(scheduled=True)
        hands.move(seconds=30)
    assert len(runs) == 1
    hands.move(hours=4)
    cerebro.run_once(scheduled=True)
    assert len(runs) == 2


@pytest.mark.parametrize("said", ["admitted by hand", "instructed"])
def test_a_thing_a_person_admitted_or_instructed_stays_when_its_source_removes_its_record(tmp_path: Path,
                                                                                         said: str) -> None:
    from cerebro.caller import Caller

    (tmp_path / "settings.yaml").write_text("{}\n")
    part = Record(id="P1", source="parts", revision="1", at="2026-11-10T09:00:00Z", body="Alder part.",
                  keys={"part_id": ["P1"]}, into="things")
    source = FakeConnector("parts", [part])
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), connectors=[source],
                      agent_run=ScriptedRun(lambda prompt: {}))
    person = Caller("you", "person")
    if said == "admitted by hand":
        cerebro.store.create(Identity("things/alder", "things", "Alder", {"part_id": ["P1"]}, "active", person,
                                      "2026-11-10T09:00:00Z"))
    else:
        cerebro.store.create(Identity("things/alder", "things", "Alder", {"part_id": ["P1"]}))
    cerebro.run_once()
    if said == "instructed":
        cerebro.tell(person, "Keep this one short.", target="things/alder", kind="instruct")
    source.remove("P1")
    result = cerebro.run_once()
    assert cerebro.store.article("things/alder").identity.status == "active"
    assert cerebro.store.article("things/alder").identity.in_source is False
    assert any("sources/parts removed P1" in line and "it stays" in line for line in result.notices), result.notices


def test_the_state_of_a_source_or_an_agent_that_went_is_let_go(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 18, 9, tzinfo=UTC))
    cerebro = workspace(tmp_path, "[change]", FakeExtraction(), clock=hands)
    cerebro.run_once()
    cerebro.store.set_state("held:notes", [{"id": "x"}])
    cerebro.store.set_state("material:reports/weekly", {"reports/subagents/x": {"text": "returned"}})
    assert cerebro.store.state("heartbeat:reports/weekly") is not None
    (tmp_path / "agents" / "reports" / "weekly.md").unlink()
    gone = Cerebro(tmp_path, store=cerebro.store, extraction=FakeExtraction(), clock=hands,
                   agent_run=ScriptedRun(lambda prompt: {}))
    gone.run_once()
    assert cerebro.store.state("heartbeat:reports/weekly") is None and cerebro.store.state("held:notes") is None
    assert cerebro.store.state("material:reports/weekly") is None  # what its subagents returned goes with it


class Counting(FakeConnector):
    """A stand-in that counts each listing, as each would be a request to a real source."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.asked = 0

    def list(self, since: str | None = None) -> Any:
        self.asked += 1
        return super().list(since)


def test_under_serve_a_source_is_checked_on_its_freshness_window_and_a_quiet_pass_costs_nothing(
        tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 18, 9, tzinfo=UTC))
    notes = Counting("notes", NOTES)
    runs: list[str] = []
    cerebro = workspace(tmp_path, "[change]", FakeExtraction(), notes, hands,
                    run=ScriptedRun(lambda prompt: runs.append(prompt) or cited(prompt), clock=hands))
    first = cerebro.run_once(scheduled=True)  # never checked: due
    assert notes.asked == 1 and first.listed == {"notes": 3} and len(runs) == 1
    check = cerebro.checks()["notes"]
    assert check == {"at": "2026-11-18T09:00:00Z", "finished": True, "next": "2026-11-18T09:15:00Z"}
    hands.move(seconds=30)
    quiet = cerebro.run_once(scheduled=True)  # the agents' ping: no source due, nothing moved
    assert notes.asked == 1 and quiet.listed == {} and len(runs) == 1 and quiet.runs == []
    assert cerebro.run_once().listed == {"notes": 0} and notes.asked == 2  # serve --once checks now
    hands.move(minutes=15)
    cerebro.run_once(scheduled=True)
    assert notes.asked == 3


def test_under_serve_a_file_seen_first_is_read_at_once_and_a_change_waits_until_seen_unchanged_for_the_settle_time(
        tmp_path: Path) -> None:
    import os

    hands = Hands(datetime(2026, 11, 18, 9, tzinfo=UTC))
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "local.yaml").write_text("kind: files\nwindow: { behind: all }\nselect:\n"
                                                     "  - glob: \"local/*.md\"\n")
    (tmp_path / "local").mkdir()
    note = tmp_path / "local" / "n.md"
    note.write_text("Harbor moved.\n")
    os.utime(note, (0, 0))  # what the disk says of when it was saved is never read as a time
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands,
                      agent_run=ScriptedRun(lambda prompt: {}))
    assert cerebro.run_once(scheduled=True).listed == {"local": 1}  # first seen: read at once
    note.write_text("Harbor moved again.\n")  # a change Cerebro sees happen
    hands.move(minutes=16)
    at = hands.at
    assert cerebro.run_once(scheduled=True).listed == {"local": 0}  # it waits to be seen unchanged
    assert cerebro.checks()["local"]["next"] == stamp(at + timedelta(minutes=2))  # once it has settled
    (tmp_path / "local" / "new.md").write_text("A new note.\n")
    hands.move(seconds=95)
    assert cerebro.run_once(scheduled=True).listed == {}  # not due until the change has settled
    hands.move(seconds=30)
    assert cerebro.run_once(scheduled=True).listed == {"local": 2}  # the settled change, and the new file at once
    note.write_text("Harbor moved a third time.\n")
    assert cerebro.run_once().listed == {"local": 1}  # a pass outside serve reads at once, as serve --once does


def test_a_kept_record_whose_extraction_failed_is_extracted_on_the_next_pass(tmp_path: Path) -> None:
    from cerebro.caller import Caller
    from cerebro.fakes import notes_app

    (tmp_path / "settings.yaml").write_text("{}\n")
    tries: list[str] = []

    class FailsOnce(FakeExtraction):
        def extract(self, record: Record) -> Any:
            tries.append(record.id)
            if len(tries) == 1:
                raise RuntimeError("the model was down")
            return super().extract(record)

    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FailsOnce(), apps=[notes_app()],
                      agent_run=ScriptedRun(lambda prompt: {}))
    kept = cerebro.handle.keep(Caller("you", "person"), "note", "Harbor moved.", {"collection": "notes"})
    assert cerebro.run_once().failed
    second = cerebro.run_once()
    assert second.failed == {} and tries == [kept.id, kept.id] and second.extracted > 0


def kept_notes(tmp_path: Path, extraction: FakeExtraction, clock: Clock | None = None) -> Cerebro:
    from cerebro.fakes import notes_app

    (tmp_path / "settings.yaml").write_text("{}\n")
    return Cerebro(tmp_path, store=MemoryStore(), extraction=extraction, apps=[notes_app()], clock=clock,
                   agent_run=ScriptedRun(lambda prompt: {}))


@pytest.mark.parametrize("taker", ["person", "app", "agent"])
def test_a_later_revision_of_a_retracted_kept_record_is_read_unless_a_caller_retracted_one_no_caller_made(
        tmp_path: Path, taker: str) -> None:
    from cerebro.caller import Caller

    you, app, sent = Caller("you", "person"), Caller("notes", "app"), []
    takes = {"person": you, "app": app, "agent": Caller("things/curator", "agent")}[taker]

    class Reading(FakeExtraction):
        def extract(self, record: Record) -> Any:
            sent.append(record.body)
            return super().extract(record)

    extraction, hands = Reading(), Hands(datetime(2026, 11, 20, 9, tzinfo=UTC))
    cerebro = kept_notes(tmp_path, extraction, hands)
    kept = cerebro.handle.keep(you, "note", "The east gate opens at nine.", {"collection": "notes"})
    own = cerebro.handle.keep(app, "note", "The west gate opens at nine.", {"collection": "notes"})
    assert kept.id in cerebro.retract(you, kept.id, "reworded").withdrew
    assert own.id in cerebro.retract(takes, own.id, "wrong").withdrew
    cerebro.run_once()
    hands.move(minutes=5)
    cerebro.handle.keep(you, "note", "The east gate opens at ten.", {"collection": "notes"}, id=kept.id)
    assert kept.id in cerebro.retract(you, kept.id, "reworded again").withdrew  # before a pass reads that revision
    cerebro.run_once()
    hands.move(minutes=5)
    cerebro.handle.keep(you, "note", "The east gate opens at noon.", {"collection": "notes"}, id=kept.id)
    cerebro.handle.keep(app, "note", "The west gate opens at noon.", {"collection": "notes"}, id=own.id)
    cerebro.run_once()
    assert sent == ["The east gate opens at noon.", *(["The west gate opens at noon."] if taker != "person" else [])]
    assert [f.text for f in extraction.facts(record=kept.id)] == ["The east gate opens at noon."]


def test_a_callers_retract_after_an_apps_own_is_filed_and_holds_back_the_apps_later_saves(tmp_path: Path) -> None:
    from cerebro.caller import Caller

    you, app, hands = Caller("you", "person"), Caller("notes", "app"), Hands(datetime(2026, 11, 20, 9, tzinfo=UTC))
    cerebro = kept_notes(tmp_path, FakeExtraction(), hands)
    kept = cerebro.handle.keep(app, "note", "The gate opens at nine.", {"collection": "notes"})
    cerebro.run_once()
    cerebro.retract(app, kept.id, "its own")
    hands.move(seconds=5)
    assert kept.id in cerebro.retract(you, kept.id, "gone for good").withdrew
    hands.move(seconds=5)
    cerebro.handle.keep(app, "note", "The gate opens at ten.", {"collection": "notes"}, id=kept.id)
    assert cerebro.run_once().read == 0


def test_a_retract_that_lands_while_a_pass_reads_its_kept_record_leaves_nothing_of_the_read(tmp_path: Path) -> None:
    from cerebro.caller import Caller

    you, held = Caller("you", "person"), {}

    class Racing(FakeExtraction):
        def extract(self, record: Record) -> Any:
            if not held:
                held["retract"] = cerebro.retract(you, record.id, "wrong")
            return super().extract(record)

    extraction = Racing(names=["Alder"])
    cerebro = kept_notes(tmp_path, extraction)
    cerebro.store.create(Identity("things/alder", "things", "Alder"))
    kept = cerebro.handle.keep(you, "note", "Alder opens at eight.", {"collection": "notes"})
    passed = cerebro.run_once()
    assert (passed.read, passed.extracted, passed.retracted, passed.linked) == (0, 0, 1, 0)
    assert f"note/{kept.id}: record {kept.id} was retracted, so the facts read from it are withdrawn" in passed.notices
    assert extraction.facts(record=kept.id) == []


def test_a_pass_withdraws_what_an_earlier_pass_saved_of_a_retracted_record_and_stopped_before_it_checked(
        tmp_path: Path) -> None:
    from cerebro.caller import Caller

    you, held = Caller("you", "person"), {}

    class Stopping(FakeExtraction):
        def extract(self, record: Record) -> Any:
            if held:
                return super().extract(record)
            held["retract"] = cerebro.retract(you, record.id, "wrong")  # it finds no fact to withdraw yet
            super().extract(record)  # the facts layer saves them
            raise RuntimeError("the store is busy")  # and the pass stops before it checks for a retract

    extraction = Stopping()
    cerebro = kept_notes(tmp_path, extraction)
    kept = cerebro.handle.keep(you, "note", "The south gate opens at six.", {"collection": "notes"})
    cerebro.run_once()
    assert cerebro.run_once().retracted == 1 and extraction.facts(record=kept.id) == []


@pytest.mark.parametrize("path", ["retract", "tell", "decide"])
def test_a_pass_finishes_a_retract_whose_take_back_stopped_partway(tmp_path: Path, path: str) -> None:
    from cerebro.caller import Caller
    from cerebro.errors import CerebroError

    you, app, failing = Caller("you", "person"), Caller("notes", "app"), [1, 1]  # the take-back, then the first finish

    class Failing(FakeExtraction):
        def retract(self, fact_id: str) -> None:
            if failing:
                failing.pop()
                raise OSError("the disk is full")
            super().retract(fact_id)

    extraction = Failing()
    cerebro = kept_notes(tmp_path, extraction)
    kept = cerebro.handle.keep(app if path == "decide" else you, "note", "The east gate opens at six.",
                               {"collection": "notes"})
    cerebro.run_once()
    if path == "decide":
        asked = cerebro.tell(app, "take it back", target=kept.id, as_="proposal", kind="record.retract").proposals
        cerebro.decide(you, accept=asked)  # the store keeps it, and the facts layer then keeps the fact
    else:
        with pytest.raises(CerebroError, match="stopped partway"):
            if path == "retract":
                cerebro.retract(you, kept.id, "wrong")
            else:
                cerebro.tell(you, "take it back", target=kept.id, as_="proposal", kind="record.retract")
    tried = cerebro.run_once()
    assert tried.failed == {f"retract of {kept.id}": "OSError: the disk is full. The next pass tries again."}
    passed = cerebro.run_once()  # the record did not change, and its retract is finished all the same
    said = f"note/{kept.id}: record {kept.id} was retracted, so the facts read from it are withdrawn"
    assert passed.retracted == 1 and said in passed.notices and extraction.facts(record=kept.id) == []
    assert said not in cerebro.run_once().notices  # a finished retract is finished once


def test_a_facts_layer_error_withdrawing_a_retracted_records_facts_fails_that_record_alone(tmp_path: Path) -> None:
    from cerebro.caller import Caller

    you, app, busy = Caller("you", "person"), Caller("notes", "app"), []

    class Busy(FakeExtraction):
        def facts(self, **filters: Any) -> list[Any]:
            if filters.get("record") in busy:
                busy.remove(filters["record"])
                raise OSError("database is locked")
            return super().facts(**filters)

    extraction = Busy()
    cerebro = kept_notes(tmp_path, extraction)
    held = cerebro.handle.keep(app, "note", "The west gate opens at nine.", {"collection": "notes"})
    cerebro.run_once()
    cerebro.retract(you, held.id, "wrong")
    cerebro.handle.keep(app, "note", "The west gate opens at ten.", {"collection": "notes"}, id=held.id)
    other = cerebro.handle.keep(app, "note", "The east gate opens at eight.", {"collection": "notes"})
    busy.append(held.id)
    passed = cerebro.run_once()
    assert passed.failed == {f"note/{held.id}": "OSError: database is locked"} and extraction.facts(record=other.id)
    assert cerebro.run_once().tried["cerebro:failed:kept"] == [held.id]


def test_a_retract_is_filed_before_its_take_back_reads_the_records_facts(tmp_path: Path) -> None:
    from cerebro.caller import Caller
    from cerebro.records import retracted_records, was_retracted

    you, held = Caller("you", "person"), {}

    class Reading(FakeExtraction):
        def facts(self, **filters: Any) -> list[Any]:
            if filters.get("record") == held.get("id"):  # what a pass that checks now finds of the retract
                record = cerebro.store.record(held["id"])
                held.setdefault("seen", was_retracted(record, retracted_records(cerebro.store)))  # type: ignore[arg-type]
            return super().facts(**filters)

    cerebro = kept_notes(tmp_path, Reading())
    held["id"] = cerebro.handle.keep(you, "note", "The west gate opens at eight.", {"collection": "notes"}).id
    cerebro.retract(you, held["id"], "wrong")
    assert held["seen"] is True


def test_a_revision_a_caller_keeps_while_a_retract_takes_back_the_last_one_keeps_its_facts(tmp_path: Path) -> None:
    from cerebro.caller import Caller

    you, held, hands = Caller("you", "person"), {}, Hands(datetime(2026, 11, 20, 9, tzinfo=UTC))

    class Racing(FakeExtraction):
        def facts(self, **filters: Any) -> list[Any]:
            if filters.get("record") == held.get("id") and held.pop("retracting", False):
                hands.move(seconds=2)  # the take-back is slow: the maker keeps a new revision, and a pass reads it
                cerebro.handle.keep(you, "note", "The north gate opens at ten.", {"collection": "notes"}, id=held["id"])
                cerebro.run_once()
            return super().facts(**filters)

    extraction = Racing()
    cerebro = kept_notes(tmp_path, extraction, hands)
    kept = cerebro.handle.keep(you, "note", "The north gate opens at nine.", {"collection": "notes"})
    cerebro.run_once()
    held.update(id=kept.id, retracting=True)
    cerebro.retract(you, kept.id, "wrong")
    assert [f.text for f in extraction.facts(record=kept.id)] == ["The north gate opens at ten."]


@pytest.mark.parametrize("taker", ["person", "app"])
def test_a_retract_takes_back_what_an_apps_later_save_gave_though_a_pass_read_it_before_the_retract_landed(
        tmp_path: Path, taker: str) -> None:
    from cerebro.caller import Caller

    you, app, held = Caller("you", "person"), Caller("notes", "app"), {}

    class Sweeping(FakeExtraction):
        def facts(self, **filters: Any) -> list[Any]:
            if filters.get("record") == held.get("id") and held.pop("sweeping", False):
                saved_again()  # the app's own retract is dated as it sweeps
            return super().facts(**filters)

    def saved_again() -> None:  # the app saves again, and a pass reads it, before the retract lands
        hands.move(seconds=2)
        cerebro.handle.keep(app, "note", "The south gate opens at ten.", {"collection": "notes"}, id=kept.id)
        cerebro.run_once()

    extraction, hands = Sweeping(), Hands(datetime(2026, 11, 20, 9, tzinfo=UTC))
    cerebro = kept_notes(tmp_path, extraction, hands)
    kept = cerebro.handle.keep(app, "note", "The south gate opens at nine.", {"collection": "notes"})
    asked = cerebro.tell(app, "take it back", target=kept.id, as_="proposal", kind="record.retract").proposals
    cerebro.run_once()
    if taker == "person":
        cerebro.lookup.decide(you, accept=asked, measure=lambda write: (saved_again(), write()))
    else:  # an app's own retract takes back only what was kept up to it
        held.update(id=kept.id, sweeping=True)
        cerebro.retract(app, kept.id, "its own")
    later = [] if taker == "person" else ["The south gate opens at ten."]
    assert [f.text for f in extraction.facts(record=kept.id)] == later


def test_status_gives_todays_spend_alone_and_counts_what_waits(tmp_path: Path) -> None:
    from cerebro.budget import LOG
    from cerebro.caller import Caller
    from cerebro.records import Proposal, propose

    hands = Hands(datetime(2026, 11, 18, 9, tzinfo=UTC))
    cerebro = workspace(tmp_path, "[manual]", FakeExtraction(), clock=hands)
    cerebro.store.append(LOG, {"day": "2026-11-17", "layer": "compilation", "usd": 5.0})
    cerebro.store.append(LOG, {"day": "2026-11-18", "layer": "compilation", "usd": 0.25})
    curator = Caller("projects/curator", "agent")
    propose(cerebro.store, Proposal("p_1", "article.admit", "projects/lumen", curator, "2026-11-18T08:00:00Z"))
    status = cerebro.status()
    assert status["spent_today"]["total"] == 0.25 and status["waiting"] == 1


def test_an_agent_that_reads_a_file_is_due_once_the_file_changes(tmp_path: Path) -> None:
    (tmp_path / "agents" / "reports").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("schedule: { articles: 0 }\n")
    (tmp_path / "plan.md").write_text("The launch is on 8 December.\n")
    (tmp_path / "agents" / "reports" / "plan.md").write_text(
        "---\nevidence:\n  - files: [plan.md]\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n")
    runs: list[str] = []
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(),
                      agent_run=ScriptedRun(lambda prompt: runs.append(prompt) or {"Summary": "x [^file:plan.md]"}))
    cerebro.run_once()
    cerebro.run_once()
    assert len(runs) == 1  # nothing moved
    (tmp_path / "plan.md").write_text("The launch is on 15 December.\n")
    assert cerebro.run_once().due.get("reports/plan") == "change" and len(runs) == 2


def own_runs_workspace(tmp_path: Path, seen: list[str], calls: list[tuple[str, dict[str, Any]]] | None = None) -> Cerebro:
    """A workspace whose weekly report reads the logs its own runs write, its own article, and a part rendered in
    its name, and sets a field of its own article on each run."""
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    (tmp_path / "agents" / "reports").mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings.yaml").write_text("schedule: { articles: 0 }\n")
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: notes }\n  - query: { from: agent-runs }\n"
        "  - query: { from: spend }\n  - query: { from: proposals }\n  - articles: [reports/weekly]\n"
        "  - query: { from: reports }\nrendered: { Contents: collections }\nheartbeat: [change]\n---\n\n"
        "# Output\n\n## Summary\n\n## Contents\n")
    return Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=hands, trusted=True,
                   connectors=[FakeConnector("notes", NOTES)],
                   agent_run=ScriptedRun(lambda prompt: seen.append(prompt) or cited(prompt),
                                         calls=calls or [("override", {"field": "level", "value": "high"})],
                                         clock=hands))


def test_an_agent_that_reads_its_own_runs_spend_and_article_makes_one_model_call_in_three_passes_with_nothing_new(
        tmp_path: Path) -> None:
    seen: list[str] = []
    cerebro = own_runs_workspace(tmp_path, seen)
    passes = [cerebro.run_once() for _ in range(3)]
    assert [p.compiled for p in passes] == [["reports/weekly"], [], []] and len(seen) == 1
    assert passes[2].skipped["reports/weekly"].startswith("nothing it reads has moved")
    assert cerebro.store.rows("agent-runs") and cerebro.store.rows("spend")  # it wrote them, and they wake nothing
    assert cerebro.store.article("reports/weekly").properties["level"].value == "high"
    logs = [row.data for log in ("proposals", "merges", "agent-runs", "mcp-requests", "spend")
            for row in cerebro.store.rows(log)]
    assert "set level" not in str(logs)  # a run's transcript is kept nowhere a line reads, so it wakes nothing


def test_an_agent_that_reads_its_own_runs_still_reads_them_and_wakes_on_another_agents_run(tmp_path: Path) -> None:
    seen: list[str] = []
    cerebro = own_runs_workspace(tmp_path, seen)
    cerebro.run_once()
    (tmp_path / "agents" / "reports" / "monthly.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [manual]\n---\n\n# Output\n\n"
        "## Summary\n")
    other = Cerebro(tmp_path, store=cerebro.store, extraction=cerebro.extraction, clock=cerebro.clock, trusted=True,
                    agent_run=ScriptedRun(cited))
    other.compile(other.agent("reports/monthly"))
    assert cerebro.run_once().compiled == ["reports/weekly"] and len(seen) == 2
    assert '"agent": "reports/monthly"' in seen[1] and '"agent": "reports/weekly"' in seen[1]
    assert cerebro.run_once().compiled == []


def test_extraction_keeps_the_names_of_a_records_own_parties_and_its_removal_drops_them(tmp_path: Path) -> None:
    mine = Record(id="n7", source="notes", revision="1", keys={"topic": ["Apollo"]}, fields={"ours": ["Kim Vale"]},
                  body="Topic: Apollo\n\nKim Vale ran the review.")
    plain = Record(id="n8", source="notes", revision="1", keys={"topic": ["Apollo"]}, body="Topic: Apollo\n\nNo one.")
    notes = FakeConnector("notes", [mine, plain])
    cerebro = workspace(tmp_path, extraction=FakeExtraction(), notes=notes)
    cerebro.run_once()
    assert cerebro.store.state("ours:notes") == {"n7": ["Kim Vale"]}  # a record with no own party keeps nothing
    notes.remove("n7")
    cerebro.run_once()
    assert cerebro.store.state("ours:notes") == {}


def test_a_pass_hands_a_records_fields_to_the_facts_layer_as_its_source_gave_them(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    part = Record(id="P1", source="parts", revision="1", at="2026-11-10T09:00:00Z", body=None,
                  keys={"part_id": ["P1"]}, fields={"phase": "draft", "parent": "G9"}, extract="records", into="things")
    source = FakeConnector("parts", [part])
    facts = FakeExtraction()
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=facts, connectors=[source],
                      agent_run=ScriptedRun(lambda prompt: {}))
    cerebro.store.create(Identity("things/alder", "things", "Alder", {"part_id": ["P1"]}))
    cerebro.store.create(Identity("groups/nine", "groups", "Group Nine", {"part_id": ["G9"]}))
    result = cerebro.run_once()
    assert result.failed == {} and result.extracted == 2
    said = sorted(f.text for f in facts.facts(record="P1"))
    assert said == ["The parent of Alder is G9.", "The phase of Alder is draft."]  # whatever groups/nine holds
    assert sorted(cerebro.store.facts_of("things/alder")) == sorted(f.id for f in facts.facts(record="P1"))
    assert cerebro.store.article("things/alder").properties == {}  # a pass writes no field
    again = cerebro.run_once()
    assert again.extracted == 0  # the same fields again give nothing new


def test_a_records_fields_that_fail_once_wait_with_its_text_and_come_in_on_a_later_pass(tmp_path: Path) -> None:
    from cerebro.facts import Extracted, ExtractionFailed

    class FailsOnce(FakeExtraction):
        failed = False

        def extract_fields(self, record: Record) -> Extracted:
            if not self.failed:
                self.failed = True
                raise ExtractionFailed("stand-in failure", Extracted([]))
            return super().extract_fields(record)

    (tmp_path / "settings.yaml").write_text("{}\n")
    part = Record(id="P1", source="parts", revision="1", at="2026-11-10T09:00:00Z", body="Alder moved along.",
                  keys={"part_id": ["P1"]}, fields={"phase": "draft"}, into="things")
    facts = FailsOnce()
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=facts, connectors=[FakeConnector("parts", [part])],
                      agent_run=ScriptedRun(lambda prompt: {}))
    cerebro.store.create(Identity("things/alder", "things", "Alder", {"part_id": ["P1"]}))
    first = cerebro.run_once()
    assert "sources/parts/P1#fields" in first.failed and cerebro.store.state("failed:parts") == ["P1"]
    cerebro.run_once()
    assert "The phase of Alder is draft." in [f.text for f in facts.facts(record="P1")]
    assert cerebro.store.state("failed:parts") == []


def test_a_removed_record_whose_id_is_a_number_is_forgotten_by_its_text() -> None:
    from types import SimpleNamespace

    from cerebro.fakes import MemoryStore
    from cerebro.runner import _keep_keys

    store = MemoryStore()
    store.set_state("keys:files", {"7": {"email": ["a@b.example"]}, "8": {"email": ["c@d.example"]}})
    store.set_state("ours:files", {"7": ["Halden"]})
    _keep_keys(SimpleNamespace(store=store), "files", 7)  # type: ignore[arg-type]
    assert store.state("keys:files") == {"8": {"email": ["c@d.example"]}} and store.state("ours:files") == {}


def test_a_field_its_record_no_longer_gives_is_shown_retired_to_the_articles_writer(tmp_path: Path) -> None:
    from cerebro.agent import load_agent
    from cerebro.article import Property
    from cerebro.caller import Caller
    from cerebro.evidence import gather, render

    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "agents" / "things" / "article.md").write_text("---\nevidence:\n  - facts: {}\n---\n\nWrite it.\n")
    part = Record(id="P1", source="parts", revision="1", at="2026-11-10T09:00:00Z", body=None,
                  keys={"part_id": ["P1"]}, fields={"phase": "draft", "owner": "Ada"}, extract="records",
                  into="things")
    source = FakeConnector("parts", [part])
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), connectors=[source],
                      agent_run=ScriptedRun(lambda prompt: {}))
    cerebro.store.create(Identity("things/alder", "things", "Alder", {"part_id": ["P1"]}))
    cerebro.run_once(agents=False)
    cerebro.store.set_field("things/alder", "owner", Property("Ada", Caller("things/article", "agent"),
                                                              "2026-11-10T10:00:00Z"))
    source.add(Record(id="P1", source="parts", revision="2", at="2026-11-11T09:00:00Z", body=None,
                      keys={"part_id": ["P1"]}, fields={"phase": "draft"}, extract="records", into="things"))
    cerebro.run_once(agents=False)
    agent = load_agent(tmp_path / "agents" / "things" / "article.md", tmp_path / "agents")
    read = render(gather(agent, cerebro.lookup, {}, this="things/alder"))
    assert ("retired: The owner of Alder is Ada. Its record no longer gives the field owner, so this fact no longer "
            "stands.") in read
    assert "field phase" not in read  # a field whose fact stands is not said to be retired


def test_an_admitted_article_no_writer_has_written_has_its_copy_at_once_front_matter_and_the_empty_note(
        tmp_path: Path) -> None:
    from cerebro.article import NOTE_LEAD, NOTE_RAN_LEAD

    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "agents" / "things" / "curator.md").write_text("---\nauthority: { admit: true, archive: true, merge: true, split: true }\n---\n\nKeep the things.\n")
    admits = ScriptedRun(lambda prompt: {}, calls=[("admit", {"name": "Alder", "reason": "a thing we keep",
                                                              "keys": {"part_id": ["P1"]},
                                                              "fields": {"phase": "draft"}})])
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), agent_run=admits)
    cerebro.run_once()
    assert cerebro.store.article("things/alder").properties["phase"].value == "draft"  # its curator set it
    copy = tmp_path / "articles" / "things" / "alder.md"
    assert cerebro.store.article("things/alder") is not None and copy.is_file()  # a link to it opens
    text = copy.read_text()
    assert text.startswith("---\nid: things/alder\ntitle: Alder\n") and (NOTE_LEAD in text or NOTE_RAN_LEAD in text)
    assert cerebro.store.article("things/alder").compilations == []  # only a run writes its text
    # an outside caller over MCP finds, queries and reads it, each reply marking it pending, as status lists it:
    # the field its curator set at admit is no run's writing
    import anyio
    from mcp import Client

    async def ask() -> list[str]:
        async with Client(cerebro.mcp_server()) as client:
            return [(await client.call_tool(name, args)).content[0].text for name, args in (
                ("find", {"name": "Alder"}), ("query", {"from": "things"}), ("read", {"ids": ["things/alder"]}))]

    found, queried, read = anyio.run(ask)
    assert "things/alder (matched on name) [pending: article admitted but not yet written]" in found
    assert "things/alder: Alder (phase: draft) [pending]" in queried
    assert "pending: article admitted but not yet written" in read and "compiled_at" not in read
    assert cerebro.status()["pending"] == ["things/alder"] and "things" not in cerebro.status()["articles"]
    assert "unwritten" not in cerebro.status()
    # a search of every collection labels it in the data too, and gives its title as no passage of text
    from cerebro.caller import Caller
    from cerebro.reply import outside

    [item] = outside(cerebro.lookup.query(Caller("you", "person"), None, {}, matching="alder"))["items"]
    assert item == {"kind": "article", "id": "things/alder", "title": "Alder", "pending": True}


def test_a_copy_whose_lists_sit_level_with_their_keys_is_left_as_it_is_until_its_next_compile_rewrites_it(
        tmp_path: Path) -> None:
    import yaml

    from cerebro.article import Property
    from cerebro.caller import Caller

    hands = Hands(datetime(2026, 11, 16, 9, tzinfo=UTC))
    notes = FakeConnector("notes", NOTES)
    cerebro = workspace(tmp_path, "[change]", FakeExtraction(), notes, hands)
    assert cerebro.run_once().compiled == ["reports/weekly"]
    writer = Caller("reports/weekly", "agent")
    cerebro.store.set_field("reports/weekly", "tags", Property(["alpha", "beta"], writer, stamp(hands())))
    cerebro.store.set_field("reports/weekly", "parts", Property({"ids": ["p1"]}, writer, stamp(hands())))
    copy = tmp_path / "articles" / "reports" / "weekly.md"
    head, body = copy.read_text().split("\n---\n", 1)
    front = {**yaml.safe_load(head.removeprefix("---\n")), "tags": ["alpha", "beta"], "parts": {"ids": ["p1"]}}
    level = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1 << 20) + "---\n" + body
    assert "\ntags:\n- alpha\n" in level and "\n  ids:\n  - p1\n" in level
    copy.write_text(level)
    hands.move(hours=5)
    quiet = cerebro.run_once()
    assert (quiet.compiled, quiet.failed, quiet.warnings, quiet.refused) == ([], {}, [], [])
    assert copy.read_text() == level and "unwritten" not in cerebro.status()  # left as it is, and nothing complains
    notes.add(Record(id="n9", source="notes", revision="1", at="2026-11-16T12:00:00Z", body="Note 9."))
    hands.move(hours=5)
    again = cerebro.run_once()
    assert again.compiled == ["reports/weekly"] and (again.failed, again.warnings, again.refused) == ({}, [], [])
    text = copy.read_text()
    assert "\ntags:\n  - alpha\n  - beta\n" in text and "\nparts:\n  ids:\n    - p1\n" in text
    assert yaml.safe_load(text.split("\n---\n")[0].removeprefix("---\n")) | {"compiled_at": None} == \
        front | {"compiled_at": None}


def test_a_new_accepted_proposal_wakes_its_curator_whatever_its_heartbeat_and_again_after_budget_spent(
        tmp_path: Path) -> None:
    from cerebro.caller import Caller

    class Acts(AgentRun):
        """The curator's acts for each run in turn, each run ending as `ends` says."""

        def __init__(self) -> None:
            self.calls: list[list[tuple[str, dict[str, Any]]]] = []
            self.ends: list[str] = []

        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            done = ScriptedRun(lambda prompt: {}, self.calls.pop(0)).run(agent, evidence, tools, budget)
            return replace(done, status=self.ends.pop(0))

    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things" / "curator.md").write_text("---\nheartbeat: [manual]\n---\n\nKeep the things.\n")
    acts = Acts()
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), agent_run=acts,
                      clock=lambda: datetime(2026, 11, 17, 9, tzinfo=UTC))
    you = Caller("you", "person")
    alder, birch = (cerebro.tell(you, f"{name} is ours.", target=f"things/{name.lower()}", as_="proposal",
                                 kind="admit").proposals[0] for name in ("Alder", "Birch"))

    def carry(id: str, name: str) -> list[tuple[str, dict[str, Any]]]:
        return [("admit", {"name": name, "reason": "Asked for.", "proposals": [id]})]

    acts.calls, acts.ends = [carry(alder, "Alder"), carry(birch, "Birch")], ["budget spent", "done"]
    woken = cerebro.run_once()  # a hand-run curator, woken by a new yes; its run stops at its budget after one
    assert "things/curator" in woken.due and cerebro.store.article("things/alder") is not None
    again = cerebro.run_once()  # a yes is left after a run that carried one out and stopped at its budget
    assert "things/curator" in again.due and cerebro.store.article("things/birch") is not None
    quiet = cerebro.run_once()  # what it carried out landing is no new yes
    assert "things/curator" not in quiet.due and acts.calls == []


def test_a_decision_or_a_rejected_admit_does_not_wake_its_curator(tmp_path: Path) -> None:
    from test_held import PerAgent

    from cerebro.caller import Caller
    from cerebro.records import Decision

    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things" / "curator.md").write_text("---\nheartbeat: [change]\n---\n\nKeep the things.\n")
    run = PerAgent()
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), agent_run=run,
                 clock=lambda: datetime(2026, 11, 17, 9, tzinfo=UTC)) as cerebro:
        run.calls["things/curator"] = [("admit", {"name": "Alder", "reason": "a maker"})]
        cerebro.curate(cerebro.agent("things/curator"))
        [asked] = proposals(cerebro.store, "waiting")
        # a caller's decision on proposals of several curators is not measured as one curator's act
        cerebro.lookup.decide(Caller("you", "person"), reject=[Decision(asked.id, "not one of ours")])
        assert "things/curator" not in cerebro.run_once().due
        run.calls["things/curator"] = []
        cerebro.curate(cerebro.agent("things/curator"))
        assert "Your deferred list" in run.prompts["things/curator"][-1]
