"""Creating a workspace and scaffolding its files: what `init` lays down loads as a workspace, nothing is overwritten,
and each scaffold loads as written or is refused with one line and no file left behind."""

from __future__ import annotations

from pathlib import Path

import pytest

from cerebro import scaffold
from cerebro.agent import load_agent
from cerebro.models import DEFAULTS
from cerebro.project import load
from cerebro.scaffold import ScaffoldError
from cerebro.source import read_source

GTM = scaffold.TEMPLATES / "gtm" / "agents"
DOCS = Path(__file__).resolve().parents[1] / "docs"
HOW_TO = ["add-a-source", "why-is-this-article-stale", "write-a-context-agent"]


def _count(folder) -> int:
    return sum(_count(item) if item.is_dir() else 1 for item in folder.iterdir())


def test_init_from_the_gtm_template_lays_down_a_workspace_whose_every_agent_and_source_loads(tmp_path: Path) -> None:
    done = scaffold.init(tmp_path / "team" / "workspace", template="gtm", daily_usd=12.5, timezone="America/Chicago", domains=["@Example.com"],
                         environ={}, command="/bin/cerebro")
    root = done.root
    project = load(root)
    assert project.refused == [] and len(project.agents) == _count(GTM) > 20
    assert project.settings["budget"] == {"daily_usd": 12.5} and project.settings["timezone"] == "America/Chicago"
    assert project.models == DEFAULTS
    assert read_source(root / "sources" / "local.yaml").kind == "files" and (root / "local").is_dir()
    assert "domains: [example.com]" in (root / "workspace.md").read_text()
    guide = (root / "AGENTS.md").read_text()
    assert "cerebro agents list" in guide
    lines = guide.splitlines()  # each table's head is followed by its separator row, or it renders as plain text
    heads = [n for n, line in enumerate(lines) if line.startswith("|") and not lines[n - 1].startswith("|")]
    assert heads and all(lines[n + 1].replace("|", "").strip(" -:") == "" for n in heads), heads
    assert not (root / ".claude").exists()
    assert (root / ".gitignore").read_text().splitlines() == ["articles/", ".cerebro/"]
    assert done.written[-1] == "settings.yaml" and done.ignored == ["articles/", ".cerebro/"]
    assert done.missing_keys == ["FIREWORKS_API_KEY", "TYPESAFE_API_KEY"]
    assert done.mcp == {"name": "cerebro-workspace", "command": "/bin/cerebro", "args": ["mcp", "--workspace", str(root)]}


def test_a_blank_workspace_holds_its_base_and_no_agent_and_names_no_key_the_environment_holds(tmp_path: Path) -> None:
    done = scaffold.init(tmp_path, environ={"FIREWORKS_API_KEY": "fw-test", "TYPESAFE_API_KEY": "ts-test"})
    bare = scaffold.init(tmp_path / "bare").missing_keys
    assert done.missing_keys == [] and bare == ["FIREWORKS_API_KEY", "TYPESAFE_API_KEY"]
    assert load(tmp_path).settings["budget"] == {"daily_usd": 50} and (tmp_path / "local").is_dir()
    assert not any(b"fw-test" in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    assert load(tmp_path).agents == [] and (tmp_path / "agents").is_dir()
    assert read_source(tmp_path / "sources" / "local.yaml").kind == "files"
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.iterdir() if p.name != "bare") == [
        ".cerebro", ".gitignore", "AGENTS.md", "agents", "local", "settings.yaml", "sources", "workspace.md"]


def test_an_unknown_template_is_refused_with_the_templates_there_are(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError, match=r"^--template 'sales' is no template: use one of gtm, or leave it out"):
        scaffold.init(tmp_path / "workspace", template="sales", environ={})
    assert not (tmp_path / "workspace").exists()


def test_init_adds_only_the_lines_a_gitignore_lacks_and_keeps_the_rest(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\n.cerebro\n")
    done = scaffold.init(tmp_path, environ={})
    assert (tmp_path / ".gitignore").read_text() == "*.log\n.cerebro\narticles/\n"
    assert done.ignored == ["articles/"]
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / ".gitignore").write_text("*.log")
    scaffold.init(tmp_path / "other", environ={})
    assert (tmp_path / "other" / ".gitignore").read_text() == "*.log\narticles/\n.cerebro/\n"


def test_init_refuses_a_workspace_or_a_file_it_would_overwrite_and_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("{}\n")
    with pytest.raises(ScaffoldError, match="already a workspace"):
        scaffold.init(tmp_path, environ={})
    other = tmp_path / "other"
    (other / "agents" / "reports").mkdir(parents=True)
    (other / "agents" / "reports" / "pipeline.md").write_text("mine")
    with pytest.raises(ScaffoldError, match="agents/reports/pipeline.md is already in"):
        scaffold.init(other, template="gtm", environ={})
    assert sorted(p.relative_to(other).as_posix() for p in other.rglob("*")) == [
        "agents", "agents/reports", "agents/reports/pipeline.md"]


