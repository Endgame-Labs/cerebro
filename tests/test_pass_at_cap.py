"""A pass at a daily cap: it starts no more of the work the cap holds, counts
nothing as failed for it, keeps what waits and says so in one stop line, which `status` shows again. What waited runs
on the next pass once the cap lifts, with no wait for having waited. Every state here is built in a stand-in store,
with fake extraction and a scripted run: no model is called."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cerebro import Cerebro, Record, cli
from cerebro.article import Identity
from cerebro.budget import HeldForRoom
from cerebro.cli import main
from cerebro.fakes import FakeConnector, FakeExtraction, FakeSecrets, MemoryStore, ScriptedRun
from cerebro.store import Store

DAY = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
KEYS = {"fireworks": "fake-key-1"}  # no decision key: a pass here asks no decision model, so nothing leaves the test
THINGS = [Record(id=f"t{n}", source="things", revision="1", at="2026-11-10T09:00:00Z", body=f"Thing {n}.")
          for n in range(3)]
WEEKLY = ("---\nevidence:\n  - query: { from: facts, source: things }\nheartbeat: [daily]\n---\n\n# Output\n\n"
          "## Summary\n")


@pytest.fixture(autouse=True)
def no_decision_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


class Hands:
    """A clock moved by hand."""

    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def cited(prompt: str) -> dict[str, str]:
    first = re.search(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])
    return {"Summary": f"x [^{first.group(1)}]"} if first else {"Summary": "x"}


class Workspace:
    """A workspace folder with one writer, a store, a source of three records and a clock, opened as a test opens it."""

    def __init__(self, tmp_path: Path, settings: str):
        self.root = tmp_path / "workspace"
        (self.root / "agents" / "reports").mkdir(parents=True)
        (self.root / "agents" / "reports" / "weekly.md").write_text(WEEKLY)
        self.settings(settings)
        self.store: Store = MemoryStore()
        self.clock = Hands(DAY)
        self.things = FakeConnector("things", list(THINGS))
        self.called: list[str] = []
        self.extraction = FakeExtraction()  # the facts layer keeps its facts across each opening

    def settings(self, text: str) -> None:
        (self.root / "settings.yaml").write_text(text)

    def open(self, folder: Path | None = None) -> Cerebro:
        return Cerebro(self.root, store=self.store, extraction=self.extraction, connectors=[self.things],
                       clock=self.clock, secrets=FakeSecrets(KEYS),
                       agent_run=ScriptedRun(lambda prompt: self.called.append(prompt) or cited(prompt)))

    def primed(self) -> None:
        """A day before: the three records extracted and the article written, with no cap reached. Two more records
        come after, and the daily writer is due again the next day."""
        self.clock.at = DAY - timedelta(days=1)
        assert self.open().run_once().compiled == ["reports/weekly"]
        self.clock.at = DAY
        for n in (3, 4):
            self.things.add(Record(id=f"t{n}", source="things", revision="1", at="2026-11-19T09:00:00Z",
                                   body=f"Thing {n}."))
        self.called.clear()

    def spent(self, at: str, layer: str, usd: float) -> None:
        self.store.append("spend", {"at": at, "day": at[:10], "layer": layer, "usd": usd, "provider": "fireworks",
                                    "model": "glm-5p3", "run": ""})

    def cli(self, monkeypatch: pytest.MonkeyPatch, *args: str) -> Any:
        monkeypatch.setattr(cli, "open_workspace", self.open)
        return CliRunner().invoke(main, [*args, "--workspace", str(self.root)])


def test_a_pass_at_the_cap_fails_nothing_says_what_waits_and_exits_1_with_nothing_landed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    space.spent("2026-11-19T00:03:00Z", "compilation", 30.0)
    space.spent("2026-11-19T00:05:00Z", "extraction", 30.0)
    ran = space.cli(monkeypatch, "serve", "--once", "--plain")
    lines = ran.output.splitlines()
    assert ran.exit_code == 1, ran.output
    assert not [line for line in lines if line.startswith("failed")], ran.output
    assert "BudgetSpent" not in ran.output
    assert "Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 2 records and 1 agent run wait." in lines
    assert lines[-1] == "Done: 0 written, 0 failed."
    assert space.called == []
    assert space.store.state("failed:things") == ["t3", "t4"]  # each waits for the next pass
    assert space.store.state("failing:reports/weekly:reports/weekly") is None  # no back off for having waited
    shown = space.cli(monkeypatch, "status")
    assert shown.output.splitlines()[0] == ("Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 2 records and 1 agent "
                                            "run wait.")
    assert "failed    " not in shown.output  # what waits for the cap gave no facts for no fault of its own
    data = json.loads(space.cli(monkeypatch, "status", "--json").output)
    assert data["caps_reached"][0]["records"] == 2 and data["caps_reached"][0]["agent_runs"] == 1
    assert data["failed_records"] == []


@pytest.mark.parametrize("lift", ["raise the cap", "the next UTC day"])
def test_what_waited_at_the_cap_runs_on_the_next_pass_once_it_lifts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lift: str) -> None:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    stopped = space.open().run_once()
    assert stopped.failed == {} and stopped.extracted == 0 and stopped.compiled == [] and space.called == []
    assert stopped.stops == ["Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 2 records and 1 agent run wait."]
    if lift == "raise the cap":
        space.settings("budget: { daily_usd: 500 }\n")
    else:
        space.clock.at = DAY + timedelta(days=1)
    assert space.cli(monkeypatch, "status").output.splitlines()[0].startswith("workspace ")  # the stop line went
    ran = space.cli(monkeypatch, "serve", "--once", "--plain")  # serve --once checks every source now
    assert ran.exit_code == 0, ran.output
    lines = ran.output.splitlines()
    assert "facts     2 records read, 2 facts extracted" in lines and "wrote     reports/weekly  articles/reports/weekly.md" in lines
    assert lines[-1] == "Done: 1 written, 0 failed."
    assert space.store.state("failed:things") == []
    assert space.store.state("failing:reports/weekly:reports/weekly") is None


def test_a_layers_cap_holds_only_its_own_work_and_the_rest_runs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = Workspace(tmp_path, "budget: { compilation_daily_usd: 15 }\n")
    space.primed()
    space.spent("2026-11-19T00:03:00Z", "compilation", 30.0)
    ran = space.cli(monkeypatch, "serve", "--once", "--plain")
    lines = ran.output.splitlines()
    assert ran.exit_code == 0, ran.output  # facts landed, so reaching the cap is no failure
    assert "facts     2 records read, 2 facts extracted" in lines
    assert ("Daily compilation budget: $30.00 of $15.00 spent at 00:03 UTC. 1 agent run waits.") in lines
    assert not [line for line in lines if line.startswith("failed")], ran.output
    assert space.called == [] and space.store.state("failed:things") == []
    space.clock.at = DAY + timedelta(days=1)
    assert space.open().run_once().compiled == ["reports/weekly"]


def test_a_model_with_no_price_is_no_cap_reached_and_still_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\nmodels:\n  extraction: { provider: fireworks, model: "
                                "made-up-7 }\n  decision: { provider: typesafe, model: jev-1.13.0 }\n  compilation: "
                                "{ provider: fireworks, model: glm-5p3 }\n")
    result = space.open().run_once()
    assert result.stops == [] and len([key for key in result.failed if key.startswith("sources/")]) == 3
    assert all("UnpricedModel" in why for key, why in result.failed.items() if key.startswith("sources/"))


def test_a_later_pass_that_took_the_waiting_records_drops_their_count(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = Workspace(tmp_path, "budget: { extraction_daily_usd: 5, compilation_daily_usd: 15 }\n")
    space.primed()
    space.spent("2026-11-19T00:05:00Z", "extraction", 6.0)
    space.spent("2026-11-19T00:06:00Z", "compilation", 16.0)
    first = space.open().run_once()
    assert first.failed == {}
    lines = space.cli(monkeypatch, "status").output.splitlines()
    assert lines[:2] == ["Daily extraction budget: $6.00 of $5.00 spent at 00:05 UTC. 2 records wait.",
                         "Daily compilation budget: $16.00 of $15.00 spent at 00:06 UTC. 1 agent run waits."]
    space.settings("budget: { compilation_daily_usd: 15 }\n")  # the extraction cap goes; compilation's stands
    second = space.cli(monkeypatch, "distill")
    assert second.exit_code == 0, second.output
    lines = space.cli(monkeypatch, "status").output.splitlines()
    assert lines[0] == "Daily compilation budget: $16.00 of $15.00 spent at 00:06 UTC. 1 agent run waits."


def test_a_kind_of_work_the_pass_looked_at_counts_as_zero_beside_one_that_waits(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop line counts each kind of work the command looked at, nothing of it waiting included."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    # no writer: one whose evidence holds nothing to cite runs, so at the cap it would wait
    (space.root / "agents" / "reports" / "weekly.md").unlink()
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    assert space.open().run_once().stops == [
        "Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 3 records and 0 agent runs wait."]
    assert space.cli(monkeypatch, "status").output.splitlines()[0] == (
        "Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 3 records and 0 agent runs wait.")
    distilled = space.cli(monkeypatch, "distill")
    assert distilled.exit_code == 1, distilled.output  # nothing landed: the cap is the fix
    assert "Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 3 records wait." in distilled.output.splitlines()


ARTICLE = "---\nevidence:\n  - query: { from: facts, source: things }\nheartbeat: [daily]\n---\n\n# Output\n\n## Summary\n"


def test_a_collection_writer_whose_second_run_waits_keeps_its_period_due(tmp_path: Path) -> None:
    """The run on the first article reaches the cap and the second waits: the agent's daily period stays due, so the
    next pass once the cap lifts runs what waited, as a run held for room keeps its period."""

    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\ncompile: { concurrency: 1 }\n")
    (space.root / "agents" / "reports" / "weekly.md").unlink()
    (space.root / "agents" / "things").mkdir()
    (space.root / "agents" / "things" / "article.md").write_text(ARTICLE)
    space.store.create(Identity("things/x", "things", "X"))
    space.store.create(Identity("things/y", "things", "Y"))
    space.clock.at = DAY - timedelta(days=1)
    assert sorted(space.open().run_once().compiled) == ["things/x", "things/y"]
    space.clock.at = DAY

    def reaches_the_cap(prompt: str) -> dict[str, str]:
        space.spent("2026-11-19T10:00:00Z", "compilation", 60.0)  # this run's own spend reaches the cap
        return cited(prompt)

    def opened() -> Cerebro:
        return Cerebro(space.root, store=space.store, extraction=space.extraction, connectors=[space.things],
                       clock=space.clock, secrets=FakeSecrets(KEYS), agent_run=ScriptedRun(reaches_the_cap))

    stopped = opened().run_once()
    assert len(stopped.compiled) == 1 and len(stopped.waiting_runs) == 1 and stopped.failed == {}
    waited = stopped.waiting_runs[0].split(" on ")[-1]
    space.settings("budget: { daily_usd: 500 }\ncompile: { concurrency: 1 }\n")
    space.clock.at = DAY + timedelta(minutes=5)
    again = opened().run_once()
    assert waited in again.compiled, again.skipped


TWO = {"things": THINGS, "other": [Record(id="o1", source="other", revision="1", at="2026-11-10T09:00:00Z",
                                          body="Other.")]}


@pytest.mark.parametrize("how", ["a serve pass whose source is not due", "distill of another source"])
def test_a_pass_that_did_not_try_a_source_keeps_what_waits_on_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    other = FakeConnector("other", TWO["other"])
    space.open = lambda folder=None: Cerebro(  # type: ignore[method-assign]
        space.root, store=space.store, extraction=space.extraction, connectors=[space.things, other],
        clock=space.clock, secrets=FakeSecrets(KEYS),
        agent_run=ScriptedRun(lambda prompt: space.called.append(prompt) or cited(prompt)))
    space.primed()
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    space.open().run_once()
    before = json.loads(space.cli(monkeypatch, "status", "--json").output)
    assert before["caps_reached"][0]["records"] == 2
    space.clock.at = DAY + timedelta(minutes=5)
    if how.startswith("a serve"):
        space.open().run_once(scheduled=True)
    else:
        space.open().run_once(only="other", agents=False)
    after = json.loads(space.cli(monkeypatch, "status", "--json").output)
    assert after["caps_reached"][0]["records"] == 2 and after["failed_records"] == []


def test_a_record_that_truly_failed_stays_on_the_failed_line_though_it_waits_at_the_cap(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap refuses every record it reaches, so one that failed before is counted among what waits, and as
    its last try truly failed, `status` still names it as failed."""
    class Broken(FakeExtraction):
        def extract(self, record: Record) -> Any:
            if record.id == "t1":
                raise RuntimeError("the record does not parse")
            return super().extract(record)

    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.extraction = Broken()
    space.open().run_once()
    space.clock.at = DAY + timedelta(minutes=5)
    space.spent("2026-11-19T10:01:00Z", "extraction", 60.0)
    space.things.add(Record(id="t3", source="things", revision="1", at="2026-11-19T09:00:00Z", body="Thing 3."))
    space.open().run_once()
    data = json.loads(space.cli(monkeypatch, "status", "--json").output)
    assert data["failed_records"] == [{"source": "things", "id": "t1"}]
    assert data["caps_reached"][0]["records"] == 2


