"""The command line: `init` makes a workspace, each command calls the app object and prints, a refusal is one line,
and no command looks in a folder above the workspace."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from test_thin_path import WEEKLY

import cerebro.trust
from cerebro import Cerebro, cli, scaffold
from cerebro.agent import Agent, Budget
from cerebro.cli import main
from cerebro.evidence import Evidence
from cerebro.fakes import FakeExtraction, FakeSecrets, ScriptedRun
from cerebro.run import AgentRun, Run
from cerebro.shown import shown
from cerebro.trust import trust


def test_serve_once_prints_each_failure_and_exits_1_when_nothing_was_written(tmp_path: Path) -> None:
    (tmp_path / "agents" / "reports").mkdir(parents=True)
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(WEEKLY)
    (tmp_path / "agents" / "reports" / "broken.md").write_text("no front matter")
    (tmp_path / "settings.yaml").write_text("budget: { daily_usd: 20 }\n")
    result = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(tmp_path)])
    assert result.exit_code == 1, result.output
    lines = result.output.splitlines()
    assert any("agents/reports/broken.md" in line for line in lines)
    assert any(line.startswith("failed") and "reports/weekly" in line and "run cerebro secrets set fireworks" in line
               for line in lines)  # no key for its model: refused in one line before any run starts


def test_agents_run_and_compile_without_a_model_key_refuse_in_one_line_before_any_run(tmp_path: Path) -> None:
    (tmp_path / "agents" / "reports").mkdir(parents=True)
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(WEEKLY)
    (tmp_path / "settings.yaml").write_text("{}\n")
    for args in (["agents", "run", "reports/weekly"], ["compile", "--article", "reports/weekly"],
                 ["compile", "--article", "reports"]):
        result = CliRunner().invoke(main, [*args, "--workspace", str(tmp_path)])
        assert result.exit_code == 1 and "Traceback" not in result.output, result.output
        assert result.output.strip().splitlines() == [
            ("Error: agents/reports/weekly.md runs on fireworks glm-5p3, and no key for it is kept or set: run cerebro "
            "secrets set fireworks, or export FIREWORKS_API_KEY")]


def test_a_named_workspace_folder_must_hold_settings_and_is_never_looked_for_above(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "extensions.py").write_text("raise SystemExit('the parent ran')\n")
    (tmp_path / "child").mkdir()
    result = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(tmp_path / "child")])
    assert result.exit_code == 1
    assert "child is not a workspace (no settings.yaml in it)" in result.output
    assert not (tmp_path / ".cerebro").exists()


BROKEN = [  # (settings.yaml, extensions.py, the culprit the one line must name)
    ("compile: { concurrency: lots }\n", "", "compile.concurrency"),
    ("models:\n  compilation: { provider: fireworks }\n", "", "models.extraction"),
    ("models: {}\nmodels: {}\n", "", "models"),
    ("{}\n", "from cerebro import agent_tool\n\n@agent_tool\ndef bare(x: int) -> int:\n    return x\n", "bare"),
]


def test_a_workspace_that_does_not_load_prints_one_line_naming_the_culprit(tmp_path: Path) -> None:
    for n, (settings, extension, culprit) in enumerate(BROKEN):
        root = tmp_path / f"workspace-{n}"
        root.mkdir()
        (root / "settings.yaml").write_text(settings)
        if extension:
            (root / "extensions.py").write_text(extension)
            trust(root)
        result = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
        assert result.exit_code == 1 and len(result.output.strip().splitlines()) == 1, result.output
        assert culprit in result.output and "Traceback" not in result.output


def test_mcp_serves_over_standard_input_and_output(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    result = CliRunner().invoke(main, ["mcp", "--help"])
    assert result.exit_code == 0 and "standard input and output" in result.output
    outside = CliRunner().invoke(main, ["mcp", "--workspace", str(tmp_path / "none")])
    assert outside.exit_code == 1 and "not a workspace" in outside.output


def test_a_bare_serve_runs_the_current_folder_and_never_one_above(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "extensions.py").write_text("raise SystemExit('the parent ran')\n")
    (tmp_path / "child").mkdir()
    monkeypatch.chdir(tmp_path / "child")
    result = CliRunner().invoke(main, ["serve", "--once"])
    assert result.exit_code == 1 and "child is not a workspace" in result.output and "--workspace" in result.output
    monkeypatch.chdir(tmp_path)
    (tmp_path / "extensions.py").unlink()
    loaded = CliRunner().invoke(main, ["serve", "--once"])
    assert loaded.output.splitlines()[0] == f"workspace {tmp_path.resolve()}"


# -- init, then every command, through click's test runner, on stand-ins for the model and the facts layer ----------

NOTE = "Date: 2026-11-10\n\nThe launch review moved to March 3.\n\nVega leads the review.\n"
WEEKLY_AGENT = """\
---
evidence:
  - query: { from: facts, source: local }   # the facts of every note
heartbeat: [manual]
---

Summarize the team's notes.

# Output

