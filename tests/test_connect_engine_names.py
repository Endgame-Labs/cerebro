"""The shared connect code names no source, in its code, comments or docstrings: what one source does is said in
that connector's own file."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import cerebro.connect
import cerebro.connectors.connecting

NAMES = re.compile(r"\b(github|slack|gong|gmail|linear|granola|calendar)('s)?\b", re.IGNORECASE)


def docstrings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found = [ast.get_docstring(node) or "" for node in ast.walk(tree)
             if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    comments = [line.split("#", 1)[1] for line in path.read_text().splitlines() if "#" in line]
    return found + comments


def test_the_shared_words_of_a_connect_name_no_source() -> None:
    said = [text for text in docstrings(Path(cerebro.connectors.connecting.__file__)) if NAMES.search(text)]
    assert said == []


def test_connect_says_which_logins_are_asked_each_time_by_the_connector_not_by_source() -> None:
    module = ast.get_docstring(ast.parse(Path(cerebro.connect.__file__).read_text())) or ""
    kept = next(p for p in module.split("\n\n") if p.startswith("A login the store holds already"))
    assert not NAMES.search(kept), kept