def test_a_model_with_no_price_at_a_reached_cap_still_fails(tmp_path: Path) -> None:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\nmodels:\n  extraction: { provider: fireworks, model: "
                                "made-up-7 }\n  decision: { provider: typesafe, model: jev-1.13.0 }\n  compilation: "
                                "{ provider: fireworks, model: glm-5p3 }\n")
    space.spent("2026-11-19T00:05:00Z", "compilation", 60.0)
    result = space.open().run_once(agents=False)
    assert result.stops == [] and len(result.failed) == 3
    assert all("UnpricedModel" in why for why in result.failed.values())


def test_a_pass_the_cap_stopped_that_ends_after_midnight_still_says_so(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import runner

    late = datetime(2026, 11, 19, 23, 59, tzinfo=UTC)
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    space.clock.at = late
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    real = runner._keep_stop

    def ends_after_midnight(workspace: Any, result: Any, **kw: Any) -> None:
        space.clock.at = late + timedelta(minutes=2)
        real(workspace, result, **kw)

    monkeypatch.setattr(runner, "_keep_stop", ends_after_midnight)
    ran = space.cli(monkeypatch, "serve", "--once", "--plain")
    assert ran.exit_code == 1, ran.output
    assert "Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 2 records and 1 agent run wait." in ran.output


def test_a_cap_raised_to_less_than_the_days_spend_keeps_the_count(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    space.open().run_once()
    space.settings("budget: { daily_usd: 55 }\n")
    assert space.cli(monkeypatch, "status").output.splitlines()[0] == (
        "Daily budget: $60.00 of $55.00 spent at 00:05 UTC. 2 records and 1 agent run wait.")


class Counted(FakeExtraction):
    """A stand-in facts layer that counts each record handed to it for extraction."""

    def __init__(self) -> None:
        super().__init__()
        self.handed: list[str] = []

    def extract(self, record: Record) -> Any:
        self.handed.append(record.id)
        return super().extract(record)


def test_at_the_cap_a_record_that_needs_no_model_lands_and_one_that_does_waits(tmp_path: Path) -> None:
    """The budget is checked once a record is known to need the model: a record whose source
    maps it to fields alone is fetched and leaves its retry list, and one that needs the model is handed to none."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.extraction = Counted()
    space.things = FakeConnector("things", [
        Record(id="r1", source="things", revision="1", at="2026-11-10T09:00:00Z", body="Row.", extract="records"),
        Record(id="t9", source="things", revision="1", at="2026-11-10T09:00:00Z", body="Thing 9.")])
    space.clock.at = DAY - timedelta(days=1)
    space.open().run_once(agents=False)
    space.clock.at = DAY
    space.store.set_state("failed:things", ["r1", "t9"])  # each one's fetch failed on an earlier pass
    space.extraction.handed.clear()
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    result = space.open().run_once(agents=False)
    assert space.store.state("failed:things") == ["t9"] and space.extraction.handed == [] and result.failed == {}
    assert result.stops == ["Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 1 record waits."]


def test_at_the_cap_a_records_fields_go_to_no_model(tmp_path: Path) -> None:
    class Fielded(FakeExtraction):
        def __init__(self) -> None:
            super().__init__()
            self.fielded: list[str] = []

        def extract_fields(self, record: Record) -> Any:
            if record.fields:
                self.fielded.append(record.id)
            return super().extract_fields(record)

    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.extraction = Fielded()
    space.things = FakeConnector("things", [Record(id="r1", source="things", revision="1", at="2026-11-10T09:00:00Z",
                                                   keys={"domain": ["one.example"]}, fields={"stage": "open"},
                                                   body="Row.", extract="records")])
    space.store.create(Identity("things/one", "things", "One", {"domain": ["one.example"]}))
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    space.open().run_once(agents=False)
    assert space.extraction.fielded == []


class Runs(ScriptedRun):
    """A scripted run that counts each run by the article it runs on, and may cross the compilation cap at a run."""

    def __init__(self, space: Workspace, cross_at: int | None = None) -> None:
        super().__init__(cited)
        self.space, self.cross_at, self.by_article = space, cross_at, Counter[str]()

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        self.by_article[agent.target] += 1
        if self.cross_at is not None and sum(self.by_article.values()) == self.cross_at:
            self.space.spent(self.space.clock.at.isoformat().replace("+00:00", "Z"), "compilation", 60.0)
        return super().run(agent, evidence, tools, budget)


def collection(tmp_path: Path, names: tuple[str, ...], settings: str = "budget: { daily_usd: 50 }\ncompile: { "
               "concurrency: 1 }\n", before: timedelta = timedelta(days=1)) -> Workspace:
    """A workspace whose one writer, due daily, writes each article of the things collection, all written the day
    before with no cap reached."""
    space = Workspace(tmp_path, settings)
    (space.root / "agents" / "reports" / "weekly.md").unlink()
    (space.root / "agents" / "things").mkdir()
    (space.root / "agents" / "things" / "article.md").write_text(ARTICLE)
    for name in names:
        space.store.create(Identity(f"things/{name}", "things", name.upper()))
    space.clock.at = DAY - before
    assert sorted(space.open().run_once().compiled) == [f"things/{name}" for name in names]
    space.clock.at = DAY
    return space


def opener(space: Workspace, run: ScriptedRun, hold: set[str] | None = None) -> Any:
    def opened(folder: Path | None = None) -> Cerebro:
        workspace = Cerebro(space.root, store=space.store, extraction=space.extraction, connectors=[space.things],
                        clock=space.clock, secrets=FakeSecrets(KEYS), agent_run=run)
        if hold:
            real = workspace.compile

            def compile(agent: Any, this: str | None = None, **kw: Any) -> Any:
                if this in hold:
                    raise HeldForRoom("held for room under the cap, runs next pass: runs under way hold it")
                return real(agent, this=this, **kw)
            workspace.compile = compile  # type: ignore[method-assign]
        return workspace
    return opened


def test_an_article_held_on_every_pass_leaves_the_written_one_alone(tmp_path: Path) -> None:
    """Over five passes of one day with things/y held each time, things/x is written once."""
    space = collection(tmp_path, ("x", "y"))
    run = Runs(space)
    opened = opener(space, run, hold={"things/y"})
    for n in range(5):
        space.clock.at = DAY + timedelta(minutes=5 * n)
        opened().run_once()
    assert run.by_article == {"things/x": 1}


def test_what_waited_at_the_cap_runs_once_and_nothing_written_runs_again(tmp_path: Path) -> None:
    """Three articles, the second run reaches the cap, the cap is raised the same day: each article runs once."""
    space = collection(tmp_path, ("a", "b", "c"))
    run = Runs(space, cross_at=2)
    opened = opener(space, run)
    first = opened().run_once()
    assert sorted(first.compiled) == ["things/a", "things/b"] and first.waiting_runs == ["things/article on things/c"]
    space.settings("budget: { daily_usd: 5000 }\ncompile: { concurrency: 1 }\n")
    space.clock.at = DAY + timedelta(minutes=5)
    assert opened().run_once().compiled == ["things/c"]
    space.clock.at = DAY + timedelta(minutes=10)
    assert opened().run_once().compiled == []
    assert run.by_article == {"things/a": 1, "things/b": 1, "things/c": 1}


def test_the_article_written_last_still_waits_at_the_cap_in_path_order(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compile and `agents run` list articles by path: things/c, written last, still waits
    at the cap."""
    import cerebro.clock

    ticks = iter(DAY - timedelta(days=1) + timedelta(seconds=n) for n in range(1000))
    monkeypatch.setattr(cerebro.clock, "system_clock", lambda: next(ticks))  # each stand-in run a second later
    space = collection(tmp_path, ("a", "b", "c"))
    first = opener(space, Runs(space, cross_at=2))().run_once()
    assert sorted(first.compiled) == ["things/a", "things/b"] and first.waiting_runs == ["things/article on things/c"]
    space.clock.at = DAY + timedelta(days=1)
    monkeypatch.setattr(space, "open", opener(space, Runs(space, cross_at=1)))
    lines = space.cli(monkeypatch, "agents", "run", "things/article").output.splitlines()
    assert "wrote    things/a  articles/things/a.md" in lines and "waiting  things/c: the daily budget is spent" in lines


def test_a_new_period_runs_every_article_whatever_waited_in_the_last(tmp_path: Path) -> None:
    space = collection(tmp_path, ("a", "b", "c"))
    opened = opener(space, Runs(space, cross_at=2))
    opened().run_once()
    space.clock.at = DAY + timedelta(days=1)
    assert sorted(opened().run_once().compiled) == ["things/a", "things/b", "things/c"]


def test_status_shows_the_count_of_a_later_crossing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The next day's stop, and a raise to less than the day's spend, each keep the count `status` shows."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    space.cli(monkeypatch, "serve", "--once", "--plain")
    space.clock.at = DAY + timedelta(days=1)
    space.spent("2026-11-20T00:07:00Z", "extraction", 70.0)
    two = space.cli(monkeypatch, "serve", "--once", "--plain")
    line = next(line for line in two.output.splitlines() if line.startswith("Daily"))
    assert space.cli(monkeypatch, "status").output.splitlines()[0] == line
    space.spent("2026-11-20T00:09:00Z", "extraction", 20.0)
    space.settings("budget: { daily_usd: 80 }\n")  # $90 spent: the call at 00:09 reaches the raised cap
    assert space.cli(monkeypatch, "status").output.splitlines()[0] == (
        "Daily budget: $90.00 of $80.00 spent at 00:09 UTC. 2 records and 1 agent run wait.")


class Dry(FakeExtraction):
    """A facts layer whose first call spends past the cap and finds no fact."""

    def __init__(self, space: Workspace) -> None:
        super().__init__()
        self.space = space

    def extract(self, record: Record) -> Any:
        self.space.spent("2026-11-19T10:00:00Z", "extraction", 60.0)
        got = super().extract(record)
        return type(got)([], input_tokens=10)


@pytest.mark.parametrize("command", [("serve", "--once", "--plain"), ("distill",)])
def test_a_paid_call_at_the_cap_is_something_done_though_nothing_landed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...]) -> None:
    """A command that recorded a model call before the stop exits 0."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.extraction = Dry(space)
    ran = space.cli(monkeypatch, *command)
    assert "Daily budget: $60.00 of $50.00 spent at 10:00 UTC." in ran.output
    assert ran.exit_code == 0, ran.output


def test_at_the_cap_a_source_is_read_up_to_its_first_record_that_needs_the_model(tmp_path: Path) -> None:
    """A pass at the cap stops at the first record that needs the model, so it fetches no record of
    the source past it, and each counts as waiting."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    for n in range(5, 15):
        space.things.add(Record(id=f"t{n}", source="things", revision="1", at="2026-11-19T09:00:00Z",
                                body=f"Thing {n}."))
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    space.open().run_once()
    fetched: list[str] = []
    real = space.things.get
    space.things.get = lambda id: fetched.append(id) or real(id)  # type: ignore[method-assign]
    space.clock.at = DAY + timedelta(minutes=15)
    again = space.open().run_once()
    assert len(fetched) <= 1, fetched
    assert again.stops == ["Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 12 records and 1 agent run wait."]