## Summary
"""


def _cites_each_fact(prompt: str) -> dict[str, str]:
    facts = re.findall(r"\[\^(fact_\w+)\] ([^(\n]+)", prompt)
    return {"Summary": " ".join(f"{text.strip()} [^{fid}]" for fid, text in facts) or "Nothing yet."}


class EachHeading(AgentRun):
    """The scripted model, writing each heading the agent's `# Output` names, each citing every fact it was given;
    the accounts curator admits Acme Corp, naming the proposals of its list it carries out."""

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        headings = agent.output or ["Summary"]
        yes = [item.key.removeprefix("proposal:") for entry in evidence for item in entry.items
               if item.key.startswith("proposal:")]
        admit = [("admit", {"name": "Acme Corp", "keys": {"domain": ["acme.example"]}, "reason": "a note names it"}
                  | ({"proposals": yes} if yes else {}))]
        write = ScriptedRun(lambda prompt: {h: _cites_each_fact(prompt)["Summary"] for h in headings},
                            calls=admit if agent.id == "accounts/curator" else [])
        return write.run(agent, evidence, tools, budget)


@pytest.fixture
def stand_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every command's app object runs the scripted model and the stand-in facts layer."""
    extraction = FakeExtraction()  # one facts layer for every command, as one workspace's would be
    now = lambda: datetime(2026, 11, 18, 9, tzinfo=UTC)
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, agent_run=EachHeading(),
                                                                extraction=extraction, clock=now))
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)


def run(*args: str) -> Result:
    return CliRunner().invoke(main, list(args))


def test_serve_once_with_every_agent_file_refused_says_the_refusals_and_not_to_write_one(tmp_path: Path,
                                                                                         stand_ins: None) -> None:
    workspace = tmp_path / "workspace"
    assert run("init", str(workspace), "--daily-usd", "1").exit_code == 0
    (workspace / "agents" / "reports").mkdir(parents=True)
    (workspace / "agents" / "reports" / "weekly.md").write_text("---\ntools: [qurey]\n---\n\nWrite it.\n")
    passed = run("serve", "--once", "--workspace", str(workspace))
    assert "refused   agents/reports/weekly.md: tool qurey" in passed.output, passed.output
    assert "write one" not in passed.output


