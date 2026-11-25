"""One workspace reading another through a `kind: mcp` source: a record is the article alone, and its revision moves
only when the article does, however many proposals wait in the workspace it reads."""

from __future__ import annotations

from pathlib import Path

from test_mcp import YOU

from cerebro import Caller, Cerebro
from cerebro.article import Compilation, Identity, Property
from cerebro.connectors.mcp_source import McpSource
from cerebro.connectors.mcp_source_client import McpClient, Reach
from cerebro.fakes import MemoryStore
from cerebro.records import Proposal, propose
from cerebro.source import read_source

BINDING = """kind: mcp
command: cerebro mcp
capabilities: [enumerate_history, fetch_by_id]
read_only: [query, read]
binding:
  list: { tool: query, args: { from: things }, id: "$.items[*].id" }
  get: { tool: read, args: { ids: ["{id}"] } }
"""


def proposed(workspace: Cerebro, id: str) -> None:
    propose(workspace.store, Proposal(id, "article.archive", "things/a", Caller("things/curator", "agent"),
                                  "2026-11-03T09:00:00Z", reason="Alpha closed."))


def test_a_record_read_from_another_workspace_holds_the_article_alone_and_moves_only_with_it(tmp_path: Path) -> None:
    root = tmp_path / "upstream"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    upstream = Cerebro(root, store=MemoryStore())
    upstream.store.create(Identity("things/a", "things", "Alpha"))
    upstream.store.write_compilation(Compilation("things/a", "Overview", "Alpha ships in May.", by="things/article",
                                         at="2026-11-01T09:00:00Z"))
    upstream.store.set_field("things/a", "stage", Property("open", YOU, "2026-11-01T09:00:00Z"))
    proposed(upstream, "p_a1")
    down = tmp_path / "downstream"
    (down / "sources").mkdir(parents=True)
    (down / "sources" / "upstream.yaml").write_text(BINDING)
    served = upstream.mcp_server()
    source = McpSource(read_source(down / "sources" / "upstream.yaml"), down,
                       client=lambda: McpClient(Reach("upstream", server=served)))
    first = source.list()
    body = source.get("things/a")
    proposed(upstream, "p_a2")
    again = source.get("things/a")  # the same pass, after the count moved
    second = source.list(first.cursor)  # the next pass
    assert [r.id for r in first.records] == ["things/a"] and second.records == []
    assert body.body is not None and body.body.endswith("Alpha ships in May.") and "waiting" not in body.body
    assert (again.body, again.revision) == (body.body, body.revision)
    upstream.store.write_compilation(Compilation("things/a", "Overview", "Alpha ships in June.", by="things/article",
                                         at="2026-11-05T09:00:00Z"))
    assert [r.id for r in source.list(second.cursor).records] == ["things/a"]