def test_agents_run_at_the_cap_lists_what_waits_and_exits_0_after_writing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In `agents run`, each article the cap keeps waits, the stop line follows, and `status` keeps the count."""
    space = collection(tmp_path, ("a", "b", "c"))
    monkeypatch.setattr(space, "open", opener(space, Runs(space, cross_at=1)))
    ran = space.cli(monkeypatch, "agents", "run", "things/article")
    lines = ran.output.splitlines()
    assert ran.exit_code == 0, ran.output
    assert "wrote    things/a  articles/things/a.md" in lines
    assert "waiting  things/b: the daily budget is spent" in lines and "waiting  things/c: the daily budget is spent" in lines
    assert lines[-1] == "Daily budget: $60.00 of $50.00 spent at 10:00 UTC. 2 agent runs wait."
    assert space.cli(monkeypatch, "status").output.splitlines()[0] == lines[-1]


COMPOSER = "---\nsubagents: [risks, people]\n---\n\nA note on one thing.\n\n# Composing\n\nJoin the drafts.\n"
PART = ("---\nevidence:\n  - query: {{ from: facts, source: things }}\nheartbeat: [change]\n---\n\n# Instructions\n\n"
        "Draft it.\n\n# Output\n\n## {h}\n")


def test_the_cap_inside_a_writer_with_subagents_is_said_and_counted(tmp_path: Path) -> None:
    """The first subagent's run reaches the cap; the composer's own run is refused. The pass says the cap and
    counts what waits, and no note calls it a hold for room."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\ncompile: { concurrency: 1 }\n")
    (space.root / "agents" / "reports" / "weekly.md").unlink()
    (space.root / "agents" / "things" / "subagents").mkdir(parents=True)
    (space.root / "agents" / "things" / "article.md").write_text(COMPOSER)
    (space.root / "agents" / "things" / "subagents" / "risks.md").write_text(PART.format(h="Risks"))
    (space.root / "agents" / "things" / "subagents" / "people.md").write_text(PART.format(h="People"))
    space.store.create(Identity("things/a", "things", "A"))
    space.clock.at = DAY - timedelta(days=1)
    space.open().run_once()
    space.clock.at = DAY
    space.things.add(Record(id="t7", source="things", revision="1", at="2026-11-19T09:00:00Z", body="Thing 7."))
    crossed = []

    def write(prompt: str) -> Any:
        if not crossed:
            crossed.append(1)
            space.spent("2026-11-19T10:00:00Z", "compilation", 60.0)
        return cited(prompt)

    result = opener(space, ScriptedRun(write))().run_once()
    assert result.stops and result.stops[0].startswith("Daily budget: $60.00 of $50.00 spent at 10:00 UTC.")
    assert "things/article on things/a" in result.waiting_runs
    assert not [line for line in result.warnings if "held for room" in line], result.warnings


