"""What a person or an outside agent sees never calls a part of an article a section, and never names how a run
hands in its text: that is the run's own business. The three hand-in names reach only the model, in the tool list it
is given, and the one refusal of a file that lists one of them. The command line's help, the MCP surface, the
bundled prompts as `cerebro prompts` shows them, the gtm template's agent files, a read, and every message the
framework's code spells are held to it."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import anyio
from click.testing import CliRunner
from conftest import GTM
from mcp import Client
from test_archive_command import carry
from test_workspace_words import commands, presumed_lines

from cerebro import Caller, Cerebro, prompts
from cerebro.article import Compilation, Identity
from cerebro.cli import main
from cerebro.fakes import MemoryStore
from cerebro.reply import outside

WORDS = re.compile(r"\bsections?\b|write_section|write_article|return_content|nothing_to_write", re.IGNORECASE)
SRC = Path(__file__).resolve().parents[1] / "src" / "cerebro"
# What code spells that no person or outside agent sees, or that must name what it names, by file.
SPELLED_ALONE = {
    "run.py": {"write_article", "return_content", "nothing_to_write"},  # the model's own tool list
    "connectors/gmail_text.py": {"section"},  # an HTML tag
    "checklist.py": {"section"},  # the terminal's style of a heading, which names no part of an article
    "output.py": {"section"},
}


def test_no_command_help_names_a_section_or_a_hand_in() -> None:
    for path, _ in commands():
        shown = CliRunner().invoke(main, [*path, "--help"])
        assert shown.exit_code == 0 and not WORDS.search(shown.output), (path, shown.output)


def test_no_bundled_prompt_and_no_gtm_agent_file_names_a_section_or_a_hand_in() -> None:
    for name in prompts.names():
        assert not WORDS.search(prompts.load(name)), name
    shown = CliRunner().invoke(main, ["prompts", "list"])
    assert not WORDS.search(shown.output)
    for path in sorted(GTM.rglob("*.md")):
        assert not WORDS.search(path.read_text(encoding="utf-8")), path


def test_no_mcp_tool_description_and_no_read_names_a_section_or_a_hand_in(tmp_path: Path) -> None:
    root = tmp_path / "desk"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    store = MemoryStore()
    store.create(Identity("calls/kickoff", "calls", "Kickoff"))
    store.write_compilation(Compilation("calls/kickoff", "calls/article", "They met.\n\n## Decisions\n\nA pilot.",
                                "calls/article", whole=True))
    workspace = Cerebro(root, store=store)
    served = workspace.mcp_server()

    async def listed() -> list[Any]:
        async with Client(served) as client:
            return (await client.list_tools()).tools

    try:
        tools = anyio.run(listed)
        read = outside(workspace.lookup.read(Caller("you", "person"), ["calls/kickoff"]))
    finally:
        workspace.close()
    for tool in tools:
        assert not WORDS.search(tool.description or ""), (tool.name, tool.description)
        assert not WORDS.search(json.dumps(tool.input_schema)), (tool.name, tool.input_schema)
    assert read["articles"][0]["text"] == "They met.\n\n## Decisions\n\nA pilot." and not WORDS.search(json.dumps(read))


def test_every_message_the_code_spells_calls_a_part_a_part_and_names_no_hand_in() -> None:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        name = path.relative_to(SRC).as_posix()
        allowed = SPELLED_ALONE.get(name, set())
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = {id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs \
                    and WORDS.search(node.value) and allowed is not None and node.value not in allowed:
                found.append(f"{name}:{node.lineno}: {node.value[:80]!r}")
    assert found == []


def test_what_a_person_reads_of_a_joined_article_a_wait_and_the_new_refusals_says_no_section_brain_or_page(
        tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from cerebro.agent import AgentFileError, load_agent, load_agents
    from cerebro.compile import _backs_off, _tried, compile_article
    from cerebro.fakes import ScriptedRun
    from cerebro.lookup import StoreLookup

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    folder = tmp_path / "agents" / "briefs"
    (folder / "subagents").mkdir(parents=True)
    part = "---\nevidence:\n  - items: {}\n---\n\n# Instructions\n\nDraft it.\n\n# Output\n\n## {}\n"
    (folder / "subagents" / "risks.md").write_text(part.replace("{}", "Risks"))
    (folder / "subagents" / "people.md").write_text(part.replace("{}", "People"))
    (folder / "article.md").write_text("---\nsubagents: [risks, people]\n---\n\nA brief.\n")
    agents = {a.id: a for a in load_agents(tmp_path / "agents")[0]}
    store = MemoryStore()
    store.create(Identity("briefs/acme", "briefs", "Acme"))
    compile_article(agents["briefs/article"], lookup=StoreLookup(store), store=store, root=tmp_path,
                    functions={"items": lambda: [{"id": "r0", "text": "one"}]}, agent_tools={},
                    agent_run=ScriptedRun(lambda p: "One [^r0]."), this="briefs/acme")
    joined = next(r.data for r in store.rows("agent-runs") if r.data.get("status") == "joined")
    seen = [joined["status"], joined["note"]]
    now = datetime(2026, 11, 18, 9, tzinfo=UTC)
    _tried(store, agents["briefs/subagents/risks"], "briefs/acme", "sha256:x", now, True)
    seen.append(_backs_off(store, agents["briefs/subagents/risks"], "briefs/acme", "sha256:x", now))
    (folder / "subagents" / "people.md").write_text(part.replace("---\nevidence", "---\nheading: Risks\nevidence"))
    seen += load_agents(tmp_path / "agents")[1]
    (folder / "article.md").write_text("---\nsubagents: [risks]\ntarget: { from: notes }\n---\n\nA brief.\n")
    try:
        load_agent(folder / "article.md", tmp_path / "agents")
    except AgentFileError as error:
        seen.append(str(error))
    assert len(seen) == 5 and all(seen) and seen[0] == "joined" and "with no model call and no cost" in seen[1]
    assert not [line for line in seen if words.search(line) or WORDS.search(line)], seen


def test_a_facts_tie_label_and_its_relation_and_via_fields_name_no_section(tmp_path: Path) -> None:
    from test_ties import YOU, Workspace, everything

    from cerebro.mcp import render_query

    w = Workspace(tmp_path, fields=True)
    try:
        everything(w)
        lines = [item.text for sub in ("stakeholders", "overview") for entry in w.handed(sub)
                 for item in entry.items]
        query = w.lookup.query(YOU, "facts", {"articles": "accounts/initech"})
        shown = json.dumps(outside(query)) + render_query(query) + json.dumps(outside(
            w.lookup.read(YOU, ["accounts/initech"])))
        assert any("; subject: " in line for line in lines) and '"relation"' in shown and '"via"' in shown
        assert not WORDS.search("\n".join(lines) + shown)
    finally:
        w.store.close()


def test_a_passs_live_progress_says_no_section_brain_or_page_in_its_frames_or_its_plain_lines() -> None:
    import sys

    from cerebro.output import strip_escapes

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
    import progress_demo

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    data = Path(__file__).parent / "data" / "tui"
    for drawn in (progress_demo.frames(dark=False), progress_demo.frames(dark=True),
                  (data / "progress-plain.txt").read_text()):
        assert not words.search(strip_escapes(drawn)) and not WORDS.search(strip_escapes(drawn))


def test_a_first_runs_live_steps_and_its_region_say_no_section_brain_or_page() -> None:
    """Every line a first run's screen draws: the sources read, discovery's line, each curator's step
    queued, reviewing and resolved every way, the approval, a line said and the build's region."""
    import io

    from cerebro.output import Printer, strip_escapes
    from cerebro.progress import FirstRunScreen
    from cerebro.runner import Progress

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    out = Tty()
    p = Printer(out, out, getenv={"TERM": "xterm-256color"}.get, dark=True, width=lambda: 100, tick=3600)
    screen = FirstRunScreen(p, sources=["notes", "mail"], ticking=False, height=lambda: 60)
    events = [Progress("read", "start"), Progress("check", "end", "notes", mark="done", total=3),
              Progress("check", "end", "mail", mark="failed", why="refused"), Progress("read", "end", total=3),
              Progress("discover", "open", total=4)]
    events += [Progress("discover", "queue", f"{c}/curator", text=c) for c in ("parts", "things", "places", "kinds")]
    events += [Progress("discover", "start", "parts/curator", text="parts"),
               Progress("discover", "end", "parts/curator", mark="done", text="2 articles to parts collection"),
               Progress("discover", "end", "things/curator", mark="nothing"),
               Progress("discover", "end", "places/curator", mark="budget"),
               Progress("discover", "end", "kinds/curator", mark="failed", why="Discovery for kinds collection did not "
                        "finish: TimeoutError: no answer"),
               Progress("arrange", "start", total=2), Progress("arrange", "end", total=2),
               Progress("approve", "start", total=2),
               Progress("approve", "end", mark="done", text="Admitted 2 articles to 2 collections", why="parts: 1 "
                        "article\nthings: 1 article"),
               Progress("build", "start"), Progress("distill", "open", total=1), Progress("distill", "start", "notes"),
               Progress("distill", "count", "notes", total=3, done=1, text="2 facts")]
    for event in events:
        screen(event)
    screen.say("note      took over the leases of pid 42")
    screen(Progress("build", "end"))
    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    drawn = strip_escapes(out.getvalue())
    for line in ("Reading sources", "Read 3 records from 1 source",
                 "Running discovery for kinds, parts, places and things collections.", "Queued for parts collection", "Reviewing sources for parts collection",
                 "Proposed 2 articles", "Nothing new for things collection",
                 "Budget reached for places collection", "Reviewed 2 proposals", "Recording 2 approvals",
                 "Admitted 2 articles", "Distilling", "1 of 3 records • 2 facts"):
        assert line in drawn, line
    assert not words.search(drawn) and not WORDS.search(drawn)


