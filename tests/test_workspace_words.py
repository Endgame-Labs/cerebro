"""What a person or an outside agent sees names the workspace and the wiki, never a brain. `--workspace` names the
folder on every command."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import anyio
import click
import pytest
from click.testing import CliRunner, Result
from mcp import Client

from cerebro import Caller, Cerebro
from cerebro.cli import main

BRAIN = re.compile("brain", re.IGNORECASE)


def run(*args: str) -> Result:
    return CliRunner().invoke(main, list(args))


def commands(group: click.Group = main, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], click.Command]]:
    found: list[tuple[tuple[str, ...], click.Command]] = [(path, group)]
    for name, command in sorted(group.commands.items()):
        if isinstance(command, click.Group):
            found += commands(command, (*path, name))
        else:
            found.append(((*path, name), command))
    return found


def folder_option(command: click.Command) -> click.Option | None:
    return next((p for p in command.params if isinstance(p, click.Option) and "--workspace" in p.opts), None)


def made(tmp_path: Path) -> Path:
    root = tmp_path / "notes-wiki"
    assert run("init", str(root), "--daily-usd", "1", "--timezone", "UTC").exit_code == 0
    return root


def test_no_help_of_any_command_says_brain_and_each_that_takes_a_folder_shows_workspace() -> None:
    walked = commands()
    assert len(walked) > 20
    for path, command in walked:
        shown = run(*path, "--help")
        assert shown.exit_code == 0, (path, shown.output)
        assert not BRAIN.search(shown.output), (path, shown.output)
        if folder_option(command) is not None:
            assert "--workspace DIRECTORY" in shown.output, path


def test_every_command_that_takes_a_folder_takes_workspace_and_no_other_name_for_it() -> None:
    taking = [(path, command) for path, command in commands() if not isinstance(command, click.Group)]
    assert sum(folder_option(command) is not None for _, command in taking) >= 20
    for path, command in taking:
        option = folder_option(command)
        if option is None:
            continue
        assert option.opts == ["--workspace"] and option.secondary_opts == [] and option.name == "root", path


def test_workspace_names_the_folder_and_a_bad_one_is_refused_by_its_name(tmp_path: Path) -> None:
    root = made(tmp_path)
    new = run("status", "--workspace", str(root))
    assert new.exit_code == 0 and new.output.splitlines()[0] == f"workspace {root.resolve()}"
    (tmp_path / "a-file").write_text("")
    refused = run("status", "--workspace", str(tmp_path / "a-file"))
    assert refused.exit_code == 2 and "'--workspace'" in refused.output and not BRAIN.search(refused.output)


def test_init_status_trust_and_forget_on_a_small_workspace_never_say_brain(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "notes-wiki"
    said = [run("init", str(root), "--daily-usd", "1", "--timezone", "UTC")]
    said += [run("status", "--workspace", str(root)), run("trust", str(root)), run("serve", "--once", "--workspace",
                                                                                     str(root))]
    said += [run("init", str(root)), run("trust", str(tmp_path)), run("secrets", "list", "--workspace", str(root))]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    said += [run("forget", str(root), "--yes"), run("forget")]
    for result in said:
        assert not BRAIN.search(result.output), result.output
    assert said[0].output.splitlines()[0] == f"workspace {root.resolve()}"
    assert said[1].output.splitlines()[0] == f"workspace {root.resolve()}"


@pytest.mark.untrusted
def test_an_untrusted_workspace_is_refused_and_described_without_the_word_brain(tmp_path: Path) -> None:
    root = tmp_path / "cloned"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    (root / "extensions.py").write_text("")
    refused = run("serve", "--once", "--workspace", str(root))
    assert refused.exit_code == 1
    assert refused.output.startswith(f"Error: {root} is not a workspace you trust, so cerebro runs nothing of it: ")
    status = run("status", "--workspace", str(root))
    assert status.output.splitlines()[:2] == [
        f"workspace {root}", f"trust     not trusted, so cerebro runs nothing of it: cerebro trust {root} would allow this"]
    trusted = run("trust", str(root))
    for result in (refused, status, trusted):
        assert not BRAIN.search(result.output), result.output
    assert trusted.output.splitlines()[-1] == f"trusted   {root}"


def test_every_mcp_tool_and_argument_description_and_the_server_say_no_brain(tmp_path: Path) -> None:
    root = tmp_path / "desk"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    workspace = Cerebro(root)

    served = workspace.mcp_server()

    async def listed() -> list[Any]:
        async with Client(served) as client:
            return (await client.list_tools()).tools

    try:
        name, tools = served.name, anyio.run(listed)
    finally:
        workspace.close()
    assert name == "cerebro-desk"
    assert {"find", "query", "read", "tell", "decide", "retract"} <= {tool.name for tool in tools}
    for tool in tools:
        assert tool.description and not BRAIN.search(tool.description), (tool.name, tool.description)
        assert not BRAIN.search(str(tool.input_schema)), (tool.name, tool.input_schema)


def test_what_every_client_is_told_on_connecting_names_the_workspace_wiki_and_articles_never_a_brain_or_page(
        tmp_path: Path) -> None:
    from cerebro.mcp import DATA

    root = tmp_path / "desk"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    workspace = Cerebro(root)

    async def told() -> str | None:
        async with Client(workspace.mcp_server()) as client:
            return client.instructions

    try:
        said = anyio.run(told)
    finally:
        workspace.close()
    assert said is not None and said.split("\n\n")[0] == DATA and all(word in said for word in ("workspace", "wiki", "article"))
    assert not BRAIN.search(said) and not re.search(r"\bpages?\b", said, re.IGNORECASE)


def test_what_a_session_is_told_of_collections_and_proposals_and_the_argument_refusals_never_say_brain_page_or_section(
        tmp_path: Path) -> None:
    from cerebro.article import Identity, Property
    from cerebro.fakes import MemoryStore
    from cerebro.mcp import proposals_note, server

    words = re.compile(r"\b(brains?|pages?|sections?)\b", re.IGNORECASE)
    root = tmp_path / "desk"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    workspace = Cerebro(root, store=MemoryStore())
    workspace.store.create(Identity("projects/apollo", "projects", "Apollo"))
    workspace.store.set_field("projects/apollo", "stage", Property("open", Caller("you", "person"), "2026-11-01T09:00:00Z"))

    async def session() -> list[str]:
        async with Client(server(workspace)) as client:
            refused = [await client.call_tool("find", {"name": "Apollo", "bogus": 1}),
                       await client.call_tool("tell", {"text": "Apollo is green.", "set": {}})]
            return [client.instructions or "", *(reply.content[0].text for reply in refused)]

    try:
        said = anyio.run(session)
    finally:
        workspace.close()
    notes = [proposals_note(n) for n in (0, 1, 3)]
    assert said[0].endswith("Its collections are projects (1).") and said[1:] == ['find: unknown field "bogus"', 'tell: unknown field "set"']
    assert not [line for line in said + notes if words.search(line)], said + notes


def test_the_notes_session_instructions_and_what_a_query_of_notes_says_never_say_brain(tmp_path: Path) -> None:
    from cerebro.compile import agent_surface
    from cerebro.fakes import MemoryStore, ScriptedRun

    notes = Path(__file__).resolve().parents[1] / "apps" / "notes"
    root = tmp_path / "desk"
    (root / "agents").mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "extensions.py").write_text(f"from cerebro import load_app\n\nnotes = load_app({str(notes)!r})\n")
    workspace = Cerebro(root, trusted=True, store=MemoryStore(), agent_run=ScriptedRun(lambda prompt: {}))

    async def session() -> list[Any]:
        async with Client(workspace.mcp_server()) as client:
            empty = client.instructions
            kept = await client.call_tool("note", {"text": "We ship on Monday.", "collection": "notes/projects"})
            too_big = await client.call_tool("note", {"text": "x" * 60_000})
            return [empty, client.instructions, kept, too_big,
                    await client.call_tool("query", {"from": "note"}),
                    await client.call_tool("browse_notes", {"collection": "notes"}),
                    await client.call_tool("browse_notes", {"collection": "nowhere"}),
                    {tool.name: tool.description for tool in (await client.list_tools()).tools}]

    async def again() -> str | None:
        async with Client(workspace.mcp_server()) as client:
            return client.instructions

    try:
        said = anyio.run(session)
        said.append(anyio.run(again))
        query, read = agent_surface(workspace.lookup, Caller("projects/standing", "agent"))
        record = said[2].structured_content["record"]
        said += [query("note"), read([record])]
    finally:
        workspace.close()
    assert said[0] and "notes yet" in said[0] and "newest notes" in said[-3]
    assert said[3].is_error and "50 KiB" in said[3].content[0].text
    assert "read_as" in said[4].structured_content and said[5].structured_content["recent"]
    assert said[6].structured_content["warnings"] and "browse_notes" in said[7]
    assert said[-2]["warnings"] and said[-1]["warnings"]
    for item in said:
        text = item if isinstance(item, (str, dict)) else (item.content[0].text, item.structured_content)
        assert not BRAIN.search(str(text)), text


# ---- the command line's look: its pieces and lists name the workspace, the wiki and articles ----


def test_the_style_demo_and_the_lists_it_shows_never_say_brain_or_page() -> None:
    import io
    import sys

    checks = Path(__file__).resolve().parents[1] / "checks"
    sys.path.insert(0, str(checks))
    try:
        import style_demo
    finally:
        sys.path.remove(str(checks))
    from cerebro.checklist import Menu, Model, write_plain

    shown = io.StringIO()
    style_demo.main([], shown)
    style_demo.main(["--plain"], shown)
    text = shown.getvalue() + write_plain(style_demo.made_up_list())
    text += "\n".join(Model(style_demo.made_up_list()).render(80, 40)) + "\n".join(
        Menu(style_demo.made_up_menu()).render(80, 24))
    assert not BRAIN.search(text)
    assert not re.search(r"\bpages?\b", text, re.IGNORECASE)
    assert not re.search(r"\b(actionable|mail|inbox|received)\b", text, re.IGNORECASE)


# ---- the first run: its discovery lines, its list and the lines that say it waits ----


def test_the_first_runs_lines_list_and_waiting_line_never_say_brain_or_page(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from test_first_run import ADMITS, Script, lay_out_workspace, open_it

    from cerebro import cli

    words = re.compile(r"\b(brain|pages?|actionable|mail|inbox|received)\b", re.IGNORECASE)
    script = Script({"things/curator": list(ADMITS)})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    root = lay_out_workspace(tmp_path / "first")
    said = [run("serve", "--once", "--workspace", str(root)), run("review", "--json", "--workspace", str(root))]
    said.append(CliRunner().invoke(main, ["serve", "--once", "--plain", "--workspace", str(root)], input="q\n"))
    said.append(run("serve", "--once", "--yes", "--workspace", str(root)))
    assert [result.exit_code for result in said] == [2, 0, 0, 0], [result.output for result in said]
    assert "Working set proposal" in said[2].output and "wrote     things/alder-works" in said[3].output
    for result in said:
        assert not words.search(result.output), result.output


def test_the_first_runs_checkbox_screen_never_says_brain_page_or_section(tmp_path: Path) -> None:
    """The checkbox screen on a first run's real pile: rows with what hangs off them and a left-out row checked, so
    every line the screen draws shows, as the screen does at a terminal."""
    from test_first_run import ADMITS, Script, lay_out_workspace, open_it

    from cerebro.checklist import Key, Model, write_plain
    from cerebro.first_run import LocalApproval, run
    from cerebro.first_run_list import checklist

    words = re.compile(r"\b(brain|pages?|sections?|actionable|mail|inbox|received)\b", re.IGNORECASE)
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        run(workspace, choose=None)
        lst, _ = checklist(LocalApproval(workspace).pile())
    model = Model(lst)
    text = "\n".join(model.render(80, 60, cursor=False))
    for _ in model.stops():  # every row and header toggled once, so each line it shows unchecked shows too
        model.press(Key.TOGGLE)
        text += "\n" + "\n".join(model.render(80, 60, cursor=False))
        model.press(Key.DOWN)
    text += write_plain(lst)
    assert "Left out (1)" in text and "Not grouped (2)" in text
    assert not words.search(text), words.findall(text)


def test_every_line_the_first_run_and_its_meter_spell_and_a_declined_runs_lines_never_say_brain_page_or_section(
        tmp_path: Path) -> None:
    """What the first run, its list and the meter that cuts a round spell, outside docstrings, and what a run whose
    list was declined whole says, its next line too, with each curator's own line under its collection, the count
    under a row of the held records its admit binds, and the line of a writer run that had nothing to cite."""
    import ast

    from test_first_run import ADMITS, Script, lay_out_workspace, open_it

    from cerebro.compile import KEPT, KEPT_LAST, NONE_LEFT
    from cerebro.first_run import Listed, Pile, Row, lines, run
    from cerebro.tree import Group

    words = re.compile(r"\b(brain|pages?|sections?)\b", re.IGNORECASE)
    src = Path(__file__).resolve().parents[1] / "src" / "cerebro"
    for name in ("first_run.py", "first_run_list.py", "tree.py", "budget.py"):
        tree = ast.parse((src / name).read_text(encoding="utf-8"))
        docs = {id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        spelled = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                   and isinstance(node.value, str) and id(node) not in docs]
        assert not [text for text in spelled if words.search(text)], name
    from test_first_run import review_rest

    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        out = run(workspace, choose=review_rest(workspace))  # declined whole in cerebro review
    assert out is not None and out.next
    bound = Row("p_1", "things/curator", "things", "Alder Works", "Named twice.",
                binds=[("local", "a.md"), ("local", "b.md")])
    pile = Pile([Listed("things/curator", "things", [bound, Row("left-out:1", "things/curator", "things", "Kim Vale",
                                                                "Named once.", left_out=True)],
                        said="Left out 1: 1 named once."),
                 Listed("parts/curator", "parts", [], said="Found nothing to weigh, so nothing was admitted.")],
                groups=[Group([bound])])
    text = "\n".join([*out.lines, out.next, KEPT, NONE_LEFT, KEPT_LAST, *lines(pile), *lines(Pile(pile.lists, late=True))])
    assert "Named once." in text and "2 records" in text and "parts: " not in text
    assert not words.search(text), words.findall(text)


def test_show_prompt_and_a_dry_run_never_say_brain_or_page_in_their_own_lines(tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from test_try_first import Workspace, note, own_lines

    words = re.compile(r"\b(brain|pages?)\b", re.IGNORECASE)
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    said = [w.run("agents", "run", "reports/weekly", "--show-prompt"), w.run("compile", "--dry-run"),
            w.run("agents", "new", "things/subagents/risks"), w.run("agents", "run", "things/article", "--show-prompt")]
    for result in said:
        assert not [line for line in own_lines(result.output) + result.output.splitlines()[-1:]
                    if words.search(line)], result.output



def test_the_line_after_a_first_runs_summary_never_says_brain_page_or_section(tmp_path: Path) -> None:
    """The line that says how many rows wait once what was ticked is built."""
    from test_first_run import ADMITS, Script, lay_out_workspace, open_it, settled

    from cerebro.first_run import run

    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        out = run(workspace, choose=lambda pile: settled(pile, {r.id for r in pile.rows() if r.name == "Alder Works"}))
    assert out is not None and out.after and out.after[0].startswith("1 more is waiting")
    assert not re.search(r"\b(brain|pages?|sections?|accounts?)\b", " ".join(out.after), re.IGNORECASE)


def test_what_the_first_runs_list_shows_says_selected_and_never_pick(tmp_path: Path, monkeypatch: Any) -> None:
    """A row is selected, never picked. Held over the screen, the plain list, `--yes`, and every
    string the list's modules spell outside docstrings."""
    import ast

    from click.testing import CliRunner
    from test_first_run import ADMITS, Script, lay_out_workspace, open_it

    from cerebro import cli
    from cerebro.checklist import Model
    from cerebro.first_run import LocalApproval, arranged, lines, run
    from cerebro.first_run_list import checklist

    pick = re.compile(r"\bpick", re.IGNORECASE)
    src = Path(__file__).resolve().parents[1] / "src" / "cerebro"
    for name in ("first_run.py", "first_run_list.py", "tree.py"):
        tree = ast.parse((src / name).read_text(encoding="utf-8"))
        docs = {id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        spelled = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                   and isinstance(node.value, str) and id(node) not in docs]
        assert not [text for text in spelled if pick.search(text)], name
    script = Script({"things/curator": list(ADMITS)})
    root = lay_out_workspace(tmp_path)
    with open_it(root, script) as workspace:
        run(workspace, choose=None)
        pile = arranged(workspace, LocalApproval(workspace).pile())
    shown = [*Model(checklist(pile)[0]).render(120, 80, True, None), *lines(pile)]
    monkeypatch.setattr(cli, "open_workspace", lambda where=None: open_it(root, script))
    shown += CliRunner().invoke(cli.main, ["serve", "--once", "--yes", "--workspace", str(root)]).output.splitlines()
    text = "\n".join(shown)
    assert "2 of 3 selected" in text and "approved  every proposed article" in text
    assert not pick.search(text), pick.findall(text)