def test_serve_says_a_stop_once_and_says_when_calls_resume(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Serve logs the stop once until it changes, and logs that model calls resume."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    workspace = space.open()
    printed = cli._passes(workspace)
    for minutes in (0, 5):
        space.clock.at = DAY + timedelta(minutes=minutes)
        printed(workspace.run_once(scheduled=True))
    space.clock.at = DAY + timedelta(days=1)
    printed(workspace.run_once(scheduled=True))
    out = capsys.readouterr().out.splitlines()
    assert [line for line in out if line.startswith("Daily budget:")] == [
        "Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 2 records and 1 agent run wait.",
        "Daily budget: a new UTC day began, so model calls resume"]


def test_a_record_waiting_on_its_retry_list_and_listed_again_counts_once(tmp_path: Path) -> None:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    space.store.set_state("failed:things", ["t3"])  # it failed on an earlier pass, and is listed again
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    result = space.open().run_once(agents=False)
    assert space.store.state("failed:things") == ["t3", "t4"]
    assert result.stops == ["Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 2 records wait."]


def first_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, paid: bool) -> Any:
    """A new workspace's first run with --yes: a curator admits one article, and the build waits for the cap. With
    `paid`, the curator's own run is the call that reaches the cap; otherwise the cap was reached before it began."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    (space.root / "agents" / "reports" / "weekly.md").unlink()
    (space.root / "agents" / "things").mkdir(parents=True)
    (space.root / "agents" / "things" / "curator.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: things }\n---\n\nAdmit what the facts name.\n")
    (space.root / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - query: { from: facts, source: things }\n---\n\n# Output\n\n## Summary\n")
    admit = [("admit", {"name": "Thing Nine", "reason": "a fact names it"})]

    def write(prompt: str) -> Any:
        if paid and "Admit what the facts name" in prompt:
            space.spent("2026-11-19T10:00:00Z", "compilation", 60.0)
        return cited(prompt)

    class Run(ScriptedRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            calls = admit if agent.id == "things/curator" else []
            return ScriptedRun(write, calls=calls).run(agent, evidence, tools, budget)

    if not paid:
        space.spent("2026-11-19T00:05:00Z", "compilation", 60.0)
    monkeypatch.setattr(space, "open", lambda folder=None: Cerebro(
        space.root, store=space.store, extraction=space.extraction, connectors=[space.things], clock=space.clock,
        secrets=FakeSecrets(KEYS), agent_run=Run(write)))
    return space.cli(monkeypatch, "serve", "--once", "--plain", "--yes")


def test_a_first_run_the_cap_left_with_nothing_done_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A command that did nothing before its stop fails, with the cap as the fix."""
    ran = first_run(tmp_path, monkeypatch, paid=False)
    assert "Daily budget: $60.00 of $50.00 spent at 00:05 UTC." in ran.output and ran.exit_code == 1, ran.output