def test_connects_lobby_and_its_lists_to_tick_say_no_section_brain_or_page() -> None:
    import sys

    from cerebro.output import strip_escapes

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
    import connect_demo

    from cerebro.connect_screen import written_line

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    offer = connect_demo.made_up_offer()
    said = [connect_demo.frames(dark=False), connect_demo.frames(dark=True), connect_demo.frames(None),
            connect_demo.views(dark=False), connect_demo.views(dark=True), connect_demo.views(None),
            written_line(offer, "code", ["a/b"]), written_line(offer, "code", []), written_line(offer, "code", None),
            prompts.load("slack-channel-grouping")]
    for drawn in said:
        assert not words.search(strip_escapes(drawn)) and not WORDS.search(strip_escapes(drawn))


def test_the_admit_lines_the_review_and_connect_waits_the_cooldown_and_the_compile_order_say_no_section_brain_or_page(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Each of these lines a person sees, as it prints: admit's lines and refusals, review's list and its
    exit-2 line, a decision that stood already, connect's exit-2 line, the cooling line, the order line of a pass and
    of status, and the at-once reasons."""
    from datetime import UTC, datetime, timedelta

    from cerebro import cli
    from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
    from cerebro.records import Proposal, propose
    from cerebro.runner import _at_once

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    root = tmp_path / "w"
    (root / "agents" / "projects").mkdir(parents=True)
    (root / "agents" / "places").mkdir()
    (root / "agents" / "reports").mkdir()
    (root / "sources").mkdir()
    (root / "settings.yaml").write_text("{}\n")
    (root / "sources" / "code.yaml").write_text("kind: github\n")
    curator = "---\nevidence:\n  - query: { from: facts }\nheartbeat: [manual]\n---\n\nKeep it.\n"
    (root / "agents" / "projects" / "curator.md").write_text(curator)
    (root / "agents" / "places" / "curator.md").write_text(curator)
    (root / "agents" / "projects" / "article.md").write_text("---\nevidence:\n  - articles: [places]\n---\n\n# Output\n\n## Summary\n")
    (root / "agents" / "places" / "article.md").write_text("---\nevidence:\n  - articles: [projects]\n---\n\n# Output\n\n## Summary\n")
    (root / "agents" / "reports" / "article.md").write_text("---\nevidence:\n  - query: { from: facts }\n---\n\n# Output\n\n## Summary\n")
    extraction = FakeExtraction()
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(folder, extraction=extraction,
                                                                   agent_run=ScriptedRun(lambda p: {"Summary": "x"})))
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def run(*args: str, input: str | None = None) -> str:
        return CliRunner().invoke(main, [*args, "--workspace", str(root)], input=input).output

    seen = [run("admit", "projects/Apollo Launch", "places/North depot")]
    with Cerebro(root, extraction=extraction) as workspace:
        carry(workspace)  # the curators make what was asked for
    seen += [run("admit", "projects/Apollo Launch"),
            run("admit", "Ada Park"), run("admit", "reports/Weekly"), run("admit", "plaecs/north-depot"),
            run("admit", "projects/Apollo Rollout"), run("admit", "projects/Apollo"), run("admit")]
    with Cerebro(root, extraction=extraction) as workspace:
        propose(workspace.store, Proposal(id="p_one", kind="article.admit", target="projects/lumen",
                                      by=Caller("projects/curator", "agent"), at="2026-11-18T09:00:00Z"))
        propose(workspace.store, Proposal(id="p_two", kind="article.admit", target="projects/vega",
                                          by=Caller("places/curator", "agent"), at="2026-11-18T09:00:00Z"))
    seen += [walked := run("review", input=""), run("review", "--accept", "p_one"), run("review", "--accept", "p_one"),
             run("connect", "code", input=""), run("status")]
    hands = [datetime(2026, 11, 17, 9, tzinfo=UTC)]
    with Cerebro(root, extraction=extraction, clock=lambda: hands[0], connectors=[FakeConnector("notes", [])],
                 agent_run=ScriptedRun(lambda p: {"Summary": "x"})) as workspace:
        first = workspace.run_once(scheduled=True)
        hands[0] += timedelta(hours=1)
        (root / "agents" / "places" / "article.md").write_text(
            "---\nevidence:\n  - articles: [projects]\n---\n\n# Output\n\n## Summary\n\nShort.\n")
        article = workspace.store.query("places")[0].identity.id
        mark = {"ran": "2026-11-17T09:00:00Z", "file": "", "setting": ""}
        seen += [*first.warnings, *first.skipped.values(), *workspace.run_once(scheduled=True).skipped.values(),
                 _at_once(workspace, workspace.agent("places/article"), article, mark)]
    assert any("compile order: places <-> projects" in line for line in seen), seen
    assert any("is already in the working set" in line for line in seen), seen
    assert any("is already accepted, nothing to do" in line for line in seen), seen
    assert any("input is not a terminal" in line for line in seen), seen
    assert "1 proposal waits with its collection's curator" in walked, walked
    assert not [line for line in seen if words.search(line) or WORDS.search(line)], seen


def test_a_runs_own_lines_on_its_hand_in_and_its_room_say_no_section_brain_or_page() -> None:
    """The lines a run gives the model or a person about its hand-in and its room: the note on a reply taken as text,
    on a run that handed in nothing at each ending, the step of a run that ran and wrote nothing, the line to decide
    now, the note before a curator's turn, and what a curator out of room or calls is told, which never asks it to
    hand in text, since it hands in none."""
    from types import SimpleNamespace

    from cerebro.run import (
        CUT_NOTHING,
        DECIDE_NOTE,
        HANDED_NOTHING,
        NO_CALLS,
        NO_CALLS_DECIDING,
        NO_ROOM,
        NO_ROOM_DECIDING,
        SPENT_NOTHING,
        STANDS,
        TOOK_REPLY,
        WROTE_NOTHING,
        _Room,
    )
    from cerebro.runner import _compiled_step

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    floor = _Room(100_000, "", 40, deciding=True, decisions=["admit", "leave_out"]).floor_line()
    ran = SimpleNamespace(skipped=False, article=None, runs=[],
                          run=SimpleNamespace(status=WROTE_NOTHING, note=HANDED_NOTHING))
    step = _compiled_step(SimpleNamespace(id="things/article"), ran)["text"]
    lines = [TOOK_REPLY, HANDED_NOTHING, SPENT_NOTHING, CUT_NOTHING, STANDS, step, DECIDE_NOTE, floor, NO_ROOM,
             NO_CALLS, NO_ROOM_DECIDING, NO_CALLS_DECIDING]
    assert not [line for line in lines if words.search(line) or WORDS.search(line)]
    assert "admit or leave_out" in floor
    assert not [line for line in (DECIDE_NOTE, floor, NO_ROOM_DECIDING, NO_CALLS_DECIDING) if "hand in" in line.lower()]


def test_the_lines_a_pass_and_a_run_by_hand_end_with_for_a_run_that_wrote_nothing_say_no_section_brain_or_page(
        capsys: Any) -> None:
    """A pass's line for each run that ran and wrote nothing, its count of what landing made safe and its closing count,
    and a run by hand's line for one."""
    from cerebro import cli
    from cerebro.compile import Compiled
    from cerebro.run import HANDED_NOTHING, WROTE_NOTHING, Run, Usage
    from cerebro.runner import PassResult

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    cli._print(PassResult(compiled=["things/first"], ran={"things/article on things/second": HANDED_NOTHING},
                          cleaned={"things/first": 2}))
    run = Run(id="r1", agent="things/article", target="things", status=WROTE_NOTHING, transcript=[],
              usage=Usage(model="scripted"), note=HANDED_NOTHING)
    cli._print_compiled("things/article", "things/second", Compiled(run=run, article=None, runs=[run]))
    lines = capsys.readouterr().out.splitlines()
    assert any("ran and wrote nothing" in line for line in lines) and any(line.startswith("ran      ") for line in lines)
    assert not [line for line in lines if words.search(line) or WORDS.search(line)], lines


def test_agents_run_on_many_articles_and_a_run_by_hand_over_earlier_text_say_no_section_brain_or_page(
        tmp_path: Path, monkeypatch: Any) -> None:
    """The failed line of each article `agents run` names, its closing count, and the lines of a run by hand that
    wrote nothing over its earlier text."""
    from test_handin_cli import Workspace

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    w = Workspace(tmp_path / "w", monkeypatch)
    for name in ("alpha", "beta"):
        assert w.cli("admit", f"things/{name}").exit_code == 0
    with w.open() as workspace:
        carry(workspace)
    w.failing.add("things/alpha")
    many = w.cli("agents", "run", "things/article")
    assert w.cli("agents", "run", "reports/weekly").exit_code == 0
    w.said[0] = lambda prompt: ""
    again = w.cli("agents", "run", "reports/weekly")
    lines = [*many.stdout.splitlines(), *many.stderr.splitlines(), *again.output.splitlines()]
    assert any(line.startswith("failed   ") for line in lines) and any("runs failed" in line for line in lines), lines
    assert any(line.startswith("ran      reports/weekly") for line in lines), lines
    assert not [line for line in lines if words.search(line) or WORDS.search(line)], lines


def test_a_composed_articles_failed_and_dropped_lines_and_agents_runs_skipped_lines_say_no_section_brain_or_page(
        tmp_path: Path, monkeypatch: Any) -> None:
    """The `failed` line of a composed article a subagent's failure failed, in a pass, by hand and in `status`; the
    notes of one whose content grounding dropped whole; and `agents run`'s `skipped` line and its line when every
    article skipped."""
    from test_handin_composed import CURATOR, Workspace, cited, composed

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    failing = composed(tmp_path / "a", monkeypatch, subs=("risks", "next"))
    failing.run.how |= {"things/subagents/risks": "fail", "things/subagents/next": cited}
    lines = failing.cli("serve", "--once", "--plain").output.splitlines()
    lines += failing.cli("agents", "run", "things/article").output.splitlines()
    lines += failing.cli("status").output.splitlines()
    assert any("a subagent's run failed, so nothing of the article's run lands" in line for line in lines), lines
    dropped = composed(tmp_path / "b", monkeypatch)
    said: list[str] = []
    dropped.run.how["things/subagents/overview"] = lambda prompt: said.append(cited(prompt)) or said[-1]
    dropped.cli("agents", "run", "things/article")
    dropped.move()
    dropped.run.how["things/subagents/overview"] = lambda prompt: said[0]
    lines += dropped.cli("agents", "run", "things/article").output.splitlines()
    assert any("nothing it wrote grounds now" in line for line in lines), lines  # grounded again, as keep_part
    kept = composed(tmp_path / "d", monkeypatch)
    kept.run.how["things/subagents/overview"] = cited
    kept.cli("agents", "run", "things/article")
    kept.notes.add(kept.note("yard", 1))  # its evidence moves, and what it cited still stands, so its part grounds
    kept.take_in()
    kept.run.how["things/subagents/overview"] = ""
    lines += kept.cli("agents", "run", "things/article").output.splitlines()
    assert any("the article keeps what it held" in line for line in lines), lines
    curator = ("---\nevidence:\n  - query: { from: facts, source: notes }\nheartbeat: [change]\n---\n\n"
               "Keep the things.\n")
    curating = Workspace(tmp_path / "e", monkeypatch, {"agents/things/curator.md": curator}, admitted=("things/alpha",))
    curating.take_in()
    curating.run.how["things/curator"] = "fail"
    lines += curating.cli("serve", "--once", "--plain").output.splitlines()
    assert any(line.startswith("failed    things/curator: ") for line in lines), lines
    writer = ("---\nevidence:\n  - facts: { window: last_365_days }\n---\n\nWrite the thing.\n\n# Output\n\n## Summary\n")
    many = Workspace(tmp_path / "c", monkeypatch, {"agents/things/article.md": writer, "agents/things/curator.md": CURATOR},
                     admitted=("things/alpha", "things/beta"), notes=())
    lines += many.cli("agents", "run", "things/article").output.splitlines()
    assert any(line.startswith("ran      things/article on things/beta: wrote nothing") for line in lines), lines  # run
    assert not [line for line in lines if words.search(line) or WORDS.search(line)], lines


def test_a_composers_own_failed_run_a_run_whose_text_went_whole_and_a_curator_by_hand_say_no_section_brain_or_page(
        tmp_path: Path, monkeypatch: Any) -> None:
    """The `failed` lines of a composed article whose agent's own run failed, by hand and in a pass, and the warning
    of a run that landed nothing whose earlier text went whole, each line of it citing only retracted facts."""
    from test_handin_composed import CURATOR, OWN, SUB, WRITER, Workspace, cited

    from cerebro.compile import NONE_LEFT

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    own = Workspace(tmp_path / "a", monkeypatch, {"agents/things/article.md": OWN, "agents/things/curator.md": CURATOR,
                                                  "agents/things/subagents/overview.md": SUB.format(heading="Overview")},
                    admitted=("things/alpha",))
    own.take_in()
    own.run.how |= {"things/subagents/overview": cited, "things/article": "fail"}
    by_hand = own.cli("agents", "run", "things/article")
    lines = [*by_hand.stdout.splitlines(), *by_hand.stderr.splitlines()]
    own.at[0] = own.at[0].replace(hour=11)
    lines += own.cli("serve", "--once", "--plain").output.splitlines()
    assert any(line.startswith("failed    things/article on things/alpha: run ") for line in lines), lines
    gone = Workspace(tmp_path / "b", monkeypatch, {"agents/reports/weekly.md": WRITER})
    gone.take_in()
    gone.run.how["reports/weekly"] = lambda prompt: f"## Summary\n\n{cited(prompt)}"
    gone.cli("agents", "run", "reports/weekly")
    gone.move()
    gone.run.how["reports/weekly"] = ""
    lines += gone.cli("agents", "run", "reports/weekly").output.splitlines()
    assert any(NONE_LEFT in line for line in lines), lines
    curator = ("---\nevidence:\n  - query: { from: facts, source: notes }\n---\n\nKeep the things.\n")
    for n, ending in enumerate((("wrote nothing", "it ended with an empty reply"), ("budget spent", "its budget"))):
        curating = Workspace(tmp_path / f"c{n}", monkeypatch, {"agents/things/curator.md": curator},
                             admitted=("things/alpha",))
        curating.take_in()
        curating.run.how["things/curator"] = ending
        lines += curating.cli("agents", "run", "things/curator").output.splitlines()
    assert sum(line.startswith("ran      things/curator: changed nothing, ") for line in lines) == 2, lines
    assert not [line for line in lines if words.search(line) or WORDS.search(line)], lines


def test_every_line_connects_lobby_its_lists_and_their_writes_spell_says_no_section_brain_or_page() -> None:
    """Every string the connect lobby's code spells, docstrings left out: its screens, the command's lines, the
    writer's refusals and each connector's offer and notes."""
    src = Path(__file__).resolve().parents[1] / "src" / "cerebro"
    checks = Path(__file__).resolve().parents[1] / "checks"
    where = {src / "connect_screen.py": None,
             src / "cli.py": {"_drawing", "_NoSource", "_rows", "_places", "_table", "_answer", "_connect_one",
                              "_text", "_pick", "_offer", "_open", "_fails", "_no_connect", "_failed", "_end",
                              "_lobby", "_walk", "_pick_off_terminal", "connect_command", "_connect", "_ConnectUsage",
                              "_ConnectCommand", "_known", "_unwrite", "_guided_init",
                              "_budget_and_zone", "_init_summary", "_run_tool", "init", "_public_mail",
                              "_prove_on", "_own_command", "_client_environ", "secrets",
                              "_keys_alone", "_key_session"},
             src / "scaffold.py": {"own_domains", "set_identity", "set_model", "check_email",
                                   "check_text", "check_domain", "website_domain", "org_guess", "check_answers",
                                   "_workspace_text", "_profile_text", "PLACEHOLDER", "PROFILE_BODY", "init"},
             src / "connect.py": {"picks", "answers", "logs_in", "places", "write_source", "new_source", "add_source",
                                  "_nothing", "_read", "connect", "_Check", "_take_back", "_set_profile",
                                  "_profile_list"},
             src / "connectors" / "gong.py": {"find_you", "_users", "prove", "prove_key"},
             src / "connectors" / "mcp_oauth.py": {"authorize"},
             src / "connector.py": {"with_list", "_block", "_item", "_comment_of"},
             src / "connectors" / "slack.py": {"offer", "_choice", "_grouped", "_grouping_request", "_grouping",
                                               "find_you"},
             src / "connectors" / "github.py": {"offer"}, src / "connectors" / "linear.py": {"offer"},
             src / "app.py": {"asking", "ask_room", "ask_model", "prove_key"}, src / "run.py": {"rewrite"},
             checks / "live_channel_grouping.py": None, src / "guided.py": None, src / "mcp_clients.py": None,
             checks / "init_demo.py": None, src / "keys.py": None,
             checks / "secrets_demo.py": None}
    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    said = []
    for path, names in where.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.ClassDef) or (names is not None and node.name not in names):
                continue
            docs = {id(n.value) for n in ast.walk(node) if isinstance(n, ast.FunctionDef | ast.ClassDef)
                    and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
                    for n in [n.body[0]]}
            said += [c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)
                     and id(c) not in docs]
        for node in tree.body:  # module constants too, such as a line a step says or a file's body
            targets = [t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target]
                                      if isinstance(node, ast.AnnAssign) else []) if isinstance(t, ast.Name)]
            if targets and (names is None or set(targets) & names):
                said += [c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    assert len(said) > 100
    bad = [line for line in said if words.search(line) or WORDS.search(line)]
    assert not bad, bad



def test_the_lines_show_prompt_and_a_dry_run_add_say_no_section_brain_or_page(tmp_path: Path, monkeypatch: Any) -> None:
    """The lines `agents run --show-prompt` and `compile --dry-run` write around what a run sends: the headers, each
    note, each row and count, and their refusals. What the model is given, the prompt and its tool list, is shown as
    it is sent."""
    from test_try_first import TIGHT_WRITER, Workspace, note, own_lines

    from cerebro import preview

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    many = [note(f"b{i}", f"Detail number {i} of the week: " + "words " * 60) for i in range(60)]
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."), *many)
    (tmp_path / "agents" / "reports" / "tight.md").write_text(TIGHT_WRITER)
    tight = w.run("agents", "run", "reports/tight", "--show-prompt").output.splitlines()
    assert any(line.startswith("sent with tool_choice none") for line in tight), tight
    seen = [w.run("agents", "run", "reports/weekly", "--show-prompt").output,
            w.run("agents", "run", "things/article", "--show-prompt").output,
            w.run("agents", "run", "things/article", "gamma", "--show-prompt").output,
            w.run("agents", "run", "things/curator", "alpha", "--show-prompt").output,
            w.run("compile", "--dry-run").output, w.run("compile", "--dry-run", "--article", "things/gamma").output]
    lines = [line for out in seen for line in own_lines(out)]
    lines += [line for line in tight if line.startswith("sent with tool_choice none")]
    lines += [preview.HAND_IN_ONLY, preview.NEXT_PASS, preview.ROOM, preview.FIRST_RUN, preview.FIRST_WAITS,
              preview.FIRST_BUILDS, preview.SERVE_HOLDS, preview.NOT_DUE, preview.cap_words("model calls spent"),
              preview.cap_words("compilation spent")]
    lines += [preview.SHOWN_END, preview.DRY_END, preview.UNSEARCHED, preview.UNJUDGED, preview.UNRANKED,
              preview.UNTOLD, preview.UNREAD, preview.DROPPED, *preview.NOT_KNOWN.values(),
              preview.UNRUN, preview.NOT_RUN, preview.UNREAD_SOURCES, preview.NO_TOOL, preview.THEN,
              "things/gamma is archived, left alone",
              preview.UNKNOWN_OWN, "1 written, 0 fresh, 1 failed.",
              "Invalid value for '--article': give an article's id, its path under articles/, or a collection's folder",
              ("no article or collection beta: name an article by its id or its path under articles/, or a "
               "collection by its folder"), "Error: 1 agent run failed and 1 agent file refused: see their lines above"]
    assert len(lines) > 12 and not [line for line in lines if words.search(line) or WORDS.search(line)], lines


# Rule 1 of AGENTS.md: the engine is domain agnostic, in its code, its comments, its docstrings and its examples.
SALES = re.compile(r"(?-i:(?<![a-z]))(accounts?|deals?|opportunit(?:y|ies)|contacts?|crms?|salesforce|prospects?"
                   r"|customers?|vendors?|invoices?|pitch(?:es)?|sales|acme[a-z-]*|globex|compan(?:y|ies)|close_date|last_activity"
                   r"|pipelines?|quotas?|stakeholders?)"
                   r"(?-i:(?![a-z]))", re.IGNORECASE)
# Source the engine's rule does not reach, each with its reason.
NOT_ENGINE = {
    "templates/": "a template holds its own domain's words",
    **{f"connectors/{name}": "a connector's own file names its own service's things" for name in (
        "calendar.py", "calendar_ical.py", "calendar_rrule.py", "github.py", "github_render.py", "gmail.py",
        "gmail_imap.py", "gmail_text.py", "gong.py", "granola.py", "granola_parse.py", "linear.py", "salesforce.py",
        "slack.py")},
}
# Stated departures: by file, each phrase the engine still spells, with why it stays. A phrase is taken out of a
# line before the line is read, so a new sales word beside it still fails.
SALES_KEPT: dict[str, dict[str, str]] = {
    "cli.py": {
        "other accounts on this machine": "an operating system's account, not a sales one",
    },
    "guided.py": {
        "accounts/fireworks/models/": "a provider's own path",
    },
    "connectors/__init__.py": {
        "from .salesforce import": "the Salesforce connector's module",
        "SalesforceConnector": "the Salesforce connector's class",
        '"salesforce": SalesforceConnector': "a connector's kind name, the registry's key",
        "linear, mcp_source, salesforce, slack": "the connectors' modules, each by its kind's name",
        '"salesforce": salesforce': "a connector's kind name, the key of its module",
    },
    "connectors/questions.py": {
        '"invoice", "invoices"': "a data list of machine senders that code matches addresses on",
    },
    "curate.py": {
        '{"domain": ["acme.example"]}': "the admit tool's description, which a model reads: it waits for a live run",
    },
    "evals.py": {
        '("accounts/article", "accounts/eval")': "the grounding eval's agent ids, which its saved cases name",
        '"Synthetic accounts, for an eval of grounding."': "what the grounding judge is given",
    },
    "evidence.py": {
        '"vendor"': "a folder name code skips, as package managers name it",
        "VENDORED": "the name of that list of folders",
        "accounts/fireworks/": "a provider's own model path",
    },
    "models.py": {"accounts/fireworks/": "a provider's own model path"},
    "private.py": {"other accounts on this": "an operating system's account, not a sales one"},
    "run.py": {"accounts/fireworks/": "a provider's own model path"},
    "secrets.py": {
        "account: str": "the keyring API's own parameter name",
        "{account}": "the keyring API's own parameter name",
        '_password", account)': "the keyring API's own parameter name",
        "_read(account)": "the keyring API's own parameter name",
    },
}


def sales_words(name: str, text: str) -> list[str]:
    kept = sorted(SALES_KEPT.get(name, {}), key=len, reverse=True)
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        rest = line
        for phrase in kept:
            rest = rest.replace(phrase, " ")
        if SALES.search(rest):
            found.append(f"{name}:{n}: {line.strip()[:100]}")
    return found


def test_no_engine_source_outside_templates_and_connectors_says_a_sales_word() -> None:
    found = []
    for path in sorted(p for p in SRC.rglob("*") if p.suffix in (".py", ".md", ".j2", ".yaml")):
        name = path.relative_to(SRC).as_posix()
        if not any(name == skipped or name.startswith(skipped) for skipped in NOT_ENGINE):
            found += sales_words(name, path.read_text(encoding="utf-8"))
    assert found == []


def test_the_sales_word_guard_finds_a_word_in_a_comment_a_docstring_and_a_key_and_reads_past_a_kept_phrase() -> None:
    assert sales_words("x.py", '"""Bind a deal to its Account."""\n# the CRM row\nkey = "salesforce_id"\nfine = 1\n') == [
        'x.py:1: """Bind a deal to its Account."""', "x.py:2: # the CRM row", 'x.py:3: key = "salesforce_id"']
    assert sales_words("models.py", "discount = 1  # recount, dealt, accountable\n") == []
    assert len(sales_words("x.py", "AccountId = 1\ncustomerName = 2\nclass DealStage: ...\nCRMs\n")) == 4
    assert len(sales_words("x.md", "sort by close_date\nlast_activity: 1\nthe pipeline\nquotas\n")) == 4
    SALES_KEPT["x.py"] = {"accounts/fireworks/": "a provider's own path"}
    try:
        assert sales_words("x.py", 'a = "accounts/fireworks/models/m"\n') == []
        assert sales_words("x.py", 'a = "accounts/fireworks/models/m"  # an account\n') != []
    finally:
        del SALES_KEPT["x.py"]


def test_reviews_refusals_of_a_curators_flag_and_of_a_faulty_line_say_no_section_brain_or_page(
        tmp_path: Path, monkeypatch: Any) -> None:
    from cerebro import cli
    from cerebro.fakes import FakeExtraction

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    root = tmp_path / "w"
    (root / "agents" / "projects").mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "agents" / "projects" / "curator.md").write_text(
        "---\nevidence:\n  - query: { from: facts }\nheartbeat: [manual]\n---\n\nKeep it.\n")
    (root / "agents" / "projects" / "article.md").write_text("---\nheartbeat: [manual]\n---\n\nWrite it.\n")
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(folder, extraction=FakeExtraction()))

    def run(*args: str) -> str:
        return CliRunner().invoke(main, ["review", *args, "--workspace", str(root)]).output

    seen = [run("--reject-from", "agents/projects/article.md"), run("--accept-from", "agents/projects/curator.md"),
            run("--reject-from", "agents/projects/curator.md,projects/curator"), run("--accept", "p_nothing")]
    assert any("curates no collection" in line for line in seen), seen
    assert any("no admit from agents/projects/curator.md is awaiting your review" in line for line in seen), seen
    assert not [line for line in seen if words.search(line) or WORDS.search(line)], seen


