"""settings.yaml refuses a key it does not know, at every level, with the file, the line and the key, and a value of
the wrong kind names its line too."""

from __future__ import annotations

import ast
import functools
import re
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro import scaffold
from cerebro.cli import main
from cerebro.project import WorkspaceError, load, read_schedule

PY = Path(__file__).resolve().parents[1]
MODELS = ("models:\n"
          "  extraction: { provider: fireworks, model: glm-5p3-flash, effort: low }\n"
          "  decision: { provider: typesafe, model: jev-1.13.0 }\n"
          "  compilation: { provider: fireworks, model: glm-5p3, effort: high }\n")


def _refused(tmp_path: Path, text: str) -> str:
    (tmp_path / "settings.yaml").write_text(text)
    with pytest.raises(WorkspaceError) as refused:
        load(tmp_path)
    return str(refused.value)


@pytest.mark.parametrize(("text", "says"), [
    ("timzone: UTC\n", 'settings.yaml: line 1: unknown key "timzone"'),
    ("id: w-1\nbudgte:\n  daily_usd: 5\n", 'settings.yaml: line 2: unknown key "budgte"'),
    (MODELS + "budget:\n  dialy_usd: 5\n", 'settings.yaml: line 6: unknown key "dialy_usd"'),
    ("budget: {dialy_usd: 5}\n", 'settings.yaml: line 1: unknown key "dialy_usd"'),
    (MODELS.replace("effort: low", "efort: low"), 'settings.yaml: line 2: unknown key "efort"'),
    ("serve:\n  host: 127.0.0.1\n  intreval: 60\n", 'settings.yaml: line 3: unknown key "intreval"'),
    ("collections:\n  things:\n    descripton: x\n", 'settings.yaml: line 3: unknown key "descripton"'),
    ("store:\n  paht: x.db\n", 'settings.yaml: line 2: unknown key "paht"'),
    ("store:\n  class: sqlite\n  path: x.db\n  shards: 2\n", 'settings.yaml: line 4: unknown key "shards"'),
    ("facts: { revisions: 3 }\n", 'settings.yaml: line 1: unknown key "revisions"'),
    ("compile:\n  concurency: 4\n", 'settings.yaml: line 2: unknown key "concurency"'),
    ("search:\n  rerank: true\n  rerank_tpo: 5\n", 'settings.yaml: line 3: unknown key "rerank_tpo"'),
    ("schedule:\n  sources: 15m\n  setle: 1m\n", 'settings.yaml: line 3: unknown key "setle"'),
])
def test_an_unknown_key_at_any_level_is_refused_with_the_file_its_line_and_the_key(
        tmp_path: Path, text: str, says: str) -> None:
    assert _refused(tmp_path, text) == says


