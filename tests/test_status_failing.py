"""`status` says what failed, what waits after failing, and when a spent cap lets model calls go on, and ends with
what to check. Every state here is built in a stand-in store, with no model call."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_thin_path import WEEKLY

from cerebro import Cerebro, cli
from cerebro.article import Identity
from cerebro.cli import main
from cerebro.fakes import FakeConnector, FakeExtraction, FakeSecrets, MemoryStore
from cerebro.records import RETRY
from cerebro.store import Store

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
KEYS = {"fireworks": "fake-key-1", "typesafe": "fake-key-2"}
CLOSING = "[!] Done with warnings. Check: "


def lay_out_workspace(tmp_path: Path, settings: str = "budget: { daily_usd: 50 }\n", agent: str = WEEKLY) -> Path:
    root = tmp_path / "workspace"
    (root / "agents" / "reports").mkdir(parents=True)
    (root / "agents" / "reports" / "weekly.md").write_text(agent)
    (root / "settings.yaml").write_text(settings)
    return root


def status(monkeypatch: pytest.MonkeyPatch, root: Path, store: Store, *args: str) -> Any:
    def opened(folder: Path | None) -> Cerebro:
        return Cerebro(root, store=store, extraction=FakeExtraction(), connectors=[FakeConnector("local", [])],
                       clock=lambda: NOW, secrets=FakeSecrets(KEYS))

    monkeypatch.setattr(cli, "open_workspace", opened)
    result = CliRunner().invoke(main, ["status", *args, "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    return result


def failed_run(store: Store, agent: str, at: str, note: str = "") -> None:
    store.append("agent-runs", {"run": f"run_{at[11:13]}{at[14:16]}", "agent": agent, "target": agent,
                                "status": "failed", "provider": "fireworks", "model": "glm-5p3", "ended_at": at,
                                **({"note": note} if note else {})})


def spend(store: Store, at: str, layer: str, usd: float) -> None:
    store.append("spend", {"at": at, "day": at[:10], "layer": layer, "usd": usd, "provider": "fireworks",
                           "model": "glm-5p3", "run": ""})


def test_a_quiet_workspace_shows_its_zone_and_ends_with_no_line_to_check(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, "id: ws-quiet\ntimezone: America/Los_Angeles\nbudget: { daily_usd: 50 }\n")
    result = status(monkeypatch, root, MemoryStore())
    lines = result.output.splitlines()
    assert lines[:3] == [f"workspace {root}", "id        ws-quiet", "timezone  America/Los_Angeles"]
    assert CLOSING not in result.output and "Done" not in result.output
    data = json.loads(status(monkeypatch, root, MemoryStore(), "--json").output)
    assert data["id"] == "ws-quiet" and data["timezone"] == "America/Los_Angeles"
    assert data["failing"] == [] and data["held_runs"] == [] and data["failed_records"] == []
    assert data["caps_reached"] == [] and data["check"] == []


def test_an_agent_whose_last_run_failed_is_named_with_when_and_why_and_status_ends_with_what_to_check(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path)
    store = MemoryStore()
    failed_run(store, "reports/weekly", "2026-11-19T09:00:00Z", "RuntimeError: the provider timed out")
    failed_run(store, "reports/weekly", "2026-11-19T09:30:00Z", "RuntimeError: the provider timed out")
    result = status(monkeypatch, root, store)
    lines = result.output.splitlines()
    at = lines.index("failing   1 agent whose last run failed, each tried again when next due or by cerebro "
                     "agents run <id>")
    assert lines[at + 1] == ("          reports/weekly at 2026-11-19T09:30:00Z, failing since 2026-11-19T09:00:00Z: "
                             "RuntimeError: the provider timed out")
    assert lines[-1] == CLOSING + "1 agent failed its last run: see the failing line"
    data = json.loads(status(monkeypatch, root, store, "--json").output)
    assert data["failing"] == [{"agent": "reports/weekly", "at": "2026-11-19T09:30:00Z",
                                "since": "2026-11-19T09:00:00Z", "reason": "RuntimeError: the provider timed out"}]
    assert data["check"] == ["1 agent failed its last run: see the failing line"]


def test_a_later_run_that_did_not_fail_clears_the_failing_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path)
    store = MemoryStore()
    failed_run(store, "reports/weekly", "2026-11-19T09:00:00Z", "RuntimeError: once")
    store.append("agent-runs", {"run": "run_b", "agent": "reports/weekly", "target": "reports/weekly",
                                "status": "done", "model": "glm-5p3", "ended_at": "2026-11-19T09:10:00Z"})
    result = status(monkeypatch, root, store)
    assert "failing   " not in result.output and CLOSING not in result.output


def test_a_run_held_after_failing_says_when_it_runs_again_and_one_whose_wait_ended_is_not_held(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path)
    store = MemoryStore()
    failed_run(store, "reports/weekly", "2026-11-19T09:59:30Z")
    store.set_state("failing:reports/weekly:reports/weekly", {"stamp": "s1", "times": 2, "at": "2026-11-19T09:59:30Z"})
    lines = status(monkeypatch, root, store).output.splitlines()
    at = lines.index("held      1 agent run held after failing, each until the time shown, its evidence moves or "
                     "its schedule comes")
    assert lines[at + 1] == ("          reports/weekly on reports/weekly: failed 2 times, runs again after "
                             "2026-11-19T10:00:30Z: failed")  # a run kept with no note gives its end as the reason
    assert lines[-1] == CLOSING + "1 agent failed its last run: see the failing line (and 1 more warning above)"
    data = json.loads(status(monkeypatch, root, store, "--json").output)
    assert data["held_runs"] == [{"agent": "reports/weekly", "article": "reports/weekly", "times": 2,
                                  "at": "2026-11-19T09:59:30Z", "until": "2026-11-19T10:00:30Z", "reason": "failed"}]
    assert "1 agent run held after failing: see the held line" in data["check"]
    store.set_state("failing:reports/weekly:reports/weekly", {"stamp": "s1", "times": 1, "at": "2026-11-19T09:00:00Z"})
    assert "held      " not in status(monkeypatch, root, store).output


def test_records_that_gave_no_facts_are_named_by_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path)
    store = MemoryStore()
    store.set_state("failed:local", ["notes/a.md", "notes/b.md"])
    store.set_state(RETRY, ["tell_0a1f"])
    lines = status(monkeypatch, root, store).output.splitlines()
    at = lines.index("failed    3 records gave no facts on their last try, and each pass tries them again")
    assert lines[at + 1:at + 3] == ["          local: notes/a.md, notes/b.md", "          told: tell_0a1f"]
    assert lines[-1] == CLOSING + "3 records gave no facts on their last try: see the failed line"
    data = json.loads(status(monkeypatch, root, store, "--json").output)
    assert data["failed_records"] == [{"source": "local", "id": "notes/a.md"}, {"source": "local", "id": "notes/b.md"},
                                      {"source": "told", "id": "tell_0a1f"}]


def test_a_spent_cap_leads_status_with_when_it_was_reached_and_when_calls_resume(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, "budget: { daily_usd: 50, compilation_daily_usd: 15 }\n")
    store = MemoryStore()
    spend(store, "2026-11-18T23:00:00Z", "compilation", 40.0)  # yesterday's: another day's cap
    spend(store, "2026-11-19T00:03:00Z", "compilation", 30.0)
    spend(store, "2026-11-19T00:05:00Z", "extraction", 30.0)
    result = status(monkeypatch, root, store)
    lines = result.output.splitlines()
    assert lines[:3] == ["Daily budget: $60.00 of $50.00 spent at 00:05 UTC. Model calls resume at 00:00 UTC.",
                         ("Daily compilation budget: $30.00 of $15.00 spent at 00:03 UTC. Compilation calls resume at "
                          "00:00 UTC."),
                         f"workspace {root}"]
    assert lines[-1] == (CLOSING + "the daily budget is spent: model calls resume at 00:00 UTC, or raise "
                         "budget.daily_usd in settings.yaml (and 1 more warning above)")
    data = json.loads(status(monkeypatch, root, store, "--json").output)
    assert data["caps_reached"] == [
        {"cap": "daily_usd", "layer": "all", "usd": 50.0, "spent": 60.0, "at": "2026-11-19T00:05:00Z",
         "resumes": "2026-11-20T00:00:00Z"},
        {"cap": "compilation_daily_usd", "layer": "compilation", "usd": 15.0, "spent": 30.0,
         "at": "2026-11-19T00:03:00Z", "resumes": "2026-11-20T00:00:00Z"}]


def test_the_cap_line_reads_in_the_workspaces_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, "timezone: America/Los_Angeles\nbudget: { daily_usd: 50 }\n")
    store = MemoryStore()
    spend(store, "2026-11-19T00:05:00Z", "compilation", 52.4)
    assert status(monkeypatch, root, store).output.splitlines()[0] == (
        "Daily budget: $52.40 of $50.00 spent at 16:05 PST. Model calls resume at 16:00 PST.")


MODELS = ("models:\n  extraction: { provider: fireworks, model: glm-5p3-flash }\n  decision: { provider: typesafe, "
          "model: jev-1.13.0 }\n  compilation: { provider: fireworks, model: made-up-8 }\n")
OWN_MODEL = WEEKLY.replace("heartbeat: [change]\n", "heartbeat: [change]\nmodel: { provider: openai, model: made-up-9 }\n")


def test_a_model_with_no_price_under_a_cap_and_an_agents_own_missing_key_are_named(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root = lay_out_workspace(tmp_path, "budget: { daily_usd: 50 }\n" + MODELS, OWN_MODEL)
    result = status(monkeypatch, root, MemoryStore())
    lines = result.output.splitlines()
    assert ("key       agents/reports/weekly.md runs on openai made-up-9, and no key for it is kept or set: run cerebro "
            "secrets set openai, or export OPENAI_API_KEY") in lines
    assert ("price     models.compilation runs on fireworks made-up-8, which the model catalog has no price for, so no "
            "call runs on it while budget.daily_usd is set: pick a priced model, or remove the cap") in lines
    assert ("price     agents/reports/weekly.md runs on openai made-up-9, which the model catalog has no price for, so "
            "no call runs on it while budget.daily_usd is set: pick a priced model, or remove the cap") in lines
    assert lines[-1] == CLOSING + "1 agent lacks a key for its own model: see the key lines (and 1 more warning above)"
    data = json.loads(status(monkeypatch, root, MemoryStore(), "--json").output)
    assert data["agent_keys"] == [{"agent": "reports/weekly", "provider": "openai", "model": "made-up-9",
                                   "variable": "OPENAI_API_KEY"}]
    assert data["unpriced"] == [
        {"who": "models.compilation", "provider": "fireworks", "model": "made-up-8", "layer": "compilation",
         "cap": "daily_usd", "caps": ["daily_usd"]},
        {"who": "agents/reports/weekly.md", "provider": "openai", "model": "made-up-9", "layer": "compilation",
         "cap": "daily_usd", "caps": ["daily_usd"]}]


def test_no_cap_means_no_price_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, MODELS)
    assert "price     " not in status(monkeypatch, root, MemoryStore()).output


def test_requests_and_archived_articles_are_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path)
    store = MemoryStore()
    for result in ("hit", "hit", "hit", "miss"):
        store.append("mcp-requests", {"at": "2026-11-19T09:00:00Z", "tool": "find", "caller": "desk",
                                      "result": result, "surface": "stdio"})
    store.create(Identity("things/gone", "things", "Gone", status="archived"))
    store.create(Identity("things/kept", "things", "Kept"))
    lines = status(monkeypatch, root, store).output.splitlines()
    assert "requests  75% hit (3 of 4), 25% miss (1 of 4)" in lines
    assert "archived  1 article out of the working set" in lines
    data = json.loads(status(monkeypatch, root, store, "--json").output)
    assert data["requests"] == {"total": 4, "hits": 3, "misses": 1} and data["archived"] == 1


def test_the_plain_closing_line_is_the_printers_own(capsys: pytest.CaptureFixture[str]) -> None:
    from cerebro.output import Printer

    for checks in (["one thing: see the failing line"], ["one thing: see the failing line", "another", "a third"]):
        printer = Printer(plain=True)
        for line in checks:
            printer.check(line)
        cli._closing(checks)
        assert capsys.readouterr().err == printer.last_line() + "\n"


def test_every_new_line_in_every_state_says_no_page_section_or_brain(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root = lay_out_workspace(tmp_path, "id: ws-words\nbudget: { daily_usd: 50 }\n" + MODELS, OWN_MODEL)
    store = MemoryStore()
    failed_run(store, "reports/weekly", "2026-11-19T09:59:30Z", "RuntimeError: the provider timed out")
    store.set_state("failing:reports/weekly:reports/weekly", {"stamp": "s1", "times": 2, "at": "2026-11-19T09:59:30Z"})
    store.set_state("failed:local", ["notes/a.md"])
    spend(store, "2026-11-19T00:05:00Z", "compilation", 60.0)
    store.append("mcp-requests", {"at": "2026-11-19T09:00:00Z", "tool": "find", "result": "miss"})
    store.create(Identity("things/gone", "things", "Gone", status="archived"))
    said = status(monkeypatch, root, store).output + status(monkeypatch, root, store, "--json").output
    assert not re.search(r"\b(pages?|sections?|brains?)\b", said, re.IGNORECASE), said


class CountingStore(MemoryStore):
    """A stand-in store that counts each read of the `agent-runs` log."""

    def __init__(self) -> None:
        super().__init__()
        self.runs_read = 0

    def rows(self, log: str, where: dict[str, Any] | None = None, **options: Any) -> Any:
        if log == "agent-runs":
            self.runs_read += 1
        return super().rows(log, where, **options)


def test_status_reads_the_agent_runs_log_once_however_many_agents(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path)
    for n in range(5):
        (root / "agents" / "reports" / f"extra-{n}.md").write_text(WEEKLY)
    store = CountingStore()
    failed_run(store, "reports/weekly", "2026-11-19T09:00:00Z", "RuntimeError: once")
    status(monkeypatch, root, store)
    assert store.runs_read == 1


COMPOSER = "---\nsubagents: [risks]\n---\n\nA weekly.\n\n# Composing\n\nWrite a verdict.\n"
SUBAGENT = "---\nevidence:\n  - query: { from: facts, source: local }\n---\n\n# Instructions\n\nDraft it.\n\n# Output\n\n## Risks\n"


def test_a_subagent_held_on_its_composers_article_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, agent=COMPOSER)
    (root / "agents" / "reports" / "subagents").mkdir()
    (root / "agents" / "reports" / "subagents" / "risks.md").write_text(SUBAGENT)
    store = MemoryStore()
    failed_run(store, "reports/subagents/risks", "2026-11-19T09:59:30Z")
    store.set_state("failing:reports/subagents/risks:reports/weekly",
                    {"stamp": "s1", "times": 1, "at": "2026-11-19T09:59:50Z"})
    lines = status(monkeypatch, root, store).output.splitlines()
    assert ("          reports/subagents/risks on reports/weekly: failed 1 time, runs again after "
            "2026-11-19T10:00:20Z") in lines


DECIDES_ON_RUNE = ("models:\n  extraction: { provider: fireworks, model: glm-5p3-flash }\n  decision: { provider: "
                   "fireworks, model: rune-9 }\n  compilation: { provider: fireworks, model: glm-5p3 }\n")


def test_a_decision_model_with_no_price_is_named_under_the_caps_that_always_stand(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, DECIDES_ON_RUNE)
    data = json.loads(status(monkeypatch, root, MemoryStore(), "--json").output)
    assert data["unpriced"] == [{"who": "models.decision", "provider": "fireworks", "model": "rune-9",
                                 "layer": "evaluation", "cap": "evaluation_daily_usd",
                                 "caps": ["evaluation_daily_usd"]}]
    line = ("price     models.decision runs on fireworks rune-9, which the model catalog has no price for, so no "
            "evaluation call runs on it under budget.evaluation_daily_usd, $2.00 a day, a cap that is always set: "
            "pick a priced model")
    assert line in status(monkeypatch, root, MemoryStore()).output.splitlines()
    rerank = lay_out_workspace(tmp_path / "rerank", DECIDES_ON_RUNE + "search: { rerank: true }\n")
    data = json.loads(status(monkeypatch, rerank, MemoryStore(), "--json").output)
    assert data["rerank"]["on"] and data["unpriced"][0]["cap"] == "evaluation_daily_usd"
    assert data["unpriced"][0]["caps"] == ["rerank_daily_usd", "evaluation_daily_usd"]
    assert line in status(monkeypatch, rerank, MemoryStore()).output.splitlines()  # turning rerank off lifts no eval


def test_one_failed_record_reads_as_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path)
    store = MemoryStore()
    store.set_state("failed:local", ["notes/a.md"])
    lines = status(monkeypatch, root, store).output.splitlines()
    assert "failed    1 record gave no facts on its last try, and each pass tries it again" in lines
    assert lines[-1] == CLOSING + "1 record gave no facts on its last try: see the failed line"


def test_an_agents_own_key_already_named_by_a_job_is_not_named_twice(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root = lay_out_workspace(tmp_path, "models:\n  extraction: { provider: fireworks, model: glm-5p3-flash }\n  decision: { "
                               "provider: typesafe, model: jev-1.13.0 }\n  compilation: { provider: openai, model: "
                               "made-up-9 }\n", OWN_MODEL)
    result = status(monkeypatch, root, MemoryStore())
    assert [line for line in result.output.splitlines() if line.startswith("key       ")] == [
        ("key       OPENAI_API_KEY is not set: keep the key in the workspace's secrets store with cerebro secrets set "
         "openai, or export OPENAI_API_KEY=<your key> in the shell that runs cerebro")]
    assert result.output.splitlines()[-1] == CLOSING + "1 model key not set: see the key lines"


PRICE_CASES = [
    # the budget block, the agent file, the models, and the model with the layers its calls are made on
    ({"daily_usd": 50, "compilation_daily_usd": 15}, OWN_MODEL, "", ("openai", "made-up-9"), ["compilation"]),
    ({"daily_usd": 50}, WEEKLY, DECIDES_ON_RUNE, ("fireworks", "rune-9"),
     ["compilation", "extraction", "evaluation"]),
    ({"daily_usd": 50, "extraction_daily_usd": 5},
     WEEKLY, MODELS.replace("glm-5p3-flash", "made-up-7"), ("fireworks", "made-up-7"), ["extraction"]),
    ({}, WEEKLY, DECIDES_ON_RUNE + "search: { rerank: true }\n", ("fireworks", "rune-9"),
     ["compilation", "extraction", "rerank", "evaluation"]),
]


@pytest.mark.parametrize(("budget", "agent", "models", "model", "layers"), PRICE_CASES)
def test_doing_what_the_price_line_says_lets_the_models_calls_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: dict[str, float], agent: str, models: str,
        model: tuple[str, str], layers: list[str]) -> None:
    """The line names every cap that refuses the model, each of which can be removed, or else a priced model alone:
    after a person does what it says, the ledger refuses none of the model's calls."""
    import yaml

    from cerebro.budget import BudgetSpent, Ledger, read_caps
    from cerebro.models import ModelSpec

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-3")
    root = lay_out_workspace(tmp_path, yaml.safe_dump({"budget": budget}) + models, agent)
    line = next(line for line in status(monkeypatch, root, MemoryStore()).output.splitlines()
                if line.startswith("price     ") and model[1] in line)
    said = line.split(": pick a priced model")[1]
    named = re.findall(r"budget\.(\w+)", line.split(": pick a priced model")[0])
    assert said in ("", ", or remove the cap", ", or remove both caps"), line
    if not said:  # a priced model alone lifts it: the line says why no cap can go
        assert any(key in ("evaluation_daily_usd", "rerank_daily_usd") for key in named), line
        return
    lifted = Ledger(MemoryStore(), read_caps({"budget": {k: v for k, v in budget.items() if k not in named}}))
    for layer in layers:
        try:
            lifted.check(layer, NOW, ModelSpec(*model))
        except BudgetSpent as error:
            pytest.fail(f"{line}\nremoving {named} still refuses {layer}: {error}")


