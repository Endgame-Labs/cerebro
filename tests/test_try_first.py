"""Trying something writes nothing: `agents run <agent> --show-prompt` prints what a run would send its model, the
prompt and then the evidence, and `compile --dry-run` prints what is stale, fresh and failed. Neither calls a model, takes a lease that outlives it, moves a clock, counts a spend or writes, and both work
with no model key. `agents new` points at the first: a try that writes nothing."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from cerebro import Cerebro, Record, cli
from cerebro.agent import Agent, Budget
from cerebro.article import Identity
from cerebro.cli import main
from cerebro.evidence import Evidence
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
from cerebro.preview import NOT_KNOWN, UNREAD_SOURCES, UNTOLD
from cerebro.run import AgentRun, Run, instructions, opening
from cerebro.shown import shown

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)
ESCAPE = "\x1b[2J\x1b[31mred\x07"
WRITER = "---\nevidence:\n  - query: {{ from: facts, source: notes }}\n{front}---\n\nSum up {what}.\n\n# Output\n\n## Summary\n"
CURATOR = "---\nevidence:\n  - query: { from: facts }\nheartbeat: [manual]\n---\n\nKeep the things.\n"


class Hands:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        return self.at


class Refuses(AgentRun):
    """A model that fails the test if it is ever asked."""

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        pytest.fail(f"a model was asked to run {agent.id}")


class Records(AgentRun):
    """A model that keeps what each run sends it, and writes a summary citing the first fact."""

    def __init__(self, clock: Hands, root: Path):
        self.clock, self.root, self.sent = clock, root, []

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        self.sent.append((instructions(agent, self.root), opening(agent, evidence, self.clock(), self.root),
                          [getattr(t, "__name__", "") for t in tools]))
        return ScriptedRun(cited, clock=self.clock).run(agent, evidence, tools, budget)


def cited(prompt: str) -> dict[str, str]:
    """A summary that cites the first fact the evidence shows, as a run must for it to land."""
    first = re.search(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])
    return {"Summary": f"x [^{first.group(1)}]"} if first else {"Summary": "x"}


def note(id: str, body: str) -> Record:
    return Record(id=id, source="notes", revision="1", at="2026-11-16T08:00:00Z", body=body)


class Workspace:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, *notes: Record, front: str = "",
                 settings: str = "{}\n", decider: Any = None):
        self.root, self.hands, self.extraction, self.decider = root, Hands(), FakeExtraction(), decider
        self.notes = FakeConnector("notes", list(notes))
        (root / "agents" / "things").mkdir(parents=True)
        (root / "agents" / "reports").mkdir()
        (root / "settings.yaml").write_text(settings)
        (root / "agents" / "things" / "curator.md").write_text(CURATOR)
        (root / "agents" / "things" / "article.md").write_text(WRITER.format(front=front, what="each thing"))
        (root / "agents" / "reports" / "weekly.md").write_text(WRITER.format(front=front, what="the week"))
        self.model: AgentRun = Refuses()
        monkeypatch.setattr(cli, "open_workspace", lambda folder: self.open())
        for provider in ("FIREWORKS_API_KEY", "TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(provider, raising=False)
        with self.open() as workspace:
            for record in notes:
                self.extraction.extract(record)
            for name in ("alpha", "beta"):
                workspace.store.create(Identity(f"things/{name}", "things", name.title()))

    def open(self) -> Cerebro:
        return Cerebro(self.root, extraction=self.extraction, connectors=[self.notes], clock=self.hands,
                       agent_run=self.model, decider=self.decider)

    def run(self, *args: str) -> Result:
        return CliRunner().invoke(main, [*args, "--workspace", str(self.root)])

    def snapshot(self) -> tuple[dict[str, bytes], list[str]]:
        files = {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*"))
                 if p.is_file() and ".cerebro" not in p.parts and "__pycache__" not in p.parts}
        with sqlite3.connect(self.root / ".cerebro" / "cerebro.sqlite") as db:
            return files, list(db.iterdump())


WORDS = re.compile(r"\b(section|sections|page|pages|brain|account|deal|opportunity|contact|crm)\b", re.IGNORECASE)


def own_lines(output: str) -> list[str]:
    """The lines Cerebro writes itself, around what a prompt holds."""
    return [line for line in output.splitlines() if line.startswith(("===", "note ", "Error:", "Nothing", "things/",
                                                                     "reports/")) or "stale, " in line]


def test_show_prompt_prints_the_prompt_then_the_evidence_and_writes_nothing(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", f"The launch moved to March 3.{ESCAPE}"))
    before = w.snapshot()
    shown_ = w.run("agents", "run", "reports/weekly", "--show-prompt")
    assert shown_.exit_code == 0, shown_.output
    lines = shown_.output.splitlines()
    assert lines[0] == "=== system" and "=== tools" in lines and "=== opening message" in lines
    assert lines.index("=== tools") < lines.index("=== opening message")
    assert lines[lines.index("=== tools") + 2] == "query, read, override, write_article, nothing_to_write"
    assert "Sum up the week." in shown_.output and "The launch moved to March 3." in shown_.output
    assert re.fullmatch(r"=== evidence digest sha256:[0-9a-f]{16} · no model called, nothing written", lines[-1])
    assert "\x1b" not in shown_.output and "\x07" not in shown_.output and "\\x1b[2J" in shown_.output
    assert w.snapshot() == before  # no write, no lease left, no clock moved, no spend counted
    assert not any(WORDS.search(line) for line in own_lines(shown_.output))


def test_show_prompt_is_what_the_run_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    output = w.run("agents", "run", "reports/weekly", "--show-prompt").output
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "reports/weekly").exit_code == 0
    [(system, text, tools)] = w.model.sent
    assert "\n".join(shown(line, None) for line in system.split("\n")) in output
    assert "\n".join(shown(line, None) for line in text.rstrip("\n").split("\n")) in output
    assert ", ".join([*tools, "write_article", "nothing_to_write"]) in output


def test_show_prompt_on_an_agent_of_many_articles_needs_one_named(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    many = w.run("agents", "run", "things/article", "--show-prompt")
    assert many.exit_code == 1 and "things/article runs on 2 articles in the working set: name one" in many.output
    for name in ("alpha", "things/alpha", "articles/things/alpha.md"):
        one = w.run("agents", "run", "things/article", name, "--show-prompt")
        assert one.exit_code == 0 and "Sum up each thing." in one.output, one.output
    elsewhere = w.run("agents", "run", "things/article", "gamma", "--show-prompt")
    assert elsewhere.exit_code == 1 and "gamma is not in articles/things" in elsewhere.output
    curator = w.run("agents", "run", "things/curator", "alpha", "--show-prompt")
    assert curator.exit_code == 1 and "give no article" in curator.output
    assert not any(WORDS.search(line) for line in own_lines(many.output + elsewhere.output + curator.output))


def test_show_prompt_with_empty_evidence_shows_it_as_a_run_sends_it(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch)
    before = w.snapshot()
    empty = w.run("agents", "run", "reports/weekly", "--show-prompt")
    assert empty.exit_code == 0, empty.output
    assert "no fact matches source: notes" in empty.output
    assert "a run would not run it" not in empty.output  # a run runs it, told so
    assert w.snapshot() == before


def test_show_prompt_on_a_curator_shows_its_tools_and_what_woke_it(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    before = w.snapshot()
    curator = w.run("agents", "run", "things/curator", "--show-prompt")
    assert curator.exit_code == 0, curator.output
    lines = curator.output.splitlines()
    assert lines[lines.index("=== tools") + 2].startswith("query, read, admit, link, instruct, archive, merge, split")
    assert "Keep the things." in curator.output and "What woke this run: the first run." in curator.output
    assert w.snapshot() == before


def test_a_run_by_hand_on_one_named_article_writes_that_one_alone(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    w.model = Records(w.hands, tmp_path)
    ran = w.run("agents", "run", "things/article", "beta")
    assert ran.exit_code == 0 and ran.output.splitlines() == [
        "wrote    things/beta  articles/things/beta.md",
        "grounding things/article on things/beta: left 1 sentence unscored"], ran.output  # it has no decision model
    curator = w.run("agents", "run", "things/curator", "beta")
    assert curator.exit_code == 1 and "give no article" in curator.output, curator.output


def test_compile_dry_run_says_what_is_stale_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    before = w.snapshot()
    dry = w.run("compile", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert dry.output.splitlines() == [
        "What the next pass, cerebro serve --once, would run now:",
        "reports/weekly  stale    reports/weekly  its first build",
        "things/alpha    stale    things/article  its first build",
        "things/beta     stale    things/article  its first build",
        "things          not due  things/curator  heartbeat is manual: run it by hand",
        "3 stale, 0 fresh, 0 failed, 1 not due. Nothing was run or written.", UNREAD_SOURCES]
    one = w.run("compile", "--dry-run", "--article", "things/beta")
    assert one.output.splitlines() == ["things/beta  stale  things/article  its first build",
                                       "1 stale, 0 fresh, 0 failed. Nothing was run or written."], one.output
    assert w.snapshot() == before
    assert not any(WORDS.search(line) for line in own_lines(dry.output))
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "things/article").exit_code == 0
    assert w.run("agents", "run", "reports/weekly").exit_code == 0
    w.model = Refuses()
    assert w.run("compile", "--dry-run").output.splitlines() == [
        "What the next pass, cerebro serve --once, would run now:", "things  not due  things/curator  heartbeat is manual: run it by hand",
        "Nothing stale: 3 agent runs fresh, 1 not due. Nothing was run or written.", UNREAD_SOURCES]


def test_compile_dry_run_heeds_an_agents_cooldown_unless_the_article_is_named(tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."),
                  front="heartbeat: { on: [change], cooldown: 6h }\n")
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "reports/weekly").exit_code == 0
    w.model = Refuses()
    w.extraction.extract(note("n2", "The budget is set."))
    w.hands.at += timedelta(hours=1)
    dry = w.run("compile", "--dry-run")
    assert "reports/weekly  cooling down  reports/weekly  new evidence waits until 2026-11-17 15:00, cooldown 6h" \
        in dry.output.splitlines(), dry.output
    assert dry.output.splitlines()[-2] == \
        "2 stale, 0 fresh, 0 failed, 1 cooling down, 1 not due. Nothing was run or written."
    named = w.run("compile", "--dry-run", "--article", "reports/weekly")
    assert named.output.splitlines() == ["reports/weekly  stale  reports/weekly  its evidence moved",
                                         "1 stale, 0 fresh, 0 failed. Nothing was run or written."], named.output


def test_compile_still_needs_its_article_without_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch)
    bare = w.run("compile")
    assert bare.exit_code == 2 and "Missing option '--article'" in bare.output


def test_agents_new_points_at_a_try_that_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch)
    for id in ("projects/harbor", "places/riverside"):
        made = w.run("agents", "new", id)
        assert made.exit_code == 0, made.output
        assert made.output.splitlines()[-1] == f"next     fill in agents/{id}.md, then cerebro agents run {id} --show-prompt"


def test_compile_dry_run_lists_what_fails_to_read_and_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "agents" / "reports" / "broken.md").write_text(
        "---\nevidence:\n  - files: [blob.md]\n---\n\nSum up the blob.\n\n# Output\n\n## Summary\n")
    (tmp_path / "blob.md").write_bytes(b"\xff\xfe not text")
    before = w.snapshot()
    dry = w.run("compile", "--dry-run")
    assert dry.exit_code == 1, dry.output
    lines = dry.output.splitlines()
    assert lines[1].startswith("reports/broken  failed   reports/broken  ") and "cannot be read as UTF-8 text" in lines[1]
    assert lines[-3:] == ["3 stale, 0 fresh, 1 failed, 1 not due. Nothing was run or written.", UNREAD_SOURCES,
                          "Error: 1 agent run failed: see its line above"]
    assert w.snapshot() == before


COMPOSER = "---\nsubagents: [risks]\n---\n\nA brief on each thing.\n\n# Composing\n\nWrite a verdict.\n"
RISKS = "---\nevidence:\n  - query: { from: facts, source: notes }\n---\n\nName the risks.\n\n# Output\n\n## Risks\n"


def unescaped(output: str, sent: str) -> bool:
    return "\n".join(shown(line, None) for line in sent.rstrip("\n").split("\n")) in output


def test_show_prompt_of_a_subagent_and_its_composer_is_what_their_runs_send(tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "agents" / "things" / "article.md").write_text(COMPOSER)
    (tmp_path / "agents" / "things" / "subagents").mkdir()
    (tmp_path / "agents" / "things" / "subagents" / "risks.md").write_text(RISKS)
    part = w.run("agents", "run", "things/subagents/risks", "alpha", "--show-prompt")
    assert part.exit_code == 0, part.output
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "things/article", "alpha").exit_code == 0
    [(sub_system, sub_text, sub_tools), (own_system, own_text, own_tools)] = w.model.sent
    assert "Name the risks." in sub_system and unescaped(part.output, sub_system) and unescaped(part.output, sub_text)
    assert ", ".join([*sub_tools, "return_content", "nothing_to_write"]) in part.output
    w.model = Refuses()
    whole = w.run("agents", "run", "things/article", "alpha", "--show-prompt")
    assert whole.exit_code == 0, whole.output
    assert "Write a verdict." in own_system and unescaped(whole.output, own_system)
    assert unescaped(whole.output, own_text) and "What your subagents returned" in whole.output
    assert ", ".join([*own_tools, "write_article", "nothing_to_write"]) in whole.output


def test_show_prompt_masks_what_the_secrets_store_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.fakes import FakeSecrets

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch key is tok-example-5f3a9c2e81."))
    secrets = FakeSecrets({"notes-token": "tok-example-5f3a9c2e81"})
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(
        tmp_path, extraction=w.extraction, connectors=[w.notes], clock=w.hands, agent_run=Refuses(), secrets=secrets))
    masked = w.run("agents", "run", "reports/weekly", "--show-prompt")
    assert masked.exit_code == 0 and "The launch key is" in masked.output, masked.output
    assert "tok-example-5f3a9c2e81" not in masked.output


class NoSearch(FakeExtraction):
    """The stand-in facts layer, whose search by meaning would ask a model: it fails the test if made."""

    def search(self, text: str, top_k: int = 10, *, source: str | None = None) -> list[Any]:
        pytest.fail("a search by meaning was made")


NEAR = """from cerebro import Workspace, Caller, evidence


