"""The keys of a source file: each kind declares the keys it reads beside the code that reads them, the loader
refuses every other key with the line it stands on, and the declaration, the code and the docs stay in step."""

from __future__ import annotations

import ast
import functools
import re
from dataclasses import fields
from pathlib import Path

import pytest

import cerebro
from cerebro.connectors import CONNECTORS, KINDS
from cerebro.source import ENGINE_KEYS, NOT_A_WORD, Select, SourceError, source_of

SRC = Path(cerebro.__file__).parent
PACKAGE = SRC / "connectors"
DOCS = SRC.parent.parent / "docs"
# The fields of a source that name it or are not keys of its file.
NOT_KEYS = {"name", "kind", "path", "settings", "behind"}
SELECT_FIELDS = {f.name for f in fields(Select)}


# -- what the code reads, found by reading the code


def _refusal(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether a read only serves to refuse: it sits in a raise, or in the test of an `if` whose body raises."""
    child, here = node, parents.get(node)
    while here is not None:
        if isinstance(here, ast.Raise):
            return True
        if (isinstance(here, ast.If) and child is here.test and len(here.body) == 1
                and isinstance(here.body[0], ast.Raise)):
            return True
        child, here = here, parents.get(here)
    return False


def _is_source(node: ast.AST, names: set[str]) -> bool:
    """`source`, `self.source` or another name that holds the source file."""
    if isinstance(node, ast.Name):
        return node.id in names
    return isinstance(node, ast.Attribute) and node.attr == "source" and isinstance(node.value, ast.Name) \
        and node.value.id == "self"


def _reads(tree: ast.AST, names: set[str]) -> tuple[set[str], set[str], list[tuple[str, int]]]:
    """The keys `tree` reads of a source held in `names`, the keys it reads of a select entry, and each call it
    hands the source to, by the callee's name and the argument's place."""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    keys: set[str] = set()
    entry: set[str] = set()
    calls: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_source(node.value, names) and not _refusal(node, parents):
            if node.attr == "behind":
                keys.add("window")
            elif node.attr not in NOT_KEYS:
                keys.add(node.attr)
        if isinstance(node, ast.Attribute) and node.attr in SELECT_FIELDS and not _refusal(node, parents) and (
                (isinstance(node.value, ast.Name) and node.value.id == "entry")
                or (isinstance(node.value, ast.Attribute) and node.value.attr == "entry")):
            entry.add(node.attr)
        settings = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            settings, key = node.func.value, node.args[0] if node.args else None
        elif isinstance(node, ast.Subscript):
            settings, key = node.value, node.slice
        if (isinstance(settings, ast.Attribute) and settings.attr == "settings" and _is_source(settings.value, names)
                and isinstance(key, ast.Constant) and isinstance(key.value, str) and not _refusal(node, parents)):
            keys.add(key.value)
        if isinstance(node, ast.Call):
            callee = node.func.id if isinstance(node.func, ast.Name) else \
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            calls += [(callee, n) for n, arg in enumerate(node.args) if _is_source(arg, names)]
    return keys, entry, calls


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text())
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


ENGINE = {**_functions(SRC / "secrets.py"), **_functions(SRC / "app.py"),
          **_functions(PACKAGE / "mcp_source_client.py")}


def _engine_reads(calls: list[tuple[str, int]]) -> set[str]:
    """What the app reads of a source on its way to the connector, from `calls`, each callee a kind's module hands
    the source to with its place, through each function of the app, the secrets store and the MCP client, by name."""
    keys: set[str] = set()
    todo, done = list(calls), set()
    while todo:
        name, place = todo.pop()
        function = ENGINE.get(name)
        if function is None or (name, place) in done:
            continue
        done.add((name, place))
        params = [a.arg for a in function.args.args if a.arg != "self"]
        if place >= len(params):
            continue
        found, _, calls = _reads(function, {params[place]})
        keys |= found
        todo += calls
    return keys


@functools.cache
def _parsed(path: Path) -> ast.Module:
    """A module of the package, parsed once: each kind's check reads every module."""
    return ast.parse(path.read_text())


def _modules(kind: str) -> list[Path]:
    """A kind's module and its helpers: each module named for it with a suffix, as `gmail_imap`, and each that
    imports from it, as `postgres` from `database`."""
    main = KINDS[kind].__name__.rsplit(".", 1)[1]
    out = [PACKAGE / f"{main}.py"]
    for path in sorted(PACKAGE.glob("*.py")):
        imports = {node.module for node in ast.walk(_parsed(path))
                   if isinstance(node, ast.ImportFrom) and node.level == 1}
        if path.stem.startswith(main + "_") or (main in imports and path.stem != "__init__"):
            out.append(path)
    return out


def read_by(kind: str) -> tuple[set[str], set[str]]:
    """The keys of a source file the code reads for `kind`, and of a select entry: what its modules read, what the
    app reads building it, and what the runner reads of every source that lists, `freshness:`, and of every source
    that lists by changes, `full_every:`."""
    keys: set[str] = set()
    entry: set[str] = set()
    for path in _modules(kind):
        found, selected, calls = _reads(_parsed(path), {"source"})
        keys |= found | _engine_reads(calls)
        entry |= selected
    built = CONNECTORS[kind]
    keys |= set() if built.lists_nothing else {"freshness"}
    keys |= {"full_every"} if built.lists_changes else set()
    return keys - set(ENGINE_KEYS), entry


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_each_kind_declares_the_keys_its_code_reads_and_no_other(kind: str) -> None:
    read, entry = read_by(kind)
    module = KINDS[kind]
    assert sorted(set(module.SOURCE_KEYS)) == sorted(module.SOURCE_KEYS), f"{kind} declares a key twice"
    assert set(module.SOURCE_KEYS) - read == set(), f"{kind} declares keys its code never reads"
    assert read - set(module.SOURCE_KEYS) == set(), f"{kind}'s code reads keys it does not declare"
    declared = set(getattr(module, "SELECT_KEYS", ()))
    assert ("select" in module.SOURCE_KEYS) == bool(declared), f"{kind} reads select: and declares no keys of an entry"
    if "glob" in declared:
        assert declared == entry | {"glob"} or declared == entry, f"{kind}'s select entry keys and code differ"
    assert not set(getattr(module, "REFUSED_KEYS", {})) & set(module.SOURCE_KEYS), f"{kind} refuses a key it reads"
    for path in getattr(module, "NESTED_KEYS", {}):
        assert path[0] in module.SOURCE_KEYS, f"{kind} declares keys under {path[0]}, which it does not read"


def test_a_module_no_kind_owns_reads_nothing_of_a_source() -> None:
    owned = {path for kind in KINDS for path in _modules(kind)}
    for path in sorted(PACKAGE.glob("*.py")):
        if path not in owned and path.stem != "__init__":
            assert _reads(_parsed(path), {"source"})[0] == set(), path.name


# -- what the loader refuses


@pytest.mark.parametrize(("text", "line"), [
    ("kind: files\nselect:\n  - glob: a/*.md\nscope: private\n",
     ("sources/s.yaml line 4: scope is not a key a source of kind files reads: delete it, or fix its spelling (kind, "
      "extract, window, select, notes, freshness)")),
    ("kind: slack\n# the team\nurl: https://x.example/mcp\n",
     ("sources/s.yaml line 3: url: a slack source reads Slack's Web API and takes no url or command, so the token "
      "is not sent anywhere else: delete url")),
    ("kind: slack\ncommand: run\n", "sources/s.yaml line 2: command: a slack source reads Slack's Web API"),
    ("kind: slack\ncommand: run\n", "so the token is not sent anywhere else: delete command"),
    ("kind: gmail\ncommand: run\n", ("line 2: command: a gmail source reads imap.gmail.com and takes no url or "
                                       "command, so the login is sent nowhere else: delete command")),
    ("kind: gmail\nurl: https://x.example\n",
     "sources/s.yaml line 2: url: a gmail source reads imap.gmail.com and takes no url or command"),
    ("kind: files\nowner: me\n",
     "sources/s.yaml line 2: owner belongs to a source of kind calendar, github, gmail, granola, linear or slack"),
    ("kind: calendar\nrepos: [a/b]\n", "sources/s.yaml line 2: repos belongs to a source of kind github: delete it"),
    ("kind: gong\nfull_every: 6h\n",
     "sources/s.yaml line 2: full_every belongs to a source of kind github, gmail, linear or salesforce: delete it"),
    ("kind: github\nselect:\n  - glob: a/*.md\n",
     "sources/s.yaml line 2: select belongs to a source of kind files, granola or mcp: delete it"),
    ("kind: files\ninto: things\nselect:\n  - glob: a/*.md\n",
     ("line 2: into: a files record lands by its select entry's into, never by a top-level one: "
      "move it onto each entry, as select: [{ glob: \"notes/*.md\", into: things }]")),
    ("kind: github\ninto: issues\n", "sources/s.yaml line 2: into: a github record names no collection"),
    ("kind: calendar\nwindow: { behind: 30d }\nselect: { folders: [x] }\n",
     "line 3: select: a calendar reads its whole feed"),
    ("kind: database\nurl: sqlite:a.sqlite\nwindow: { behind: 30d }\n", "sources/s.yaml line 3: window belongs to"),
    ("kind: files\nselect:\n  - glob: a/*.md\n    extract: facts\n    globb: b\n",
     "sources/s.yaml line 5: select[0].globb is not a key a select entry of a source of kind files reads"),
    ("kind: mcp\ncommand: run\nbinding: contract\nselect:\n  - glob: \"a-*\"\n    rows: csv\n",
     ("sources/s.yaml line 6: select[0].rows is not a key a select entry of a source of kind mcp reads: delete it, "
      "or fix its spelling (glob, extract, into)")),
    ("kind: granola\nselect:\n  - glob: a\n",
     "sources/s.yaml line 2: select: a granola source reads folders alone, never a glob"),
    ("kind: files\nselect: { folders: [x] }\n", "sources/s.yaml line 2: select: a source of kind files takes select:"),
    ("kind: files\nwindow: { behind: 30d, after: 2d }\n", "sources/s.yaml line 2: window.after is not a key"),
    ("kind: github\nrepos: [things-org/parts]\nteams: [ENG]\n", "line 3: teams belongs to a source of kind linear"),
    ("kind: gmail\ninto: people\n", "line 2: into: a message is a record and never an article"),
    ("kind: gong\nchannels: [x]\n", "line 2: channels belongs to a source of kind slack"),
    ("kind: linear\nteams: [ENG]\ninto: things\n", "line 3: into: a linear record names no collection"),
    ("kind: linear\ncommand: run\n", "line 2: command: a linear source reads linear's own server: delete command"),
    ("kind: slack\ninto: things\n", "line 2: into: a slack message is a record and never an article"),
    ("kind: slack\nrepos: [a/b]\n", "line 2: repos belongs to a source of kind github"),
    ("kind: salesforce\nchannels: [x]\n", "line 2: channels belongs to a source of kind slack"),
    ("kind: salesforce\ninto: things\n", "line 2: into: a salesforce source lands each object where objects: says"),
    ("kind: salesforce\nauth: oauth\n", "line 2: auth belongs to a source of kind calendar"),
    ("kind: calendar\nchannels: [x]\n", "line 2: channels belongs to a source of kind slack"),
    ("kind: files\nyes: 1\n", "sources/s.yaml line 2: yes is not a key a source file reads: a key is a word"),
    ("kind: files\n~: 1\n", "sources/s.yaml line 2: a key that is not a word is not a key a source file reads"),
    ("kind: files\n2: x\n", "sources/s.yaml line 2: a key that is not a word is not a key a source file reads"),
])
def test_a_key_a_kind_does_not_read_is_refused_with_the_file_the_line_and_the_key(text: str, line: str) -> None:
    with pytest.raises(SourceError) as refused:
        source_of("s", text)
    assert line in str(refused.value) and "\n" not in str(refused.value)


@pytest.mark.parametrize(("kind", "text"), [
    ("github", "owner: pat@colleague.example\nrepos: [octo-org/api]\nbots: include\nfull_every: 6h\n"),
    ("linear", "owner: me\nteams: [ENG]\nurl: https://mcp.linear.app/mcp\nauth: oauth\nfull_every: always\n"),
    ("slack", "owner: me\nchannels: [general]\nbots: exclude\nauth: { keychain: team }\n"),
    ("granola", "owner: me\nselect: { folders: [Projects] }\ninto: meetings\n"),
    ("files", "select:\n  - glob: a/*.csv\n    rows: csv\n    id: \"{row.id}\"\nnotes: { each: Project, with: Crew }\n"),
    ("mcp", ("command: run\nenv: [RUN_TOKEN]\nbinding: contract\ncapabilities: [fetch_by_id]\nread_only: [x]\n"
             "select:\n  - glob: \"a-*\"\n    into: things\n")),
    ("database", "url: sqlite:a.sqlite\n"),
])
def test_a_key_a_kind_reads_still_loads(kind: str, text: str) -> None:
    assert source_of("s", f"kind: {kind}\n{text}").kind == kind


# -- the docs


# -- every source file Cerebro writes or shows still loads


def _yaml_blocks(text: str) -> list[str]:
    return [b for b in re.findall(r"```ya?ml\n(.*?)```", text, re.DOTALL) if re.search(r"^kind: ", b, re.MULTILINE)]


def _source_strings(path: Path) -> list[str]:
    """Each string in a script that is a whole source file: one whose first line that is not a comment is a kind."""
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lines = [line for line in node.value.splitlines() if line.strip() and not line.startswith("#")]
            if lines and re.fullmatch(r"kind: [a-z]+", lines[0]):
                out.append(node.value)
    return out


def test_every_source_file_a_template_the_scaffold_connect_a_doc_or_a_check_writes_loads(tmp_path: Path) -> None:
    from cerebro import scaffold
    from cerebro.connect import add_source, new_source, write_source

    root = SRC.parent.parent
    texts = {str(p): p.read_text() for p in sorted((SRC / "base").rglob("sources/*.yaml"))}
    texts |= {str(p): p.read_text() for p in sorted((SRC / "templates").rglob("sources/*.yaml"))}
    for doc in [root / "README.md", *sorted(DOCS.rglob("*.md"))]:
        texts |= {f"{doc} block {n}": block for n, block in enumerate(_yaml_blocks(doc.read_text()))}
    for script in sorted([*(root / "checks").rglob("*.py"), *(root / "apps").rglob("*.py")]):
        texts |= {f"{script} string {n}": text for n, text in enumerate(_source_strings(script))}
    assert any("sources.md" in name for name in texts) and any("checks" in name for name in texts)
    assert any("gtm.md" in name and "kind: salesforce" in text and "objects:" in text for name, text in texts.items())
    for name, text in texts.items():
        source_of("s", text, name)
    workspace = tmp_path / "w"
    scaffold.init(workspace, template="gtm", environ={})
    scaffold.new_source(workspace, "notes")
    scaffold.new_source(workspace, "more", ["a/*.md", "b/**/*.txt"], "records")
    for kind in KINDS:
        new_source(kind)
        if kind not in ("files", "database", "salesforce"):
            add_source(workspace, kind)
    for kind, key, value in (("github", "repos", "[octo-org/api]"), ("linear", "teams", "[ENG]"),
                             ("slack", "channels", "[general]")):
        path = workspace / "sources" / f"{kind}.yaml"
        write_source(path, path.read_text(), f"kind: {kind}\n{key}: {value}\n")
    for path in sorted((workspace / "sources").glob("*.yaml")):
        source_of(path.stem, path.read_text(), str(path))


# -- several faults at once, and a key that is not a word


def test_every_key_fault_of_a_file_is_named_in_one_line() -> None:
    text = ("kind: mcp\nserver: warehouse\nurl: https://wh.example/mcp\nbinding: contract\n"
            "retention: { mode: transient }\nselect:\n  - glob: \"a-*\"\n    colour: red\n")
    with pytest.raises(SourceError) as refused:
        source_of("s", text)
    said = str(refused.value)
    assert said.startswith("sources/s.yaml line 2: server is not a key") and "\n" not in said
    assert "; line 5: retention is not a key" in said and "; line 8: select[0].colour is not a key" in said


@pytest.mark.parametrize("text", [
    "kind: github\nrepos: [a/b]\nghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE1234: 1\n",
    "kind: files\n\"postgresql://reader:hunter2@db.example/sales\": 1\n",
    "kind: files\nselect:\n  - glob: a\n    \"postgresql://reader:hunter2@db.example/sales\": 1\n",
    "kind: salesforce\nobjects:\n  Account:\n    into: a\n    \"https://u:hunter2@x.example\": 1\n",
])
def test_a_key_that_is_not_a_short_word_is_named_by_its_line_and_never_printed(text: str) -> None:
    with pytest.raises(SourceError) as refused:
        source_of("s", text)
    said = str(refused.value)
    assert "hunter2" not in said and "FAKEFAKE" not in said and "postgresql" not in said, said
    assert re.search(r"sources/s\.yaml line \d: .*a key that is not a word", said), said


@pytest.mark.parametrize("text", [
    "kind: files\nselect:\n  - glob: a\n    identity: { ghpXmadeupTOKENvalue0123456789: [a] }\n",
    "kind: files\nselect:\n  - glob: a\n    fields: { ghpXmadeupTOKENvalue0123456789: \"{bogus}\" }\n",
    "kind: files\ndeadbeefcafe0123456789abcdef0123: 1\n",
])
def test_an_identity_or_field_name_or_a_long_key_that_may_be_a_token_is_never_printed(text: str) -> None:
    with pytest.raises(SourceError) as refused:
        source_of("s", text)
    assert "madeupTOKEN" not in str(refused.value) and "deadbeef" not in str(refused.value)
    assert NOT_A_WORD in str(refused.value)


def test_a_window_key_fault_is_named_in_the_same_line_as_the_others() -> None:
    with pytest.raises(SourceError) as refused:
        source_of("s", "kind: files\nbogus: 1\nwindow: { nope: 1 }\nselect:\n  - glob: a\n    zzz: 1\n")
    said = str(refused.value)
    assert "line 2: bogus " in said and "; line 3: window.nope " in said and "; line 6: select[0].zzz " in said