def test_a_first_run_whose_curator_paid_before_the_cap_exits_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A model call recorded before the stop is something done, so the stop is no failure: the build's curator
    run, which makes the approved article, waits for the cap."""
    ran = first_run(tmp_path, monkeypatch, paid=True)
    assert "held      things/curator: model calls spent $60.00 of the $50 daily cap on 2026-11-19 (UTC)" in ran.output
    assert ran.exit_code == 0, ran.output


class Logged(ScriptedRun):
    """A scripted run that counts runs by (day, agent, article), may cross the compilation cap at its n-th run, and may
    raise for an article a number of times, as a model that broke."""

    def __init__(self, space: Workspace, cross_at: int | None = None, breaks: dict[str, int] | None = None) -> None:
        super().__init__(cited)
        self.space, self.cross_at, self.breaks = space, cross_at, dict(breaks or {})
        self.n, self.by = 0, Counter[tuple[str, str, str]]()

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        if self.breaks.get(agent.target):
            self.breaks[agent.target] -= 1
            raise RuntimeError("the model broke")
        self.n += 1
        self.by[(self.space.clock.at.date().isoformat(), agent.id, agent.target)] += 1
        if self.cross_at is not None and self.n == self.cross_at:
            self.space.spent(self.space.clock.at.isoformat().replace("+00:00", "Z"), "compilation", 60.0)
        return super().run(agent, evidence, tools, budget)

    def day(self, day: str = "2026-11-19") -> dict[str, int]:
        return {target: n for (d, _, target), n in self.by.items() if d == day}


def test_a_held_article_still_runs_after_another_spent_its_one_more_try(tmp_path: Path) -> None:
    """Article b fails twice (its run and its one more try), d is held on the first two passes: d runs once that day."""
    space = collection(tmp_path, ("a", "b", "c", "d"))
    run, hold = Logged(space, breaks={"things/b": 2}), {"things/d"}
    opened = opener(space, run, hold)
    for n in range(6):
        space.clock.at = DAY + timedelta(minutes=10 * n)
        if n == 2:
            hold.clear()
        opened().run_once()
    assert run.day() == {"things/a": 1, "things/c": 1, "things/d": 1}


def test_an_article_that_waited_for_the_cap_runs_in_its_week_after_a_failure_spent_its_try(tmp_path: Path) -> None:
    """Weekly: d waits for the cap; b's one more try fails; the cap is raised: d runs that week."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\ncompile: { concurrency: 1 }\n")
    (space.root / "agents" / "reports" / "weekly.md").unlink()
    (space.root / "agents" / "things").mkdir()
    (space.root / "agents" / "things" / "article.md").write_text(ARTICLE.replace("[daily]", "[weekly]"))
    for name in "abcd":
        space.store.create(Identity(f"things/{name}", "things", name.upper()))
    space.clock.at = DAY - timedelta(days=7)
    space.open().run_once()
    class Spending(Logged):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            if self.breaks.get(agent.target) == 1:  # its one more try spends past the raised cap, then fails
                self.space.spent(self.space.clock.at.isoformat().replace("+00:00", "Z"), "compilation", 6000.0)
            return super().run(agent, evidence, tools, budget)

    run = Spending(space, cross_at=2, breaks={"things/b": 2})
    opened = opener(space, run)
    for n in range(4):
        space.clock.at = DAY + timedelta(minutes=10 * n)
        if n == 2:
            space.settings("budget: { daily_usd: 5000 }\ncompile: { concurrency: 1 }\n")
        opened().run_once()
    space.clock.at = DAY + timedelta(days=1)  # the spend is past the raised cap until the next UTC day
    opened().run_once()
    assert sum(n for (_, _, target), n in run.by.items() if target == "things/d") == 1