@evidence("near")
def near(caller: Caller, workspace: Workspace) -> list[dict[str, str]]:
    \"\"\"What the facts and the things say of the launch.\"\"\"
    found = [*workspace.query(caller, "facts", matching="launch").items, *workspace.query(caller, "things", matching="alpha").items]
    return [{"id": "near-1", "text": f"{len(found)} found", "source": "near"}]
"""


def test_show_prompt_makes_no_search_by_meaning_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    w.extraction = NoSearch()
    w.extraction.extract(note("n1", "The launch moved to March 3."))
    (tmp_path / "extensions.py").write_text(NEAR)
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(
        "---\nevidence:\n  - near: {}\n---\n\nSum up the launch.\n\n# Output\n\n## Summary\n")
    before = w.snapshot()
    searched = w.run("agents", "run", "reports/weekly", "--show-prompt")
    assert searched.exit_code == 0, searched.output
    assert "note     a search by meaning on facts was not made, since it embeds its words with a model" in searched.output
    assert w.run("compile", "--dry-run", "--article", "reports/weekly").exit_code == 0
    assert w.snapshot() == before  # nor is the search index of the store built


# -- neither command writes, asks a model, or prints an unmasked or unclean line -----------------------------------


def agent_file(root: Path, id: str, evidence: str, body: str = "Sum up.") -> None:
    path = root / "agents" / f"{id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nevidence:\n{evidence}---\n\n{body}\n\n# Output\n\n## Summary\n")


TELLS = """from cerebro import Workspace, Caller, evidence


@evidence("tells")
def tells(caller: Caller, workspace: Workspace) -> list[dict[str, str]]:
    \"\"\"Keeps a note as it reads.\"\"\"
    workspace.tell(caller, "Write about Gamma too.", target="things/alpha", as_="proposal", kind="article.archive")
    for fact in workspace.query(caller, "facts").items:
        workspace.retract(caller, fact.id, "it was a test")
    return [{"id": "t1", "text": "told", "source": "tells"}]
"""


class Counting(FakeExtraction):
    def __init__(self) -> None:
        super().__init__()
        self.extracted = 0

    def extract(self, record: Any) -> Any:
        self.extracted += 1
        return super().extract(record)


@pytest.mark.parametrize("args", [("agents", "run", "reports/weekly", "--show-prompt"), ("compile", "--dry-run")])
def test_an_articles_line_and_an_evidence_function_that_tells_write_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]) -> None:
    w = Workspace(tmp_path, monkeypatch)
    w.extraction = Counting()
    w.extraction.extract(note("n1", "The launch moved to March 3."))
    (tmp_path / "extensions.py").write_text(TELLS)
    agent_file(tmp_path, "reports/weekly", "  - articles: [things]\n  - tells: {}\n")
    w.extraction.extracted = 0
    before = w.snapshot()
    out = w.run(*args)
    assert out.exit_code == 0, out.output
    assert w.snapshot() == before and w.extraction.extracted == 0
    if args[0] == "agents":
        assert "note     " + UNTOLD in out.output, out.output
    else:
        assert "reports/weekly  not known  reports/weekly  " + NOT_KNOWN[UNTOLD] in out.output.splitlines(), out.output


LEAKS = """from cerebro import Workspace, Caller, evidence


@evidence("leaks")
def leaks(caller: Caller, workspace: Workspace) -> list:
    \"\"\"Returns a bare string by mistake.\"\"\"
    return ["the key is tok-example-5f3a9c2e81"]


@evidence("boom")
def boom(caller: Caller, workspace: Workspace) -> list:
    \"\"\"Fails.\"\"\"
    raise RuntimeError("bad \\x1b[2J row")
