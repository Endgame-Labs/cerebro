"""A raw control character in an agent file, in settings.yaml or in a sources file refuses that file with its line and
the character written as an escape, and no command ends in a traceback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro import scaffold
from cerebro.agent import AgentFileError, load_agent, read_yaml
from cerebro.cli import main

# YAML takes a tab, a line feed, a carriage return and U+0085 as they are, and refuses every other one of these.
CONTROLS = [*range(32), 127, *range(0x80, 0xA0)]
TAKEN = {0x09, 0x0A, 0x0D, 0x85}
REFUSED = [code for code in CONTROLS if code not in TAKEN]
# The commands below are run over one of each kind: a taken C0 and C1 character, NUL, Escape, DEL and a refused C1
# one. Every character is read by the one YAML reader, which the two tests above run over each of them.
AT_COMMANDS = [0x00, 0x09, 0x1B, 0x7F, 0x85, 0x9B]


def escaped(code: int) -> str:
    return ascii(chr(code))[1:-1]


def front(code: int) -> str:
    return f"---\nheartbeat: [change]\n# a comment {chr(code)}[31m\nevidence:\n  - query: {{ from: notes }}\n---\nWrite.\n"


@pytest.mark.parametrize("code", CONTROLS, ids=hex)
def test_an_agent_file_with_a_control_character_loads_or_is_refused_at_its_line(tmp_path: Path, code: int) -> None:
    path = tmp_path / "agents" / "things" / "writer.md"
    path.parent.mkdir(parents=True)
    path.write_text(front(code), encoding="utf-8")
    if code in TAKEN:  # each breaks the comment's line or ends it, and the file loads or is refused as YAML says
        try:
            load_agent(path, tmp_path / "agents")
        except AgentFileError as error:
            assert "front matter is not YAML at line" in str(error)
        return
    with pytest.raises(AgentFileError) as refused:
        load_agent(path, tmp_path / "agents")
    assert str(refused.value) == (f"agents/things/writer.md: front matter is not YAML at line 3: it holds the control "
                                  f"character {escaped(code)}, which YAML does not allow: delete it")


@pytest.mark.parametrize("code", REFUSED, ids=hex)
def test_yaml_with_a_control_character_names_its_line_and_the_character(code: int) -> None:
    value, problem = read_yaml(f"id: one\nname: a{chr(code)}b\n")
    assert value is None
    assert problem == f"line 2: it holds the control character {escaped(code)}, which YAML does not allow: delete it"


def test_a_line_counts_crlf_lines_once() -> None:
    assert read_yaml("a: 1\r\nb: 2\r\nc: \x1b\r\n")[1] == ("line 3: it holds the control character \\x1b, which YAML "
                                                            "does not allow: delete it")


def planted(root: Path, kind: str, code: int) -> int:
    """Init a workspace at `root` and write the character into one file of `kind`; gives the line it sits on."""
    scaffold.init(root, environ={})
    if kind == "agent":
        (root / "agents" / "things").mkdir(parents=True, exist_ok=True)
        (root / "agents" / "things" / "writer.md").write_text(front(code), encoding="utf-8")
        return 3
    if kind == "settings":
        settings = (root / "settings.yaml").read_text(encoding="utf-8")
        (root / "settings.yaml").write_text(f"{settings}# a comment {chr(code)}\n", encoding="utf-8")
        return settings.count("\n") + 1
    (root / "sources" / "local.yaml").write_text(f"kind: files\n# a comment {chr(code)}\n", encoding="utf-8")
    return 2


COMMANDS = [["trust"], ["status"], ["agents", "list"], ["agents", "list", "--json"], ["prompts", "list"],
            ["sources", "list"], ["distill"], ["agents", "run", "things/writer"], ["compile", "--article", "things/writer"]]
SAID_BY = {"agent": (["status"], ["agents", "list"], ["agents", "list", "--json"]), "settings": (["status"],),
           "source": (["status"],)}


@pytest.mark.parametrize("kind", ["agent", "settings", "source"])
def test_no_command_ends_in_a_traceback_over_any_control_character(tmp_path: Path, kind: str) -> None:
    for code in AT_COMMANDS:
        root = tmp_path / f"{code:02x}"
        line = planted(root, kind, code)
        for args in COMMANDS:
            where = [str(root)] if args == ["trust"] else ["--workspace", str(root)]
            result = CliRunner().invoke(main, [*args, *where])
            assert result.exception is None or isinstance(result.exception, SystemExit), (
                kind, hex(code), args, result.output, result.exception)
            assert "Traceback" not in result.output, (kind, hex(code), args, result.output)
            said = result.output
            if args[-1] == "--json" and result.exit_code == 0:
                said = "\n".join(json.loads(result.output)["refused"])
            if code not in TAKEN and args in SAID_BY[kind]:
                assert f"line {line}: it holds the control character {escaped(code)}" in said, (
                    kind, hex(code), args, result.output)
