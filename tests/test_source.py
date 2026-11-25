"""Source files: a workspace's `sources/` folder becomes connectors, and a bad file is refused alone with one line."""

from __future__ import annotations

from pathlib import Path

import pytest

from cerebro.connectors.files import FilesConnector
from cerebro.source import Select, SourceError, load_sources, read_source

# sources/local.yaml as a new workspace's base copies it.
LOCAL_YAML = """\
# Notes, call notes and emails you drop into local/ in this workspace, one
# record per file, its body the text. connect adds a source with your login.
kind: files
window: { behind: all }   # every file you drop in, whatever its Date: line, as when absent
select:
  - glob: "local/**/*.md"
    extract: facts
"""
CRM = "kind: salesforce\norg: work\nwindow: { behind: 30d }\nobjects:\n  Account: { into: accounts }\n"


def workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "sources").mkdir()
    for name, text in files.items():
        (tmp_path / "sources" / name).write_text(text)
    return tmp_path


def test_a_workspaces_sources_become_connectors_and_a_bad_file_or_a_kind_with_no_connector_stops_only_itself(
        tmp_path: Path) -> None:
    root = workspace(tmp_path, {"local.yaml": LOCAL_YAML, "crm.yaml": CRM, "broken.yaml": "kind: files\nwindow: 90d\n",
                            "notes.txt": "not a source"})
    (root / "local").mkdir()
    (root / "local" / "call.md").write_text("Topic: Apollo\n\nWe met.\n")
    (root / "local" / "empty.md").write_text("")
    (root / "local" / "readme.txt").write_text("Not markdown, so the glob leaves it out.")
    found = load_sources(root, {"files": FilesConnector})
    assert [c.name for c in found.connectors] == ["local"]
    assert [r.id for r in found.connectors[0].list().records] == ["local/call.md", "local/empty.md"]
    assert found.refused == ["sources/broken.yaml window: write a mapping, as { behind: 90d }"]
    assert found.notices == ["sources/crm.yaml: kind salesforce has no connector here: it loads and lists nothing"]
    crm = next(f for f in found.files if f.name == "crm")
    assert crm.window.behind == "30d" and crm.settings["org"] == "work"
    local = next(f for f in found.files if f.name == "local")
    assert local.kind == "files" and local.select[0].glob == "local/**/*.md" and local.select[0].extract == "facts"
    assert local.behind() is None and crm.behind().days == 30
    assert load_sources(tmp_path / "nowhere", {}).files == []


@pytest.mark.parametrize(("text", "line"), [
    ("kind: files\nwindow: { ahead: 14d }\n",
     ("window.ahead: files reads nothing ahead of now: delete ahead: and keep behind: alone, as window: "
     "{ behind: 90d }")),
    ("kind: files\nwindow: { behind: 90 }\n", "window.behind: '90' is not a duration"),
    ("kind: files\nwindow: { behind: 2w }\n", "or all"),
    ("kind: files\nextract: notes\n", "extract notes: write one of facts, directives, documents, records or none"),
    ("kind: files\nselect:\n  - extract: facts\n", "select[0]: glob is required"),
    ("kind: files\nselect:\n  - glob: a/*.md\n    id: \"{name}\"\n", "select[0].id: {name} is not a template"),
    ("kind: files\nselect:\n  - glob: a/*.md\n    id: \"{row.id}\"\n", "{row.id} needs rows"),
    ("kind: files\nselect:\n  - glob: a/*.md\n    globs: b\n", "select[0].globs is not a key"),
    ("kind: files\nrepos: [a/b]\n", "repos belongs to a source of kind github"),
    ("kind: files\nfolder: notes\n", "line 2: folder is not a key a source of kind files reads"),
    ("kind: gong\nteams: [ENG]\n", "teams belongs to a source of kind linear"),
    ("kind: gmail\nlabels: [INBOX]\n", "line 2: labels is not a key a source of kind gmail reads"),
    ("kind: linear\nteams: [ENG]\nprojects: [x]\n", "line 3: projects is not a key a source of kind linear reads"),
    ("kind: mcp\ncommand: run\nbinding: contract\nbindings: x\n", "line 4: bindings is not a key a source of kind mcp"),
    ("kind: files\nbinding: contract\n", "binding belongs to a source of kind mcp"),
    ("kind: crm\n", "kind crm is not a kind of source"),
    ("window: { behind: 30d }\n", "kind is missing"),
    ("kind: gong\nauth: { item: x }\n", "auth: write a word such as oauth"),
    ("kind: mcp\nurl: https://x.example\ncommand: run\n", "url and command"),
    ("kind: files\nkind: files\n", "line 2: the key kind is given twice: fix the YAML"),
])
def test_a_bad_source_file_is_refused_with_one_line_that_names_the_file_and_the_key(tmp_path: Path, text: str,
                                                                                   line: str) -> None:
    path = tmp_path / "local.yaml"
    path.write_text(text)
    with pytest.raises(SourceError) as refused:
        read_source(path)
    message = str(refused.value)
    assert message.startswith("sources/local.yaml ") and line in message and "\n" not in message


def test_a_built_kind_keeps_the_lines_of_its_own(tmp_path: Path) -> None:
    (tmp_path / "work.yaml").write_text("kind: linear\nteams: [ENG]\n")
    (tmp_path / "crm.yaml").write_text(CRM)
    assert read_source(tmp_path / "work.yaml").settings == {"teams": ["ENG"]}
    assert read_source(tmp_path / "crm.yaml").settings == {"org": "work", "objects": {"Account": {"into": "accounts"}}}


def test_the_calendar_alone_reads_ahead(tmp_path: Path) -> None:
    path = tmp_path / "calendar.yaml"
    path.write_text("kind: calendar\nwindow: { ahead: 14d, behind: 30d }\nauth: { keychain: calendar }\n")
    source = read_source(path)
    assert source.window.ahead == "14d" and source.auth == "keychain: calendar"


def test_every_line_of_a_source_file_reads_into_its_record(tmp_path: Path) -> None:
    path = tmp_path / "shared.yaml"
    path.write_text("kind: files\nextract: records\nfreshness: 1d\n"
                    "select:\n  - glob: \"~/shared/*.md\"\n    extract: facts\n    id: \"shared:{path}\"\n"
                    "    url: \"https://files.example/{path}\"\n    into: notes\n    at: \"2026-11-01\"\n"
                    "    identity: { origin: \"{path}\" }\n    fields: { kind: shared, size: 3 }\n")
    source = read_source(path)
    assert (source.name, source.extract, source.into, source.freshness) == ("shared", "records", "", "1d")
    assert source.select == [Select(glob="~/shared/*.md", extract="facts", id="shared:{path}",
                                    url="https://files.example/{path}", into="notes", at="2026-11-01",
                                    identity={"origin": "{path}"}, fields={"kind": "shared", "size": "3"})]
    assert source.behind() is None and source.settings == {} and source.path == path
    mcp = tmp_path / "wiki.yaml"
    mcp.write_text("kind: mcp\ncommand: wiki-server\nenv: [WIKI_TOKEN]\nbinding: contract\ncapabilities: [fetch_by_id]\n")
    read = read_source(mcp)
    assert read.env == ["WIKI_TOKEN"] and read.command == "wiki-server" and read.capabilities == ["fetch_by_id"]
    assert read.settings == {"binding": "contract"} and read.behind().days == 90


def test_a_span_reads_hours_as_hours() -> None:
    from datetime import timedelta

    from cerebro.source import span

    assert span("36h") == timedelta(hours=36) and span("90d") == timedelta(days=90)