"""


def test_every_error_either_command_prints_is_masked_and_clean(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.fakes import FakeSecrets

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "extensions.py").write_text(LEAKS)
    agent_file(tmp_path, "reports/weekly", "  - leaks: {}\n")
    agent_file(tmp_path, "reports/broken", "  - boom: {}\n")
    secrets = FakeSecrets({"notes-token": "tok-example-5f3a9c2e81"})
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(
        tmp_path, extraction=w.extraction, connectors=[w.notes], clock=w.hands, agent_run=Refuses(), secrets=secrets))
    leaked = w.run("agents", "run", "reports/weekly", "--show-prompt")
    boom = w.run("agents", "run", "reports/broken", "--show-prompt")
    dry = w.run("compile", "--dry-run")
    for out in (leaked, boom, dry):
        assert out.exit_code == 1 and isinstance(out.exception, SystemExit), (out.output, out.exception)
        assert "tok-example-5f3a9c2e81" not in out.output and "\x1b" not in out.output, out.output
    assert "Error: RuntimeError: bad \\x1b[2J row" in boom.output
    lines = dry.output.splitlines()
    assert "reports/broken  failed   reports/broken  RuntimeError: bad \\x1b[2J row" in lines, dry.output
    assert any(line.startswith("things/alpha ") for line in lines)  # the other articles are still told
    assert lines[-3] == "2 stale, 0 fresh, 2 failed, 1 not due. Nothing was run or written."


def test_a_run_by_hand_links_the_facts_first_and_both_commands_do_the_same(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    told = Record(id="n1", source="notes", revision="1", at="2026-11-16T08:00:00Z",
                  body="Alpha shipped its first part.", fields={"subject": "Alpha"})
    w = Workspace(tmp_path, monkeypatch, told)
    agent_file(tmp_path, "things/article", "  - facts: {}\n", "Sum up each thing.")
    before = w.snapshot()
    shown_ = w.run("agents", "run", "things/article", "alpha", "--show-prompt")
    dry = w.run("compile", "--dry-run", "--article", "things/alpha")
    assert w.snapshot() == before
    assert dry.output.splitlines()[0] == "things/alpha  stale  things/article  its first build", dry.output
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "things/article", "alpha").exit_code == 0
    [(_, sent, _)] = w.model.sent
    assert "Alpha shipped its first part." in sent and unescaped(shown_.output, sent)


def test_a_search_by_meaning_or_a_judge_makes_a_dry_run_say_it_cannot_tell(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    near = NEAR.replace(', *workspace.query(caller, "things", matching="alpha").items', "")
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "extensions.py").write_text(near)
    agent_file(tmp_path, "reports/weekly", "  - near: {}\n")
    w.model = Records(w.hands, tmp_path)
    assert "wrote" in w.run("agents", "run", "reports/weekly").output
    w.model = Refuses()
    dry = w.run("compile", "--dry-run", "--article", "reports/weekly")
    assert dry.exit_code == 0 and dry.output.splitlines() == [
        ("reports/weekly  not known  reports/weekly  its evidence searches facts by meaning, which asks a model, so a "
        "dry run cannot tell"),
        "Nothing stale: 0 agent runs fresh, 1 not known. Nothing was run or written."], dry.output


class Asks:
    """A decision model that fails the test if it is asked."""

    def decide(self, state: Any, questions: Any) -> Any:
        pytest.fail("the decision model was asked")


def test_a_decision_model_and_a_reranker_are_never_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.models import Decider

    decider = type("Failing", (Asks, Decider), {})()
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."),
                  settings="search: { rerank: true }\n", decider=decider)
    (tmp_path / "extensions.py").write_text(NEAR)
    agent_file(tmp_path, "reports/weekly",
               "  - articles: [things]\n    text_only: true\n    keep: \"What each thing is.\"\n  - near: {}\n")
    before = w.snapshot()
    shown_ = w.run("agents", "run", "reports/weekly", "--show-prompt")
    assert shown_.exit_code == 0, shown_.output
    for line in ("an articles line with keep was not cut, since its judge asks the decision model",
                 "a search was left in its own order, since the re-ranker asks the decision model"):
        assert f"note     {line}" in shown_.output, shown_.output
    dry = w.run("compile", "--dry-run", "--article", "reports/weekly")
    assert dry.output.splitlines()[0].startswith("reports/weekly  not known  reports/weekly  "), dry.output
    assert "its keep line is cut by the decision model" in dry.output
    assert w.snapshot() == before


def test_on_a_store_that_is_not_sqlite_nothing_is_kept_either(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import copy as copying

    from cerebro.fakes import MemoryStore

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    store = MemoryStore()
    store.create(Identity("things/alpha", "things", "Alpha"))
    (tmp_path / "extensions.py").write_text(TELLS)
    agent_file(tmp_path, "reports/weekly", "  - articles: [things]\n  - tells: {}\n")
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(
        tmp_path, store=store, extraction=w.extraction, connectors=[w.notes], clock=w.hands, agent_run=Refuses()))
    cli.open_workspace(None).close()  # opening is what every command does first, and may write: the preview is after it
    kept = {k: copying.deepcopy(v) for k, v in vars(store).items() if k not in ("_exclusive", "scrub", "masks")}
    assert w.run("agents", "run", "reports/weekly", "--show-prompt").exit_code == 0
    assert w.run("compile", "--dry-run").exit_code == 0
    assert {k: v for k, v in vars(store).items() if k in kept} == kept


# -- the curator, the cooldown, the back-off, the inbox and the facts read through tools ------------------------------


def blocks(output: str) -> dict[str, str]:
    lines, out, head = output.splitlines(), {}, ""
    for line in lines:
        if line.startswith("=== "):
            head = line
            out[head] = ""
        elif head:
            out[head] += line + "\n"
    return {k: v.strip("\n") for k, v in out.items()}


def sent_as_shown(text: str) -> str:
    return "\n".join(shown(line, None) for line in text.rstrip("\n").split("\n"))


TOOLS = """from cerebro import agent_tool


@agent_tool
def lookup_thing(name: str) -> str:
    \"\"\"Look a thing up.\"\"\"
    return name
