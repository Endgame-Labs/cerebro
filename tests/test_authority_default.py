"""A curator's authority: a file with no `authority:` line, or a kind its line leaves out, does nothing of that kind on
its own, and what it would have done waits for a person. After its first run a curator is shown what it filed that
waits for the person, so it does not file it again."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from cerebro import Cerebro, cli
from cerebro.agent import Agent, Authority, Budget, load_agent
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.cli import main
from cerebro.evidence import Evidence
from cerebro.fakes import FakeExtraction, ScriptedRun
from cerebro.records import Proposal, proposals
from cerebro.run import Run

CURATOR = """\
---
{line}evidence:
  - query: {{ from: {collection} }}
heartbeat: [manual]
---

Decide which {collection} the workspace keeps.
"""


class Recording(ScriptedRun):
    """The scripted model, keeping each run and each prompt, so a test reads the tool replies and the opening."""

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]):
        self.prompts: list[str] = []
        super().__init__(lambda prompt: self.prompts.append(prompt) or {})
        self.calls = calls
        self.runs: list[Run] = []

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
        made = super().run(agent, evidence, tools, budget)
        self.runs.append(made)
        return made


class Workspace:
    """A workspace folder with a curator of things, its authority line as given, and the commands run on it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, line: str = ""):
        self.root, self.calls = root, []
        (root / "settings.yaml").parent.mkdir(parents=True, exist_ok=True)
        (root / "settings.yaml").write_text("{}\n")
        self.write("things", line)
        self.model = Recording(self.calls)
        extraction = FakeExtraction()
        self.ahead = timedelta(0)
        monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(folder, agent_run=self.model,
                                                                      extraction=extraction, clock=self.clock))
        monkeypatch.setattr(cli, "_asking", lambda: True)
        monkeypatch.setattr(Cerebro, "weighs_nothing", lambda self, agent: False)  # its scripted calls stand in

    def write(self, collection: str, line: str, held: bool = False) -> None:
        """The curator file of `collection`, with `held` its held line, where another curator's proposal reaches it."""
        (self.root / "agents" / collection).mkdir(parents=True, exist_ok=True)
        text = CURATOR.format(line=f"{line}\n" if line else "", collection=collection)
        (self.root / "agents" / collection / "curator.md").write_text(
            text.replace("heartbeat:", "  - held: {}\nheartbeat:") if held else text)

    def clock(self) -> datetime:
        return datetime.now(UTC) + self.ahead

    def later(self) -> None:
        self.ahead += timedelta(minutes=1)

    def run(self, *args: str, input: str | None = None) -> Result:
        return CliRunner().invoke(main, [*args, "--workspace", str(self.root)], input=input)

    def curate(self, *calls: tuple[str, dict[str, Any]], agent: str = "things/curator") -> None:
        self.calls[:] = list(calls)
        done = self.run("agents", "run", agent)
        assert done.exit_code == 0, done.output

    def replies(self, tool: str) -> list[str]:
        return [entry.text for entry in self.model.runs[-1].transcript if entry.role == "tool" and entry.tool == tool]

    def waiting(self) -> list[dict[str, Any]]:
        listed = self.run("review", "--json")
        assert listed.exit_code == 0, listed.output
        return json.loads(listed.output)

    def rows(self) -> list[Proposal]:
        with Cerebro(self.root, extraction=FakeExtraction()) as opened:
            return proposals(opened.store)

    def admitted(self, *names: str) -> None:
        """The things in the working set, admitted by the curator on its own, so no caller decided them."""
        with Cerebro(self.root, extraction=FakeExtraction()) as opened:
            for name in names:
                opened.store.create(Identity(f"things/{name.lower().replace(' ', '-')}", "things", name, {}, "active",
                                             Caller("things/curator", "agent"), "2026-11-01T09:00:00Z",
                                             f"{name} came up in two notes"))


def _curator(tmp_path: Path, line: str) -> Agent:
    agents = tmp_path / "agents"
    (agents / "things").mkdir(parents=True)
    (agents / "things" / "curator.md").write_text(CURATOR.format(line=f"{line}\n" if line else "",
                                                                 collection="things"))
    return load_agent(agents / "things" / "curator.md", agents)


def test_a_file_with_no_authority_line_does_none_of_the_four_on_its_own(tmp_path: Path) -> None:
    assert (_curator(tmp_path, "").authority or Authority()) == Authority(False, False, False, False)


def test_a_kind_the_line_leaves_out_is_off(tmp_path: Path) -> None:
    assert _curator(tmp_path, "authority: { admit: true }").authority == Authority(True, False, False, False)


def test_with_no_line_an_admit_an_archive_and_a_merge_each_wait_for_a_person(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    space = Workspace(tmp_path, monkeypatch)
    space.admitted("Alder", "Alder Works", "Birch")
    space.curate(("admit", {"name": "Cedar", "reason": "Cedar came up in two notes"}),
                 ("archive", {"article": "Birch", "reason": "no activity since spring"}),
                 ("merge", {"article": "Alder", "other": "Alder Works", "reason": "one thing"}))
    for tool in ("admit", "archive", "merge"):
        (reply,) = space.replies(tool)
        assert "'note': 'waits for a caller, in p_" in reply
    assert sorted(row["kind"] for row in space.waiting()) == ["article.admit", "article.archive", "article.merge"]
    with Cerebro(space.root, extraction=FakeExtraction()) as opened:
        assert opened.store.article("things/cedar") is None
        assert opened.store.article("things/birch").identity.status == "active"  # type: ignore[union-attr]


def test_with_no_line_a_split_waits_for_a_person(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = Workspace(tmp_path, monkeypatch)
    space.admitted("Alder", "Alder Works")
    space.write("things", "authority: { merge: true }")  # its own merge lands, so no caller's decision guards it
    space.curate(("merge", {"article": "Alder", "other": "Alder Works", "reason": "one thing"}))
    assert "merged" in space.replies("merge")[0]
    space.write("things", "")
    space.later()
    space.curate(("split", {"article": "Alder Works", "reason": "two things after all"}))
    (reply,) = space.replies("split")
    assert "'note': 'waits for a caller, in p_" in reply
    assert [row["kind"] for row in space.waiting()] == ["article.split"]


def test_after_its_first_run_a_curator_is_shown_what_waits_and_an_admit_of_it_is_filed_beside_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = Workspace(tmp_path, monkeypatch)
    space.admitted("Alder", "Birch")
    space.curate(("admit", {"name": "Cedar Works", "reason": "Cedar came up in two notes", "domain": ["cedar.example"]}),
                 ("archive", {"article": "Birch", "reason": "no activity since spring"}))
    filed = len(space.rows())
    space.later()
    # another name, the same domain: the one that waits, matched by its domain
    space.curate(("admit", {"name": "Cedar Co", "reason": "named again", "domain": ["cedar.example"]}))
    opening = space.model.prompts[-1]
    assert "Waiting for a caller" in opening and "- Cedar Works, domain cedar.example, filed " in opening
    assert "Also waiting for a caller" in opening and "archive things/birch, by things/curator" in opening
    (reply,) = space.replies("admit")
    assert "'proposed'" in reply and len(space.rows()) == filed + 1


def test_a_curator_in_its_first_run_is_shown_nothing_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    space = Workspace(tmp_path, monkeypatch)
    space.curate(("admit", {"name": "Alder", "reason": "Alder came up in two notes"}))  # filed in proposal mode
    assert [row["target"] for row in space.waiting()] == ["things/alder"]
    space.later()
    space.curate()
    assert "Waiting for a caller" not in space.model.prompts[-1]