def test_init_says_a_key_set_blank_in_the_environment_is_not_set(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.models import DEFAULTS
    from cerebro.secrets import model_key

    monkeypatch.setenv("FIREWORKS_API_KEY", "  ")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key")
    made = run("init", str(tmp_path / "workspace"), "--daily-usd", "1")
    keys = [line for line in made.output.splitlines() if line.startswith("key ")]
    assert made.exit_code == 0 and len(keys) == 1 and keys[0].startswith("key       FIREWORKS_API_KEY is not set")
    stored = FakeSecrets({"fireworks": " "})  # a blank key kept in the store hides no key the environment holds
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-test-key")
    assert model_key(stored, DEFAULTS["compilation"])() == "fw-test-key"


@pytest.mark.parametrize("template", [None, "gtm"])
def test_init_then_a_note_then_serve_once_then_status(tmp_path: Path, stand_ins: None, template: str | None) -> None:
    workspace = tmp_path / "team-workspace"
    made = run("init", str(workspace), "--daily-usd", "20", *(["--template", template] if template else []))
    assert made.exit_code == 0, made.output
    lines = made.output.splitlines()
    assert lines[0] == f"workspace {workspace.resolve()}"
    assert any(line.startswith("key       FIREWORKS_API_KEY is not set: keep the key in the workspace's secrets store with "
                               "cerebro secrets set fireworks") for line in lines)
    assert "cerebro-team-workspace -- " in made.output and f"mcp --workspace {workspace.resolve()}" in made.output
    if template is None:
        commands = ["cerebro agents new <collection>/<name>", "cerebro init --template gtm", "cerebro connect <name>"]
        assert all(line.startswith("next      ") for line in lines[-3:])
        assert [command in line for command, line in zip(commands, lines[-3:], strict=True)] == [True] * 3
        assert f"--workspace {workspace.resolve()}" in lines[-3] and f"--workspace {workspace.resolve()}" in lines[-1]
    else:
        assert lines[-2].startswith("next      ") and f"cerebro serve --once --workspace {workspace.resolve()}" in lines[-2]
        assert "cerebro connect <name>" in lines[-1]
    (workspace / "local" / "kickoff.md").write_text("Domain: acme.example\n" + NOTE)
    passed = run("serve", "--once", "--yes", "--workspace", str(workspace))
    assert "listed    local: 1 record\n" in passed.output and "facts     1 record read, 2 facts extracted" in passed.output
    status = run("status", "--workspace", str(workspace))
    assert status.exit_code == 0, status.output
    assert re.search(r"^spent     today \$\d+\.\d+ of \$20\.00: extraction \$\d+\.\d+, compilation \$\d+\.\d+$",
                     status.output, re.MULTILINE), status.output  # no layer says "of no cap" under the one cap
    assert "facts     2 current, from 1 record: 2 from local\n" in status.output  # what the pass made, said plainly
    if template is None:
        assert "agents    none: write one with cerebro agents new <collection>/<name>" in passed.output
        assert passed.exit_code == 0 and passed.output.splitlines()[-1] == "Done."
        assert "agents    0 loaded" in status.output and "articles  none yet" in status.output
        assert "working   0 articles in the working set" in status.output
    else:
        assert passed.exit_code == 0, passed.output
        assert "wrote     accounts/acme-corp  articles/accounts/acme-corp.md" in passed.output
        assert "Vega leads the review" in (workspace / "articles" / "accounts" / "acme-corp.md").read_text()
        # each fixed article that was due is born before its first run, and holds nothing until a run lands a part
        assert re.search(r"^ran       accounts/subagents/overview: done at \S+Z on ", status.output, re.MULTILINE), status.output
        # the report is written too: its stand-in writes a line though it has nothing to cite
        assert "agents    29 loaded" in status.output and "articles  accounts: 1, digests: 1, reports: 1\n" in status.output
        assert "pending" not in status.output and (workspace / "articles" / "reports" / "pipeline.md").exists()
        index = (workspace / "articles" / "digests" / "index.md").read_text()
        assert "](../accounts/)" in index
        assert "working   3 articles in the working set" in status.output


def test_a_new_gtm_workspaces_first_serve_once_off_a_terminal_prints_the_list_writes_nothing_and_exits_2(
        tmp_path: Path, stand_ins: None) -> None:
    root = tmp_path / "team-wiki"
    assert run("init", str(root), "--template", "gtm").exit_code == 0
    (root / "local" / "kickoff.md").write_text("Domain: acme.example\n" + NOTE)
    waits = run("serve", "--once", "--workspace", str(root))
    assert waits.exit_code == 2, waits.output
    assert "Working set proposal" in waits.output and "    [ ] Acme Corp  accounts" in waits.output.splitlines()
    assert waits.output.splitlines()[-1].startswith("waiting   the first run's list: 1 admit proposal awaiting review")
    with cli.open_workspace(root) as workspace:
        assert not any(workspace.store.query(c) for c in workspace.store.collections())  # no article written or admitted
        waiting = workspace.review()
        assert waiting and {item.proposal.status for item in waiting} == {"waiting"}  # admitted nothing
        curators = {a.id for a in workspace.agents if a.role == "curator"}
        assert {row.data["agent"] for row in workspace.store.rows("agent-runs")} <= curators  # nothing spent on writing
    assert not (root / "articles" / "accounts").exists()


def test_each_command_runs_on_a_new_workspace(tmp_path: Path, stand_ins: None) -> None:
    workspace = str(tmp_path)
    assert run("init", "--workspace", workspace, "--daily-usd", "5").exit_code == 0
    (tmp_path / "local" / "kickoff.md").write_text(NOTE)
    new = run("agents", "new", "reports/weekly", "--workspace", workspace)
    assert new.output.splitlines() == [
        "wrote    agents/reports/weekly.md",
        "next     fill in agents/reports/weekly.md, then cerebro agents run reports/weekly --show-prompt"]
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(WEEKLY_AGENT)
    assert run("serve", "--once", "--workspace", workspace).exit_code == 0  # extracts the note; the manual agent waits
    distilled = run("distill", "--workspace", workspace)
    assert distilled.exit_code == 0 and "agents    none" not in distilled.output  # it runs none, and has one

    ran = run("agents", "run", "reports/weekly", "--workspace", workspace)
    unscored = "grounding reports/weekly: left 2 sentences unscored"  # it has no decision model
    assert ran.output.splitlines() == ["wrote    reports/weekly  articles/reports/weekly.md", unscored], ran.output
    again = run("compile", "--article", "reports/weekly", "--workspace", workspace)  # a rebuild now runs whatever moved
    assert again.output.splitlines() == ["wrote    reports/weekly  articles/reports/weekly.md", unscored], again.output

    read = json.loads(run("tools", "read", "--json", '{"ids": ["reports/weekly"]}', "--workspace", workspace).output)
    summary = read["articles"][0]["text"]
    assert "The launch review moved to March 3." in summary and "[^fact_" in summary
    query = json.loads(run("tools", "query", "--json", '{"from": "reports"}', "--workspace", workspace).output)
    assert query["count"] == 1

    listed = json.loads(run("agents", "list", "--json", "--workspace", workspace).output)
    assert listed["agents"] == [{"id": "reports/weekly", "role": "writer", "target": "reports/weekly",
                                 "heartbeat": ["manual"], "path": "agents/reports/weekly.md", "last_run": "done"}]
    said = run("agents", "list", "--workspace", workspace).output.splitlines()
    assert said == ["reports/weekly  writer of reports/weekly  heartbeat manual  last run done", "1 agent, 0 refused."]
    status = json.loads(run("status", "--json", "--workspace", workspace).output)
    assert status["articles"] == {"reports": 1} and status["sources"] == ["local"]
    assert status["caps"]["total"] == 5 and status["spent_today"]["compilation"] >= 0
    assert status["missing_keys"] == ["FIREWORKS_API_KEY", "TYPESAFE_API_KEY"]  # the default decision model is Jev
    shown = run("status", "--workspace", workspace).output
    assert "articles  reports: 1" in shown and "FIREWORKS_API_KEY is not set" in shown


def test_tell_decide_and_retract_run_through_tools(tmp_path: Path, stand_ins: None) -> None:
    run("init", str(tmp_path), "--daily-usd", "20")
    workspace = ["--workspace", str(tmp_path)]
    told = json.loads(run("tools", "tell", "--json", '{"text": "The review moved.", "as": "fact"}', *workspace).output)
    assert told["record"].startswith("r_") and len(told["facts"]) == 1
    decided = json.loads(run("tools", "decide", "--json", '{"accept": ["p_1"]}', *workspace).output)
    assert decided["answers"] == {"p_1": "unknown"}
    taken = json.loads(run("tools", "retract", "--json", json.dumps({"id": told["record"]}), *workspace).output)
    assert taken["withdrew"] == [told["record"], *told["facts"]]
    refused = run("tools", "retract", "--json", '{"id": "r_none"}', *workspace)
    assert refused.exit_code == 1 and len(refused.output.strip().splitlines()) == 1 and "nothing has the id" in \
        refused.output


def test_a_command_given_what_it_cannot_use_says_what_to_give_instead(tmp_path: Path, stand_ins: None) -> None:
    run("init", str(tmp_path), "--daily-usd", "20")
    workspace = ["--workspace", str(tmp_path)]
    for args, said in [(["agents", "run", "reports/none"], "there is no agent reports/none"),
                       (["compile", "--article", "reports/none"], "no agent writes reports/none"),
                       (["tools", "read", "--json", "{ids"], "--json is not JSON"),
                       (["tools", "find", "--json", '{"who": "Vega"}'], 'find: unknown field "who"'),
                       (["tools", "search", "--json", "{}"], "there is no tool search"),
                       (["agents", "new", "reports"], "has no folder"),
                       (["init", "--daily-usd", "5"], "already a workspace")]:  # an answer to a question it asked
        refused = run(*args, *workspace)
        assert refused.exit_code == 1 and said in refused.output, (args, refused.output)
        assert len(refused.output.strip().splitlines()) == 1


def test_init_asks_only_at_a_terminal_and_takes_every_answer_as_a_flag(tmp_path: Path, stand_ins: None,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    flags = run("init", str(tmp_path / "a"), "--no-cap", "--timezone", "Europe/Paris", "--domain", "example.com")
    assert flags.exit_code == 0 and "?" not in flags.output
    assert "budget:" not in (tmp_path / "a" / "settings.yaml").read_text()
    monkeypatch.setattr(cli, "_asking", lambda: True)
    asked = CliRunner().invoke(main, ["init", str(tmp_path / "b")],
                               input="\n\n\nexample.org\n7\nAsia/Tokyo\n")  # name, email, company, domain, budget, zone
    assert asked.exit_code == 0, asked.output
    settings = (tmp_path / "b" / "settings.yaml").read_text()
    assert "daily_usd: 7 " in settings and "timezone: Asia/Tokyo" in settings
    assert "domains: [example.org]" in (tmp_path / "b" / "workspace.md").read_text()


def test_prompts_list_and_eject_run_from_the_command_line(tmp_path: Path, stand_ins: None,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / "rules.md").write_text("Cite every claim.\n")
    monkeypatch.setattr(scaffold, "PROMPTS", tmp_path / "bundle")
    workspace = ["--workspace", str(tmp_path / "workspace")]
    run("init", *workspace, "--daily-usd", "20")
    assert run("prompts", "list", *workspace).output.splitlines() == ["rules  bundled", "1 prompts, 0 ejected."]
    assert run("prompts", "eject", "rules", *workspace).output.splitlines()[0] == "wrote    prompts/rules.md"
    listed = json.loads(run("prompts", "list", "--json", *workspace).output)
    assert listed == [{"name": "rules", "ejected": True, "bundled": True, "path": "prompts/rules.md"}]
    again = run("prompts", "eject", "rules", *workspace)
    assert again.exit_code == 1 and "already exists" in again.output


def test_serve_runs_passes_beside_the_mcp_server_and_stops_them_when_it_ends(tmp_path: Path, stand_ins: None,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from cerebro import mcp

    run("init", str(tmp_path), "--daily-usd", "20")
    passed, printed = threading.Event(), cli._print
    monkeypatch.setattr(cli, "_print", lambda result, said=None: printed(result, said) or passed.set())
    monkeypatch.setattr(mcp, "serve", lambda workspace, port, host, allowed, **k: passed.wait(5))  # once a pass ran
    from test_tokens import taken_port

    taken_port(monkeypatch)
    served = run("serve", "--workspace", str(tmp_path))
    assert served.exit_code == 0, served.output
    assert passed.is_set() and "a pass every 30 seconds" in served.output and "Done." in served.output.splitlines()


def test_a_new_workspace_keeps_a_key_at_once_and_init_and_status_name_every_key_a_pass_needs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.facts import FACTOID_DECISION

    class AsFactoid(FakeExtraction):
        def needs(self) -> list[Any]:
            return [FACTOID_DECISION]  # Factoid's own decision step, on Typesafe

    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, agent_run=EachHeading(), extraction=AsFactoid()))
    workspace = tmp_path / "launch"
    made = run("init", str(workspace), "--daily-usd", "20")
    assert re.search(r"^id: launch-[0-9a-f]{6}\b", (workspace / "settings.yaml").read_text(), re.MULTILINE)
    rule = "is not set: keep the key in the workspace's secrets store with cerebro secrets set"
    assert f"key       FIREWORKS_API_KEY {rule} fireworks" in made.output
    assert f"key       TYPESAFE_API_KEY {rule} typesafe" in made.output  # one rule for every key
    for name, value in (("fireworks", "fw-test"), ("typesafe", "ts-test")):
        kept = CliRunner().invoke(main, ["secrets", "set", name, "--workspace", str(workspace)], input=f"{value}\n")
        assert kept.exit_code == 0 and kept.output.endswith(f"kept     {name} in the workspace's secrets store\n")
    status = run("status", "--workspace", str(workspace)).output
    assert "key       " not in status, status


def test_connect_keeps_a_login_given_on_standard_input_and_off_a_terminal_asks_nothing_and_exits_2(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    gong = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200 if request.url.path in ("/v2/workspaces", "/v2/calls") else 404, json={"records": {"totalRecords": 0}})))  # a stand-in
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, agent_run=EachHeading(),
                                                                extraction=FakeExtraction(), http=gong))
    workspace = tmp_path / "workspace"
    assert run("init", str(workspace), "--daily-usd", "20").exit_code == 0
    (workspace / "sources" / "calls.yaml").write_text("kind: gong\n")
    status = run("status", "--workspace", str(workspace)).output
    assert "login     sources/calls.yaml needs a login, and none is kept: run cerebro connect calls\n" in status
    asked = run("connect", "calls", "--workspace", str(workspace))
    assert asked.exit_code == 2 and "input is not a terminal, so nothing is asked: pass the gong access key and its " \
        "secret with --stdin or --from-env NAME" in asked.output
    kept = CliRunner().invoke(main, ["connect", "calls", "--stdin", "--workspace", str(workspace)], input="key-test:secret-test\n")
    assert kept.exit_code == 0 and kept.output.splitlines()[0] == "[ok] Gong connection verified", kept.output
    assert kept.output.splitlines()[1].startswith("Connected to Gong. The key is kept under gong in ")
    assert "secret-test" not in kept.output
    assert "login     " not in run("status", "--workspace", str(workspace)).output