"""


def curator_matches(w: Workspace, tmp_path: Path, curator: str) -> str:
    shown_ = w.run("agents", "run", curator, "--show-prompt")
    assert shown_.exit_code == 0, shown_.output
    w.model = Records(w.hands, tmp_path)
    ran = w.run("agents", "run", curator)
    [(system, text, tools)] = w.model.sent
    got = blocks(shown_.output)
    assert got["=== tools"] == ", ".join(tools)
    assert got["=== system"] == sent_as_shown(system)
    assert got["=== opening message"].split("\n\nnote     ")[0] == sent_as_shown(text)
    w.model = Refuses()
    return shown_.output + ran.output


def test_every_curator_is_given_one_set_of_tools_and_preview_shows_it_and_a_tools_line_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "extensions.py").write_text(TOOLS)
    file = "---\nevidence:\n  - query: { from: facts }\n  - held: {}\nheartbeat: [manual]\n---\n\nKeep the things.\n"
    (tmp_path / "agents" / "things" / "curator.md").write_text(file)
    shown_ = curator_matches(w, tmp_path, "things/curator")
    assert blocks(shown_)["=== tools"] == ("query, read, admit, link, instruct, archive, merge, split, leave_out, reject, "
                                           "lookup_thing")
    (tmp_path / "agents" / "things" / "curator.md").write_text(file.replace("---\n", "---\ntools: [admit]\n", 1))
    refused = w.run("agents", "run", "things/curator", "--show-prompt")
    assert refused.exit_code == 1 and "a curator is given every curator tool, fetch where a source answers " \
        "questions, and every app tool: delete the tools: line" in refused.output, refused.output


def test_a_curator_in_the_first_run_is_shown_as_its_run_by_hand_sends_it(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "agents" / "things" / "curator.md").unlink()
    (tmp_path / "agents" / "projects").mkdir()
    (tmp_path / "agents" / "projects" / "curator.md").write_text(
        "---\nevidence:\n  - held: {}\nheartbeat: [manual]\n---\n\nKeep the projects.\n")
    before = w.snapshot()
    shown_ = w.run("agents", "run", "projects/curator", "--show-prompt")
    assert w.snapshot() == before  # the first run did not begin
    assert "note     the workspace's first run has not read its sources yet, and a run by hand reads them first" \
        in shown_.output
    curator_matches(w, tmp_path, "projects/curator")


def test_a_curator_still_proposing_after_its_first_run_is_offered_leave_out(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.held import FIRST_RUN_KEY

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    with w.open() as workspace:
        workspace.store.set_state(FIRST_RUN_KEY, {"status": "done"})
        workspace.store.append("agent-runs", {"run": "run_0", "agent": "things/curator", "status": "done"})
    output = curator_matches(w, tmp_path, "things/curator")
    assert "leave_out" in blocks(output)["=== tools"]


def test_the_workspaces_cooldown_holds_no_dry_run_and_a_failed_run_waits(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "reports/weekly").exit_code == 0
    w.extraction.extract(note("n2", "The budget is set."))
    w.hands.at += timedelta(hours=1)
    w.model = Refuses()
    lines = [" ".join(line.split()) for line in w.run("compile", "--dry-run").output.splitlines()]
    assert ("reports/weekly stale reports/weekly its evidence moved; a running cerebro serve holds it: new evidence "
            "waits until 2026-11-17 13:00, cooldown 4h") in lines, lines

    class Fails(AgentRun):
        def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
            return Run("run_failed", agent.id, agent.target, "failed", note="the stand-in failed")

    w.model = Fails()
    w.run("agents", "run", "reports/weekly")
    w.model = Refuses()
    waits = w.run("compile", "--dry-run").output.splitlines()
    assert "reports/weekly  waiting  reports/weekly  its run failed 1 time on the same evidence, so it waits until " \
        "2026-11-17T10:00:30Z before it runs again, or until its evidence moves" in waits, waits
    named = w.run("compile", "--dry-run", "--article", "reports/weekly").output.splitlines()
    assert named[0] == "reports/weekly  stale  reports/weekly  its evidence moved", named  # a person's ask ends the wait


def test_what_people_asked_is_shown_and_facts_read_through_tools_keep_a_run_fresh(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.caller import Caller
    from cerebro.records import Proposal, propose

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    with w.open() as workspace:
        [fact] = workspace.extraction.facts()
        propose(workspace.store, Proposal(id="p_lead", kind="article.instruct", target="reports/weekly",
                                      by=Caller("you", "person"), at="2026-11-17T08:00:00Z",
                                      payload={"text": "Lead with the launch."}, status="accepted",
                                      decided_by=Caller("you", "person"), decided_at="2026-11-17T08:00:00Z"))
    shown_ = w.run("agents", "run", "reports/weekly", "--show-prompt")
    assert "## What people asked of you" in shown_.output and "Lead with the launch." in shown_.output

    class Reads(Records):
        def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
            next(t for t in tools if t.__name__ == "read")(ids=[fact.id])
            return super().run(agent, evidence, tools, budget)

    w.model = Reads(w.hands, tmp_path)
    assert w.run("agents", "run", "reports/weekly").exit_code == 0
    w.model = Refuses()
    whole = w.run("compile", "--dry-run").output  # the next pass finds it fresh; compile --article rebuilds it
    assert ("reports/weekly", "reports/weekly") not in dry_rows(whole), whole


# -- the composer's own reasons, the forms an article is named by, and the lines around the commands ------------------


def test_a_part_left_by_a_subagent_no_longer_listed_makes_its_composer_stale(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "agents" / "things" / "subagents").mkdir()
    (tmp_path / "agents" / "things" / "subagents" / "risks.md").write_text(RISKS)
    (tmp_path / "agents" / "things" / "subagents" / "people.md").write_text(RISKS.replace("Risks", "People"))
    joined = "---\nsubagents: [risks, people]\n---\n\nA brief on each thing.\n"
    (tmp_path / "agents" / "things" / "article.md").write_text(joined)
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "things/article", "alpha").exit_code == 0
    (tmp_path / "agents" / "things" / "article.md").write_text(joined.replace("[risks, people]", "[risks]"))
    w.model = Refuses()
    dry = w.run("compile", "--dry-run")  # the next pass: compile --article runs every part though fresh
    assert dry_rows(dry.output)[("things/alpha", "things/article")] == "stale", dry.output
    assert "things/subagents/people left a part on it, and no agent lists it now" in dry.output, dry.output


def test_dry_run_refuses_what_compile_refuses_and_compile_alone_is_unchanged(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch)
    w.model = Counts(w.hands, tmp_path)
    for form in ("weekly", "articles/reports/weekly.md"):  # the second has nothing to cite yet: both run it
        dry, real = w.run("compile", "--dry-run", "--article", form), w.run("compile", "--article", form)
        assert dry.exit_code == real.exit_code == int(form == "weekly"), (dry.output, real.output)
    assert w.run("compile", "--dry-run", "--article", "weekly").output == w.run("compile", "--article", "weekly").output
    bare = w.run("compile")
    assert (bare.exit_code, bare.output) == (2, ("Usage: main compile [OPTIONS]\nTry 'main compile --help' for help."
                                                "\n\nError: Missing option '--article'.\n"))


def test_agents_new_for_a_subagent_not_listed_yet_says_to_list_it_first(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch)
    made = w.run("agents", "new", "things/subagents/risks")
    assert made.output.splitlines()[-1] == (
        "next     fill in agents/things/subagents/risks.md, list risks under subagents: in agents/things/article.md, "
        "then cerebro agents run things/subagents/risks <article> --show-prompt")
    (tmp_path / "agents" / "things" / "article.md").write_text(
        "---\nsubagents: [risks]\n---\n\nA brief on each thing.\n")
    tried = w.run("agents", "run", "things/subagents/risks", "alpha", "--show-prompt")
    assert tried.exit_code == 0, tried.output


# -- a dry run decides as the command it previews: run the real one after it, with a stand-in that counts ------------


class Counts(Records):
    """A model that keeps which agent each run was of, and on which article, and writes as `Records` does."""

    def __init__(self, clock: Hands, root: Path):
        super().__init__(clock, root)
        self.asked: list[tuple[str, str]] = []

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        self.asked.append((agent.target, agent.id))
        return super().run(agent, evidence, tools, budget)


class Fails(AgentRun):
    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        return Run("run_failed", agent.id, agent.target, "failed", note="the stand-in failed")


def dry_rows(output: str) -> dict[tuple[str, str], str]:
    """Each row of a dry run: (article, agent) to its status."""
    out = {}
    for line in output.splitlines():
        cells = re.split(r"\s{2,}", line)
        if len(cells) >= 3 and "/" in cells[2] and not line.startswith(("refused", "Error", "What")):
            out[(cells[0], cells[2])] = cells[1]
    return out


def agrees(w: Workspace, root: Path, *command: str, joins: tuple[str, ...] = ()) -> tuple[str, set[tuple[str, str]]]:
    """Run `compile --dry-run` with `command`'s options, then `command` itself with a model that counts, and check
    that the runs the dry run calls stale are the runs the model was asked for. `joins` are composers that ask no
    model: stale, they land again with no call."""
    w.model = Refuses()
    dry = w.run("compile", "--dry-run", *command[1:]) if command[0] == "compile" else w.run("compile", "--dry-run")
    w.model = Counts(w.hands, root)
    w.run(*command)
    asked = set(w.model.asked)
    stale = {key for key, status in dry_rows(dry.output).items() if status == "stale" and key[1] not in joins}
    assert stale == asked, (dry.output, asked)
    w.model = Refuses()
    return dry.output, asked


def archive(w: Workspace, id: str, status: str = "archived") -> None:
    from dataclasses import replace as replaced

    with w.open() as workspace:
        workspace.store.set_identity(replaced(workspace.store.article(id).identity, status=status))


def test_compile_dry_run_on_a_named_article_says_what_compile_then_does_in_each_state(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    named = ("compile", "--article", "things/alpha")
    out, asked = agrees(w, tmp_path, *named)  # its first build
    assert asked == {("things/alpha", "things/article")}, out
    out, asked = agrees(w, tmp_path, *named)  # fresh: compile --article rebuilds it
    assert asked == {("things/alpha", "things/article")} and out.splitlines()[0] == (
        "things/alpha  stale  things/article  compile --article runs it again whether or not anything moved"), out
    w.extraction.extract(note("n2", "The budget is set."))  # a note changed, and distill took it
    out, asked = agrees(w, tmp_path, *named)
    assert asked, out
    w.extraction.extract(note("n3", "The review moved to Friday."))
    w.model = Fails()
    w.run(*named)  # it fails on this evidence, and a pass would wait
    out, asked = agrees(w, tmp_path, *named)  # a run by hand ends the wait
    assert asked and dry_rows(out)[("things/alpha", "things/article")] == "stale", out
    (tmp_path / "agents" / "things" / "article.md").write_text(WRITER.format(front="", what="every thing"))
    out, asked = agrees(w, tmp_path, *named)  # its instructions edited
    assert asked, out
    agrees(w, tmp_path, "compile", "--article", "reports/weekly")  # a fixed article not born yet


def test_compile_dry_run_on_a_named_article_with_nothing_to_cite_or_cooling_down_says_what_compile_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch)
    out, asked = agrees(w, tmp_path, "compile", "--article", "things/alpha")  # nothing to cite: it runs
    assert asked and dry_rows(out)[("things/alpha", "things/article")] == "stale", out
    cool = Workspace(tmp_path / "cool", monkeypatch, note("n1", "The launch moved to March 3."),
                     front="heartbeat: { on: [change], cooldown: 6h }\n")
    cool.model = Records(cool.hands, tmp_path / "cool")
    assert cool.run("agents", "run", "reports/weekly").exit_code == 0
    cool.extraction.extract(note("n2", "The budget is set."))
    cool.hands.at += timedelta(hours=1)
    out, asked = agrees(cool, tmp_path / "cool", "compile", "--article", "reports/weekly")  # a person's ask
    assert asked, out


def test_compile_dry_run_on_a_composed_article_says_what_compile_then_does(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "agents" / "things" / "subagents").mkdir()
    (tmp_path / "agents" / "things" / "subagents" / "risks.md").write_text(RISKS)
    (tmp_path / "agents" / "things" / "subagents" / "people.md").write_text(RISKS.replace("Risks", "People"))
    joined = "---\nsubagents: [risks, people]\n---\n\nA brief on each thing.\n"
    (tmp_path / "agents" / "things" / "article.md").write_text(joined)
    named = ("compile", "--article", "things/alpha")
    out, asked = agrees(w, tmp_path, *named, joins=("things/article",))
    assert len(asked) == 2, out
    out, asked = agrees(w, tmp_path, *named, joins=("things/article",))
    assert len(asked) == 2, out  # fresh, and compile --article runs each part again
    (tmp_path / "agents" / "things" / "article.md").write_text(joined.replace("risks, people", "people, risks"))
    agrees(w, tmp_path, *named, joins=("things/article",))  # landed again in the new order
    w.extraction.extract(note("n2", "The budget is set."))
    w.model = Fails()
    w.run(*named)
    out, asked = agrees(w, tmp_path, *named, joins=("things/article",))
    assert len(asked) == 2, out


def test_a_whole_dry_run_says_what_a_pass_then_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    out, asked = agrees(w, tmp_path, "serve", "--once")
    assert len(asked) == 3, out
    w.extraction.extract(note("n2", "The budget is set."))
    w.model = Fails()
    w.run("serve", "--once")
    out, asked = agrees(w, tmp_path, "serve", "--once")  # each failed on this evidence: a pass waits
    assert not asked and dry_rows(out)[("things/alpha", "things/article")] == "waiting", out


def test_a_whole_dry_run_says_it_did_not_read_the_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.preview import UNREAD_SOURCES

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    whole = w.run("compile", "--dry-run").output.splitlines()
    assert whole[-1] == UNREAD_SOURCES, whole
    one = w.run("compile", "--dry-run", "--article", "things/alpha").output.splitlines()
    assert UNREAD_SOURCES not in one  # compile --article reads no source either
    assert not WORDS.search(UNREAD_SOURCES)


# -- the forms an article is named by, a file that does not load, an archived article ---------------------------------


def test_compile_and_its_dry_run_take_an_article_by_path_and_a_collection_by_its_folder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    by_id = w.run("compile", "--dry-run", "--article", "things/alpha")
    by_path = w.run("compile", "--dry-run", "--article", "articles/things/alpha.md")
    assert by_path.exit_code == 0 and by_path.output == by_id.output, by_path.output
    out, asked = agrees(w, tmp_path, "compile", "--article", "articles/things/alpha.md")
    assert asked == {("things/alpha", "things/article")}, out
    out, asked = agrees(w, tmp_path, "compile", "--article", "things/")  # alpha rebuilt though fresh
    assert asked == {("things/alpha", "things/article"), ("things/beta", "things/article")}, out
    whole = w.run("compile", "--dry-run", "--article", "things")
    assert whole.exit_code == 0 and "2 stale, 0 fresh, 0 failed." in whole.output, whole.output
    for form in ("beta", "nowhere/else/x"):
        dry, real = w.run("compile", "--dry-run", "--article", form), w.run("compile", "--article", form)
        assert dry.exit_code == real.exit_code == 1 and dry.output == real.output, (dry.output, real.output)
        assert "agents new" not in dry.output or "/" in form, dry.output


BOGUS = "---\nbogus: 1\nevidence:\n  - query: { from: facts }\n---\n\nSum up each thing.\n"


def test_an_agent_file_that_does_not_load_is_told_with_its_refusal(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "agents" / "things" / "article.md").write_text(BOGUS)
    whole = w.run("compile", "--dry-run")
    assert whole.exit_code == 1, whole.output
    assert any(line.startswith("refused  agents/things/article.md: ") for line in whole.output.splitlines()), \
        whole.output
    named = w.run("compile", "--dry-run", "--article", "things/beta")
    assert named.exit_code == 1 and "agents new" not in named.output, named.output
    assert "agents/things/article.md" in named.output
    shown_ = w.run("agents", "run", "things/article", "beta", "--show-prompt")
    assert shown_.exit_code == 1 and "Error: agents/things/article.md is refused, so it does not run: " in shown_.output, shown_.output


def test_a_run_by_hand_on_an_archived_article_leaves_it_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    with w.open() as workspace:
        workspace.store.create(Identity("things/gamma", "things", "Gamma", status="archived"))
        workspace.store.create(Identity("things/delta", "things", "Delta", status="merged"))
    left = w.run("agents", "run", "things/article", "gamma")
    assert (left.exit_code, left.output) == (0, "things/gamma is archived, left alone\n"), left.output
    merged = w.run("agents", "run", "things/article", "delta")
    assert merged.exit_code == 1 and "out of the working set" in merged.output, merged.output
    shown_ = w.run("agents", "run", "things/article", "gamma", "--show-prompt")
    assert shown_.exit_code == 1 and "out of the working set" in shown_.output, shown_.output


# -- the line agents new ends with works as printed -------------------------------------------------------------------


@pytest.mark.parametrize("id", ["things/article", "things/subagents/risks", "reports/digest", "places/curator"])
def test_the_try_agents_new_names_runs_as_printed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, id: str) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    if id == "things/article":
        (tmp_path / "agents" / "things" / "article.md").unlink()
    made = w.run("agents", "new", id)
    assert made.exit_code == 0, made.output
    if id == "things/subagents/risks":
        (tmp_path / "agents" / "things" / "article.md").write_text(COMPOSER)
    try_ = made.output.splitlines()[-1].partition(", then cerebro ")[2].split()
    if id.startswith("things/"):
        assert "<article>" in try_, made.output
    tried = w.run(*["alpha" if word == "<article>" else word for word in try_])
    assert tried.exit_code == 0, (made.output, tried.output)


# -- a script line is not run by either preview ----------------------------------------------------------------------


def test_a_script_line_is_not_run_by_a_preview_and_each_says_so(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.preview import NOT_KNOWN, UNRUN

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "scripts").mkdir()
    marker = tmp_path / "ran.txt"
    script = tmp_path / "scripts" / "usage.sh"
    script.write_text(f"#!/bin/sh\necho ran >> {marker}\necho usage is 42 units\n")
    script.chmod(0o755)
    agent_file(tmp_path, "reports/weekly", "  - script: { run: scripts/usage.sh }\n")
    shown_ = w.run("agents", "run", "reports/weekly", "--show-prompt")
    assert shown_.exit_code == 0 and f"note     {UNRUN}" in shown_.output, shown_.output
    assert "## Script scripts/usage.sh" in shown_.output
    dry = w.run("compile", "--dry-run", "--article", "reports/weekly")
    assert dry.output.splitlines()[0] == f"reports/weekly  not known  reports/weekly  {NOT_KNOWN[UNRUN]}", dry.output
    assert w.run("compile", "--dry-run").exit_code == 0
    assert not marker.exists()
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "reports/weekly").exit_code == 0
    assert marker.read_text() == "ran\n"  # a run runs it


# -- what the first request holds, as a real run sends it, against what --show-prompt prints --------------------------


class FirstRequest:
    """A stand-in model behind the real run: it keeps each request it is sent, whole, and fails the run."""

    def __init__(self) -> None:
        self.requests: list[tuple[list[Any], Any]] = []

    def __call__(self, messages: list[Any], info: Any) -> Any:
        self.requests.append((list(messages), info))
        raise RuntimeError("the stand-in keeps the request and answers nothing")


def as_shown(messages: list[Any], info: Any) -> str:
    """The blocks --show-prompt prints for a first request, built from what the run sent."""
    from cerebro.preview import HAND_IN_ONLY, NO_TOOL, THEN

    [request] = messages
    texts = [part.content if isinstance(part.content, str) else part.content[0] for part in request.parts]
    tools = ", ".join([t.name for t in info.function_tools] + [t.name for t in info.output_tools])
    lines = ["=== system", "", sent_as_shown(request.instructions), "", "=== tools", "", tools]
    if (info.model_settings or {}).get("tool_choice") == "none":
        lines += ["", HAND_IN_ONLY if info.output_tools else NO_TOOL]
    lines += ["", "=== opening message", "", sent_as_shown(texts[0])]
    for text in texts[1:]:
        lines += ["", THEN, "", sent_as_shown(text)]
    return "\n".join(lines)


TIGHT_WRITER = WRITER.format(front="budget: { tokens_per_run: 1500 }\n", what="the week")
TIGHT_CURATOR = CURATOR.replace("heartbeat: [manual]\n", "heartbeat: [manual]\nbudget: { tokens_per_run: 20000 }\n")


@pytest.mark.parametrize("case", ["writer", "writer with its room filled", "curator", "curator short of room"])
def test_show_prompt_prints_the_first_request_a_real_run_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                case: str) -> None:
    from pydantic_ai.models.function import FunctionModel

    from cerebro.run import PydanticRun

    many = [note(f"b{i}", f"Detail number {i} of the week: " + "words " * 60) for i in range(60)]
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."), *many)
    if case == "writer with its room filled":
        (tmp_path / "agents" / "reports" / "weekly.md").write_text(TIGHT_WRITER)
    if case == "curator short of room":
        (tmp_path / "agents" / "things" / "curator.md").write_text(TIGHT_CURATOR)
    agent = "things/curator" if case.startswith("curator") else "reports/weekly"
    shown_ = w.run("agents", "run", agent, "--show-prompt")
    assert shown_.exit_code == 0, shown_.output
    model = FirstRequest()
    w.model = PydanticRun(FunctionModel(model), clock=w.hands, root=tmp_path)
    w.run("agents", "run", agent)
    messages, info = model.requests[0]
    sent = as_shown(messages, info)
    assert shown_.output.startswith(sent + "\n\n"), (sent[-600:], shown_.output[len(sent) - 600:len(sent) + 200])
    after = shown_.output[len(sent) + 2:]
    assert after.startswith(("note ", "=== evidence digest")), after[:300]
    if case == "curator short of room":
        assert len(messages[0].parts) == 2
    if case == "writer with its room filled":
        assert info.model_settings["tool_choice"] == "none"


# -- an archived article is left alone, a collection goes on past a failure, a composer's own run cannot be known -----


def test_compile_and_its_dry_run_leave_an_archived_article_alone(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    with w.open() as workspace:
        workspace.store.create(Identity("things/gamma", "things", "Gamma", status="archived"))
        workspace.store.create(Identity("things/delta", "things", "Delta", status="merged"))
    dry = w.run("compile", "--dry-run", "--article", "things/gamma")
    w.model = Counts(w.hands, tmp_path)
    real = w.run("compile", "--article", "things/gamma")
    assert (dry.exit_code, real.exit_code) == (0, 0) and not w.model.asked, (dry.output, real.output)
    assert dry.output.splitlines()[0] == real.output.splitlines()[0] == (
        "Nothing to run. things/gamma is archived, left alone."), (dry.output, real.output)
    out, asked = agrees(w, tmp_path, "compile", "--article", "things")  # a collection leaves it out, and says so
    assert "things/gamma is archived, left alone" in out.splitlines() and ("things/gamma", "things/article") \
        not in asked, out
    for form in ("things/delta", "articles/things/delta.md"):  # merged away: out of the working set, as agents run says
        dry, real = w.run("compile", "--dry-run", "--article", form), w.run("compile", "--article", form)
        assert dry.exit_code == real.exit_code == 0 and dry.output == real.output, (dry.output, real.output)
        assert dry.output == "Nothing to run. things/delta is out of the working set, left alone.\n", dry.output


class Latest(Counts):
    """Counts each run, and hands in text that cites every fact it is shown, so moved evidence returns new content."""

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        self.asked.append((agent.target, agent.id))

        def write(prompt: str) -> str:
            ids = re.findall(r"\[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1])
            return " ".join(f"x [^{id}]" for id in dict.fromkeys(ids)) or "x"

        return ScriptedRun(write, clock=self.clock).run(agent, evidence, tools, budget)


@pytest.mark.parametrize("command", [("compile", "--article", "things/alpha"), ("serve", "--once")])
def test_a_composer_whose_subagent_moved_is_not_known_and_never_called_fresh(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...]) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "agents" / "things" / "subagents").mkdir()
    (tmp_path / "agents" / "things" / "subagents" / "risks.md").write_text(RISKS)
    (tmp_path / "agents" / "things" / "article.md").write_text(COMPOSER)
    w.model = Latest(w.hands, tmp_path)
    assert w.run(*command).exit_code == 0
    w.extraction.extract(note("n2", "The budget is set."))  # only what the subagent reads moved
    w.hands.at += timedelta(days=1)
    w.model = Refuses()
    dry = w.run("compile", "--dry-run", *command[1:]) if command[0] == "compile" else w.run("compile", "--dry-run")
    rows = {key: status for key, status in dry_rows(dry.output).items() if key[0] == "things/alpha"}
    w.model = Latest(w.hands, tmp_path)
    w.run(*command)
    asked = {key for key in w.model.asked if key[0] == "things/alpha"}
    if command[0] == "compile":  # compile --article runs its own run too, whatever they return
        assert rows[("things/alpha", "things/article")] == "stale", dry.output
    else:
        assert rows[("things/alpha", "things/article")] == "not known", dry.output
        assert "it runs again if its subagents return new content" in dry.output
    assert {key for key, status in rows.items() if status == "stale"} <= asked <= set(rows), (dry.output, asked)


class FailsOnAlpha(Counts):
    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        if agent.target == "things/alpha":
            self.asked.append((agent.target, agent.id))
            return Fails().run(agent, evidence, tools, budget)
        return super().run(agent, evidence, tools, budget)


def test_a_collection_compile_goes_on_past_a_failed_article_and_exits_1(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    w.model = FailsOnAlpha(w.hands, tmp_path)
    real = w.run("compile", "--article", "things")
    assert ("things/beta", "things/article") in w.model.asked, real.output
    lines = real.output.splitlines()
    assert real.exit_code == 1 and "wrote    things/beta  articles/things/beta.md" in lines, real.output
    assert "failed   things/alpha  things/article: run run_failed wrote nothing: failed: the stand-in failed" in lines
    assert lines[-2:] == ["1 written, 0 fresh, 1 failed.", "Error: 1 agent run failed: see its line above"], lines
    one = w.run("compile", "--article", "things/beta")  # one article named: its line alone, rebuilt though fresh
    assert one.output.splitlines() == ["wrote    things/beta  articles/things/beta.md"], one.output


# -- the small ones: refused forms, an empty name, refused files with --article, the sources line -----------------------


def test_compile_and_its_dry_run_refuse_the_same_forms(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    for form in ("", "/things/alpha", "things/alpha/"):
        dry, real = w.run("compile", "--dry-run", "--article", form), w.run("compile", "--article", form)
        assert dry.exit_code == real.exit_code != 0 and dry.output == real.output, (form, dry.output, real.output)
    collection = w.run("compile", "--dry-run", "--article", "/things/")  # a folder, trimmed of its slashes
    assert collection.exit_code == 0 and "things/beta" in collection.output, collection.output


def test_a_named_dry_run_lists_refused_agent_files_as_compile_does(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    agent_file(tmp_path, "places/article", "  - query: { from: facts }\n")
    (tmp_path / "agents" / "places" / "article.md").write_text(BOGUS)
    dry = w.run("compile", "--dry-run", "--article", "reports/weekly")
    w.model = Counts(w.hands, tmp_path)
    real = w.run("compile", "--article", "reports/weekly")
    for out in (dry, real):
        assert out.exit_code == 1 and out.output.splitlines()[0].startswith("refused  agents/places/article.md: "), \
            out.output
    assert w.model.asked == [("reports/weekly", "reports/weekly")]  # what loaded still runs


def test_the_sources_line_sends_no_one_to_a_paid_command(tmp_path: Path) -> None:
    from cerebro.preview import UNREAD_SOURCES

    assert "distill" not in UNREAD_SOURCES and "count" not in UNREAD_SOURCES


@pytest.mark.parametrize("beat", ["[manual]", "[daily]"])
def test_a_whole_dry_run_tells_what_a_heartbeat_fires_not_what_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                      beat: str) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."), front=f"heartbeat: {beat}\n")
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "reports/weekly").exit_code == 0
    w.hands.at += timedelta(days=2)
    if beat == "[manual]":
        w.extraction.extract(note("n2", "The budget is set."))
    w.model = Refuses()
    rows = dry_rows(w.run("compile", "--dry-run").output)
    assert rows.get(("reports/weekly", "reports/weekly")) == ("not due" if beat == "[manual]" else "stale"), rows


# -- the whole dry run is the next pass; a collection compile counts the same -------------------------------------------


HEARTBEATS = ["[change]", "[daily]", "[weekly]", "[manual]", "[change, daily]", "[change, manual]",
              "{ on: [change, weekly], cooldown: 3h }"]


def pass_agrees(w: Workspace, root: Path) -> tuple[str, set[tuple[str, str]]]:
    """The whole dry run, then a pass on what it saw: every run the dry run lists stale is a run the pass pays for,
    and every run the pass pays for is listed stale."""
    from cerebro import runner

    w.model, before, taken, at = Refuses(), w.snapshot(), dict(runner._TAKEN), w.hands.at
    dry = w.run("compile", "--dry-run")
    assert (w.snapshot(), runner._TAKEN, w.hands.at) == (before, taken, at), dry.output  # no write, lease or clock
    w.model = Counts(w.hands, root)
    w.run("serve", "--once")
    asked = set(w.model.asked)
    stale = {key for key, status in dry_rows(dry.output).items() if status == "stale"}
    assert stale == asked, (dry.output, asked)
    w.model = Refuses()
    return dry.output, asked


@pytest.mark.parametrize("beat", HEARTBEATS)
def test_a_whole_dry_run_lists_every_run_the_next_pass_pays_for_and_no_other(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, beat: str) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."), front=f"heartbeat: {beat}\n")
    pass_agrees(w, tmp_path)  # never run
    w.model = Records(w.hands, tmp_path)
    for agent in ("things/article", "reports/weekly"):
        assert w.run("agents", "run", agent).exit_code == 0
    pass_agrees(w, tmp_path)  # fresh by hand: a schedule that never ran is due all the same
    pass_agrees(w, tmp_path)  # fresh after a pass
    w.hands.at += timedelta(days=8)
    pass_agrees(w, tmp_path)  # its schedule's period has come round
    w.hands.at += timedelta(hours=1)
    w.extraction.extract(note("n2", "The budget is set."))
    pass_agrees(w, tmp_path)  # evidence moved
    w.hands.at += timedelta(hours=1)
    w.extraction.extract(note("n3", "The review moved to Friday."))
    pass_agrees(w, tmp_path)  # evidence moved again inside a cooldown
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(WRITER.format(front=f"heartbeat: {beat}\n",
                                                                             what="the whole week"))
    pass_agrees(w, tmp_path)  # its agent file changed
    w.hands.at += timedelta(days=8)
    w.extraction.extract(note("n4", "The launch moved to April 1."))
    w.model = Fails()
    w.run("serve", "--once")
    w.model = Records(w.hands, tmp_path)
    for agent in ("things/article", "reports/weekly"):
        w.model = Fails()
        w.run("agents", "run", agent)
    pass_agrees(w, tmp_path)  # failed on this evidence: a pass waits


def test_a_whole_dry_run_says_first_it_is_the_next_pass_and_names_what_is_not_due(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."), front="heartbeat: [manual]\n")
    w.extraction.extract(note("n2", "The budget is set."))
    lines = w.run("compile", "--dry-run").output.splitlines()
    assert lines[0] == "What the next pass, cerebro serve --once, would run now:", lines
    assert "reports/weekly  not due  reports/weekly  heartbeat is manual: run it by hand" in lines, lines
    assert "things          not due  things/curator  heartbeat is manual: run it by hand" in lines, lines
    assert lines[-2] == "Nothing stale: 0 agent runs fresh, 3 not due. Nothing was run or written.", lines


def test_a_whole_dry_run_says_what_serve_once_does_where_a_running_serve_holds_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    w.model = Records(w.hands, tmp_path)
    assert w.run("serve", "--once").exit_code == 0
    w.extraction.extract(note("n2", "The budget is set."))
    w.hands.at += timedelta(hours=1)
    out, asked = pass_agrees(w, tmp_path)
    assert ("reports/weekly", "reports/weekly") in asked
    assert ("reports/weekly stale reports/weekly its evidence moved; a running cerebro serve holds it: new evidence "
            "waits until 2026-11-17 13:00, cooldown 4h") in [" ".join(line.split()) for line in out.splitlines()], out


def test_a_whole_dry_run_lists_a_curator_a_pass_would_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "agents" / "things" / "curator.md").write_text(CURATOR.replace("[manual]", "[change]"))
    out, asked = pass_agrees(w, tmp_path)
    assert ("things", "things/curator") in asked, out
    out, asked = pass_agrees(w, tmp_path)
    assert ("things", "things/curator") not in asked, out
    w.extraction.extract(note("n2", "The budget is set."))
    out, asked = pass_agrees(w, tmp_path)
    assert ("things", "things/curator") in asked, out


def test_a_whole_dry_run_in_the_first_run_lists_the_writers_its_build_may_run(tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import first_run
    from cerebro.preview import FIRST_RUN

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))

    monkeypatch.setattr(first_run, "in_first_run", lambda workspace: True)
    lines = w.run("compile", "--dry-run").output.splitlines()
    assert lines[1] == FIRST_RUN, lines
    assert dry_rows("\n".join(lines)) == {("reports/weekly", "reports/weekly"): "stale",
                                          ("things/alpha", "things/article"): "stale",
                                          ("things/beta", "things/article"): "stale"}, lines


def test_a_first_run_whose_curators_propose_nothing_builds_no_run_the_dry_run_did_not_list(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two articles a person admitted, so the curators' first run has not happened, and a curator that proposes
    nothing: serve --once discovers, then builds at once. Its curator's run is said to be unlisted; every writer run
    it pays for is listed stale, on the first run's pass and on the two after it."""
    from dataclasses import replace as replaced

    from cerebro.caller import Caller
    from cerebro.held import FIRST_RUN_KEY

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    with w.open() as workspace:
        for id in ("things/alpha", "things/beta"):
            workspace.store.set_identity(replaced(workspace.store.article(id).identity,
                                              admitted_by=Caller(kind="person", name="me")))
    for step in range(3):
        with w.open() as workspace:
            first = (workspace.store.state(FIRST_RUN_KEY) or {}).get("status") != "done"
        w.model = Refuses()
        dry = w.run("compile", "--dry-run").output
        w.model = Counts(w.hands, tmp_path)
        w.run("serve", "--once")
        asked = {key for key in w.model.asked if not (first and key[1] == "things/curator")}
        assert asked == {key for key, status in dry_rows(dry).items() if status == "stale"}, (step, dry, asked)
        assert not first or "their runs are not listed" in dry
    w.model = Refuses()