def test_the_pending_mark_and_status_block_call_it_an_article_and_never_a_brain_or_page() -> None:
    from cerebro.article import PENDING

    words = re.compile(r"\b(brain|pages?|sections?)\b", re.IGNORECASE)
    assert PENDING == "pending: article admitted but not yet written" and not words.search(PENDING)
    source = (Path(__file__).resolve().parents[1] / "src" / "cerebro" / "cli.py").read_text(encoding="utf-8")
    block = source[source.index('if waits := data["pending"]'):source.index('if data["requests"]["total"]')]
    assert "not yet written:" in block and not words.search(block)


def test_what_grounding_tells_its_judge_a_writer_a_reader_and_a_runs_log_says_no_brain_page_section_or_sales_word(
        ) -> None:
    from cerebro import ground
    from cerebro.article import NOT_FOUND, UNSUPPORTED
    from cerebro.fakes import FakeDecider

    editor = ground.Editor(ground.Judge(FakeDecider({"s1": 0, "s2": 2})), lambda ids: {}, heading="Where it stands")
    told = editor.review("Alder renews in March [^f_1]. Alder signs for ten years.")
    editor.marked("Alder renews in March [^f_1]. Alder signs for ten years.")
    lines = [ground.CONTRACT, ground.QUESTION, *ground.LEVELS, told, ground.CITE_BY_ID, *editor.scores, UNSUPPORTED,
             NOT_FOUND, ground.counted({"marked": 2, "unscored": 1})]
    words = re.compile(r"\b(brains?|pages?|sections?|accounts?|deals?|opportunit(?:y|ies)|contacts?|crm)\b",
                       re.IGNORECASE)
    assert told and len(editor.scores) == 2 and not [line for line in lines if words.search(line)]
