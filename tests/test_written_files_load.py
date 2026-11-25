"""Every agent file, eval suite and prompt file that Cerebro writes or shows loads as a workspace would load it: the
template's agent files and suite, what `init` and `agents new` write, the app's agent files with the app's kinds, each
whole agent file and suite the docs show, and each agent file and suite a check writes."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from cerebro import Cerebro, evals, prompts, scaffold
from cerebro.agent import load_agent, refuse_unknown_kinds
from cerebro.apps import collect, load_app
from cerebro.evidence import BUILTIN
from cerebro.fakes import ScriptedRun

PY = Path(__file__).resolve().parents[1]
NOTES = PY / "apps" / "notes"
DOCS = PY / "docs"
BLOCK = re.compile(r"^```(\w*)\n(.*?)^```", re.DOTALL | re.MULTILINE)
# Where each whole agent file the docs show sits in a workspace, by its doc and its order among the doc's agent files.
DOC_AGENTS = {
    ("agents.md", 0): "projects/atlas.md",
    ("agents.md", 1): "parts/curator.md",
    ("agents.md", 2): "teams/article.md",
    ("quickstart.md", 0): "projects/atlas.md",
}


def opened(root: Path, *, notes: bool = True) -> Cerebro:
    """The workspace at `root` as it loads, with the notes app handed in as well when `notes` says so."""
    return Cerebro(root, agent_run=ScriptedRun(lambda _: ""), apps=[load_app(NOTES)] if notes else [])


def blank(tmp_path: Path) -> Path:
    root = tmp_path / "w"
    scaffold.init(root, environ={})
    return root


def place(root: Path, rel: str, text: str) -> None:
    """Write an agent file at `agents/<rel>`, with a file for each subagent it lists, as its folder would hold."""
    path = root / "agents" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    front = yaml.safe_load(text.split("---")[1]) or {}
    for name in front.get("subagents") or []:
        sub = path.parent / "subagents" / f"{name}.md"
        sub.parent.mkdir(exist_ok=True)
        if not sub.exists():
            sub.write_text("---\nevidence:\n  - facts: {}\n---\n\nWrite it.\n", encoding="utf-8")


def test_the_gtm_template_loads_whole_in_a_workspace(tmp_path: Path) -> None:
    root = tmp_path / "w"
    scaffold.init(root, template="gtm", environ={})
    files = sorted((root / "agents").rglob("*.md"))
    with opened(root, notes=False) as workspace:  # only what init of the template gives
        assert workspace.refused == [] and len(files) == len(workspace.agents)
    assert evals.load(root / "evals" / "example.yaml").probes


def test_what_init_and_agents_new_write_loads(tmp_path: Path) -> None:
    root = blank(tmp_path)
    for id in ("things/curator", "things/article", "things/subagents/overview", "reports/weekly", "reports/curator"):
        scaffold.new_agent(root, id)
    with opened(root) as workspace:
        assert workspace.refused == []
        assert {"things/curator", "things/article", "things/subagents/overview", "reports/weekly",
                "reports/curator"} <= {agent.id for agent in workspace.agents}


def test_the_app_agents_load_with_the_app_kinds(tmp_path: Path) -> None:
    with opened(blank(tmp_path)) as workspace:
        assert workspace.refused == [] and "projects/standing" in [agent.id for agent in workspace.agents]


def doc_agents() -> dict[tuple[str, int], str]:
    found = {}
    for path in sorted(DOCS.rglob("*.md")):
        bodies = [m.group(2) for m in BLOCK.finditer(path.read_text(encoding="utf-8")) if m.group(2).startswith("---\n")
                  and not {"id", "name"} & set(yaml.safe_load(m.group(2).split("---")[1]) or {})]  # an article or workspace.md
        found.update({(path.relative_to(DOCS).as_posix(), i): body for i, body in enumerate(bodies)})
    return found


def test_each_whole_agent_file_the_docs_show_loads(tmp_path: Path) -> None:
    shown = doc_agents()
    assert set(shown) == set(DOC_AGENTS), "a doc shows an agent file: name where it sits in DOC_AGENTS"
    for (doc, i), rel in DOC_AGENTS.items():
        root = blank(tmp_path / f"{Path(doc).stem}-{i}")
        place(root, rel, shown[doc, i] + ("" if shown[doc, i].count("---") > 1 else "---\n"))
        with opened(root) as workspace:
            assert workspace.refused == [], (doc, i)


def written_by_checks() -> list[tuple[str, int, str]]:
    """Each agent file a check or a conformance suite spells, with what an f-string fills from a string the module
    names, or else a plain word."""
    out = []
    for path in sorted([*(PY / "checks").glob("*.py"), *(PY / "tests" / "suites").glob("*.py")]):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        named = {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
                 and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                 for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = "".join(v.value if isinstance(v, ast.Constant) else named.get(getattr(v.value, "id", ""), "things")
                               for v in node.values)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.replace("{{", "{").replace("}}", "}")
            else:
                continue
            if text.startswith("---\n") and "\n---" in text[3:]:
                out.append((path.name, node.lineno, text))
    return out


@pytest.mark.parametrize(("check", "line", "text"), written_by_checks())
def test_each_agent_file_a_check_writes_loads(tmp_path: Path, check: str, line: int, text: str) -> None:
    front = yaml.safe_load(text.split("---")[1]) or {}
    rel = ("things/curator.md" if "authority" in front else  # only a curator holds an authority: line
           "things/subagents/part.md" if "heading" in front else "things/article.md")
    place(tmp_path / "w", rel, text)
    agent = load_agent(tmp_path / "w" / "agents" / rel, tmp_path / "w" / "agents")
    kinds = collect(load_app(NOTES).functions).evidence
    assert kinds and refuse_unknown_kinds([agent], [*BUILTIN, *kinds], [])[1] == []


def test_each_eval_suite_the_docs_checks_and_tests_hold_loads(tmp_path: Path) -> None:
    text = (DOCS / "cli.md").read_text(encoding="utf-8")
    shown = [m.group(2) for m in BLOCK.finditer(text) if m.group(1) == "yaml" and "questions:" in m.group(2)]
    assert len(shown) == 1
    (suite := tmp_path / "harbor.yaml").write_text(shown[0], encoding="utf-8")
    for path in (suite, PY / "checks" / "gtm_questions.yaml", PY / "tests" / "data" / "evals" / "questions.yaml",
                 PY / "tests" / "data" / "ground" / "corpus.json",
                 PY / "src" / "cerebro" / "templates" / "gtm" / "evals" / "example.yaml"):
        assert evals.load(path).kind in ("questions", "ground"), path


def test_a_prompt_ejected_is_one_a_run_reads(tmp_path: Path) -> None:
    root = blank(tmp_path)
    for name in prompts.names():
        scaffold.eject_prompt(root, name)
    assert prompts.unread(root) == [] and prompts.outdated(root) == []