def test_records_held_for_the_first_run_make_its_builds_writers_not_known(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.held import FIRST_RUN_KEY

    w = Workspace(tmp_path, monkeypatch)
    w.model = Counts(w.hands, tmp_path)
    assert w.run("agents", "run", "reports/weekly").exit_code == 0  # it ran: nothing it reads moved since
    with w.open() as workspace:
        workspace.store.set_state(FIRST_RUN_KEY, SESSION)  # discovered, and nothing of its list waits
        workspace.store.set_state("held:notes", [{"id": f"n{n}", "source": "notes", "revision": "1", "body": "A thing.",
                                              "at": "2026-11-16T08:00:00Z"} for n in (1, 2)])
    rows = dry_rows(w.run("compile", "--dry-run").output)
    assert rows[("reports/weekly", "reports/weekly")] == "not known", rows
    assert set(rows.values()) == {"not known", "stale"}, rows  # one never written runs whatever the build reads
    out = w.run("compile", "--dry-run").output
    assert "the first run's build reads the 2 records held for it into facts before this runs" in out, out


EXTENSIONS_PICKY = '''from cerebro import Workspace, Caller, evidence
@evidence("picky")
def picky(caller: Caller, workspace: Workspace, article: str) -> list:
    """Read one thing, and on alpha return a bare string by mistake."""
    if article == "things/alpha":
        return ["a bare string by mistake"]
    return [{"id": "p-" + article.replace("/", "-"), "text": "read " + article, "source": "picky"}]
'''


def test_a_collection_compile_goes_on_past_an_article_whose_evidence_fails(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "extensions.py").write_text(EXTENSIONS_PICKY)
    (tmp_path / "agents" / "things" / "article.md").write_text(
        "---\nevidence:\n  - picky: { article: this }\n---\n\nSum up each thing.\n\n# Output\n\n## Summary\n")
    w.model = Counts(w.hands, tmp_path)
    real = w.run("compile", "--article", "things")
    assert ("things/beta", "things/article") in w.model.asked, real.output
    lines = real.output.splitlines()
    assert any(line.startswith("failed   things/alpha  things/article: evidence function picky") for line in lines), \
        real.output
    assert lines[-2:] == ["1 written, 0 fresh, 1 failed.", "Error: 1 agent run failed: see its line above"], lines


class Empty(Counts):
    """Counts each run, and hands in nothing on beta."""

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        if agent.target == "things/beta":
            self.asked.append((agent.target, agent.id))
            return ScriptedRun(lambda prompt: {}, clock=self.clock).run(agent, evidence, tools, budget)
        return super().run(agent, evidence, tools, budget)


def test_a_run_with_nothing_to_cite_or_that_wrote_nothing_is_not_counted_failed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch)  # no note: neither article has anything to cite, and both run
    w.model = Counts(w.hands, tmp_path)
    empty = w.run("compile", "--article", "things")
    assert len(w.model.asked) == 2 and empty.exit_code == 0, empty.output
    assert empty.output.splitlines()[-1] == "2 written, 0 fresh, 0 failed.", empty.output
    full = Workspace(tmp_path / "full", monkeypatch, note("n1", "The launch moved to March 3."))
    full.model = Empty(full.hands, tmp_path / "full")
    real = full.run("compile", "--article", "things")
    assert real.exit_code == 0, real.output
    assert real.output.splitlines()[-1] == "1 written, 1 ran and wrote nothing, 0 fresh, 0 failed.", real.output
    assert any(line.startswith("ran      things/article on things/beta: wrote nothing: ")
               for line in real.output.splitlines()), real.output