ARTICLE = "---\nevidence:\n  - query: { from: facts, source: local }\nheartbeat: [change]\n---\n\n# Output\n\n## Summary\n"


def test_a_held_run_carries_the_reason_its_run_there_failed_though_a_later_run_elsewhere_did_not(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path)
    (root / "agents" / "things").mkdir()
    (root / "agents" / "things" / "article.md").write_text(ARTICLE)
    store = MemoryStore()
    store.create(Identity("things/x", "things", "X"))
    store.create(Identity("things/y", "things", "Y"))
    store.append("agent-runs", {"run": "run_x", "agent": "things/article", "target": "things/x", "status": "failed",
                                "model": "glm-5p3", "ended_at": "2026-11-19T09:59:40Z",
                                "note": "RuntimeError: the provider timed out on x"})
    store.set_state("failing:things/article:things/x", {"stamp": "s1", "times": 1, "at": "2026-11-19T09:59:40Z"})
    store.append("agent-runs", {"run": "run_y", "agent": "things/article", "target": "things/y", "status": "done",
                                "model": "glm-5p3", "ended_at": "2026-11-19T09:59:50Z"})
    lines = status(monkeypatch, root, store).output.splitlines()
    assert "failing   " not in "\n".join(lines)
    assert ("          things/article on things/x: failed 1 time, runs again after 2026-11-19T10:00:10Z: "
            "RuntimeError: the provider timed out on x") in lines
    data = json.loads(status(monkeypatch, root, store, "--json").output)
    assert data["held_runs"] == [{"agent": "things/article", "article": "things/x", "times": 1,
                                  "at": "2026-11-19T09:59:40Z", "until": "2026-11-19T10:00:10Z",
                                  "reason": "RuntimeError: the provider timed out on x"}]


