"""The fiscal year's start: one line of `workspace.md`'s body states it, a workspace `init`
makes states none, and with none every fiscal window is refused on each path that takes a filter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from mcp import Client
from test_api import call, refusal
from test_mcp import over_mcp
from test_tokens import no_network, serving  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro import Caller, Cerebro, scaffold
from cerebro.cli import main
from cerebro.connector import Ours
from cerebro.errors import CerebroError
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.project import WorkspaceError, fiscal_now, fiscal_start, load

REFUSED = ("this_fiscal_year needs the fiscal year's start, and workspace.md states none: add a rule such as "
           "\"The fiscal year starts February 1.\"")
FRONT = "---\nname: things\nidentity:\n  domains: [quillmoor.example]\n---\n\n"
HINT = ("If your fiscal year does not start in January, add a line that says when it starts, as: The fiscal year "
              "starts <month> 1.")
BROKEN = {
    "unclosed": ("---\nname: things\n\nThe fiscal year starts July 1.\n",
                 "workspace.md: front matter has no closing --- line: end it with a line that is --- alone"),
    "a list": ("---\n- things\n---\n\nThe fiscal year starts July 1.\n", "workspace.md: line 2: expected a mapping: write key: value lines"),
    "not yaml": ("---\nname: [\n---\n\nThe fiscal year starts July 1.\n",
                 "workspace.md front matter: line 3: expected the node content, but found '<stream end>'"),
}
ASK = {"from": "facts", "window": "this_fiscal_year"}


def stated(tmp_path: Path, text: str) -> int | None:
    (tmp_path / "workspace.md").write_text(text, encoding="utf-8")
    return fiscal_start(tmp_path)


def made_by_init(tmp_path: Path) -> Path:
    return scaffold.init(tmp_path / "things", environ={}).root


def workspace_of(root: Path) -> Cerebro:
    return Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), agent_run=ScriptedRun(lambda prompt: ""))


def test_a_workspace_init_makes_states_no_fiscal_year(tmp_path: Path) -> None:
    root = made_by_init(tmp_path)
    assert "fiscal" not in (root / "workspace.md").read_text().lower()
    assert fiscal_start(root) is None


def test_a_rule_under_front_matter_reads_and_broken_front_matter_is_refused_with_its_fault(tmp_path: Path) -> None:
    assert stated(tmp_path, FRONT + "The fiscal year begins on October 1.\n") == 10
    for text, fault in BROKEN.values():
        with pytest.raises(WorkspaceError) as raised:
            stated(tmp_path, text)
        assert str(raised.value) == fault


def test_no_workspace_file_states_none(tmp_path: Path) -> None:
    assert fiscal_start(tmp_path) is None


def test_only_a_start_on_the_first_of_a_month_reads(tmp_path: Path) -> None:
    assert stated(tmp_path, FRONT + "Our fiscal year starts March 15.\n") is None
    assert stated(tmp_path, FRONT + "Our fiscal year starts March 1st.\n") is None
    assert stated(tmp_path, FRONT + "THE FISCAL YEAR STARTS ON MARCH 1.\n") == 3


def test_a_rule_in_the_front_matter_alone_states_none(tmp_path: Path) -> None:
    assert stated(tmp_path, "---\nname: things\nnote: The fiscal year starts July 1.\n---\n\nWho it is for.\n") is None


def test_the_first_line_that_states_a_start_wins(tmp_path: Path) -> None:
    assert stated(tmp_path, FRONT + "The fiscal year begins on April 1.\nThe fiscal year starts July 1.\n") == 4


def test_a_fiscal_filter_over_mcp_on_a_new_workspace_is_refused_with_the_rule_to_add(tmp_path: Path) -> None:
    workspace = workspace_of(made_by_init(tmp_path))

    async def calls(client: Client) -> list[Any]:
        return [await client.call_tool("query", {"from": "projects", "due": "this_fiscal_year"}),
                await client.call_tool("query", {"from": "facts", "window": "this_fiscal_year"})]

    for answer, key in zip(over_mcp(workspace, calls), ("due", "window"), strict=True):
        assert answer.is_error and f"query: filter {key}: {REFUSED}" in answer.content[0].text


def test_a_fiscal_filter_over_the_json_api_on_a_new_workspace_is_refused_with_the_rule_to_add(tmp_path: Path) -> None:
    workspace = workspace_of(made_by_init(tmp_path))
    token = workspace.make_token("agent-a").token
    with serving(workspace) as client:
        reply = call(client, "query", {"from": "projects", "due": "this_fiscal_year"}, token)
    assert refusal(reply, 400, "refused") == f"query: filter due: {REFUSED}"


def test_a_fiscal_filter_from_tools_query_on_a_new_workspace_is_refused_with_the_rule_to_add(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = made_by_init(tmp_path)
    monkeypatch.setattr("cerebro.cli.open_workspace", workspace_of)
    asked = json.dumps({"from": "facts", "window": "this_fiscal_year"})
    result = CliRunner().invoke(main, ["tools", "query", "--json", asked, "--workspace", str(root)])
    assert result.exit_code != 0 and "Traceback" not in result.output
    assert f"query: filter window: {REFUSED}" in result.output


def test_an_agent_whose_evidence_queries_a_fiscal_window_on_a_new_workspace_is_refused_with_the_rule_to_add(
        tmp_path: Path) -> None:
    root = made_by_init(tmp_path)
    (root / "agents" / "reports").mkdir()
    (root / "agents" / "reports" / "yearly.md").write_text(
        "---\nevidence:\n  - query: { from: facts, window: this_fiscal_year }\nheartbeat: [manual]\n---\n\n"
        "Sum up the year.\n\n# Output\n\n## Summary\n")
    workspace = workspace_of(root)
    with pytest.raises(CerebroError) as raised:
        workspace.compile(workspace.agent("reports/yearly"))
    assert str(raised.value) == f"query: filter window: {REFUSED}"


def test_a_hint_that_names_no_month_never_reads_as_a_rule(tmp_path: Path) -> None:
    assert stated(tmp_path, FRONT + f"Who this workspace is for. {HINT}\n") is None


def test_a_rule_added_while_an_mcp_session_is_open_reads_on_its_next_query(tmp_path: Path) -> None:
    root = made_by_init(tmp_path)
    workspace = workspace_of(root)

    async def calls(client: Client) -> list[Any]:
        first = await client.call_tool("query", ASK)
        with (root / "workspace.md").open("a", encoding="utf-8") as file:
            file.write("\nThe fiscal year starts February 1.\n")
        return [first, await client.call_tool("query", ASK)]

    before, after = over_mcp(workspace, calls)
    assert before.is_error and not after.is_error, after.content[0].text


def test_a_changed_start_reads_on_the_next_lookup_of_the_same_open_workspace(tmp_path: Path) -> None:
    root = made_by_init(tmp_path)
    path = root / "workspace.md"
    path.write_text(path.read_text() + "\nThe fiscal year starts February 1.\n")
    workspace = workspace_of(root)
    assert workspace.lookup._windows(ASK)["window"].start.month == 2
    path.write_text(path.read_text().replace("starts February 1.", "starts July 1."))
    assert workspace.lookup._windows(ASK)["window"].start.month == 7 and workspace.lookup.fiscal_start == 7


def test_status_names_front_matter_that_never_closes_is_not_a_map_or_is_not_yaml(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.cli.open_workspace", workspace_of)
    for label, (text, fault) in BROKEN.items():
        root = made_by_init(tmp_path / label)
        (root / "workspace.md").write_text(text, encoding="utf-8")
        status = CliRunner().invoke(main, ["status", "--workspace", str(root)])
        assert status.exit_code == 1 and fault in status.output and "Traceback" not in status.output, status.output
        asked = CliRunner().invoke(main, ["tools", "query", "--json", json.dumps(ASK), "--workspace", str(root)])
        said = fault.removeprefix("workspace.md").lstrip(": ")
        assert asked.exit_code == 1 and f"workspace.md does not read: {said}" in asked.output, asked.output
        plain = CliRunner().invoke(main, ["tools", "query", "--json", json.dumps({**ASK, "window": "last_30_days"}),
                                          "--workspace", str(root)])
        assert plain.exit_code == 0, plain.output  # reads go on


def test_a_fiscal_query_in_an_open_workspace_whose_front_matter_went_bad_names_the_fault(tmp_path: Path) -> None:
    root = made_by_init(tmp_path)
    workspace = workspace_of(root)
    for text, fault in BROKEN.values():
        (root / "workspace.md").write_text(text, encoding="utf-8")
        with pytest.raises(CerebroError) as raised:
            workspace.query(Caller("pat", "person"), "facts", {"window": "this_fiscal_year"})
        said = fault.removeprefix("workspace.md").lstrip(": ")
        assert str(raised.value) == ("query: filter window: this_fiscal_year needs the fiscal year's start, and "
                                     f"workspace.md does not read: {said}")
        assert workspace.query(Caller("pat", "person"), "facts", {"window": "last_30_days"}).count == 0


def yearly(root: Path, more: str = "") -> None:
    (root / "agents" / "reports").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "reports" / "yearly.md").write_text(
        f"---\nevidence:\n  - query: {{ from: facts, window: this_fiscal_year }}\n{more}heartbeat: [change]\n---\n\n"
        "Sum up the year.\n\n# Output\n\n## Summary\n")


def test_an_agent_whose_gather_fails_leaves_a_failed_run_that_status_shows_and_runs_once_the_rule_is_added(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = made_by_init(tmp_path)
    yearly(root, "  - files: [workspace.md]\n")

    def opened(at: Path | None) -> Cerebro:
        return Cerebro(at, extraction=FakeExtraction(), agent_run=ScriptedRun(lambda prompt: "## Summary\n\nA year."))

    monkeypatch.setattr("cerebro.cli.open_workspace", opened)
    with opened(root) as workspace:
        result = workspace.run_once()
        rows = [r for r in workspace.query(Caller("pat", "person"), "agent-runs", {}).items if r["agent"] == "reports/yearly"]
    assert REFUSED in result.failed["reports/yearly"]
    assert [(r["status"], r["note"]) for r in rows] == [("failed", f"gather: query: filter window: {REFUSED}")]
    status = CliRunner().invoke(main, ["status", "--workspace", str(root)]).output
    assert any(line.startswith("ran       reports/yearly: failed at ") and line.endswith(f"gather: query: filter window: "
                                                                                       f"{REFUSED}")
               for line in status.splitlines()), status
    with (root / "workspace.md").open("a", encoding="utf-8") as file:
        file.write("\nThe fiscal year starts February 1.\n")
    with opened(root) as workspace:
        again = workspace.run_once()
    assert [run.agent for run in again.runs] == ["reports/yearly"] and "fiscal" not in str(again.failed)


READ = {  # front matter lines a YAML reader converts or chokes on: the body's rule reads whatever they hold
    "an impossible date": "since: 2026-02-30",
    "a date with a zone": "since: 2026-02-03T04:05:06+25:00",
    "a float past the largest": "size: 1e400",
    "an octal that is none": "mode: 0o9",
    "a null": "name: ~",
    "an anchor with no alias": "first: &first things",
    "a very long scalar": "note: " + "x" * 200_000,
    "a tag that cannot hold its value": "size: !!int x",
    "a bool tag that cannot hold its value": "flag: !!bool x",
    "a tag YAML does not know": "thing: !thing x",
    "a collection tag on a value": "kept: !!set x",
    "a scalar tag on a list": "kept: !!int [1, 2]",
    "a tag that would build an object": "run: !!python/object/apply:os.system [echo made-up]",
}
FAULTY = {"a tab": "\tname: things", "an alias with no anchor": "first: *first", "a key written twice": "title: other"}


def with_line(tmp_path: Path, line: str) -> Path:
    root = made_by_init(tmp_path)
    (root / "workspace.md").write_text(f"---\ntitle: things\n{line}\n---\n\nThe fiscal year starts July 1.\n",
                                       encoding="utf-8")
    return root


@pytest.mark.parametrize("line", READ.values(), ids=READ.keys())
def test_the_rule_reads_whatever_a_front_matter_value_holds(tmp_path: Path, line: str) -> None:
    root = with_line(tmp_path, line)
    assert fiscal_start(root) == 7 and fiscal_now(root) == (7, None)
    workspace = workspace_of(root)
    assert workspace.lookup._windows(ASK)["window"].start.month == 7
    workspace.close()


@pytest.mark.parametrize("line", FAULTY.values(), ids=FAULTY.keys())
def test_front_matter_that_cannot_be_read_is_a_named_fault_with_its_line(tmp_path: Path, line: str) -> None:
    root = with_line(tmp_path, line)
    with pytest.raises(WorkspaceError, match=r"^workspace\.md( front matter)?: (line 3: |it )"):
        fiscal_start(root)
    load(root)  # reads go on: status, a pass and a compile name the fault
    start, why = fiscal_now(root)
    assert start is None and why is not None and why.startswith("workspace.md does not read: ")


@pytest.mark.parametrize("line", [*READ.values(), *FAULTY.values()], ids=[*READ, *FAULTY])
def test_no_front_matter_value_ends_status_or_a_query_in_a_traceback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str) -> None:
    root = with_line(tmp_path, line)
    monkeypatch.setattr("cerebro.cli.open_workspace", workspace_of)
    for command in (["status"], ["tools", "query", "--json", json.dumps(ASK)]):
        result = CliRunner().invoke(main, [*command, "--workspace", str(root)])
        assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
        assert "Traceback" not in result.output


@pytest.mark.parametrize("label", BROKEN)
def test_with_broken_front_matter_mcp_answers_last_30_days_and_refuses_a_fiscal_window_with_the_fault(
        tmp_path: Path, label: str) -> None:
    text, fault = BROKEN[label]
    root = made_by_init(tmp_path)
    (root / "workspace.md").write_text(text, encoding="utf-8")
    workspace = workspace_of(root)

    async def calls(client: Client) -> list[Any]:
        return [await client.call_tool("query", {"from": "facts", "window": "last_30_days"}),
                await client.call_tool("query", ASK)]

    plain, fiscal = over_mcp(workspace, calls)
    said = fault.removeprefix("workspace.md").lstrip(": ")
    assert not plain.is_error and fiscal.is_error and f"workspace.md does not read: {said}" in fiscal.content[0].text


def test_a_pass_and_a_compile_refuse_broken_front_matter_in_one_line(
        tmp_path: Path) -> None:
    root = made_by_init(tmp_path)
    yearly(root)
    text, fault = BROKEN["unclosed"]
    (root / "workspace.md").write_text(text, encoding="utf-8")
    with workspace_of(root) as workspace:
        with pytest.raises(WorkspaceError, match=f"^{fault}$"):
            workspace.run_once()
        with pytest.raises(WorkspaceError, match=f"^{fault}$"):
            workspace.compile(next(a for a in workspace.agents if a.id == "reports/yearly"), scheduled=True)
        assert workspace.query(Caller("pat", "person"), "facts", {"window": "last_30_days"}).count == 0
        (root / "workspace.md").write_text(FRONT + "The fiscal year starts July 1.\n", encoding="utf-8")
        workspace.run_once()


def test_status_names_a_profile_md_that_is_not_utf8_and_reads_go_on(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = made_by_init(tmp_path)
    (root / "profile.md").write_bytes(b"---\nidentity:\n  name: Sam Rivera\xe9\n  email: [sam@quillmoor.example]\n---\n")
    monkeypatch.setattr("cerebro.cli.open_workspace", workspace_of)
    status = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert status.exit_code == 1 and "profile.md front matter: it is not UTF-8 text: save the file as UTF-8" in \
        status.output and "Traceback" not in status.output, status.output
    plain = CliRunner().invoke(main, ["tools", "query", "--json", '{"from": "facts", "window": "last_30_days"}',
                                      "--workspace", str(root)])
    assert plain.exit_code == 0, plain.output


def test_a_byte_that_is_not_utf8_in_the_body_leaves_the_rule_readable_and_in_front_matter_is_a_fault(
        tmp_path: Path) -> None:
    (tmp_path / "workspace.md").write_bytes(FRONT.encode() + b"Quillmoor\xf6 sells.\nThe fiscal year starts July 1.\n")
    assert fiscal_start(tmp_path) == 7
    (tmp_path / "workspace.md").write_bytes(b"---\nname: Quillmoor\xf6\n---\n\nThe fiscal year starts July 1.\n")
    with pytest.raises(WorkspaceError, match="^workspace.md front matter: it is not UTF-8 text: save the file as UTF-8$"):
        fiscal_start(tmp_path)
    (tmp_path / "workspace.md").write_bytes((FRONT + "The fiscal year starts July 1.\n").encode("utf-16"))
    assert fiscal_start(tmp_path) is None  # no front matter, and no rule in bytes that are not UTF-8


def test_sources_list_nothing_while_profile_md_does_not_read_and_read_the_mended_side_with_no_restart(
        tmp_path: Path) -> None:
    root = made_by_init(tmp_path)
    profile = root / "profile.md"
    profile.write_bytes(b"---\nidentity:\n  email: [sam\xe9@quillmoor.example]\n---\n")
    workspace = Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), agent_run=ScriptedRun(lambda p: ""),
                    connectors=[FakeConnector("local", [])])
    with workspace:
        assert workspace.ours == Ours((), ())
        for listed in (workspace.run_once, workspace.connectors["local"].list):
            with pytest.raises(WorkspaceError, match="^profile.md front matter: it is not UTF-8 text"):
                listed()
        profile.write_text("---\nidentity:\n  email: [sam@quillmoor.example]\n---\n", encoding="utf-8")
        workspace.connectors["local"].list()  # read again, as the file is read on the next tick
        assert workspace.ours.addresses == ("sam@quillmoor.example",)
        assert workspace.query(Caller("pat", "person"), "facts", {"window": "last_30_days"}).count == 0


@pytest.mark.parametrize(("sentence", "month"), [
    ("The fiscal year starts ſeptember 1.", 9),  # a long s folds to s
    ("The fiscal year starts Kay 1.", None),  # the Kelvin sign is a k, and no month has one
    ("The fiſcal year begins on MARCH 1.", 3),
    ("The fiscal year starts aprıl 1.", None),  # a dotless i is not an i
    ("The fiscal year starts AprİL 1.", None),
    ("The fİscal year begins April 1.", None),
    ("The fiscal year begins September 1ﬅ.", 9),  # \b between ASCII and anything else
    ("Our \u00e9fiscal year starts May 1.", 5),  # é is not an ASCII letter, so a word edge holds before f
])
def test_the_rule_reads_letters_in_any_case_and_never_raises(
        tmp_path: Path, sentence: str, month: int | None) -> None:
    assert stated(tmp_path, FRONT + sentence + "\n") == month