COMPOSING = "---\nsubagents: [risks, people]\n---\n\nA note on one thing.\n\n# Composing\n\nJoin the drafts.\n"
DAILY_PART = ("---\nevidence:\n  - query: {{ from: facts, source: things }}\nheartbeat: [daily]\n---\n\n# Instructions\n\n"
              "Draft it.\n\n# Output\n\n## {h}\n")


def composed(tmp_path: Path) -> Workspace:
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\ncompile: { concurrency: 1 }\n")
    (space.root / "agents" / "reports" / "weekly.md").unlink()
    (space.root / "agents" / "things" / "subagents").mkdir(parents=True)
    (space.root / "agents" / "things" / "article.md").write_text(COMPOSING)
    (space.root / "agents" / "things" / "subagents" / "risks.md").write_text(DAILY_PART.format(h="Risks"))
    (space.root / "agents" / "things" / "subagents" / "people.md").write_text(DAILY_PART.format(h="People"))
    for name in "ab":
        space.store.create(Identity(f"things/{name}", "things", name.upper()))
    space.clock.at = DAY - timedelta(days=1)
    space.open().run_once()
    space.clock.at = DAY
    return space


def test_the_parts_the_cap_refused_run_once_and_a_paid_part_does_not_run_again(tmp_path: Path) -> None:
    """The first subagent's run reaches the cap; once it is raised, each refused part and each composer runs
    once that day, and the paid part does not run again."""
    space = composed(tmp_path)
    run = Logged(space, cross_at=1)
    opened = opener(space, run)
    for n in range(5):
        space.clock.at = DAY + timedelta(minutes=10 * n)
        if n == 2:
            space.settings("budget: { daily_usd: 5000 }\ncompile: { concurrency: 1 }\n")
        opened().run_once()
    ran = {(agent, target): n for (d, agent, target), n in run.by.items() if d == "2026-11-19"}
    want = {(agent, article): 1 for article in ("things/a", "things/b")
            for agent in ("things/subagents/risks", "things/subagents/people", "things/article")}
    assert ran == want