def test_init_refuses_a_link_along_any_path_it_would_write_and_writes_nothing_through_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "rc").write_text("original\n")
    for name, target in ((".gitignore", outside / "rc"), ("AGENTS.md", outside / "gone"), ("sources", outside)):
        workspace = tmp_path / name.strip(".")
        workspace.mkdir()
        (workspace / name).symlink_to(target)
        with pytest.raises(ScaffoldError, match=f"{name} in .* is a link, and cerebro writes nothing through a link"):
            scaffold.init(workspace, template="gtm", environ={})
        assert sorted(p.name for p in workspace.iterdir()) == [name]
    assert sorted(p.name for p in outside.iterdir()) == ["rc"] and (outside / "rc").read_text() == "original\n"


def test_init_refuses_a_bad_answer_naming_its_flag_before_writing(tmp_path: Path) -> None:
    for kwargs, flag in [({"timezone": "Mars/Olympus"}, "--timezone"), ({"daily_usd": -1}, "--daily-usd"),
                         ({"domains": ["not a domain"]}, "--domain")]:
        with pytest.raises(ScaffoldError, match=flag):
            scaffold.init(tmp_path / "workspace", environ={}, **kwargs)
        assert not (tmp_path / "workspace").exists()


def test_init_removes_what_it_wrote_when_a_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = scaffold._create

    def failing(path: Path, data: bytes) -> None:
        if path.name == "settings.yaml":
            raise OSError(28, "No space left on device")
        real(path, data)

    monkeypatch.setattr(scaffold, "_create", failing)
    with pytest.raises(ScaffoldError, match="No space left on device"):
        scaffold.init(tmp_path / "workspace", environ={})
    assert not (tmp_path / "workspace").exists()
    (tmp_path / "kept" / "notes").mkdir(parents=True)
    (tmp_path / "kept" / ".gitignore").write_bytes(b"*.log")
    with pytest.raises(ScaffoldError, match="No space left on device"):
        scaffold.init(tmp_path / "kept", environ={})
    assert sorted(p.name for p in (tmp_path / "kept").iterdir()) == [".gitignore", "notes"]
    assert (tmp_path / "kept" / ".gitignore").read_bytes() == b"*.log"
    (tmp_path / "bare").mkdir()
    with pytest.raises(ScaffoldError, match="No space left on device"):
        scaffold.init(tmp_path / "bare", environ={})
    assert list((tmp_path / "bare").iterdir()) == []


def test_each_agent_scaffold_loads_in_the_role_its_path_gives(tmp_path: Path) -> None:
    scaffold.init(tmp_path, environ={})
    agents = tmp_path / "agents"
    for id, role in [("projects/curator", "curator"), ("projects/article", "article"),
                     ("projects/subagents/next-steps", "subagent"), ("reports/weekly", "writer")]:
        made = scaffold.new_agent(tmp_path, id)
        agent = load_agent(tmp_path / made.path, agents)
        assert (agent.role, agent.heartbeat) == (role, ["manual"]), id
        runs = "projects/article, which runs it" if role == "subagent" else id  # a subagent runs in its composer
        assert made.next == f"fill in agents/{id}.md, then cerebro agents run {runs}"
    curator = load_agent(agents / "projects" / "curator.md", agents)
    # a new curator does none of the four on its own until a person sets one true in its file
    assert curator.authority is not None and not any(
        getattr(curator.authority, kind) for kind in ("admit", "archive", "merge", "split"))


def test_a_new_subagent_says_where_to_list_it_until_its_composer_does(tmp_path: Path) -> None:
    scaffold.init(tmp_path, environ={})
    (tmp_path / "agents" / "projects").mkdir()
    (tmp_path / "agents" / "projects" / "article.md").write_text("---\nsubagents: [risks]\n---\n\nCompose it.\n")
    assert scaffold.new_agent(tmp_path, "projects/subagents/risks").notes == []
    assert scaffold.new_agent(tmp_path, "projects/subagents/owners").notes == [
        "list owners under subagents: in agents/projects/article.md, or it does not run"]


def test_an_agent_id_the_layout_has_no_place_for_is_refused_and_leaves_no_file(tmp_path: Path) -> None:
    scaffold.init(tmp_path, environ={})
    for id, said in [("reports", "has no folder"), ("Reports/Weekly", "lower case"),
                     ("reports/drafts/weekly", "has no place in the layout")]:
        with pytest.raises(ScaffoldError, match=said):
            scaffold.new_agent(tmp_path, id)
    scaffold.new_agent(tmp_path, "reports/weekly")
    with pytest.raises(ScaffoldError, match="agents/reports/weekly.md already exists"):
        scaffold.new_agent(tmp_path, "reports/weekly")
    assert sorted(p.relative_to(tmp_path / "agents").as_posix() for p in (tmp_path / "agents").rglob("*.md")) == [
        "reports/weekly.md"]
    with pytest.raises(ScaffoldError, match="not a workspace"):
        scaffold.new_agent(tmp_path / "agents", "reports/other")