def test_reviews_lines_for_a_line_that_cannot_land_and_a_value_that_names_nothing_say_no_section_brain_or_page(
        tmp_path: Path, monkeypatch: Any) -> None:
    from test_review import Workspace, facts_of, retract_by

    from cerebro.facts import FactNotFound

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    workspace = Workspace(tmp_path / "w", monkeypatch)
    fact, twice = facts_of(workspace, "Lumen moved to the east wing.", "Harbor closed for a week.")
    retract_by(workspace, "p_retract00001", fact)
    retract_by(workspace, "p_retract00002", twice)
    retract_by(workspace, "p_retract00003", twice)

    def refused(fact_id: str) -> None:
        raise FactNotFound(f"{fact_id} stays")

    seen = [workspace.run("review", "--accept", "p_retract00002,p_retract00003").output,
            workspace.run("review", "--accept", ",").output, workspace.run("review", "--reject-from", " ").output]
    monkeypatch.setattr(workspace.extraction, "retract", refused)
    seen.append(workspace.run("review", "--accept", "p_retract00001").output)
    assert any("is retracted already" in line for line in seen), seen
    assert any("it names no curator" in line for line in seen), seen
    assert any("The facts layer kept" in line for line in seen), seen
    assert not [line for line in seen if words.search(line) or WORDS.search(line)], seen