def test_each_failure_of_a_collection_is_its_own_line_naming_its_article(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    names = [f"item{n:02d}" for n in range(30)]
    with w.open() as workspace:
        for name in names:
            workspace.store.create(Identity(f"things/{name}", "things", name.title()))
    w.model = Fails()
    real = w.run("compile", "--article", "things")
    lines = real.output.splitlines()
    failed = [line for line in lines if line.startswith("failed   ")]
    assert len(failed) == 32 and "\\n" not in real.output, real.output[:800]
    assert all(any(line.startswith(f"failed   things/{n}  things/article: ") for line in failed)
               for n in [*names, "alpha", "beta"]), failed[:3]
    assert lines[-1] == "Error: 32 agent runs failed: see their lines above", lines[-3:]


def test_a_fresh_composer_whose_subagent_waits_passes_compile_as_its_dry_run_says(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "Alpha launch moved to March 3."))
    (tmp_path / "agents" / "things" / "subagents").mkdir()
    (tmp_path / "agents" / "things" / "subagents" / "risks.md").write_text(RISKS)
    (tmp_path / "agents" / "things" / "subagents" / "people.md").write_text(
        RISKS.replace("Risks", "People").replace("source: notes", "source: nowhere"))
    (tmp_path / "agents" / "things" / "article.md").write_text(COMPOSER.replace("[risks]", "[risks, people]"))
    w.model = Latest(w.hands, tmp_path)
    w.run("compile", "--article", "things")
    w.hands.at += timedelta(days=1)
    for named in ("things/alpha", "things"):  # compile --article rebuilds it though fresh, as its dry run says
        out, asked = agrees(w, tmp_path, "compile", "--article", named)
        assert ("things/alpha", "things/subagents/risks") in asked, out
        assert "evidence lines hold nothing to cite now" not in out, out