def test_a_source_scaffold_reads_as_written_and_a_bad_one_leaves_no_file(tmp_path: Path) -> None:
    scaffold.init(tmp_path, environ={})
    made = scaffold.new_source(tmp_path, "meeting-notes")
    source = read_source(tmp_path / made.path)
    assert [s.glob for s in source.select] == ["meeting-notes/**/*.md"] and (tmp_path / "meeting-notes").is_dir()
    two = scaffold.new_source(tmp_path, "exports", ["exports/*.md", "more/*.txt"], extract="documents")
    assert two.next == "put files in the folders the globs name, then cerebro serve --once"
    assert not (tmp_path / "exports").exists()
    (tmp_path / "notes").mkdir()
    assert scaffold.new_source(tmp_path, "notes").next == "put files in notes, then cerebro serve --once"
    assert [(s.glob, s.extract) for s in read_source(tmp_path / two.path).select] == [
        ("exports/*.md", "documents"), ("more/*.txt", "documents")]
    for name, kwargs, said in [("Notes", {}, "lower case"), ("x", {"extract": "all"}, "--extract"),
                               ("meeting-notes", {}, "already exists")]:
        with pytest.raises(ScaffoldError, match=said):
            scaffold.new_source(tmp_path, name, **kwargs)
    assert sorted(p.name for p in (tmp_path / "sources").iterdir()) == ["exports.yaml", "local.yaml", "meeting-notes.yaml",
                                                                         "notes.yaml"]


def test_the_mcp_entry_names_the_server_after_the_folder(tmp_path: Path) -> None:
    entry = scaffold.mcp_entry(tmp_path / "my workspace!!", "cerebro")
    assert entry["name"] == "cerebro-my-workspace"
    assert entry["args"] == ["mcp", "--workspace", str((tmp_path / "my workspace!!").resolve())]


def test_prompts_are_listed_from_the_bundle_and_an_ejected_copy_is_never_overwritten(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "rules.md").write_text("Cite every claim.\n")
    (bundle / "evidence.md").write_text("Today is $today.\n")
    (bundle / "__init__.py").write_text("")
    workspace = tmp_path / "workspace"
    scaffold.init(workspace, environ={})
    monkeypatch.setattr(scaffold, "PROMPTS", bundle)
    assert [(p["name"], p["ejected"]) for p in scaffold.prompts(workspace)] == [("evidence", False), ("rules", False)]
    made = scaffold.eject_prompt(workspace, "rules")
    assert made.path == "prompts/rules.md" and (workspace / "prompts" / "rules.md").read_text() == "Cite every claim.\n"
    assert [p["ejected"] for p in scaffold.prompts(workspace)] == [False, True]
    (workspace / "prompts" / "rules.md").write_text("Mine.\n")
    with pytest.raises(ScaffoldError, match="prompts/rules.md already exists"):
        scaffold.eject_prompt(workspace, "rules")
    assert (workspace / "prompts" / "rules.md").read_text() == "Mine.\n"
    with pytest.raises(ScaffoldError, match="no bundled prompt named tone: use one of evidence, rules"):
        scaffold.eject_prompt(workspace, "tone")
    monkeypatch.setattr(scaffold, "PROMPTS", tmp_path / "none")
    assert scaffold.prompts(workspace) == []
    with pytest.raises(ScaffoldError, match="this version bundles none"):
        scaffold.eject_prompt(workspace, "rules")


def test_the_guide_init_writes_points_a_coding_agent_at_the_how_to_skills_and_ships_none(tmp_path: Path) -> None:
    guide = (scaffold.init(tmp_path / "workspace", environ={}).root / "AGENTS.md").read_text()
    assert "docs/skills/" in guide and not any(f"skills/{name}" in guide for name in HOW_TO)


def test_a_workspace_that_holds_the_how_to_skills_loads(
        tmp_path: Path) -> None:
    root = scaffold.init(tmp_path / "workspace", environ={}).root
    for name in HOW_TO:
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text((DOCS / "skills" / name / "SKILL.md").read_text())
    (root / "agents" / "projects").mkdir(parents=True)
    (root / "agents" / "projects" / "harbor.md").write_text(
        "---\nevidence:\n  - facts: { window: last_90_days }\n---\n\nWrite it.\n\n# Output\n\n## Now\n")
    project = load(root)
    assert project.refused == [] and [a.id for a in project.agents] == ["projects/harbor"]
