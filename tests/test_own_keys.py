"""A workspace whose `workspace.md` names its own domain, and a curator run on it, for the tests of the workspace's own
side. Neutral names only: `things` and `parts`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from test_held import ALL, CURATOR, replies

from cerebro.agent import Agent
from cerebro.curate import Curated, run_curator
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.lookup import StoreLookup

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)


def lay_out_workspace(root: Path) -> Path:
    agents = root / "agents"
    for collection in ("things", "parts"):
        (agents / collection).mkdir(parents=True)
        (agents / collection / "curator.md").write_text(CURATOR.format(authority=ALL, extra="", prompt=f"Keep {collection}."))
    (root / "workspace.md").write_text("---\nidentity: { domains: [ourco.example] }\n---\n\nWho we are.\n")
    return agents


def curate(store: MemoryStore, agent: Agent, calls: list[tuple[str, dict[str, Any]]]) -> Curated:
    return run_curator(agent, lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW), store=store,
                       agent_run=ScriptedRun(lambda prompt: {}, calls), root=agent.path.parent.parent.parent,
                       functions={}, agent_tools={}, clock=lambda: NOW, sources={"notes": FakeConnector("notes", [])})


def told(done: Curated) -> list[str]:
    return replies(done)


def test_the_engines_own_side_code_says_the_workspaces_own_domains_never_a_company() -> None:
    """Rule 1: the engine assumes no domain's idea, so its own-side words are the workspace's, not a company's."""
    src = Path(__file__).resolve().parents[1] / "src" / "cerebro"
    found = [f"{name}:{n}" for name in ("held.py", "curate.py", "connector.py", "project.py")
             for n, line in enumerate((src / name).read_text().splitlines(), 1) if "your company" in line]
    assert not found, found