@pytest.mark.parametrize("kind", ["held", "unpriced"])
def test_a_part_refused_for_room_or_for_no_price_is_said_as_that(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """No cap is reached. A part held for room is a hold; a part on its own model with no price fails with its
    reason. Neither is a wait for the daily cap."""
    import cerebro.compile as compile_module

    space = composed(tmp_path)
    risks = space.root / "agents" / "things" / "subagents" / "risks.md"
    if kind == "unpriced":  # its own model has no price, so the ledger refuses it while the cap is set
        risks.write_text(DAILY_PART.format(h="Risks").replace(
            "heartbeat: [daily]\n", "heartbeat: [daily]\nmodel: { provider: fireworks, model: made-up-7 }\n"))
    else:
        real = compile_module.run_agent

        def refusing(agent: Any, *args: Any, **kwargs: Any) -> Any:
            if agent.id.endswith("risks") and agent.target == "things/a":
                raise HeldForRoom("held for room under the cap, runs next pass: compilation runs under way hold $1")
            return real(agent, *args, **kwargs)
        monkeypatch.setattr(compile_module, "run_agent", refusing)
    result = opener(space, ScriptedRun(cited))().run_once()
    key = "things/subagents/risks on things/a"
    assert result.stops == [] and "compilation" not in result.stopped and result.waiting_runs == []
    assert not [line for line in result.warnings if "waits for the daily cap" in line], result.warnings
    if kind == "held":
        assert key in result.held
    else:
        assert "UnpricedModel" in result.failed.get(key, ""), result.failed


def test_agents_run_at_the_cap_keeps_the_other_writers_waiting_runs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass leaves three agent runs waiting across two writers; `agents run` of one keeps the other's."""
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\ncompile: { concurrency: 1 }\n")
    (space.root / "agents" / "things").mkdir()
    (space.root / "agents" / "things" / "article.md").write_text(ARTICLE)
    for name in "ab":
        space.store.create(Identity(f"things/{name}", "things", name.upper()))
    space.clock.at = DAY - timedelta(days=1)
    space.open().run_once()
    space.clock.at = DAY
    space.spent("2026-11-19T00:05:00Z", "compilation", 60.0)
    space.open().run_once()
    space.cli(monkeypatch, "agents", "run", "reports/weekly")
    assert space.cli(monkeypatch, "status").output.splitlines()[0] == (
        "Daily budget: $60.00 of $50.00 spent at 00:05 UTC. 0 records and 3 agent runs wait.")


@pytest.mark.parametrize("cap", [False, True])
def test_a_writers_whole_run_by_hand_is_its_periods_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: bool) -> None:
    """A person's run of any agent but a curator is recorded: the next pass that day runs it nowhere again.
    With `cap`, a pass first left b and c waiting for the cap, which is raised before the run by hand."""
    space = collection(tmp_path, ("a", "b", "c"))
    run = Logged(space, cross_at=1 if cap else None)
    opened = opener(space, run)
    monkeypatch.setattr(space, "open", opened)
    if cap:
        opened().run_once()
        space.settings("budget: { daily_usd: 5000 }\ncompile: { concurrency: 1 }\n")
    space.clock.at = DAY + timedelta(minutes=5)
    assert space.cli(monkeypatch, "agents", "run", "things/article").exit_code == 0
    space.clock.at = DAY + timedelta(minutes=10)
    assert opened().run_once().compiled == []
    assert run.day() == ({"things/a": 2, "things/b": 1, "things/c": 1} if cap else
                         {"things/a": 1, "things/b": 1, "things/c": 1})


def test_a_pass_that_begins_before_midnight_spends_the_period_it_began_in(tmp_path: Path) -> None:
    """A daily pass starts at 23:58 and ends after 00:00: every article still runs the next day."""
    space = collection(tmp_path, ("a", "b", "c"), settings="compile: { concurrency: 1 }\n", before=timedelta(days=2))
    space.clock.at = DAY.replace(hour=23, minute=58) - timedelta(days=1)

    class Slow(Logged):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            self.space.clock.at += timedelta(minutes=1)
            return super().run(agent, evidence, tools, budget)

    run = Slow(space)
    opened = opener(space, run)
    assert sorted(opened().run_once().compiled) == ["things/a", "things/b", "things/c"]  # 23:58, ends at 00:01
    space.clock.at = DAY.replace(hour=1)
    assert sorted(opened().run_once().compiled) == ["things/a", "things/b", "things/c"]  # the 19th's own run


def test_a_held_article_does_not_starve_a_change_on_another(tmp_path: Path) -> None:
    """A writer on [change, daily] whose c is held all day still writes a's new evidence."""
    space = collection(tmp_path, ("a", "b", "c"))
    (space.root / "agents" / "things" / "article.md").write_text(ARTICLE.replace("[daily]", "[change, daily]"))
    run = Logged(space)
    opened = opener(space, run, hold={"things/c"})
    opened().run_once()
    space.things.add(Record(id="t9", source="things", revision="1", at="2026-11-19T10:30:00Z", body="A grew."))
    before = run.day().get("things/a", 0)
    for n in range(1, 4):
        space.clock.at = DAY + timedelta(hours=n)
        opened().run_once()
    assert run.day().get("things/a", 0) > before


