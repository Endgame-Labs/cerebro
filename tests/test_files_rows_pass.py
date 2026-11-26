"""A pass over a rows source, through the runner's own steps with a fake facts layer: a row that stops reading, or
is gone from its file, leaves the article it binds as it was."""

from __future__ import annotations

from pathlib import Path

from cerebro import Cerebro
from cerebro.article import Identity
from cerebro.connectors.files import QUOTE_IN_VALUE
from cerebro.fakes import FakeExtraction, MemoryStore
from cerebro.runner import PassResult, _extract_all, _list

SOURCE = ('kind: files\nwindow: { behind: all }\nselect:\n  - glob: "exports/t.csv"\n    rows: csv\n'
          '    id: "{row.thing}"\n    into: things\n    identity: { thing: "{row.thing}" }\n'
          '    fields: { seats: "{row.seats}" }\n')


def make_workspace(root: Path) -> Cerebro:
    (root / "settings.yaml").write_text("{}\n")
    (root / "agents/things").mkdir(parents=True)
    (root / "agents/things/curator.md").write_text("---\nheartbeat: [change]\n---\nKeep things.\n")
    (root / "sources").mkdir()
    (root / "sources/rows.yaml").write_text(SOURCE)
    (root / "exports").mkdir()
    (root / "exports/t.csv").write_text("thing,name,seats\nt1,Alpha,4\nt2,Beta,5\n")
    workspace = Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), trusted=True)
    workspace.store.create(Identity("things/alpha", "things", "Alpha", {"thing": ["t1"]}))
    return workspace


def one_pass(workspace: Cerebro) -> PassResult:
    result = PassResult()
    _extract_all(workspace, _list(workspace, result, only="rows"), result)
    return result


def test_a_row_that_stops_reading_or_is_gone_leaves_its_article_as_it_was(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    one_pass(workspace)
    assert workspace.store.article("things/alpha").properties == {}  # a source's fields are no writer of front matter
    (tmp_path / "exports/t.csv").write_text('thing,name,seats\nt1,Al"pha,4\nt2,Beta,5\n')
    broken = one_pass(workspace)
    assert workspace.store.article("things/alpha").identity.status == "active"
    assert any(f"exports/t.csv line 2: {QUOTE_IN_VALUE}, so the row is not read" in line
               for line in broken.notices)
    (tmp_path / "exports/t.csv").write_text("thing,name,seats\nt2,Beta,5\n")
    one_pass(workspace)
    assert workspace.store.article("things/alpha").identity.status == "active"
    assert not any("archived" in line for line in broken.notices)