def test_a_store_of_another_class_takes_the_keys_its_class_takes(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("store:\n  class: cerebro.fakes:MemoryStore\n  shards: 2\n")
    assert load(tmp_path).settings["store"]["shards"] == 2


@pytest.mark.parametrize(("text", "says"), [
    ("serve:\n  interval: abc\n", "settings.yaml: line 2: serve.interval is 'abc': write a number from 1 to 86400"),
    ("serve:\n  interval: 0\n", "settings.yaml: line 2: serve.interval is 0: write a number from 1 to 86400"),
    ("serve:\n  port: 0\n", "settings.yaml: line 2: serve.port is 0: write a whole number from 1 to 65535"),
    ("serve:\n  host: [a, b]\n", ("settings.yaml: line 2: serve.host is ['a', 'b']: write an address, as host: "
                                 "127.0.0.1")),
    ("serve: 8080\n", "settings.yaml: line 1: serve is not a map: write serve: then host:, port: or interval: under it"),
    ("serve:\n  allowed_hosts: wiki.example\n", ("settings.yaml: line 2: serve.allowed_hosts is not a list of host "
                                                "names: write it as allowed_hosts: [wiki.example]")),
    ("schedule:\n  articles: 30s\n", ("settings.yaml: line 2: schedule.articles: '30s' is under a minute: write a "
                                     "minute or more, as 4h, or 0 to rewrite a changed article on every pass")),
    ("budget:\n  daily_usd: 0\n", ("settings.yaml: line 2: budget.daily_usd is 0, which would stop every model call: "
                                  "write an amount above zero, or leave the key out for no cap")),
    ("compile:\n  concurrency: lots\n", ("settings.yaml: line 2: compile.concurrency is 'lots': write a whole number "
                                        "from 1 to 64")),
    ("search:\n  rerank: yes please\n", "settings.yaml: line 2: search.rerank is 'yes please': write true or false"),
    ("id: [a]\n", "settings.yaml: line 1: id is ['a']: write the id cerebro init wrote, a word such as notes-wiki-36fe74"),
    (MODELS.replace("effort: low", "effort: extreme"),
     ("settings.yaml: line 2: models.extraction.effort is 'extreme': use none, low, medium, high, xhigh, max, or "
     "leave it out")),
])
def test_a_value_of_the_wrong_kind_names_its_line_and_its_key(tmp_path: Path, text: str, says: str) -> None:
    assert _refused(tmp_path, text) == says


@pytest.mark.parametrize("zero", ["0", "0m", "0s", "00h", "0d"])
def test_zero_with_or_without_a_unit_turns_the_articles_cooldown_off(zero: str) -> None:
    assert read_schedule({"schedule": {"articles": zero, "settle": zero}}).articles == timedelta(0)


def test_every_read_command_stops_at_an_unknown_key_with_exit_1(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("budget: {dialy_usd: 5}\n")
    for command in (["status"], ["serve", "--once"], ["review"]):
        result = CliRunner().invoke(main, [*command, "--workspace", str(tmp_path)])
        assert result.exit_code == 1, (command, result.output)
        assert 'settings.yaml: line 1: unknown key "dialy_usd"' in result.output, (command, result.output)


@pytest.mark.parametrize("template", [None, *scaffold.templates()])
def test_the_settings_init_writes_for_every_template_load(tmp_path: Path, template: str | None) -> None:
    root = tmp_path / "w"
    scaffold.init(root, template=template, daily_usd=12.5, timezone="America/Chicago", environ={})
    assert load(root).settings["budget"] == {"daily_usd": 12.5}
    scaffold.init(tmp_path / "bare", template=template, daily_usd=None, environ={})
    load(tmp_path / "bare")


def _docs_examples() -> list[tuple[str, str]]:
    found = []
    for path in sorted((PY / "docs").rglob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines):
            if line.strip() == "```yaml":
                before = " ".join(lines[max(0, n - 3):n])
                end = next(i for i in range(n + 1, len(lines)) if lines[i].strip() == "```")
                body = "\n".join(lines[n + 1:end])
                if "settings.yaml" in before or "settings turn" in before or body.startswith("# settings.yaml"):
                    found.append((f"{path.name}:{n + 1}", body + "\n"))
    return found


def test_every_settings_example_in_the_docs_loads(tmp_path: Path) -> None:
    examples = _docs_examples()
    assert len(examples) >= 4, examples
    for where, text in examples:
        root = tmp_path / where.replace(":", "-")
        root.mkdir()
        (root / "settings.yaml").write_text(text)
        load(root)


# -- the list of known keys against the code that reads them -------------------------------------------------------

SRC = PY / "src" / "cerebro"
# A `settings` name here is not the workspace's settings.yaml: a model call's settings, and a source file's.
NOT_WORKSPACE = {("run.py", "*"), ("settle.py", "summaries"), ("source.py", "*")}
HELPERS = {"_number": 2, "_cap": 1}  # helpers that take a block, then the keys under it as strings
# Reads whose key is not a string in the code, so a scan cannot see them: each from the constant that drives it.
UNSEEN = ("the keys of mcp.LIMITS, models.JOBS by models.ENTRY_KEYS, project.SCHEDULE_KEYS, project.SERVE_LIMITS, "
          "providers by models.PROVIDERS")


def _unseen() -> set[tuple[str, ...]]:
    from cerebro import mcp, models, project

    out = {("serve", key) for key in mcp.LIMITS} | {("serve", key) for key in project.SERVE_LIMITS}
    out |= {("models", job, key) for job in models.JOBS for key in models.ENTRY_KEYS}
    out |= {("schedule", key) for key in project.SCHEDULE_KEYS}
    out |= {("providers", name, key) for name in models.PROVIDERS for key in ("base_url", "headers")}
    return out


class _Reads(ast.NodeVisitor):
    """Every settings key read by a string in one file: `settings.get("k")`, `x.settings["k"]`, and the same on a
    name bound to a block read that way, as `block = settings.get("budget")`, then `block.get("daily_usd")`."""

    def __init__(self, file: str) -> None:
        self.file, self.found = file, set()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if (self.file, "*") in NOT_WORKSPACE or (self.file, node.name) in NOT_WORKSPACE:
            return
        bound: dict[str, tuple[str, ...]] = {}
        for _ in range(3):
            for child in ast.walk(node):
                if isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
                    path = self._path(child.value, bound)
                    if path is not None and child.targets[0].id not in bound:
                        bound[child.targets[0].id] = path
                if isinstance(child, ast.For) and isinstance(child.iter, ast.Call) \
                        and isinstance(child.iter.func, ast.Attribute) and child.iter.func.attr == "items":
                    path = self._path(child.iter.func.value, bound)
                    if path is not None and isinstance(child.target, ast.Tuple) and len(child.target.elts) == 2 \
                            and isinstance(child.target.elts[1], ast.Name):
                        bound[child.target.elts[1].id] = (*path, "*")
        for child in ast.walk(node):
            if isinstance(child, (ast.Call, ast.Subscript)):
                path = self._path(child, bound)
                if path:
                    self.found.add(path)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in HELPERS \
                    and child.args:
                base = self._path(child.args[0], bound)
                keys = [a.value for a in child.args[1:1 + HELPERS[child.func.id]]
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if base is not None and len(keys) == HELPERS[child.func.id]:
                    self.found.add((*base, *keys))

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _path(self, node: ast.AST, bound: dict[str, tuple[str, ...]]) -> tuple[str, ...] | None:
        if isinstance(node, ast.BoolOp):
            return self._path(node.values[0], bound)
        if isinstance(node, ast.IfExp):
            return self._path(node.body, bound)
        if isinstance(node, ast.Name):
            return () if node.id == "settings" else bound.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "settings":
            owner = node.value
            named = owner.attr if isinstance(owner, ast.Attribute) else owner.id if isinstance(owner, ast.Name) else ""
            return None if named == "source" else ()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_settings":
            return ()
        key = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            key, inner = node.args[0], node.func.value
        elif isinstance(node, ast.Subscript):
            key, inner = node.slice, node.value
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            base = self._path(inner, bound)
            return None if base is None else (*base, key.value)
        return None


@functools.cache  # both checks below read every file of the code
def _reads() -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    for path in [*SRC.rglob("*.py"), *(PY / "apps").rglob("*.py")]:
        if "templates" in path.parts or "connectors" in path.parts:
            continue  # a connector reads its source file's settings, never settings.yaml
        reads = _Reads(path.name)
        reads.visit(ast.parse(path.read_text(encoding="utf-8")))
        found |= reads.found
    return found


def _known(path: tuple[str, ...]) -> bool:
    from cerebro.project import known_keys

    tree: object = known_keys()
    for key in path:
        if not isinstance(tree, dict):
            return False
        tree = tree.get(key, tree.get("*", KeyError))
        if tree is KeyError:
            return False
    return True


def _leaves(tree: dict, at: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    out = set()
    for key, below in tree.items():
        out |= _leaves(below, (*at, key)) if isinstance(below, dict) and below else {(*at, key)}
    return out


def test_every_settings_key_the_code_reads_is_a_known_key() -> None:
    unknown = sorted(p for p in _reads() | _unseen() if not _known(p))
    assert not unknown, f"read but not in known_keys(): {unknown}"


def test_every_known_key_is_read_by_some_code() -> None:
    from cerebro.project import known_keys

    reads = _reads() | _unseen()
    read_or_under = {p[:n] for p in reads for n in range(1, len(p) + 1)}
    unread = sorted(p for p in _leaves(known_keys()) if p not in read_or_under)
    assert not unread, f"in known_keys() but read nowhere (reads by string, and {UNSEEN}): {unread}"


# -- a wrong first run cap, a number that is no number, and the order of refusals ----------------------------------

@pytest.mark.parametrize(("value", "shown"), [("abc", "'abc'"), ("0", "0"), ("-5", "-5"), (".nan", "nan"),
                                              ("1.0e+10", "10000000000.0"), ("[10]", "[10]")])
def test_a_wrong_first_run_cap_is_refused_at_load_with_its_line(tmp_path: Path, value: str, shown: str) -> None:
    assert _refused(tmp_path, f"budget:\n  daily_usd: 20\n  first_run_usd: {value}\n") == (
        f"settings.yaml: line 3: budget.first_run_usd is {shown}: write a number of dollars from 0.000000001 to "
        "1000000000, or leave the key out for $10")


@pytest.mark.parametrize("value", ["2.5", "0.000000001", "1000000000", "null"])
def test_a_first_run_cap_in_range_loads(tmp_path: Path, value: str) -> None:
    (tmp_path / "settings.yaml").write_text(f"budget:\n  first_run_usd: {value}\n")
    load(tmp_path)


@pytest.mark.parametrize(("key", "span"), [("interval", "a number from 1 to 86400"),
                                           ("head_timeout", "a number from 1 to 3600"),
                                           ("body_timeout", "a number from 1 to 3600"),
                                           ("idle_timeout", "a number from 1 to 3600")])
@pytest.mark.parametrize(("value", "shown"), [(".nan", "nan"), (".inf", "inf"), ("-.inf", "-inf")])
def test_a_number_that_is_not_finite_is_refused(tmp_path: Path, key: str, span: str, value: str, shown: str) -> None:
    assert _refused(tmp_path, f"serve:\n  {key}: {value}\n") == f"settings.yaml: line 2: serve.{key} is {shown}: write {span}"


@pytest.mark.parametrize(("text", "says"), [
    ("budget: {dialy_usd: 5}\nbogus: 1\n", 'settings.yaml: line 2: unknown key "bogus"'),
    ("budget: {daily_usd: 0}\ncompile: {concurency: 2}\n",
     ("settings.yaml: line 1: budget.daily_usd is 0, which would stop every model call: write an amount above zero, "
      "or leave the key out for no cap")),
    ("budget: {daily_usd: 0}\nschedule: {setle: 1m}\n", 'settings.yaml: line 2: unknown key "setle"'),
    ("models:\n  extraction: { provider: fireworks, model: glm-5p3-flash }\nbudget: {daily_usd: 0}\n",
     ("settings.yaml: line 3: budget.daily_usd is 0, which would stop every model call: write an amount above zero, "
      "or leave the key out for no cap")),
    ("models:\n  extraction: { provider: fireworks, model: glm-5p3-flash }\nid: [a]\n",
     "settings.yaml: line 3: id is ['a']: write the id cerebro init wrote, a word such as notes-wiki-36fe74"),
    ("port: 0\ncompile: {concurrency: 99}\n",
     "settings.yaml: line 2: compile.concurrency is 99: write a whole number from 1 to 64"),
])
def test_refusals_come_in_the_order_the_checks_reach_them(tmp_path: Path, text: str, says: str) -> None:
    assert _refused(tmp_path, text) == says


def test_a_hidden_character_in_a_key_is_shown_as_an_escape(tmp_path: Path) -> None:
    said = _refused(tmp_path, "da\u202eily_usd: 5\n")
    assert said == 'settings.yaml: line 1: unknown key "da\\u202eily_usd"'


def _init(root: Path) -> None:
    made = CliRunner().invoke(main, ["init", str(root), "--timezone", "UTC"])
    assert made.exit_code == 0, made.output


@pytest.mark.untrusted
def test_a_typo_under_store_gets_its_line_on_the_command_line(tmp_path: Path) -> None:
    root = tmp_path / "w"
    _init(root)
    text = (root / "settings.yaml").read_text()
    (root / "settings.yaml").write_text(text + "store:\n  paht: other.sqlite\n")
    line = len(text.splitlines()) + 2
    for command in (["agents", "list"], ["review", "--json"], ["agents", "new", "things/thing"]):
        result = CliRunner().invoke(main, [*command, "--workspace", str(root)])
        assert result.exit_code == 1, (command, result.output)
        assert result.output.strip() == f'Error: settings.yaml: line {line}: unknown key "paht"', (command, result.output)


@pytest.mark.untrusted
def test_an_untrusted_workspace_with_a_bad_key_is_told_of_trust_first_and_of_the_key_once_trusted(
        tmp_path: Path) -> None:
    from cerebro import trust

    root = tmp_path / "cloned"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\nbudget: {dialy_usd: 5}\n")
    refused = CliRunner().invoke(main, ["agents", "list", "--workspace", str(root)])
    assert refused.exit_code == 1 and "is not a workspace you trust" in refused.output, refused.output
    trusted = CliRunner().invoke(main, ["trust", str(root)])
    assert trusted.exit_code == 1, trusted.output
    assert f"trusted   {root}" in trusted.output, trusted.output
    assert trusted.output.strip().endswith('Error: settings.yaml: line 2: unknown key "dialy_usd"'), trusted.output
    assert trust.trusted(root)
    again = CliRunner().invoke(main, ["agents", "list", "--workspace", str(root)])
    assert again.output.strip() == 'Error: settings.yaml: line 2: unknown key "dialy_usd"', again.output


@pytest.mark.untrusted
def test_commands_that_write_refuse_a_workspace_whose_settings_are_refused_and_write_nothing(tmp_path: Path) -> None:
    from cerebro import trust

    root = tmp_path / "w"
    _init(root)
    with (root / "settings.yaml").open("a") as out:
        out.write("bogus: 1\n")
    line = len((root / "settings.yaml").read_text().splitlines())
    before = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())
    for command in (["agents", "new", "things/thing"], ["sources", "new", "more"], ["prompts", "eject", "curator"],
                    ["prompts", "list"], ["forget", "--yes"], ["forget"]):
        result = CliRunner().invoke(main, [*command, "--workspace", str(root)])
        assert result.exit_code == 1, (command, result.output)
        assert result.output.strip() == f'Error: settings.yaml: line {line}: unknown key "bogus"', (command, result.output)
    assert sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file()) == before
    assert trust.trusted(root)


@pytest.mark.untrusted
def test_trust_on_a_trusted_workspace_names_a_typo_under_store_with_its_line(tmp_path: Path) -> None:
    from cerebro import trust

    root = tmp_path / "w"
    _init(root)
    text = (root / "settings.yaml").read_text()
    (root / "settings.yaml").write_text(text + "store: { paht: x }\n")
    line = len(text.splitlines()) + 1
    records = trust.records()
    result = CliRunner().invoke(main, ["trust", str(root)])
    assert result.exit_code == 1, result.output
    assert result.output.strip() == f'Error: settings.yaml: line {line}: unknown key "paht"', result.output
    assert trust.records() == records


@pytest.mark.untrusted
@pytest.mark.parametrize("block", ["store: { paht: x }", 'store: { class: "nosuchmod_zz:Store" }',
                                   'store: { class: "ownstore_zz:Store" }'])
def test_a_line_under_store_blocks_trust_on_a_folder_not_yet_trusted_and_writes_no_record(
        tmp_path: Path, block: str) -> None:
    from cerebro import trust

    root = (tmp_path / "cloned").resolve()
    root.mkdir()
    (root / "settings.yaml").write_text(f"timezone: UTC\n{block}\n")
    marker = tmp_path / "imported"
    (root / "ownstore_zz.py").write_text(f"open({str(marker)!r}, 'w').close()\nclass Store: pass\n")
    result = CliRunner().invoke(main, ["trust", str(root)])
    assert result.exit_code == 1, result.output
    assert "trusted   " not in result.output and "Error: settings.yaml store" in result.output, result.output
    assert not any(entry["path"] == str(root) for entry in trust.records())
    assert not marker.exists()


@pytest.mark.untrusted
def test_any_other_refused_line_does_not_block_trust_on_a_folder_not_yet_trusted(tmp_path: Path) -> None:
    from cerebro import trust

    root = (tmp_path / "cloned").resolve()
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\nbogus: 1\ncompile: { concurrency: 0 }\n")
    result = CliRunner().invoke(main, ["trust", str(root)])
    assert result.exit_code == 1, result.output
    assert f"trusted   {root}" in result.output, result.output
    assert result.output.strip().endswith('Error: settings.yaml: line 2: unknown key "bogus"'), result.output
    assert any(entry["path"] == str(root) for entry in trust.records())


def _order(text: str, **jobs: str) -> str:
    entries = {"extraction": "{ provider: fireworks, model: glm-5p3-flash, effort: low }",
               "decision": "{ provider: fireworks, model: rune }",
               "compilation": "{ provider: fireworks, model: glm-5p3, effort: high }"} | jobs
    return text.replace("MODELS\n", "models:\n" + "".join(f"  {job}: {entry}\n" for job, entry in entries.items()))


ID = "id: ws_0123456789abcdef\n"


# Each file holds two or three faults; the first fault named is the first the checks reach, run on `status`.
@pytest.mark.parametrize(("text", "line", "says"), [
    (_order(ID + "port: 4747\nMODELS\nbudget:\n  dialy_usd: 5\ncompile: { concurrency: 0 }\nbogus: 1\n"), 10,
     'unknown key "bogus"'),
    (_order(ID + "port: 4747\nMODELS\nbudget: { daily_usd: 0 }\nschedule:\n  setle: 2m\n"), 9, 'unknown key "setle"'),
    (_order(ID + "port: 4747\nMODELS\ntimezone: Nowhere/Zz\nbudget: { daily_usd: -1 }\ncompile: { concurency: 2 }\n"),
     7, "timezone is"),
    (_order(ID + "port: 4747\nMODELS\nbudget: { daily_usd: 0 }\ncompile: { concurrency: 0 }\n", extraction="5"), 7,
     "budget.daily_usd is 0"),
    (_order(ID + "port: 4747\nMODELS\nbudget: { daily_usd: 0 }\n",
            extraction="{ provider: fireworks, model: glm-5p3-flash, efort: low }"), 4, 'unknown key "efort"'),
    (_order("id: [1, 2]\nMODELS\ntimezone: UTC\ncompile: { concurency: 2 }\nport: abc\n"), 7,
     'unknown key "concurency"'),
    (_order("port: 70000\n" + ID + "MODELS\nbudget: { daily_usd: 0 }\nschedule:\n  settle: soon\n"), 7,
     "budget.daily_usd is 0"),
    (_order(ID + "MODELS\nschedule:\n  sources: abc\nbudget:\n  first_run_usd: abc\n",
            decision="{ provider: nosuch, model: rune }"), 7, "schedule.sources:"),
    (_order(ID + "port: 4747\nMODELS\ncompile: { concurrency: 0 }\nbudget: { bogus: 1 }\n"), 7,
     "compile.concurrency is 0"),
    (_order(ID + "port: 4747\nMODELS\ntimezone: UTC\nbogus: 1\n", extraction="[a]"), 8, 'unknown key "bogus"'),
    (_order(ID + "port: 4747\nMODELS\nbudget: { daily_usd: 0 }\nserve: { port: 4748 }\n"), 7,
     "budget.daily_usd is 0"),
    (_order(ID + "port: 4747\nMODELS\nbudget: { daily_usd: 0 }\ntimezone: [UTC]\nschedule:\n  bogus: 1\n"), 10,
     'unknown key "bogus"'),
    (_order(ID + "MODELS\nport: abc\n", extraction="5"), 3, "models.extraction is not a map"),
    (_order("port: abc\nid: [1, 2]\nMODELS\n"), 1, "port is 'abc'"),
    (_order("MODELS\ntimezone: UTC\nid: [1, 2]\n", extraction="[a]"), 2, "models.extraction is not a map"),
    (_order("port: abc\n" + ID + "MODELS\ntimezone: [UTC]\n"), 7, "timezone is"),
    (_order(ID + "MODELS\nport: 99999999999999999999\n", extraction="{ provider: [x], model: glm-5p3-flash }"), 3,
     "models.extraction needs a provider and a model"),
    (_order(ID + "port: 4747\nMODELS\nbudget:\n  daily_usd: 0\n  bogus: 1\n"), 9, 'unknown key "bogus"'),
    (_order(ID + "port: 4747\nMODELS\nschedule:\n  settle: soon\n  bogus: 1\nzzz: 1\n"), 9, 'unknown key "bogus"'),
    (_order("id: true\nport: abc\nMODELS\n"), 2, "port is 'abc'"),
    (_order("port: true\nid: [1]\nMODELS\n"), 1, "port is True"),
    (_order("port: 1.5\nid: [1]\nMODELS\n"), 2, "id is [1]"),
    (_order("MODELS\n", extraction="{ provider: nosuch, model: glm-5p3-flash }", decision="[a]"), 3,
     "models.decision is not a map"),
])
def test_the_first_refusal_is_the_first_fault_the_checks_reach(tmp_path: Path, text: str, line: int, says: str) -> None:
    assert _refused(tmp_path, text).startswith(f"settings.yaml: line {line}: {says}")


# A value of the wrong kind under schedule waits with those under id, port and the models, in the file's order; a
# duration that does not read stops the load where it stands.
@pytest.mark.parametrize(("text", "line", "says"), [
    (_order(ID + "port: abc\nMODELS\nschedule:\n  sources: [a]\n"), 2, "port is 'abc'"),
    (_order("id: [1]\nMODELS\nschedule: 5\n"), 1, "id is [1]"),
    (_order(ID + "port: abc\nMODELS\nschedule: [a]\n"), 2, "port is 'abc'"),
    (_order("id: { a: 1 }\nMODELS\nschedule:\n  settle: { x: 1 }\n"), 1, "id is {'a': 1}"),
    (_order(ID + "MODELS\nschedule:\n  sources: [a]\n", extraction="{ provider: fireworks, model: m, effort: [a] }"),
     3, "models.extraction"),
    (_order(ID + "port: abc\nMODELS\nschedule: abc\n"), 2, "port is 'abc'"),
    (_order(ID + "schedule: 5\nport: abc\nMODELS\n"), 2, "schedule takes"),
    (_order(ID + "MODELS\nschedule:\n  sources: [a]\nport: abc\n"), 7, "schedule.sources:"),
    (_order(ID + "MODELS\nschedule: [a]\nbudget: { daily_usd: 0 }\n"), 7, "budget.daily_usd is 0"),
    (_order(ID + "MODELS\nschedule: abc\ntimezone: Nowhere/Zz\n"), 7, "timezone is"),
    (_order(ID + "MODELS\nschedule:\n  sources: [a]\n  settle: xyz\n"), 8, "schedule.settle:"),
    (_order(ID + "port: abc\nMODELS\nschedule:\n  sources: xyz\n"), 8, "schedule.sources:"),
    (_order(ID + "MODELS\nschedule:\n  articles: [a]\n  sources: { a: 1 }\nport: abc\n"), 7, "schedule.articles:"),
])
def test_a_schedule_value_of_the_wrong_kind_is_named_in_the_order_the_checks_reach_it(
        tmp_path: Path, text: str, line: int, says: str) -> None:
    assert _refused(tmp_path, text).startswith(f"settings.yaml: line {line}: {says}")


@pytest.mark.parametrize("value", ["5", "abc", "[a, b]"])
def test_a_store_of_the_wrong_kind_names_its_line(tmp_path: Path, value: str) -> None:
    said = _refused(tmp_path, _order(ID + f"MODELS\nstore: {value}\n"))
    assert said == "settings.yaml: line 6: store is not a map: write store: then path: or class: under it"


def test_a_store_of_the_wrong_kind_is_named_in_the_files_order(tmp_path: Path) -> None:
    said = _refused(tmp_path, _order("store: 5\n" + ID + "MODELS\nbudget: { daily_usd: abc }\n"))
    assert said.startswith("settings.yaml: line 1: store is not a map")


@pytest.mark.parametrize(("text", "line"), [
    (_order('id: "bad id!"\nMODELS\nbudget: { daily_usd: 5 }\nport: 70000\n'), 1),
    (_order('port: 70000\nid: "bad id!"\nMODELS\n', extraction="{ provider: nosuch, model: m }"), 2),
    (_order('MODELS\nid: "bad id!"\n'), 5),
])
def test_an_id_of_the_wrong_form_is_named_first_of_the_last_checks_with_its_line(
        tmp_path: Path, text: str, line: int) -> None:
    assert _refused(tmp_path, text) == (f"settings.yaml: line {line}: id is 'bad id!': write 1 to 64 letters, digits, "
                                        "_ and -, as the id cerebro init wrote, such as notes-wiki-36fe74")


@pytest.mark.parametrize("id", ["w-1", "123", "0", "''", "null"])
def test_an_id_the_secrets_store_takes_loads(tmp_path: Path, id: str) -> None:
    (tmp_path / "settings.yaml").write_text(f"id: {id}\n")
    load(tmp_path)


@pytest.mark.parametrize("text", ["compile: { concurrency: NUM }\n", "serve: { head_timeout: NUM }\n",
                                  "budget: { first_run_usd: NUM }\n", "budget: { daily_usd: NUM }\n",
                                  "budget: { rerank_daily_usd: -NUM }\n"])
@pytest.mark.parametrize("number", ["9" * 400, "1" + "0" * 399, "9" * 309])
def test_a_whole_number_too_large_for_a_float_is_refused_with_its_line(tmp_path: Path, text: str, number: str) -> None:
    said = _refused(tmp_path, text.replace("NUM", number))
    assert said.startswith("settings.yaml: line 1: ") and number[:20] in said


@pytest.mark.untrusted
@pytest.mark.parametrize("value", ["abc", "[a, b]", "5"])
def test_trust_on_a_trusted_workspace_names_a_store_of_the_wrong_kind_with_its_line(tmp_path: Path, value: str) -> None:
    from cerebro import trust

    root = tmp_path / "w"
    _init(root)
    text = (root / "settings.yaml").read_text()
    (root / "settings.yaml").write_text(text + f"store: {value}\n")
    records = trust.records()
    result = CliRunner().invoke(main, ["trust", str(root)])
    assert result.exit_code == 1, result.output
    assert result.output.strip() == (f"Error: settings.yaml: line {len(text.splitlines()) + 1}: store is not a map: "
                                     "write store: then path: or class: under it"), result.output
    assert trust.records() == records


@pytest.mark.untrusted
def test_trust_refuses_an_id_of_the_wrong_form_as_every_other_command_does(tmp_path: Path) -> None:
    import shutil

    from cerebro import trust

    root = tmp_path / "w"
    _init(root)
    text = re.sub(r"(?m)^id: .*$", 'id: "bad id!"', (root / "settings.yaml").read_text())
    (root / "settings.yaml").write_text(text)
    said = "Error: settings.yaml: line 2: id is 'bad id!'"
    records = trust.records()
    result = CliRunner().invoke(main, ["trust", str(root)])
    assert result.exit_code == 1 and result.output.strip().startswith(said), result.output
    assert trust.records() == records
    copy = (tmp_path / "copy").resolve()
    shutil.copytree(root, copy)
    result = CliRunner().invoke(main, ["trust", str(copy)])
    assert result.exit_code == 1, result.output
    assert f"trusted   {copy}" in result.output and result.output.strip().splitlines()[-1].startswith(said), result.output


@pytest.mark.untrusted
def test_trust_trusts_a_folder_a_stale_record_names_then_names_the_refused_line(tmp_path: Path) -> None:
    import json

    from cerebro import trust

    root = (tmp_path / "w").resolve()
    _init(root)
    stale = [entry | {"id": "0" * 32} if entry["path"] == str(root) else entry for entry in trust.records()]
    trust.trusted_path().write_text(json.dumps(stale))
    assert not trust.trusted(root)
    with (root / "settings.yaml").open("a") as out:
        out.write("bogus: 1\n")
    line = len((root / "settings.yaml").read_text().splitlines())
    result = CliRunner().invoke(main, ["trust", str(root)])
    assert result.exit_code == 1, result.output
    assert f"trusted   {root}" in result.output, result.output
    assert result.output.strip().endswith(f'Error: settings.yaml: line {line}: unknown key "bogus"'), result.output
    assert trust.trusted(root)


@pytest.mark.untrusted
@pytest.mark.parametrize(("key", "shown"), [("1", "1"), ("on", "on"), ("1.5", "1.5"), ("null", "null"), ("~", "null"),
                                            ("2024-01-01", "2024-01-01")])
@pytest.mark.parametrize("store", ["{ KEY: x }", "{ sub: { KEY: x } }"])
def test_a_store_key_that_is_not_a_word_is_refused_with_its_line_on_trust_and_every_command(
        tmp_path: Path, key: str, shown: str, store: str) -> None:
    from cerebro import trust

    root = tmp_path / "w"
    _init(root)
    text = (root / "settings.yaml").read_text()
    (root / "settings.yaml").write_text(text + f"store: {store.replace('KEY', key)}\n")
    named = "sub" if "sub:" in store else shown
    said = f'Error: settings.yaml: line {len(text.splitlines()) + 1}: unknown key "{named}"'
    records = trust.records()
    for command in (["trust", str(root)], ["agents", "list", "--workspace", str(root)],
                    ["secrets", "list", "--workspace", str(root)]):
        result = CliRunner().invoke(main, command)
        assert result.exit_code == 1 and isinstance(result.exception, SystemExit), (command, result.output)
        assert result.output.strip() == said, (command, result.output)
    result = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.output
    assert result.output.strip().startswith("Error: settings.yaml") and "\n" not in result.output.strip(), result.output
    if "sub:" not in store:  # status opens the store first, and names the key as written, never as Python spells it
        assert f'unknown key "{shown}"' in result.output, result.output
    assert trust.records() == records


@pytest.mark.parametrize("key", ["1", "on", "null"])
def test_a_key_that_is_not_a_word_under_a_store_of_another_class_is_refused_with_its_line(
        tmp_path: Path, key: str) -> None:
    said = _refused(tmp_path, f"id: w-1\nstore: {{ class: cerebro.fakes:MemoryStore, {key}: x }}\n")
    assert said.startswith("settings.yaml: line 2: unknown key"), said


def test_the_store_refuses_a_key_that_is_not_a_word_in_one_line(tmp_path: Path) -> None:
    from cerebro.store import StoreSettingsError, open_store

    for key in (1, True, None, 1.5):
        with pytest.raises(StoreSettingsError, match="unknown key"):
            open_store(tmp_path, {"store": {key: "x"}})