def test_serve_says_the_next_days_stop_and_a_stop_that_changes_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The printer keeps only the last stop it said, and the crossing it names."""
    said: list[str] = []
    monkeypatch.setattr(cli, "_echo", lambda message="", **kw: said.append(str(message)))
    space = Workspace(tmp_path, "budget: { daily_usd: 50 }\n")
    space.primed()
    space.spent("2026-11-19T00:05:00Z", "extraction", 60.0)
    workspace = space.open()
    printed = cli._passes(workspace)
    printed(workspace.run_once(scheduled=True))
    space.spent("2026-11-20T00:05:00Z", "extraction", 60.0)  # the next day's spend reaches its cap before a pass
    space.clock.at = DAY + timedelta(days=1)
    printed(workspace.run_once(scheduled=True))
    assert len([line for line in said if line.startswith("Daily budget:")]) == 2
    said.clear()
    printed(workspace.run_once(scheduled=False))  # 2 records and 1 agent run
    space.things.add(Record(id="t9", source="things", revision="1", at="2026-11-20T09:00:00Z", body="Nine."))
    printed(workspace.run_once(scheduled=False))  # 3 records
    space.store.set_state("failed:things", ["t3", "t4"])  # one left the retry list, as another process read it
    back = workspace.run_once(scheduled=False)  # 2 records again
    printed(back)
    stops = [line for line in said if line.startswith("Daily budget:")]
    assert len(stops) == 2 and stops[-1] == back.stops[0], stops


@pytest.mark.parametrize("case", ["held", "cap", "failed"])
def test_a_run_by_hand_that_left_a_part_unfinished_leaves_its_period_owed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    """A person's `agents run` of a writer with subagents where one part on things/b was held for room, refused at a
    reached cap (the composer's own run), or failed: no run with a waiting or failed outcome is recorded, so the
    period is not spent, the part is said on screen, and the next pass runs it."""
    import cerebro.compile as compile_module
    from cerebro.budget import BudgetSpent
    from cerebro.run import Run

    space = composed(tmp_path)
    run = Logged(space)
    opened = opener(space, run)
    monkeypatch.setattr(space, "open", opened)
    space.clock.at = DAY + timedelta(hours=1)
    who = "things/article" if case == "cap" else "things/subagents/risks"
    real = compile_module.run_agent

    def refusing(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if agent.id == who and agent.target == "things/b":
            if case == "held":
                raise HeldForRoom("held for room under the cap, runs next pass: compilation runs under way hold $1")
            if case == "cap":
                space.spent("2026-11-19T11:00:00Z", "compilation", 60.0)
                raise BudgetSpent("model calls spent $60.00 of the $50 daily cap on 2026-11-19 (UTC): they wait")
            return Run("run_x", agent.id, agent.target, "failed", note="the provider broke")
        return real(agent, *args, **kwargs)

    monkeypatch.setattr(compile_module, "run_agent", refusing)
    ran = space.cli(monkeypatch, "agents", "run", "things/article")
    monkeypatch.setattr(compile_module, "run_agent", real)
    lines = ran.output.splitlines()
    key = f"{who} on things/b"
    said = {"held": f"held     {key}: held for room under the cap, runs next pass",
            "cap": "waiting  things/article on things/b: the daily budget is spent",
            "failed": "failed   things/article on things/b: a subagent's run failed, so nothing of the article's run "
                      "lands: things/subagents/risks: run run_x failed: the provider broke"}[case]
    assert said in lines, ran.output
    if case == "held":  # one line, not the compile's note beside it
        assert [line for line in lines if who in line] == [said], ran.output
    if case == "cap":
        space.settings("budget: { daily_usd: 5000 }\ncompile: { concurrency: 1 }\n")
    before = run.by.copy()
    space.clock.at = DAY + timedelta(hours=2)
    opened().run_once()
    assert run.by[("2026-11-19", who, "things/b")] > before[("2026-11-19", who, "things/b")]


def test_a_run_by_hand_is_recorded_before_it_gives_back_its_lease(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass that starts as `agents run` records its period's run finds the writer still leased."""
    from cerebro import runner

    space = collection(tmp_path, ("a", "b"))
    run = Logged(space)
    opened = opener(space, run)
    monkeypatch.setattr(space, "open", opened)
    real = runner.ran_by_hand
    seen: list[list[str]] = []

    def window(workspace: Any, agent: Any, began: Any) -> None:
        seen.append(opened().run_once(scheduled=True).compiled)  # another process's pass, in that moment
        real(workspace, agent, began)

    monkeypatch.setattr(cli, "ran_by_hand", window)
    space.clock.at = DAY + timedelta(minutes=5)
    assert space.cli(monkeypatch, "agents", "run", "things/article").exit_code == 0
    assert seen == [[]] and run.day() == {"things/a": 1, "things/b": 1}


class Ticking(Runs):
    """Runs whose own clock reads one second later at each reading, so each article it writes is newer than the last."""

    def __init__(self, space: Workspace, cross_at: int | None = None) -> None:
        super().__init__(space, cross_at)
        self.order: list[str] = []
        self.ticks = 0

        def clock() -> Any:
            self.ticks += 1
            return space.clock.at + timedelta(seconds=self.ticks)
        self.clock = clock

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        self.order.append(agent.target)
        return super().run(agent, evidence, tools, budget)


def test_a_pass_takes_a_collection_in_path_order_whenever_each_article_was_written(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Articles of one collection run by path, so the cap keeps the last paths waiting."""
    space = collection(tmp_path, ("a", "b", "c"))
    opener(space, Ticking(space))().run_once()  # a, b, c written in that order: c is the newest
    space.clock.at = DAY + timedelta(days=1)
    run = Ticking(space, cross_at=1)
    passed = opener(space, run)().run_once()
    assert run.order == ["things/a"] and passed.compiled == ["things/a"]
    space.clock.at = DAY + timedelta(days=2)
    run = Ticking(space)
    monkeypatch.setattr(space, "open", opener(space, run))
    ran = space.cli(monkeypatch, "agents", "run", "things/article")
    assert ran.exit_code == 0, ran.output
    assert run.order == ["things/a", "things/b", "things/c"]