def test_a_days_spend_reads_as_parts_that_add_up_to_its_total() -> None:
    from cerebro.cli import _digits, _money

    for extraction, compilation in ((0.0047, 0.0056), (0.005, 0.005), (1.234, 2.345), (0.0, 0.0), (0.00004, 0.0)):
        digits = _digits(extraction + compilation, [extraction, compilation])
        parts = [float(_money(v, digits)[1:]) for v in (extraction, compilation)]
        assert round(sum(parts), digits) == float(_money(extraction + compilation, digits)[1:]), (extraction, digits)
    assert _digits(0.0047 + 0.0056, [0.0047, 0.0056]) == 4 and _digits(1.25, [1.0, 0.25]) == 2


def test_eval_runs_a_suite_on_the_workspace_and_prints_its_scores(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    corpus = Path(__file__).parent / "data" / "ground" / "corpus.json"
    ran = run("eval", str(corpus), "--no-model", "--group", "rows", "--workspace", str(tmp_path))
    assert ran.exit_code == 0, ran.output
    assert ran.output.startswith("corpus: ground, ") and "judge " in ran.output.splitlines()[0]
    refused = run("eval", str(corpus), "--cap", "0", "--workspace", str(tmp_path))
    assert refused.exit_code == 1 and "cap 0.0 is no amount above zero" in refused.output


def test_serve_prints_what_the_workspace_said_when_it_loaded_once_and_not_at_every_pass(capsys) -> None:
    from cerebro.cli import _print
    from cerebro.runner import PassResult

    said: set[str] = set()
    loaded = "sources/warehouse: a database source lists no records, so a pass does not list it"
    _print(PassResult(notices=[loaded], refused=["agents/x.md: bad"]), said)
    _print(PassResult(notices=[loaded, "sources/a: its cursor was cleared"], refused=["agents/x.md: bad"]), said)
    out = capsys.readouterr().out
    assert out.count(loaded) == 1 and out.count("agents/x.md: bad") == 1 and "its cursor was cleared" in out


@pytest.mark.untrusted
def test_an_untrusted_workspace_with_code_runs_none_of_it_and_status_says_what_trusting_would_allow(
        tmp_path: Path) -> None:
    root = tmp_path / "cloned"
    (root / "facts").mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "extensions.py").write_text("raise SystemExit('ran')\n")
    (root / "facts" / "risk.py").write_text("raise SystemExit('ran')\n")
    refused = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    assert refused.exit_code == 1 and refused.output == (
        f"Error: {root} is not a workspace you trust, so cerebro runs nothing of it: cerebro status --workspace {root} "
        f"lists what trusting it would allow, and cerebro trust {root} trusts it\n")
    status = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert status.exit_code == 0 and status.output.splitlines() == [
        f"workspace {root}",
        f"trust     not trusted, so cerebro runs nothing of it: cerebro trust {root} would allow this",
        "run       extensions.py", "run       facts/risk.py", f"store     class sqlite, {root}/.cerebro/cerebro.sqlite",
        "agents    0 agent files, each instructions to a model"]
    trusted = CliRunner().invoke(main, ["trust", str(root)])
    assert trusted.output.splitlines() == status.output.splitlines()[2:] + [f"trusted   {root}"]
    loaded = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    assert loaded.exit_code == 1 and loaded.output == "ran\n"  # trusted, its extensions.py now runs as it loads
    (root / "extensions.py").unlink()
    (root / "extensions.py").symlink_to(tmp_path / "elsewhere.py")
    (tmp_path / "elsewhere.py").write_text("")
    linked = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    assert "extensions.py in" in linked.output and "is a link that leads outside the workspace's folder" in linked.output