def test_the_last_line_names_what_to_check_in_order(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Status adds its checks group by group: the stop line, the models' keys, failing, held runs, held records,
    the connectors that need attention and the files that do not read, the price lines with the keys under the same
    row: a missing key is the cause a failing run is the symptom of."""
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    models = ("models:\n  extraction: { provider: fireworks, model: glm-5p3-flash }\n  decision: { provider: typesafe, "
              "model: jev-1.13.0 }\n  compilation: { provider: openai, model: made-up-9 }\n")
    root = lay_out_workspace(tmp_path, "budget: { daily_usd: 50 }\n" + models,
                     WEEKLY.replace("heartbeat: [change]\n", "heartbeat: [change]\nmodel: { provider: anthropic, "
                                                            "model: made-up-6 }\n"))
    (root / "agents" / "reports" / "broken.md").write_text("---\nheartbeat: [nonsense]\n---\n\n# Output\n\n## A\n")
    store = MemoryStore()
    failed_run(store, "reports/weekly", "2026-11-19T09:59:30Z", "RuntimeError: no key")
    store.set_state("failing:reports/weekly:reports/weekly", {"stamp": "s1", "times": 2, "at": "2026-11-19T09:59:30Z"})
    store.set_state("failed:local", ["notes/a.md"])
    spend(store, "2026-11-19T00:05:00Z", "compilation", 60.0)
    data = json.loads(status(monkeypatch, root, store, "--json").output)
    said = [line.split(":")[0] for line in data["check"]]
    assert said == ["the daily budget is spent", "1 model key not set", "1 agent lacks a key for its own model",
                    "2 models have no price while a cap is set", "1 agent failed its last run",
                    "1 agent run held after failing", "1 record gave no facts on its last try",
                    "1 file did not load"], data["check"]


def test_the_last_line_of_status_is_plain_and_asks_the_terminal_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The closing line is written as every other line of `status` is, plain, so no escape byte and no terminal
    query, even on a terminal."""
    import io

    from cerebro import output

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    def no_printer(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("status made a printer, which asks the terminal its colours")

    err = Terminal()
    monkeypatch.setattr("sys.stderr", err)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(output, "Printer", no_printer)
    cli._closing(["1 model key not set: see the key lines", "another"])
    assert err.getvalue() == "[!] Done with warnings. Check: 1 model key not set: see the key lines (and 1 more warning above)\n"