def test_a_writer_whose_opening_fills_its_room_may_still_hand_in_as_its_preview_says(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from cerebro.run import PydanticRun

    many = [note(f"b{i}", f"Detail number {i} of the week: " + "words " * 60) for i in range(60)]
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."), *many)
    (tmp_path / "agents" / "reports" / "weekly.md").write_text(TIGHT_WRITER)
    shown_ = w.run("agents", "run", "reports/weekly", "--show-prompt")
    assert "the hand-in included" not in shown_.output, shown_.output
    assert ("sent with tool_choice none: its opening fills the room its budget leaves for reading, so the model can "
            "call no tool but the two that hand in its text or say it has nothing to write") in shown_.output.splitlines()

    def hands_in(messages: list[Any], info: Any) -> ModelResponse:
        assert info.model_settings["tool_choice"] == "none"
        text = " ".join(str(part.content) for part in messages[0].parts)
        first = re.search(r"\[\^([^\]\s]+)\]", text.split("<evidence>")[-1]).group(1)
        return ModelResponse(parts=[ToolCallPart("write_article", {"text": f"## Summary\n\nx [^{first}]"})])

    w.model = PydanticRun(FunctionModel(hands_in), clock=w.hands, root=tmp_path)
    real = w.run("agents", "run", "reports/weekly")
    assert "wrote    reports/weekly  articles/reports/weekly.md" in real.output, real.output


def test_a_collection_whose_writer_did_not_load_names_the_refused_file(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "agents" / "things" / "article.md").write_text(BOGUS)
    for args in (("compile", "--dry-run", "--article", "things"), ("compile", "--article", "things")):
        out = w.run(*args)
        lines = out.output.splitlines()
        assert out.exit_code == 1 and "no article or collection" not in out.output, out.output
        assert not any(line.startswith("refused  ") for line in lines), lines  # the refusal names it, once
        assert lines[-1].startswith("Error: no agent writes things: agents/things/article.md: "), lines


@pytest.mark.parametrize("named", ["things/nothing", "things/alpha "])
def test_an_article_that_is_not_there_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                   named: str) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    dry, real = w.run("compile", "--dry-run", "--article", named), w.run("compile", "--article", named)
    assert dry.exit_code == real.exit_code == 1 and dry.output == real.output, (dry.output, real.output)
    assert dry.output.startswith(f"Error: no article or collection {named}: "), dry.output
    assert "agent run failed" not in dry.output