@pytest.mark.untrusted
def test_an_untrusted_workspace_with_no_code_is_refused_too_and_only_status_trust_init_and_help_work(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cloned"
    (root / "agents" / "notes").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "scripts").mkdir()
    (root / "settings.yaml").write_text("store:\n  class: not_a_module_anywhere:Store\n")
    (root / "scripts" / "hello.sh").write_text(f"#!/bin/sh\ntouch {tmp_path / 'SCRIPT_RAN'}\necho a line\n")
    (root / "scripts" / "hello.sh").chmod(0o755)
    (root / "agents" / "notes" / "digest.md").write_text(
        "---\nevidence:\n  - script: { run: scripts/hello.sh }\nheartbeat: [change]\n---\n# Instructions\nSay it.\n"
        "# Output\n## Digest\n")
    (root / "sources" / "warehouse.yaml").write_text(
        f"kind: mcp\ncommand: sh -c \"touch {tmp_path / 'COMMAND_RAN'}\"\nextract: records\ninto: accounts\n")
    (root / "sources" / "crm.yaml").write_text("kind: database\nurl: postgresql://reader:hunter2@db.example/crm\n")
    (root / "sources" / "local.yaml").write_text("kind: files\nselect:\n  - glob: ../../**/*.md\n")
    started: list[Any] = []
    monkeypatch.setattr("cerebro.app.start", lambda launch: started.append(launch))
    line = (f"Error: {root} is not a workspace you trust, so cerebro runs nothing of it: cerebro status --workspace "
            f"{root} lists what trusting it would allow, and cerebro trust {root} trusts it")
    for command in (["serve", "--once"], ["serve"], ["agents", "run", "notes/digest"], ["mcp"], ["tokens", "new", "a"],
                    ["distill"], ["agents", "list"], ["agents", "new", "notes/other"], ["sources", "new", "more"],
                    ["prompts", "list"], ["prompts", "eject", "tell-sort"]):
        refused = CliRunner().invoke(main, [*command, "--workspace", str(root)])
        assert refused.exit_code == 1 and refused.output.strip() == line, (command, refused.output)
    with pytest.raises(cerebro.trust.UntrustedWorkspace, match="is not a workspace you trust"):
        Cerebro(root)
    command = shown(f"sh -c \"touch {tmp_path / 'COMMAND_RAN'}\"", cerebro.trust.CAP)  # from a file, so capped
    status = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert status.exit_code == 0 and status.output.splitlines()[2:] == [
        "run       scripts/hello.sh, the script agents/notes/digest.md runs",
        f"run       {command}, the command sources/warehouse.yaml starts",
        "read      sources/crm.yaml: kind database, url postgresql://db.example/crm",
        "read      sources/local.yaml: kind files, files ../../**/*.md",
        f"read      sources/warehouse.yaml: kind mcp, command {command}",
        "store     class not_a_module_anywhere:Store", "agents    1 agent file, each instructions to a model"]
    assert "hunter2" not in status.output and CliRunner().invoke(main, ["--help"]).exit_code == 0
    assert started == [] and not (tmp_path / "SCRIPT_RAN").exists() and not (tmp_path / "COMMAND_RAN").exists()
    assert not (root / ".cerebro").exists()
    (root / "settings.yaml").write_text("{}\n")  # trust opens the workspace's store, as its settings name it
    assert CliRunner().invoke(main, ["trust", str(root)]).output.splitlines()[-1] == f"trusted   {root}"
    with Cerebro(root) as workspace:
        assert [agent.id for agent in workspace.agents] == ["notes/digest"]
    made = tmp_path / "made"
    assert CliRunner().invoke(main, ["init", str(made), "--daily-usd", "1"]).exit_code == 0
    assert CliRunner().invoke(main, ["agents", "list", "--workspace", str(made)]).exit_code == 0


