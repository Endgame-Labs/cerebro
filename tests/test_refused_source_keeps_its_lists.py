"""A source whose file is refused keeps its lists of records that wait and records held, and after the person mends
the file the record that waited is tried again. Each pass loads the workspace anew from its files, as a pass does."""

from __future__ import annotations

from pathlib import Path

from cerebro import Cerebro
from cerebro.facts import ExtractionFailed
from cerebro.fakes import FakeExtraction, MemoryStore
from cerebro.runner import PassResult, _extract_all, _forget_gone, _list

SOURCE = ('kind: files\nwindow: { behind: all }\nselect:\n  - glob: "notes/*.md"\n    extract: facts\n'
          '  - glob: "held/*.md"\n    extract: facts\n    into: things\n')


class Failing(FakeExtraction):
    def extract(self, record):  # type: ignore[no-untyped-def]
        if "FAILME" in (record.body or ""):
            raise ExtractionFailed(f"stand-in: FAILME in {record.id}")
        return super().extract(record)


def one_pass(root: Path, store: MemoryStore, extraction: FakeExtraction) -> tuple[Cerebro, PassResult]:
    workspace = Cerebro(root, store=store, extraction=extraction, trusted=True)
    result = PassResult(refused=list(workspace.refused))
    _forget_gone(workspace)
    _extract_all(workspace, _list(workspace, result), result)
    return workspace, result


def test_a_refused_source_keeps_what_waits_and_what_is_held_until_its_file_loads_again(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents/things").mkdir(parents=True)
    (tmp_path / "agents/things/curator.md").write_text("---\nheartbeat: [change]\n---\nKeep things.\n")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources/notes.yaml").write_text(SOURCE)
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes/n1.md").write_text("Date: 2026-11-01\n\nThing one went well.\n")
    (tmp_path / "notes/n2.md").write_text("Date: 2026-11-02\n\nFAILME this one waits.\n")
    (tmp_path / "held").mkdir()
    (tmp_path / "held/h1.md").write_text("Date: 2026-11-03\n\nA thing no article holds yet.\n")
    store, extraction = MemoryStore(), Failing()
    one_pass(tmp_path, store, extraction)
    failed, held = store.state("failed:notes"), store.state("held:notes")
    assert failed == ["notes/n2.md"] and [row["id"] for row in held] == ["held/h1.md"]
    (tmp_path / "sources/notes.yaml").write_text(SOURCE + "scope: private\n")
    _, refused = one_pass(tmp_path, store, extraction)
    assert any(line.startswith("sources/notes.yaml line 9: scope is not a key") for line in refused.refused)
    assert store.state("failed:notes") == failed and store.state("held:notes") == held
    (tmp_path / "sources/notes.yaml").write_text(SOURCE)
    (tmp_path / "notes/n2.md").write_text("Date: 2026-11-02\n\nThis one waited and reads now.\n")
    one_pass(tmp_path, store, FakeExtraction())
    assert store.state("failed:notes") == [] and [row["id"] for row in store.state("held:notes")] == ["held/h1.md"]
    (tmp_path / "sources/notes.yaml").unlink()
    one_pass(tmp_path, store, FakeExtraction())
    assert store.state("held:notes") is None
