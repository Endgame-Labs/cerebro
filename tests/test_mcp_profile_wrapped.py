"""A stored value written across lines in profile.md's front matter, in any YAML style, at any position: no part of it
reaches a session's instructions or the server's warnings, over standard input and output or over HTTP."""

from __future__ import annotations

import html
import logging
from pathlib import Path

import pytest
import yaml
from test_mcp import told
from test_mcp_person import person_told
from test_tokens import no_network  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro import Cerebro
from cerebro.fakes import FakeSecrets, MemoryStore
from cerebro.mcp import instructions

VALUES = {"plain": "Kx9wrapSecretTokenValue42", "marks": "Kx9<ang&le\"quo'te41", "spaced": "Kx9 spaced Secret85"}
BREAKS = {"lf": "\n", "crlf": "\r\n"}
INDENTS = {"spaces": "    ", "tab": "   \t"}


def _single(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _double(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def wrapped(value: str, nl: str, indent: str) -> list[str]:
    """Each way to write `value` under a key, wrapped once at each place a line may break: plain, quoted, folded,
    literal and in a flow list."""
    out = []
    for i in range(1, len(value)):
        head, tail = value[:i], value[i:]
        out += [f"{head}{nl}{indent}{tail}", f">{nl}{indent}{head}{nl}{indent}{tail}",
                f"|{nl}{indent}{head}{nl}{indent}{tail}", f"[{head}{nl}{indent}{tail}]"]
    for quoted in (_single(value), _double(value)):
        for i in range(2, len(quoted) - 1):
            if quoted[i - 1] != "\\" and quoted[i - 1:i + 1] != "''":
                out.append(f"{quoted[:i]}{nl}{indent}{quoted[i:]}")
    return out


def profile(entries: list[str], nl: str) -> str:
    lines = ["---", "identity:", "  name: Robin Vale", *[f"  key_{n}: {entry}" for n, entry in enumerate(entries)],
             "---", "Brief me in bullets.", ""]
    return nl.join(lines)


def reads(entry: str, nl: str) -> bool:
    try:
        yaml.safe_load(profile([entry], nl).split("---")[1])
    except yaml.YAMLError:
        return False
    return True


def desk(tmp_path: Path, text: str, value: str) -> Cerebro:
    root = tmp_path / "desk"
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "profile.md").write_bytes(text.encode())
    return Cerebro(root, store=MemoryStore(), secrets=FakeSecrets({"probe": value}))


def leaked(value: str, said: str) -> list[str]:
    """The value whole, or any six characters of it in a row, as the text holds them or with `&lt;` read as `<`."""
    windows = {value} | {value[i:i + 6] for i in range(len(value) - 5)}
    texts = (said, said.replace("&lt;", "<"))
    return sorted(w for w in windows if any(w in text for text in texts))


@pytest.mark.parametrize("indent", INDENTS)
@pytest.mark.parametrize("nl", BREAKS)
@pytest.mark.parametrize("shape", VALUES)
def test_a_stored_value_wrapped_anywhere_in_the_front_matter_reaches_no_session_in_any_part(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, shape: str, nl: str, indent: str) -> None:
    value, nl = VALUES[shape], BREAKS[nl]
    entries = wrapped(value, nl, INDENTS[indent])
    readable = [entry for entry in entries if reads(entry, nl)]
    assert len(readable) >= len(value) * 2
    caplog.set_level(logging.WARNING)
    with desk(tmp_path / "all", profile(readable, nl), value) as workspace:
        local, person = told(workspace)[0], person_told(workspace)
    for said in (local, person):
        assert said is not None and "The person you are helping is Robin Vale" in said
        assert leaked(value, said) == []
    for n, entry in enumerate(entry for entry in entries if entry not in readable):
        with desk(tmp_path / f"one-{n}", profile([], nl), value) as workspace:
            (workspace.root / "profile.md").write_bytes(profile([entry], nl).encode())
            said = instructions(workspace)
        assert leaked(value, said) == []
    assert leaked(value, caplog.text) == []


def test_no_part_of_a_stored_value_reaches_a_session_however_the_profile_writes_it(tmp_path: Path) -> None:
    long, short, inner, tab = "Kx9plainSecretVal77", "Kx9plainSec", "Kx9inner Space42Val", "Kx9tab\tSecret85"
    spaced, marks = "Kx9 oneSpace52", "Kx9<amp&\"quo'Sl/ash41"
    text = ("---\nidentity:\n  name: Robin Vale\n  eol: \"Kx9plainSec\\\n    retVal77\"\n  folded: Kx9plainSecr\n"
            "    etVal77\n  trailing: K \n    x9inner Space42Val\n  at_its_tab: Kx9tab\n    Secret85\n"
            "  literal: |\n    Kx9tab\n    Secret85\n---\nThe phrase is Kx9\noneSpace52, Kx9plainSecr\n  etVal77,\n"
            "Kx9&lt;amp&amp;&quot;quo&#x27;Sl/ash41 and Kx9\\<amp\\&\"quo'Sl/ash41.\n")
    root = tmp_path / "desk"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    (root / "profile.md").write_text(text)
    values = {"a": long, "b": short, "c": inner, "d": tab, "e": spaced, "f": marks}
    with Cerebro(root, store=MemoryStore(), secrets=FakeSecrets(values)) as workspace:
        said = instructions(workspace)
    for told_text in (said,):
        assert ("The person you are helping is Robin Vale. Their eol is [secret], their folded is [secret], their "
                "trailing is [secret], their at_its_tab is [secret] and their literal is [secret].") in told_text
        assert "The phrase is [secret], [secret],\n[secret] and [secret]." in told_text
        for value in (*values.values(), html.unescape(marks)):
            assert leaked(value, told_text) == [] and leaked(value, " ".join(told_text.split())) == []


def test_yaml_reads_the_front_matter_as_written_whatever_the_store_holds(tmp_path: Path) -> None:
    from cerebro.tokens import PREFIX

    token = PREFIX + "A1b2C3d4" * 5 + "xyz"  # made up, in a token's shape
    root = tmp_path / "desk"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    (root / "workspace.md").write_text("---\nidentity:\n  name: Quillmoor\n---\nWhat we do.\n")
    (root / "profile.md").write_text(f"---\nidentity:\n  name: Robin Vale\n  aliases: [Robin, RV]\n  desk: {token}\n"
                                     "  Kx9alphaKey77: one\n  Kx9betaKey88: two\nrole: Lead engineer\n---\nBrief me.\n")
    values = {"a": "identity", "b": "aliases", "c": "Kx9alphaKey77", "d": "Kx9betaKey88"}
    with Cerebro(root, store=MemoryStore(), secrets=FakeSecrets(values)) as workspace:
        said = instructions(workspace)
    assert "This is Cerebro, the workspace desk for Quillmoor." in said
    assert ("The person you are helping is Robin Vale, Lead engineer. They also go by Robin and RV, their desk is "
            "[a token, masked], their [secret] is one and their [secret] is two.") in said