PREFIXES = ("workspace ", "trust ", "run ", "read ", "store ", "agents ", "trusted ", "Error: ", "notice ", "sources ",
            "checked ", "facts ", "articles ", "working ", "spent ", "key ", "refused ", "0 agents", "1 agent",
            "notes/a")


@pytest.mark.untrusted
@pytest.mark.parametrize("payload", ["\x1b[2K\rtrusted   ok", "x\rtrusted   ok", "x\ntrusted   ok", "y" * 5000],
                         ids=["escape", "return", "newline", "long"])
def test_a_workspaces_own_text_prints_with_its_control_characters_shown_and_its_length_capped(
        tmp_path: Path, payload: str) -> None:
    root = tmp_path / "hostile"
    (root / "agents" / "notes").mkdir(parents=True)
    (root / "sources").mkdir()
    named = payload[:150]  # a file name holds at most 255 bytes
    (root / "settings.yaml").write_text(json.dumps({"store": {"path": f".cerebro/{named}.sqlite"}}) + "\n")
    (root / "sources" / "local.yaml").write_text(json.dumps({"kind": payload, "url": f"http://{payload}"}) + "\n")
    (root / "agents" / "notes" / f"a{named}.md").write_text(
        "---\n" + json.dumps({"evidence": [{"script": {"run": f"scripts/{payload}"}}]}) + "\n---\n# Output\n## A\n")
    printed = [CliRunner().invoke(main, [*command, "--workspace", str(root)]).output
               for command in (["status"], ["serve", "--once"])]
    printed.append(CliRunner().invoke(main, ["trust", str(root)]).output)
    (root / "settings.yaml").write_text(json.dumps({"collections": {payload: {"description": "x", "other": 1}}}))
    printed.append(CliRunner().invoke(main, ["trust", str(root)]).output)
    printed += [CliRunner().invoke(main, [*command, "--workspace", str(root)]).output
                for command in (["status"], ["agents", "list"])]
    (root / "settings.yaml").write_text("{}\n")
    printed.append(CliRunner().invoke(main, ["agents", "list", "--workspace", str(root)]).output)
    for output in printed:
        assert "\x1b" not in output and "\r" not in output, output
        for line in output.splitlines():
            assert line.startswith(PREFIXES), line[:200]
            assert "trusted   ok" not in line[:12] and len(line) < 4200
    assert printed[4].startswith("Error: settings.yaml") and printed[-1].startswith(("refused  agents/notes/a", "notes/a"))
    assert any("\\x1b" in o or "\\r" in o or "\\n" in o or "characters)" in o for o in printed)