SESSION = {"status": "waiting", "at": "2026-11-17T08:00:00+00:00", "read": True, "rounds": 1, "todo": [], "runs": [],
           "lists": {}, "unfinished": [], "raised": 0.0, "stopped": "", "cut": []}


def test_a_first_run_whose_list_is_decided_lists_the_writers_its_build_runs(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.held import FIRST_RUN_KEY
    from cerebro.preview import FIRST_BUILDS, FIRST_RUN

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    with w.open() as workspace:
        workspace.store.set_state(FIRST_RUN_KEY, {**SESSION, "status": "discovering"})
    lines = w.run("compile", "--dry-run").output.splitlines()
    assert lines[1] == FIRST_RUN and set(dry_rows("\n".join(lines)).values()) == {"stale"}, lines
    with w.open() as workspace:
        workspace.store.set_state(FIRST_RUN_KEY, SESSION)  # discovered, and nothing of its list waits
    out, asked = pass_agrees(w, tmp_path)
    assert out.splitlines()[1] == FIRST_BUILDS and len(asked) == 3, out


def spent(w: Workspace) -> None:
    with w.open() as workspace:
        workspace.store.append("spend", {"at": "2026-11-17T08:00:00Z", "day": "2026-11-17", "layer": "compilation",
                                     "usd": 5.0, "provider": "fireworks", "model": "glm-5p3", "run": ""})


def test_a_run_the_spending_cap_stops_waits_in_both_dry_runs_and_compile(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."),
                  settings="budget:\n  daily_usd: 0.01\n")
    spent(w)
    out, asked = pass_agrees(w, tmp_path)
    assert not asked and dry_rows(out)[("things/alpha", "things/article")] == "waiting", out
    assert "spent $5.00 of the $0.01 daily cap" in out and out.count("spent $5.00") == 1, out  # said once
    dry, real = w.run("compile", "--dry-run", "--article", "things"), w.run("compile", "--article", "things")
    assert dry.exit_code == 0 and real.exit_code == 1, (dry.output, real.output)  # nothing paid for: exit 1
    lines = real.output.splitlines()
    assert lines[:2] == ["waiting  things/alpha  things/article: the daily budget is spent",
                         "waiting  things/beta  things/article: the daily budget is spent"], lines
    assert lines[2] == "0 written, 0 fresh, 0 failed, 2 waiting." and "spent $5.00" in lines[3], lines
    assert dry_rows(dry.output)[("things/alpha", "things/article")] == "waiting"


def test_a_composer_whose_own_run_the_cap_refuses_waits_in_both(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Its subagent is fresh and its own evidence moved, at a spent cap: the run is refused before it starts, and
    both the named dry run and compile --article say it waits for the cap, not that it failed."""
    own = ("---\nsubagents: [risks]\nevidence:\n  - query: { from: facts, source: extra }\n---\n\nA brief on each "
           "thing.\n\n# Composing\n\nWrite a verdict.\n")
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."),
                  settings="budget:\n  daily_usd: 0.5\n")
    (tmp_path / "agents" / "things" / "article.md").write_text(own)
    (tmp_path / "agents" / "things" / "subagents").mkdir()
    (tmp_path / "agents" / "things" / "subagents" / "risks.md").write_text(RISKS)
    w.model = ScriptedRun(lambda prompt: {})  # it and its subagent hand in nothing: never written
    w.run("compile", "--article", "things/alpha")
    spent(w)
    w.extraction.extract(Record(id="x2", source="extra", revision="1", at="2026-11-16T08:00:00Z", body="A thing."))
    w.model = Refuses()
    dry = w.run("compile", "--dry-run", "--article", "things/alpha")
    real = w.run("compile", "--article", "things/alpha")
    assert set(dry_rows(dry.output).values()) == {"waiting"}, dry.output
    assert real.output.splitlines()[0] == "waiting  things/alpha  things/article: the daily budget is spent", \
        real.output
    assert "failed" not in real.output and real.exit_code == 1, real.output


def test_a_lease_another_process_holds_fails_the_named_dry_run_as_it_fails_compile(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    with w.open() as workspace:
        workspace.store.set_state("lease:things/article", {"by": "another", "until": "2026-11-17T10:00:00+00:00"})
    before = w.snapshot()
    dry = w.run("compile", "--dry-run", "--article", "things")
    assert w.snapshot() == before
    w.model = Counts(w.hands, tmp_path)
    real = w.run("compile", "--article", "things")
    assert dry.exit_code == real.exit_code == 1 and not w.model.asked, (dry.output, real.output)
    assert "things/article is running in another process" in dry.output, dry.output


class Slow(Counts):
    """Counts, and takes a moment, so runs side by side overlap as real ones do."""

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        import time

        time.sleep(0.3)
        return super().run(agent, evidence, tools, budget)


def test_a_run_the_runs_before_it_may_leave_no_room_for_under_the_cap_is_not_known(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Six articles side by side under a daily cap one run's reservation fills: the pass holds all but one for room.
    The dry run lists stale only a run nothing can come before, and each other run as not known, never stale."""
    names = ["gamma", "delta", "epsilon", "zeta"]
    w = Workspace(tmp_path, monkeypatch, *[note(f"n-{n}", f"{n.title()} moved its launch to March 3.")
                                           for n in ["alpha", "beta", *names]], settings="budget:\n  daily_usd: 0.0001\n")
    with w.open() as workspace:
        for n in names:
            workspace.store.create(Identity(f"things/{n}", "things", n.title()))
    dry = w.run("compile", "--dry-run")
    w.model = Slow(w.hands, tmp_path)
    passed = w.run("serve", "--once")
    asked, rows = set(w.model.asked), dry_rows(dry.output)
    stale = {key for key, status in rows.items() if status == "stale"}
    maybe = {key for key, status in rows.items() if status == "not known"}
    assert stale == {("reports/weekly", "reports/weekly")} and len(maybe) == 6, dry.output
    assert stale <= asked <= stale | maybe and "held for room" in passed.output, (dry.output, asked)
    named = w.run("compile", "--dry-run", "--article", "things")
    assert "whether it runs or waits for room is not known" in named.output, named.output
    w.model = Refuses()


def test_a_keep_line_whose_scores_are_kept_is_judged_as_the_pass_judges_it_with_no_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run kept the keep judge's scores in the store. A dry run opened with no decision key reads them as the pass
    reads them, with no call, so it finds the run fresh, as compile --article does; a passage with no score kept is
    not known."""
    from cerebro.fakes import FakeDecider
    from cerebro.models import Decider

    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."), decider=FakeDecider())
    agent_file(tmp_path, "reports/weekly", "  - articles: [things]\n    text_only: true\n    keep: \"What each thing is.\"\n")
    w.model = Records(w.hands, tmp_path)
    assert w.run("agents", "run", "things/article").exit_code == 0
    assert w.run("agents", "run", "reports/weekly").exit_code == 0
    w.model, w.decider = Refuses(), None
    dry = w.run("compile", "--dry-run")
    assert ("reports/weekly", "reports/weekly") not in dry_rows(dry.output), dry.output
    w.decider = type("Failing", (Asks, Decider), {})()
    assert w.run("serve", "--once").exit_code == 0  # the pass finds it fresh too: no model is asked
    w.decider = None
    agent_file(tmp_path, "reports/weekly", "  - articles: [things]\n    text_only: true\n    keep: \"Each risk.\"\n")
    dry = w.run("compile", "--dry-run", "--article", "reports/weekly")
    assert dry_rows(dry.output) == {("reports/weekly", "reports/weekly"): "not known"}, dry.output


def test_compile_article_rebuilds_a_fresh_article_and_its_dry_run_lists_that_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile --article runs the named article's agent again though nothing moved. Its dry run lists that run, which is paid for; the
    whole dry run, which stands for a pass, still counts it fresh."""
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    w.model = Records(w.hands, tmp_path)
    assert w.run("compile", "--article", "things/alpha").exit_code == 0
    out, asked = agrees(w, tmp_path, "compile", "--article", "things/alpha")
    assert asked == {("things/alpha", "things/article")}, out
    assert "compile --article runs it again whether or not anything moved" in out, out
    whole = w.run("compile", "--dry-run").output
    assert ("things/alpha", "things/article") not in dry_rows(whole), whole


def test_a_gather_that_failed_waits_in_the_whole_dry_run_and_a_run_by_hand_gathers_at_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An evidence line that fails to gather: the next pass waits before it gathers again, and so does the whole dry
    run say; compile --article gathers at once, as a scheduled run does, and fails, and so does its dry run say."""
    w = Workspace(tmp_path, monkeypatch, note("n1", "The launch moved to March 3."))
    (tmp_path / "extensions.py").write_text(LEAKS)
    agent_file(tmp_path, "reports/weekly", "  - boom: {}\n")
    w.model = Counts(w.hands, tmp_path)
    w.run("serve", "--once")
    w.hands.at += timedelta(seconds=5)
    w.model = Refuses()
    whole, named = w.run("compile", "--dry-run"), w.run("compile", "--dry-run", "--article", "reports/weekly")
    assert dry_rows(whole.output)[("reports/weekly", "reports/weekly")] == "waiting", whole.output
    assert "its evidence failed to gather, the last of 1 failure in a row" in whole.output, whole.output
    assert dry_rows(named.output)[("reports/weekly", "reports/weekly")] == "failed", named.output
    w.model = Counts(w.hands, tmp_path)
    passed, real = w.run("serve", "--once"), w.run("compile", "--article", "reports/weekly")
    assert "its evidence failed to gather" in passed.output and real.exit_code == named.exit_code == 1, real.output