def test_what_a_tell_whose_facts_wait_says_has_no_section_brain_or_page(tmp_path: Path) -> None:
    from test_tell_retry import SAID, YOU, RetractFails, in_memory, telling, told

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    said = []
    (tmp_path / "waiting").mkdir()
    (tmp_path / "taken").mkdir()
    waiting = in_memory(tmp_path / "waiting", None)
    said += waiting.tell(YOU, SAID, "projects/harbor", "fact").notes
    layer = RetractFails(RuntimeError("the facts layer is down"), 1)
    taken = in_memory(tmp_path / "taken", layer)
    thread, out = telling(taken)
    assert layer.started.wait(10)
    taken.retract(YOU, told(taken), "said in error")
    layer.go.set()
    thread.join(10)
    said += out[0].notes
    assert any("its facts did not come out yet" in n for n in said)
    assert any("taken back while its facts came out" in n for n in said)
    assert not [n for n in said if words.search(n)]


def test_the_underived_row_of_status_and_a_pass_that_leaves_a_claimed_tell_say_no_section_brain_or_page(
        tmp_path: Path, monkeypatch: Any) -> None:
    from test_tell_retry import SAID, YOU, harbor, in_memory, make_workspace
    from test_tell_waiting import Down, kept

    from cerebro.fakes import FakeExtraction
    from cerebro.records import Claimed

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    harbor(tmp_path)
    with make_workspace(tmp_path, Down()) as workspace:
        workspace.tell(YOU, SAID, "projects/harbor", "fact")
    said = [line for line in CliRunner().invoke(main, ["status", "--workspace", str(tmp_path)]).output.splitlines()
            if line.startswith("underived ")]
    (tmp_path / "held").mkdir()
    held = in_memory(tmp_path / "held", Down())
    kept(held, {"pid": 1, "until": "2099-01-01T00:00:00Z"})
    monkeypatch.setattr("os.kill", lambda pid, signal: (_ for _ in ()).throw(PermissionError(1, "refused")))
    said += held.run_once(agents=False).skipped.values()
    monkeypatch.undo()

    def lost(*args: Any, **options: Any) -> list[str]:
        raise Claimed("taken over")

    monkeypatch.setattr("cerebro.records.extract_told", lost)
    said += in_memory(tmp_path / "held", FakeExtraction()).tell(YOU, SAID, "projects/harbor", "fact").notes
    assert len(said) == 3 and not [line for line in said if words.search(line)]