@pytest.mark.untrusted
@pytest.mark.parametrize("held", ["extensions.py", "facts/risk.py", "agents/notes/a.md", "sources/crm.yaml"])
def test_init_trusts_only_what_it_makes_and_lists_what_the_folder_held_before(tmp_path: Path, held: str) -> None:
    root = tmp_path / "cloned"
    (root / held).parent.mkdir(parents=True)
    (root / held).write_text(f"open({str(tmp_path / 'RAN')!r}, 'w')\n" if held.endswith(".py") else
                             "kind: database\nurl: postgres\n" if held.endswith(".yaml") else "# Output\n## A\n")
    made = CliRunner().invoke(main, ["init", str(root), "--daily-usd", "1"])
    lines = made.output.splitlines()
    assert made.exit_code == 0 and not cerebro.trust.trusted(root) and not (tmp_path / "RAN").exists()
    assert any(line.startswith("trust     not trusted: this folder held what is listed above")
               and line.endswith(f"run cerebro trust {root}") for line in lines)
    assert lines[1].startswith(("run ", "read ", "agents "))
    fresh = tmp_path / "fresh"
    (fresh / "local").mkdir(parents=True)
    (fresh / "local" / "note.md").write_text("A note.\n")  # notes are what a workspace reads, and act on nothing
    assert CliRunner().invoke(main, ["init", str(fresh), "--daily-usd", "1"]).exit_code == 0
    assert cerebro.trust.trusted(fresh)


def test_a_sources_name_in_the_cursor_lines_of_status_prints_with_its_control_characters_shown(
        tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    name = "notes\x1b[2K\rcursor    forged"
    (root / "sources" / f"{name}.yaml").write_text("kind: files\nselect:\n  - glob: \"local/**/*.md\"\n")
    (root / "sources" / "bad\x1b[2K.yaml").write_text("kind: files\nfull_every: often\n")
    printed = CliRunner().invoke(main, ["status", "--workspace", str(root)]).output
    as_json = CliRunner().invoke(main, ["status", "--json", "--workspace", str(root)]).output
    assert "\x1b" not in printed and "\r" not in printed and "\x1b" not in as_json
    assert "cursor    notes\\x1b[2K\\rcursor    forged: " in printed
    assert not any(line.startswith("cursor    forged") for line in printed.splitlines())


@pytest.mark.untrusted
def test_a_workspace_at_a_long_path_shows_it_whole_and_the_trust_command_it_prints_trusts_it(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    while len(str(root)) <= 300:  # no longer: SQLite opens no file whose path passes 512 bytes
        root = root / "deep-folder"
    root.mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "extensions.py").write_text("")
    refused = run("serve", "--once", "--workspace", str(root))
    assert refused.exit_code == 1 and refused.output == f"Error: {cerebro.trust.refusal(root)}\n"
    lines = run("status", "--workspace", str(root)).output.splitlines()
    assert lines[0] == f"workspace {root}" and f"store     class sqlite, {root}/.cerebro/cerebro.sqlite" in lines
    command = re.fullmatch(r"trust     not trusted, so cerebro runs nothing of it: cerebro (trust .+) would allow this",
                           lines[1])
    assert command is not None
    assert run(*shlex.split(command.group(1))).exit_code == 0 and cerebro.trust.trusted(root)
    assert run("status", "--workspace", str(root)).output.splitlines()[0] == f"workspace {root}"


@pytest.mark.untrusted
def test_a_folder_name_with_a_space_and_an_apostrophe_prints_a_trust_command_that_pastes_whole(tmp_path: Path) -> None:
    root = tmp_path / "Application Support" / "Ann's workspace"
    root.mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "extensions.py").write_text("")
    quoted = shlex.quote(str(root))
    refused = run("serve", "--once", "--workspace", str(root)).output
    assert refused == (f"Error: {root} is not a workspace you trust, so cerebro runs nothing of it: cerebro status "
                       f"--workspace {quoted} lists what trusting it would allow, and cerebro trust {quoted} trusts it\n")
    lines = run("status", "--workspace", str(root)).output.splitlines()
    assert lines[0] == f"workspace {root}"
    command = re.fullmatch(r"trust     not trusted, so cerebro runs nothing of it: cerebro (trust .+) would allow this",
                           lines[1])
    assert command is not None and shlex.split(command.group(1)) == ["trust", str(root)]
    assert run(*shlex.split(command.group(1))).exit_code == 0 and cerebro.trust.trusted(root)


SHELLS = [shell for shell in ("bash", "zsh") if shutil.which(shell)]


def read_by(shell: str, word: str) -> bytes:
    """The one word a shell reads from `word`, as bytes, in a UTF-8 locale."""
    done = subprocess.run([shell, "-c", f'printf "%s" {word}'], capture_output=True, check=True,
                          env={"LC_ALL": "C.UTF-8", "PATH": os.environ.get("PATH", "")})
    return done.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_a_printed_command_names_a_folder_with_a_control_or_hidden_character_byte_for_byte(
        tmp_path: Path, shell: str) -> None:
    from cerebro.shown import CONTROL, shell_path

    names = ["desk\nwiki", "desk\twiki", "a\x1b[2Jb", "del\x7fhere", "c1\x85x", "zero\u200bwidth",
             "flip\u202eright", "Ann's \\ desk\n", "plain'\\", "café wiki"]
    for name in names:
        path = tmp_path / name
        word = shell_path(path)
        assert "\n" not in word and not CONTROL.search(word)
        assert read_by(shell, word) == str(path).encode()
        if not CONTROL.search(name):
            assert word == shlex.quote(str(path))
    folder = tmp_path / "desk\nwiki"
    made = run("init", str(folder), "--daily-usd", "1")
    words = [line.split("--workspace ", 1)[1] for line in made.output.splitlines() if "--workspace " in line]
    assert words and all(read_by(shell, w) == str(folder.resolve()).encode() for w in words)


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
@pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
def test_a_hidden_character_in_a_printed_path_is_written_as_its_utf8_bytes_and_reads_back_in_any_locale(
        locale: str) -> None:
    from cerebro.shown import shell_path

    for name, escaped in [("desk\nwiki", "\\n"), ("desk\twiki", "\\t"), ("a\x01b", "\\x01"),
                          ("zero\u200bwidth", "\\xe2\\x80\\x8b"), ("flip\u202eright", "\\xe2\\x80\\xae"),
                          ("c1\x85x", "\\xc2\\x85")]:
        path = f"/tmp/{name}"
        word = shell_path(path)
        assert escaped in word and "\\u" not in word, word
        done = subprocess.run(["bash", "-c", f"printf %s {word}"], capture_output=True, check=True,
                              env={"LC_ALL": locale, "PATH": os.environ.get("PATH", "")})
        assert done.stdout == path.encode(), (locale, word, done.stdout)
    assert [shell_path(p) for p in ("/tmp/plain desk", "/tmp/café")] == [shlex.quote("/tmp/plain desk"),
                                                                         shlex.quote("/tmp/café")]


