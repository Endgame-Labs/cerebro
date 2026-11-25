"""A token never enters the wiki's knowledge: one a caller pastes into what it sends, or one a source's record holds,
is masked before anything keeps it or hands it to a model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_tokens import Session, no_network, serving  # noqa: F401  (no_network applies here too)

from cerebro import Caller, Cerebro
from cerebro.article import Identity
from cerebro.connector import Record
from cerebro.facts import Extracted
from cerebro.fakes import FakeExtraction
from cerebro.records import Decision
from cerebro.tokens import HIDDEN, MASKED

PERSON = Caller("ana", "person")


class Seen(FakeExtraction):
    """The fake facts layer, keeping each record's body as a model would be sent it."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[str] = []

    def extract(self, record: Record) -> Extracted:
        self.bodies.append(record.body)
        return super().extract(record)


def a_workspace(root: Path) -> tuple[Cerebro, Seen]:
    root.mkdir()
    (root / "settings.yaml").write_text("budget:\n  daily_usd: 20\n")
    seen = Seen()
    workspace = Cerebro(root, extraction=seen)
    workspace.store.create(Identity("projects/apollo", "projects", "Apollo"))
    return workspace, seen


def everything(workspace: Cerebro, root: Path) -> str:
    """The store's file and its write-ahead files, as text."""
    workspace.store.close()
    return b"".join(p.read_bytes() for p in (root / ".cerebro").rglob("*") if p.is_file()).decode("latin-1")


def test_a_token_told_over_http_is_masked_before_the_record_its_facts_the_store_or_a_model_see_it(
        tmp_path: Path) -> None:
    workspace, seen = a_workspace(tmp_path / "workspace")
    agent = workspace.make_token("desk-agent")
    pasted = workspace.make_token("pasted").token
    with serving(workspace) as client:
        reply = Session(client, agent.token).call("tell", {
            "text": f"The Apollo review moved to Friday.\n\nThe deploy key is {pasted} for now.",
            "target": "projects/apollo", "as": "fact"})["structuredContent"]
    assert HIDDEN in reply["notes"] and pasted not in str(reply)
    record = workspace.store.record(reply["record"])
    assert record is not None and pasted not in record.body and MASKED in record.body
    facts = [fact for fact in workspace.lookup.read(PERSON, ["projects/apollo"]).facts]
    assert facts and not any(pasted in str(fact) for fact in facts)
    assert seen.bodies and not any(pasted in body for body in seen.bodies)
    assert pasted not in everything(workspace, tmp_path / "workspace")


def test_a_token_in_a_decide_reason_or_a_retract_reason_is_masked_and_the_reply_says_so(tmp_path: Path) -> None:
    workspace, _ = a_workspace(tmp_path / "workspace")
    pasted = workspace.make_token("pasted").token
    told = workspace.tell(PERSON, "Lead with the review date.", "projects/apollo", "proposal")
    asked = workspace.tell(Caller("desk-agent", "person"), "Apollo moved.", "projects/apollo", "fact")
    decided = workspace.decide(PERSON, reject=[Decision(told.proposals[0], f"not now, {pasted}")])
    taken = workspace.retract(Caller("desk-agent", "person"), asked.record or "", f"wrong, {pasted}")
    assert HIDDEN in decided.notes[told.proposals[0]] and HIDDEN in taken.notes
    rows = str([row.data for log in ("proposals", "merges") for row in workspace.store.rows(log)])
    assert pasted not in rows and pasted not in str(decided) and pasted not in str(taken)
    assert pasted not in everything(workspace, tmp_path / "workspace")


def test_a_token_in_a_source_file_is_masked_before_a_model_is_sent_it(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    workspace, seen = a_workspace(root)
    pasted = workspace.make_token("pasted").token
    workspace.close()
    (root / "sources").mkdir()
    (root / "sources" / "local.yaml").write_text(
        'kind: files\nwindow: { behind: all }\nselect:\n  - glob: "local/**/*.md"\n    extract: facts\n')
    (root / "local").mkdir()
    (root / "local" / "note.md").write_text(f"Apollo ships Friday.\n\nPaste for later: {pasted}\n")
    with Cerebro(root, extraction=seen) as workspace:
        workspace.run_once(agents=False)
        assert seen.bodies and not any(pasted in body for body in seen.bodies)
        assert any(MASKED in body for body in seen.bodies)
        assert workspace.mask(f"say {pasted}") == f"say {MASKED}"


def test_a_tell_with_no_token_says_nothing_of_one(tmp_path: Path) -> None:
    workspace, _ = a_workspace(tmp_path / "workspace")
    told: Any = workspace.tell(PERSON, "The Apollo review moved to Friday.", "projects/apollo", "fact")
    assert HIDDEN not in told.notes