def test_the_lines_a_stop_of_serve_says_and_the_refusal_of_a_stopped_call_say_no_section_brain_or_page() -> None:
    import inspect

    from cerebro import cli, mcp, runner

    lines = [*cli._cut_lines(1, 0), *cli._cut_lines(2, 3), mcp.STOPPED_NOTHING, mcp.STOPPED_WROTE]
    spelled = inspect.getsource(cli._serving) + inspect.getsource(runner._lease)
    lines += re.findall(r'"(stopped   [^"]*|note      [^"]*|the store is locked[^"]*)"', spelled)
    assert len(lines) >= 9, lines
    for line in lines:
        assert not re.search(r"\b(sections?|brains?|pages?)\b", line, re.IGNORECASE), line


def test_the_lines_said_of_work_a_daily_cap_or_a_hold_kept_from_starting_say_no_section_brain_or_page(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Each of these lines a person sees, as it prints: a part's note when it waits for the daily cap
    or did not run for want of a price, a part held for room, the `waiting` line and stop line of `agents run`, and
    `serve`'s line when calls resume."""
    from datetime import timedelta

    from test_pass_at_cap import DAILY_PART, DAY, Logged, collection, composed, opener

    import cerebro.compile as compile_module
    from cerebro import cli
    from cerebro.budget import HeldForRoom

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    seen: list[str] = []
    capped = composed(tmp_path / "capped")
    seen += opener(capped, Logged(capped, cross_at=1))().run_once().warnings
    priced = composed(tmp_path / "priced")
    (priced.root / "agents" / "things" / "subagents" / "risks.md").write_text(DAILY_PART.format(h="Risks").replace(
        "heartbeat: [daily]\n", "heartbeat: [daily]\nmodel: { provider: fireworks, model: made-up-7 }\n"))
    unpriced = opener(priced, Logged(priced))().run_once()
    seen += unpriced.warnings + [f"failed    {key}: {why}" for key, why in unpriced.failed.items()]
    held = composed(tmp_path / "held")
    real = compile_module.run_agent

    def refusing(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if agent.id.endswith("risks") and agent.target == "things/a":
            raise HeldForRoom("held for room under the cap, runs next pass: runs under way hold $1")
        return real(agent, *args, **kwargs)

    monkeypatch.setattr(compile_module, "run_agent", refusing)
    monkeypatch.setattr(held, "open", opener(held, Logged(held)))
    seen += held.cli(monkeypatch, "agents", "run", "things/article").output.splitlines()
    monkeypatch.setattr(compile_module, "run_agent", real)
    waits = collection(tmp_path / "waits", ("a", "b"))
    monkeypatch.setattr(waits, "open", opener(waits, Logged(waits, cross_at=1)))
    seen += waits.cli(monkeypatch, "agents", "run", "things/article").output.splitlines()
    said: list[str] = []
    monkeypatch.setattr(cli, "_echo", lambda message="", **kw: said.append(str(message)))
    workspace = waits.open()
    printed = cli._passes(workspace)
    printed(workspace.run_once(scheduled=True))
    waits.clock.at = DAY + timedelta(days=1)
    printed(workspace.run_once(scheduled=True))
    seen += said
    for wording in ("waits for the daily cap (", "did not run, since", "held     things/subagents/risks on things/a",
                    "waiting  things/b: the daily budget is spent", "so model calls resume"):
        assert any(wording in line for line in seen), (wording, seen)
    assert not [line for line in seen if words.search(line) or WORDS.search(line)], seen


def test_the_lines_said_of_runs_that_did_not_finish_say_no_section_brain_or_page(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Each line a person sees of a run that did not finish, as it prints: a pass's line for an article a part
    failed, `agents run`'s line for a failed article and its count, a run that spent its own budget, when a held part
    runs, and `serve`'s lines when a cap read again lifts or a settings file does not read."""
    from datetime import timedelta

    from test_pass_at_cap import DAY, Logged, collection, composed, opener
    from test_unfinished_runs import RISKS, Unfinished, passes

    import cerebro.compile as compile_module
    from cerebro.budget import HeldForRoom
    from cerebro.runner import _read_caps

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    seen: list[str] = []
    failing = composed(tmp_path / "failing")
    seen += [f"{key}: {why}" for key, why in passes(failing, Unfinished(failing, RISKS, "things/b", times=99), 2)[1]
             .failed.items()]
    article = collection(tmp_path / "article", ("a", "b"))
    monkeypatch.setattr(article, "open", opener(article, Unfinished(article, "things/article", "things/a")))
    seen += article.cli(monkeypatch, "agents", "run", "things/article").output.splitlines()
    own = collection(tmp_path / "own", ("a", "b"))
    monkeypatch.setattr(own, "open", opener(own, Unfinished(own, "things/article", "things/b", how="own")))
    seen += own.cli(monkeypatch, "agents", "run", "things/article").output.splitlines()
    held = composed(tmp_path / "held")
    passes(held, Logged(held), 1)
    held.clock.at = DAY + timedelta(hours=1)
    real = compile_module.run_agent

    def refusing(agent: Any, *args: Any, **kwargs: Any) -> Any:
        if agent.id == RISKS and agent.target == "things/b":
            raise HeldForRoom("held for room under the cap, runs next pass: runs under way hold $1")
        return real(agent, *args, **kwargs)

    monkeypatch.setattr(compile_module, "run_agent", refusing)
    monkeypatch.setattr(held, "open", opener(held, Logged(held)))
    seen += held.cli(monkeypatch, "agents", "run", "things/article").output.splitlines()
    monkeypatch.setattr(compile_module, "run_agent", real)
    capped = collection(tmp_path / "capped", ("a",))
    capped.spent("2026-11-19T00:05:00Z", "compilation", 60.0)
    workspace, kept = capped.open(), {}
    capped.settings("budget: { daily_usd: [\n")
    _read_caps(workspace, kept, seen.append)
    capped.settings("budget: { daily_usd: 500 }\n")
    _read_caps(workspace, kept, seen.append)
    for wording in ("a subagent's run failed, so nothing of the article's run lands",
                    "failed   things/article on things/a: run", "Error: things/article: 1 of 2 runs failed",
                    "spent its own budget, not a daily cap",
                    "runs at its writer's next scheduled run, from", "so the daily budget keeps the caps serve read",
                    "Daily budget: cap is now $500.00, so model calls resume"):
        assert any(wording in line for line in seen), (wording, seen)
    assert not [line for line in seen if words.search(line) or WORDS.search(line)], seen


def test_what_a_tell_whose_claim_was_taken_over_says_has_no_section_brain_or_page(tmp_path: Path) -> None:
    from test_tell_retry import YOU, in_memory
    from test_tell_waiting import TWO, Fresh, lapsed, taken_over

    from cerebro.records import retract

    said = []
    for name, act in [("over", lambda workspace, layer: taken_over(workspace, layer)),
                      ("back", lambda workspace, layer: retract(workspace.store, layer, YOU, lapsed(workspace), "in error"))]:
        (tmp_path / name).mkdir()
        layer = Fresh()
        workspace = in_memory(tmp_path / name, layer)
        layer.before = lambda workspace=workspace, layer=layer, act=act: act(workspace, layer)
        said += workspace.tell(YOU, TWO, "projects/harbor", "fact").notes
    assert len(said) == 2 and not [n for n in said if re.search(r"\b(sections?|brains?|pages?)\b", n, re.IGNORECASE)]


def test_a_busy_stores_line_and_the_search_behind_note_say_no_section_brain_or_page() -> None:
    from pathlib import Path

    from cerebro.lookup import SEARCH_BEHIND
    from cerebro.store import busy_line

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    lines = [busy_line(Path(".cerebro/cerebro.sqlite")), busy_line(None), SEARCH_BEHIND]
    assert not [line for line in lines if words.search(line) or WORDS.search(line)], lines


def test_what_a_find_or_a_query_that_matched_nothing_says_has_no_section_brain_or_page() -> None:
    from cerebro.fakes import FakeExtraction
    from cerebro.lookup import StoreLookup

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    you, store = Caller("you", "person"), MemoryStore()
    lookup = StoreLookup(store, FakeExtraction())
    said = lookup.find(you, "Orion").warnings + lookup.query(you, "facts").warnings
    store.create(Identity("things/a", "things", "A", status="archived"))
    store.append("proposals", {"id": "p_1", "kind": "admit"})
    for source, filters in (("facts", {"kind": "decision"}), ("proposals", {"kind": "merge"}), ("merges", {}),
                            ("things", {}), ("things", {"status": "waiting"}), ("things", {"id": "things/a"})):
        said += lookup.query(you, source, filters).warnings
    assert len(said) == 8 and not [line for line in said if words.search(line) or WORDS.search(line)], said


def test_no_line_a_presumed_mark_shows_names_a_section_or_a_hand_in() -> None:
    for line in presumed_lines():
        shown = line.split('": ', 1)[-1] if line.startswith("Part: dropped \"") else line
        assert not WORDS.search(shown), shown


def test_the_first_runs_nothing_to_propose_row_and_the_late_lists_line_say_no_section_brain_or_page() -> None:
    import io

    from cerebro.first_run import late_line
    from cerebro.output import Printer, strip_escapes
    from cerebro.progress import FirstRunScreen
    from cerebro.runner import Progress

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    out = Tty()
    p = Printer(out, out, getenv={"TERM": "xterm-256color"}.get, dark=True, width=lambda: 100, tick=3600)
    screen = FirstRunScreen(p, ticking=False, height=lambda: 60)
    screen(Progress("discover", "start", "parts/curator", text="parts"))
    screen(Progress("discover", "end", "parts/curator", mark="nothing"))
    screen(Progress("late", "open", total=2))
    drawn = strip_escapes(out.getvalue())
    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    for line in ("Nothing new for parts collection", late_line(1), late_line(2)):
        assert not words.search(line) and not WORDS.search(line), line
    assert "Nothing new for parts collection" in drawn and late_line(2) in drawn


def test_the_pending_mark_in_find_query_read_a_search_and_status_says_no_section_brain_or_page(tmp_path: Path) -> None:
    from cerebro.article import PENDING
    from cerebro.mcp import render_find, render_query, render_read
    from cerebro.store import SQLiteStore

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b|\bunwritten\b", re.IGNORECASE)
    CliRunner().invoke(main, ["init", str(tmp_path), "--daily-usd", "20"])
    with SQLiteStore.from_settings(tmp_path) as store:
        for n in range(7):
            store.create(Identity(f"calls/c{n}", "calls", f"Kickoff {n}"))
    said = [line for line in CliRunner().invoke(main, ["status", "--workspace", str(tmp_path)]).output.splitlines()
            if line.startswith(("pending ", " " * 10))]
    workspace = Cerebro(tmp_path)
    try:
        you = Caller("you", "person")
        said += [render_find(workspace.lookup.find(you, "Kickoff 0")), render_query(workspace.lookup.query(you, "calls")),
                 render_read(workspace.lookup.read(you, ["calls/c0"])),
                 json.dumps(outside(workspace.lookup.read(you, ["calls/c0"]))),
                 json.dumps(outside(workspace.lookup.query(you, None, {}, matching="kickoff")))]
    finally:
        workspace.close()
    text = "\n".join(said)
    assert "pending   7 articles not yet written:" in text and "and 2 more" in text and PENDING in text
    assert "[pending]" in text and '"pending": true' in text
    assert not words.search(text), words.findall(text)


def test_the_authority_lines_a_person_and_a_curator_read_say_no_section_brain_or_page(tmp_path: Path) -> None:
    """The line a caller is refused with on a row it may not decide, and the waiting lists a curator is shown."""
    from cerebro.agent import load_agent
    from cerebro.curate import _on_yours, _waiting
    from cerebro.records import Proposal, decide, propose

    words = re.compile(r"\bsections?\b|\bbrains?\b|\bpages?\b", re.IGNORECASE)
    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "agents" / "things" / "curator.md").write_text(
        "---\nauthority: { admit: true, merge: true }\nevidence:\n  - held: {}\nheartbeat: [manual]\n---\n\nKeep.\n")
    by, store = Caller("parts/curator", "agent"), MemoryStore()
    admit = Proposal(id="p_1", kind="article.admit", target="things/alder", by=by, at="2026-11-20T09:00:00Z",
                     payload={"name": "Alder", "keys": {"domain": ["alder.example"]}}, reason="named twice")
    apart = Proposal(id="p_2", kind="article.merge", target="things/birch", by=by, at="2026-11-20T09:00:00Z",
                     payload={"with": "things/cedar", "distinct": True}, reason="two firms")
    for row in (admit, apart):
        propose(store, row)
    lines = list(decide(store, by, reject=["p_1"], root=tmp_path).notes.values())
    lines += [line for e in _waiting([admit] * 60, [apart] * 60) for line in [e.heading, *e.lines]]
    shown = _on_yours(store, load_agent(tmp_path / "agents" / "things" / "curator.md", tmp_path / "agents"))
    lines += [line for e in shown for line in [e.heading, *e.lines]]
    assert len(shown[0].lines) == 3 and not [line for line in lines if words.search(line) or WORDS.search(line)], lines