@pytest.mark.untrusted
def test_a_store_path_a_workspaces_settings_give_is_cut_in_status(tmp_path: Path) -> None:
    root = tmp_path / "cloned"
    root.mkdir()
    given = "a/" * 5000
    (root / "settings.yaml").write_text(f"store:\n  path: {given}\n")
    store = [line for line in run("status", "--workspace", str(root)).output.splitlines() if line.startswith("store ")]
    whole = len(str(root / given))
    assert len(store) == 1 and store[0].endswith(f"… ({whole} characters)") and len(store[0]) < 4100


@pytest.mark.untrusted
def test_a_control_character_in_a_workspaces_folder_name_prints_as_an_escape(tmp_path: Path) -> None:
    root = tmp_path / "cloned\x1b[31mred"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    escaped = str(root).replace("\x1b", "\\x1b")
    refused = run("serve", "--once", "--workspace", str(root)).output
    status = run("status", "--workspace", str(root)).output
    trusted = run("trust", str(root)).output
    for printed in (refused, status, trusted):
        assert "\x1b" not in printed and escaped in printed


@pytest.mark.untrusted
def test_a_long_command_in_a_workspaces_source_file_is_still_cut_in_status(tmp_path: Path) -> None:
    root = tmp_path / "cloned"
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "sources" / "warehouse.yaml").write_text(f"kind: mcp\ncommand: echo {'a' * 1000}\nextract: records\n")
    lines = [line for line in run("status", "--workspace", str(root)).output.splitlines() if "echo" in line]
    assert len(lines) == 2 and all("… (1005 characters)" in line and len(line) < 400 for line in lines)


@pytest.mark.parametrize("git", ["folder", "file"])
def test_init_inside_a_git_working_tree_makes_the_workspace_and_says_it_usually_lives_apart(
        tmp_path: Path, git: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    checkout = tmp_path / "checkout"
    (checkout / "py").mkdir(parents=True)
    if git == "folder":
        (checkout / ".git").mkdir()
    else:  # a worktree's or a submodule's .git is a file
        (checkout / ".git").write_text("gitdir: elsewhere\n")
    made = run("init", str(checkout / "py" / "notes-workspace"), "--daily-usd", "20")
    assert made.exit_code == 0, made.output
    assert (checkout / "py" / "notes-workspace" / "settings.yaml").is_file()
    notices = [line for line in made.output.splitlines() if "git repository" in line]
    assert notices == [(f"notice    made inside the git repository at {checkout.resolve()}: a workspace usually lives in "
                        "a folder of its own, so move it out unless you mean to keep it in that repository")]


def test_init_outside_any_git_working_tree_or_at_the_top_of_its_own_says_nothing_of_a_repository(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    alone = run("init", str(tmp_path / "workspaces" / "notes-workspace"), "--daily-usd", "20")
    (tmp_path / "own").mkdir()
    (tmp_path / "own" / ".git").mkdir()
    own = run("init", str(tmp_path / "own"), "--daily-usd", "20")
    assert alone.exit_code == 0 and own.exit_code == 0, alone.output + own.output
    assert "git repository" not in alone.output + own.output


@pytest.mark.parametrize(("many", "said"), [
    (1, ["pending   1 article not yet written:", "          things/t00"]),
    (3, ["pending   3 articles not yet written:", "          things/t00", "          things/t01", "          things/t02"]),
    (60, ["pending   60 articles not yet written:", *[f"          things/t0{n}" for n in range(5)],
          "          and 55 more"]),
])
def test_status_names_each_pending_article_on_a_line_of_its_own_up_to_five_and_counts_the_rest(
        tmp_path: Path, many: int, said: list[str]) -> None:
    from cerebro.article import Compilation, Identity
    from cerebro.store import SQLiteStore

    run("init", str(tmp_path), "--daily-usd", "20")
    with SQLiteStore.from_settings(tmp_path) as store:
        for n in range(many):
            store.create(Identity(f"things/t{n:02d}", "things", f"T{n:02d}"))
        store.create(Identity("things/written", "things", "Written"))
        store.write_compilation(Compilation("things/written", "Part", "Text.", by="things/article", at="2026-11-12T09:01:00Z",
                                    started="2026-11-12T09:00:00Z"))
        store.create(Identity("things/gone", "things", "Gone", status="archived"))  # out of the working set
    shown = run("status", "--workspace", str(tmp_path))
    lines = shown.output.splitlines()
    at = lines.index(said[0])
    assert lines[at:at + len(said)] == said and "things/written" not in shown.output and "things/gone" not in shown.output
    assert not lines[at + len(said)].startswith("          "), shown.output  # nothing more under it
    assert json.loads(run("status", "--json", "--workspace", str(tmp_path)).output)["pending"] == [
        f"things/t{n:02d}" for n in range(many)]
