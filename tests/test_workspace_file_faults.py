"""Whatever `workspace.md` or `profile.md` holds, no command or lookup ends in a traceback: a window that needs no
fiscal start is answered, and a fiscal window is answered or refused in one line that names the file and the fault.
The files come from a generator: bytes, encodings, YAML tags, cuts and sizes, seeded so a failure repeats. A generated
nesting stops at 400 deep, where the parser still reads it and finds the fault: the YAML scanner's time grows with the
square of the depth, and one fixed case nests deep enough that the parser gives up."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro import Caller, Cerebro, scaffold
from cerebro.cli import main
from cerebro.errors import CerebroError
from cerebro.fakes import FakeExtraction, MemoryStore, ScriptedRun
from cerebro.project import WorkspaceError, fiscal_now, read_fiscal, read_ours

GOOD = "---\nname: things\nidentity:\n  domains: [quillmoor.example]\n---\n\nTeam in Zürich.\nThe fiscal year starts July 1.\n"
PROFILE = "---\nidentity:\n  name: Sam Rivera\n  email: [sam@quillmoor.example]\n---\n\nBrief me.\n"
TAGS = ["!!bool", "!!int", "!!float", "!!null", "!!timestamp", "!!binary", "!!set", "!!omap", "!!pairs", "!!map",
        "!!seq", "!!str", "!!merge", "!!value", "!!python/object/apply:os.system", "!!python/name:os.system",
        "!<tag:yaml.org,2002:bool>", "!<tag:example.invalid,2026:thing>", "!thing", "!", "!!yaml"]
VALUES = ["x", "", "1", "yes", "2026-02-30", "[1, 2]", "{a: 1}", "0o9", "1e400", "~", "-", "'quoted'", "\"\\x\"",
          "AAAA", "=", "<<", ".nan", "[x"]
ENCODINGS = ["latin-1", "cp1252", "utf-16", "utf-16-le", "utf-32", "utf-8-sig", "cp1250", "koi8-r", "mac-roman"]
ASK = {"from": "facts", "window": "this_fiscal_year"}
PLAIN = {"from": "facts", "window": "last_30_days"}
YOU = Caller("pat", "person")


def broken(seed: int, count: int) -> Iterator[tuple[str, bytes]]:
    """`count` files that are faulty in one or more ways, each with a label that says how."""
    rng = random.Random(seed)
    for n in range(count):
        text, how = GOOD, []
        for _ in range(rng.randint(1, 3)):
            kind = rng.choice(["tag", "tag", "encoding", "bytes", "cut", "front", "nest", "alias", "size", "bom"])
            how.append(kind)
            if kind == "tag":
                line = f"{rng.choice(['flag', 'size', 'name', 'when'])}: {rng.choice(TAGS)} {rng.choice(VALUES)}"
                text = text.replace("identity:", f"{line}\nidentity:", 1) if "identity:" in text else line + "\n" + text
            elif kind == "front":
                text = rng.choice([text.replace("\n---\n\n", "\n\n", 1), "---\n- things\n" + text[4:],
                                   "---\njust text\n---\n" + text, text.replace("name: things", "name: [things"),
                                   "---\n" + text, text.replace("---\n", "---\r\n"), "---\n---\n" + text])
            elif kind == "nest":
                depth = min(rng.randint(50, 3000), 400)  # the seeded cases stay the same
                text = text.replace("identity:", "deep: " + "[" * depth + "\nidentity:", 1)
            elif kind == "alias":
                bomb = "a: &a [x, x, x, x, x, x, x, x, x]\n" + "".join(
                    f"{chr(98 + k)}: &{chr(98 + k)} [{', '.join([f'*{chr(97 + k)}'] * 9)}]\n" for k in range(8))
                text = text.replace("identity:", bomb + "identity:", 1)
            elif kind == "size":
                text = text + ("Lorem ipsum. " * rng.randint(1, 400_000)) + "\n"
        data = text.encode()
        for kind in how:
            if kind == "encoding":
                data = text.replace("Zürich", "Zürich’s").encode(rng.choice(ENCODINGS), errors="replace")
            elif kind == "bytes":
                at = rng.randrange(len(data) + 1)
                data = data[:at] + bytes(rng.randrange(256) for _ in range(rng.randint(1, 8))) + data[at:]
            elif kind == "cut":
                data = data[:rng.randrange(len(data) + 1)]
            elif kind == "bom":
                data = rng.choice([b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\x00"]) + data
        yield f"{n}: {'+'.join(how)}", data


CASES = list(broken(20261119, 26))  # no case past the 26th reaches a line or branch of cerebro these miss
FIXED = [  # fixed cases, beside the generated ones
    ("bool tag", GOOD.replace("identity:", "flag: !!bool x\nidentity:").encode()),
    ("int tag", GOOD.replace("identity:", "size: !!int x\nidentity:").encode()),
    ("latin-1 save", GOOD.replace("Team in", "Quillmoör: team in").encode("latin-1")),
    ("cp1252 apostrophe", GOOD.replace("Team in", "We’re in").encode("cp1252")),
    ("utf-16", GOOD.encode("utf-16")),
    ("cut inside ü", GOOD.encode()[:GOOD.encode().index("ü".encode()) + 1]),
    ("unclosed", b"---\nname: things\n\nThe fiscal year starts July 1.\n"),
    ("a list", b"---\n- things\n---\n\nThe fiscal year starts July 1.\n"),
    ("a scalar", b"---\njust text\n---\n\nThe fiscal year starts July 1.\n"),
    ("rule as the first line", b"---\nWho we are: a small lab.\n\nThe fiscal year starts July 1.\n"),
    ("empty", b""),
    ("only an opening", b"---\n"),
    ("five megabytes", (GOOD + "Lorem ipsum. " * 400_000).encode()),
    ("nests too deeply to read", GOOD.replace("identity:", "deep: " + "[" * 1000 + "\nidentity:").encode()),
]
ALL = FIXED + CASES


def opened(root: Path) -> Cerebro:
    return Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), agent_run=ScriptedRun(lambda prompt: ""),
                   trusted=True)


@pytest.fixture(scope="module")
def workspace_folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = scaffold.init(tmp_path_factory.mktemp("faults") / "things", environ={}).root
    (root / "workspace.md").write_text(GOOD, encoding="utf-8")
    return root


def one_line(said: str) -> bool:
    return "\n" not in said and "Traceback" not in said


@pytest.mark.parametrize(("label", "data"), ALL, ids=[label for label, _ in ALL])
def test_a_lookup_in_an_open_workspace_answers_or_refuses_one_window_in_one_line(
        workspace_folder: Path, label: str, data: bytes) -> None:
    with opened(workspace_folder) as workspace:
        (workspace_folder / "workspace.md").write_bytes(data)
        try:
            assert workspace.query(YOU, "facts", PLAIN).count == 0
            try:
                workspace.query(YOU, "facts", ASK)
            except CerebroError as error:
                said = str(error)
                assert one_line(said) and said.startswith("query: filter window: this_fiscal_year needs the fiscal "
                                                          "year's start, and workspace.md "), said
            try:
                read_fiscal(workspace_folder)
            except WorkspaceError as error:
                assert one_line(str(error)) and str(error).startswith("workspace.md"), str(error)
            start, why = fiscal_now(workspace_folder)
            assert start in (None, *range(1, 13)) and (why is None or one_line(why))
        finally:
            (workspace_folder / "workspace.md").write_text(GOOD, encoding="utf-8")


@pytest.mark.timeout(120)
@pytest.mark.parametrize(("label", "data"), ALL, ids=[label for label, _ in ALL])
def test_no_workspace_md_ends_a_command_in_a_traceback_and_last_30_days_is_answered(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, data: bytes) -> None:
    root = scaffold.init(tmp_path / "things", environ={}).root
    (root / "workspace.md").write_bytes(data)
    monkeypatch.setattr("cerebro.cli.open_workspace", opened)
    for command in (["status"], ["tools", "query", "--json", json.dumps(ASK)]):
        result = CliRunner().invoke(main, [*command, "--workspace", str(root)])
        assert result.exception is None or isinstance(result.exception, SystemExit), (command, repr(result.exception))
        assert "Traceback" not in result.output
    plain = CliRunner().invoke(main, ["tools", "query", "--json", json.dumps(PLAIN), "--workspace", str(root)])
    assert plain.exit_code == 0, plain.output


@pytest.mark.parametrize(("label", "data"), ALL, ids=[label for label, _ in ALL])
def test_no_profile_md_ends_a_command_or_who_is_ours_in_a_traceback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, data: bytes) -> None:
    root = scaffold.init(tmp_path / "things", environ={}).root
    (root / "workspace.md").write_text(GOOD, encoding="utf-8")
    (root / "profile.md").write_bytes(data)
    try:
        read_ours(root)
    except CerebroError as error:
        assert one_line(str(error)) and str(error).startswith("profile.md"), str(error)
    monkeypatch.setattr("cerebro.cli.open_workspace", opened)
    for command in (["status"], ["tools", "query", "--json", json.dumps(PLAIN)]):
        result = CliRunner().invoke(main, [*command, "--workspace", str(root)])
        assert result.exception is None or isinstance(result.exception, SystemExit), (command, repr(result.exception))
        assert "Traceback" not in result.output


SHAPES = {  # front matter that does not read, each with the line that names the file and what to fix
    "never closes": ("---\nidentity:\n  {key}: [{value}]\n\nbody\n",
                     "{name}: front matter has no closing --- line: end it with a line that is --- alone"),
    "ends in ...": ("---\nidentity:\n  {key}: [{value}]\n...\n\nbody\n",
                    "{name}: front matter has no closing --- line: end it with a line that is --- alone"),
    "a list": ("---\n- {value}\n---\n\nbody\n", "{name}: line 2: expected a mapping: write key: value lines"),
    "a scalar": ("---\n{value}\n---\n\nbody\n", "{name}: line 2: expected a mapping: write key: value lines"),
}
WHO = {"profile.md": ("email", "sam@quillmoor.example"), "workspace.md": ("domains", "quillmoor.example")}


@pytest.mark.parametrize("name", WHO)
@pytest.mark.parametrize("shape", SHAPES)
def test_front_matter_that_does_not_read_is_a_fault_and_never_an_empty_own_side(
        tmp_path: Path, name: str, shape: str) -> None:
    root = scaffold.init(tmp_path / "things", environ={}).root
    (key, value), (text, line) = WHO[name], SHAPES[shape]
    (root / name).write_text(text.format(key=key, value=value), encoding="utf-8")
    fault = line.format(name=name)
    with pytest.raises(WorkspaceError) as raised:
        read_ours(root)
    assert str(raised.value) == fault
    workspace = Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), agent_run=ScriptedRun(lambda p: ""))
    with workspace:
        for refused in (workspace.status, workspace.run_once, workspace.files_read):
            with pytest.raises(WorkspaceError) as raised:
                refused()
            assert str(raised.value) == fault
        for listed in workspace.connectors.values():  # no source lists with an own side that could not be read
            with pytest.raises(WorkspaceError):
                listed.list()
        assert workspace.query(YOU, "facts", PLAIN).count == 0  # reads go on
    result = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    assert result.exit_code == 1 and result.output == f"Error: {fault}\n"


@pytest.mark.parametrize("name", WHO)
def test_serve_reads_a_mended_file_on_its_next_pass_with_no_restart(tmp_path: Path, name: str) -> None:
    from cerebro.runner import run_forever

    root = scaffold.init(tmp_path / "things", environ={}).root
    key, value = WHO[name]
    (root / name).write_text(SHAPES["never closes"][0].format(key=key, value=value), encoding="utf-8")
    workspace = Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), agent_run=ScriptedRun(lambda p: ""))
    errors: list[str] = []
    passes = iter(range(3))

    def mend(_: float) -> None:
        (root / name).write_text(f"---\nidentity:\n  {key}: [{value}]\n---\n", encoding="utf-8")

    with workspace:
        run_forever(workspace, interval=0, stop=lambda: next(passes, None) is None, sleep=mend,
                    on_error=lambda error: errors.append(str(error)))
        assert errors == [SHAPES["never closes"][1].format(name=name)]  # the pass after the mend ran
        assert value in (workspace.ours.addresses if key == "email" else workspace.ours.domains)
        local = workspace.connectors["local"].inner
        assert getattr(local, "_ours", None) == workspace.ours  # the source was built again with the mended side
