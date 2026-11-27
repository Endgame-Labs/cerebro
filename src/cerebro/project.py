"""Finding and loading a workspace folder.

A workspace is a folder with a `settings.yaml` and an `agents/` folder of context agent files. It may hold an
`extensions.py`, whose marked functions the workspace's app object collects, a `facts/` folder of the kinds of fact it
defines, which `facts.load_kinds` reads, `sources/`, and a `workspace.md` whose body may
state when the fiscal year starts, as `The fiscal year starts February 1.` Nothing else is required.

settings.yaml is read as YAML 1.2, by `_read`, but for `id` and `store`, which keep
YAML 1.1's value, as the trust check and the secrets store read them. Settings are checked when the workspace loads: the
models by `models.read_models`, the daily caps by `budget.read_caps`, the time zone by its IANA name, the `search:`
block by `rerank.read_rerank`, and the rest here. A key no reader declares in its `SETTINGS_KEYS` is refused at any
level, with its line, in this order: a key unknown at the top or under `schedule`, then a key given twice at the
top, then each block in the file's order, its own unknown keys before its values, then a value of the wrong kind under
`id`, `port`, a model job or `schedule`, or a key given twice in a block, in the file's order. Last come the rest of
the checks of `id`, `port` and the models, which every command runs.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import math
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .agent import Agent, _Strict, load_agents, read_yaml
from .apps import App, AppError, apps_in, marked_in
from .budget import FIRST_RUN_USD, HIGHEST, LOWEST, Caps, read_caps
from .connector import Ours
from .errors import CerebroError
from .facts import load_kinds
from .gateway import read_providers
from .models import JOBS, ModelSpec, read_model, read_models
from .rendered import RenderError, descriptions
from .secrets import SecretsError
from .secrets import _check_id as check_id
from .shown import shown_path
from .trust import check as check_trust
from .trust import records as trust_records

__all__ = [
    "SCHEDULE_KEYS",
    "SERVE_LIMITS",
    "SETTINGS_KEYS",
    "Fiscal",
    "Project",
    "Schedule",
    "WorkspaceError",
    "agent_notices",
    "check_settings",
    "checked_settings",
    "fiscal_now",
    "fiscal_start",
    "known_keys",
    "load",
    "read_fiscal",
    "read_ours",
    "read_schedule",
    "read_settings",
    "zone_of",
]


class WorkspaceError(CerebroError, ValueError):
    """A folder is not a workspace, or its settings or extensions do not load."""


@dataclass
class Project:
    """A loaded workspace folder: its settings, the agents that loaded, a line for each that did not, the functions its
    `extensions.py` marks, the apps it loads, and the kinds of fact its `facts/` folder defines."""

    root: Path
    settings: dict[str, Any]
    agents: list[Agent]
    refused: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    extensions: list[Callable[..., Any]] = field(default_factory=list)
    apps: list[App] = field(default_factory=list)
    models: dict[str, ModelSpec] = field(default_factory=dict)
    caps: Caps = field(default_factory=Caps)
    zone: tzinfo = UTC
    kinds: list[type] = field(default_factory=list)


MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
# The fiscal year sentence reads in any letter case: an ASCII letter matches its upper and lower case, and k and s also
# the Kelvin sign and the long s, which fold onto them. No other letter stands in, as a dotless i would with IGNORECASE.
CASE_FOLDS = {"k": "\u212a", "s": "\u017f"}


def _any_case(word: str) -> str:
    """A pattern for `word` in any letter case, as `CASE_FOLDS` says."""
    return "".join(f"[{c}{c.upper()}{CASE_FOLDS.get(c, '')}]" if c.isalpha() else re.escape(c) for c in word)


def _month(said: str) -> int:
    """The month, 1 to 12, of a name `FISCAL` matched, in any letter case `_any_case` matches."""
    plain = said.replace(CASE_FOLDS["k"], "k").replace(CASE_FOLDS["s"], "s").lower()
    return MONTHS.index(plain) + 1


# \b falls between an ASCII letter, digit or _ and anything else, so a letter outside ASCII never joins the sentence to
# a word beside it
FISCAL = re.compile(r"\b" + _any_case("fiscal year ") + "(?:" + _any_case("starts") + "|" + _any_case("begins") + ") (?:"
                    + _any_case("on ") + ")?(" + "|".join(map(_any_case, MONTHS)) + r") 1\b", re.ASCII)
SCHEDULE_KEYS = ("sources", "settle", "articles")
SERVE_LIMITS = ("concurrency", "head_timeout", "body_timeout", "idle_timeout")
ZERO = re.compile(r"0+[smhd]?")  # a duration of zero, with or without a unit: none, for settle and articles
DURATION = re.compile(r"([0-9]+)([smhd])")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# The keys checked here. Of their readers, cli.py, runner.py, mcp.py and app.py declare none of their own.
SETTINGS_KEYS: dict[str, Any] = {
    "id": None, "port": None, "timezone": None, "schedule": dict.fromkeys(SCHEDULE_KEYS),
    "compile": {"concurrency": None}, "serve": dict.fromkeys(("host", "port", "interval", *SERVE_LIMITS)),
    "min_grounding": None,
}


def agent_notices(agents: list[Agent]) -> list[str]:
    """Each agent's notice of keys not acted on, and each warning it loaded with."""
    return [line for agent in agents for line in ([agent.notice] if agent.notice else []) + agent.warnings]


NULL = "tag:yaml.org,2002:null"
BOM = b"\xef\xbb\xbf"


def _split(data: bytes, name: str) -> tuple[bytes | None, bytes]:
    """A file's front matter and body: front matter opens on a first line that is `---` and nothing else, and closes
    on the next line that is `---` and nothing else, a `\\r` aside. A file that does not open so is all body, and one
    that opens and never closes is a fault."""
    data = data.removeprefix(BOM)
    for fence in (b"---\n", b"---\r\n"):
        if data.startswith(fence):
            rest = data[len(fence):]
            break
    else:
        return None, data
    at = 0
    while at < len(rest):
        end = rest.find(b"\n", at)
        line, after = (rest[at:end], end + 1) if end >= 0 else (rest[at:], len(rest))
        if line.rstrip(b"\r") == b"---":
            return rest[:at], rest[after:]
        at = after
    raise WorkspaceError(f"{name}: front matter has no closing --- line: end it with a line that is --- alone")


def _top(front: bytes, name: str) -> dict[str, yaml.Node]:
    """The keys of front matter at its top level, each with its YAML node: the first
    document alone, counted in lines from the top of the file, and a map, or nothing at all."""
    try:
        text = front.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkspaceError(f"{name} front matter: it is not UTF-8 text: save the file as UTF-8") from None
    loader = _Strict("\n" + text)  # the opening --- line, so a line is the file's own
    try:
        node = loader.get_node() if loader.check_node() else None
        while loader.check_token():  # what follows the first document is not read, but a token in it that does not
            loader.get_token()  # scan is a fault
    except RecursionError:
        raise WorkspaceError(f"{name} front matter: it nests too deeply to read: write it flatter") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        problem = getattr(error, "problem", None) or "it does not parse"
        raise WorkspaceError(f"{name} front matter: " + (f"line {mark.line + 1}: {problem}" if mark else problem)) from None
    finally:
        loader.dispose()
    return _keys(node, name)


def _keys(node: yaml.Node | None, name: str) -> dict[str, yaml.Node]:
    """A map's keys and their nodes: a null is an empty map, any other
    value is a fault, and so are a key that is not a value and a key given twice."""
    if node is None or isinstance(node, yaml.ScalarNode) and node.tag == NULL:
        return {}
    if not isinstance(node, yaml.MappingNode):
        raise WorkspaceError(f"{name}: line {node.start_mark.line + 1}: expected a mapping: write key: value lines")
    found: dict[str, yaml.Node] = {}
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode):
            raise WorkspaceError(f"{name}: line {key.start_mark.line + 1}: mapping keys must be scalars")
        if key.value in found:
            raise WorkspaceError(f"{name}: line {key.start_mark.line + 1}: duplicate key \"{key.value}\"")
        found[key.value] = value
    return found


def _value(node: yaml.Node | None, name: str, key: str) -> str:
    """A value read as text: a null is empty, and a list or a map is a fault."""
    if node is None or isinstance(node, yaml.ScalarNode) and node.tag == NULL:
        return ""
    if not isinstance(node, yaml.ScalarNode):
        raise WorkspaceError(f"{name} {key}: line {node.start_mark.line + 1}: write a value, not a list or a map")
    return node.value


def _list(node: yaml.Node | None, name: str, key: str) -> tuple[str, ...]:
    """A value or a list of values: a null is none, a value is one, and
    a list is its values, each read as text; a map, or a list in a list, is a fault."""
    if isinstance(node, yaml.SequenceNode):
        return tuple(_value(item, name, key) for item in node.value)
    if isinstance(node, yaml.MappingNode):
        raise WorkspaceError(f"{name} {key}: write a list, as {key.rpartition('.')[2]}: [quillmoor.example]")
    return (text,) if (text := _value(node, name, key)) else ()


def _file_bytes(path: Path) -> bytes | None:
    """A file's bytes, or None for a file that is not there."""
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError as error:
        raise WorkspaceError(f"{path.name} cannot be read ({type(error).__name__}: {error.strerror})") from None


def _front(path: Path) -> tuple[dict[str, yaml.Node], str]:
    """A file's front matter, its top-level keys with their nodes, and its body, as `workspace.md` and `profile.md`
    are read: a missing file and a file with no front matter have no keys. A byte in the body that is not UTF-8
    reads around it. Raises `WorkspaceError`, in one line that names the file and what to fix, for
    front matter that never closes, is not UTF-8, does not parse, or is not a map."""
    data = _file_bytes(path)
    if data is None:
        return {}, ""
    front, body = _split(data, path.name)
    keys = _top(front, path.name) if front is not None else {}
    return keys, body.decode("utf-8", "replace")


def _checked(path: Path) -> tuple[dict[str, yaml.Node], str]:
    """The front matter of `workspace.md` or `profile.md`, with its known keys
    checked: `identity` is a map, its `name` a value, its `aliases`, `domains` and `email` each a value
    or a list, and `profile.md`'s `role` a value. Gives the keys under `identity`, and the body."""
    keys, body = _front(path)
    name = path.name
    identity = _keys(keys.get("identity"), f"{name} identity")
    _value(identity.get("name"), name, "identity.name")
    for key in ("aliases", "domains", "email"):
        _list(identity.get(key), name, f"identity.{key}")
    if name == "profile.md":
        _value(keys.get("role"), name, "role")
    return identity, body


def _identity_list(path: Path, key: str) -> tuple[str, ...]:
    """The values under `identity.<key>` in a file's front matter, trimmed and in lower case, or none."""
    values = _list(_checked(path)[0].get(key), path.name, f"identity.{key}")
    return tuple(v.strip().lower() for v in values if v.strip())


@dataclass(frozen=True)
class Fiscal:
    """What `workspace.md`'s body says of the fiscal year: the month it starts, or None."""

    start: int | None = None


def read_fiscal(root: Path) -> Fiscal:
    """What the body of `workspace.md` says of the fiscal year: the month the first line that states a start names,
    as in `The fiscal year starts February 1.` or `The fiscal year begins on October 1.`. A missing workspace.md
    states none. Raises `WorkspaceError`, in one line, for a file that cannot be read and for front matter that never
    closes, is not UTF-8, does not parse, or is not a map."""
    path = root / "workspace.md"
    _, body = _checked(path)
    for line in body.split("\n"):  # split at \n alone, not at every break splitlines knows
        if rule := FISCAL.search(line):
            return Fiscal(_month(rule.group(1)))
    return Fiscal()


def fiscal_start(root: Path) -> int | None:
    """The month the fiscal year starts, as `read_fiscal` reads it, or None."""
    return read_fiscal(root).start


def fiscal_now(root: Path) -> tuple[int | None, str | None]:
    """The fiscal start `workspace.md` states as it reads now, or None, and when the file does not read, what a fiscal
    window says in place of the usual line, else None. A read made while the file is being written sees part of it and
    at worst refuses that one window: nothing is kept, and the next read reads the file again."""
    try:
        fiscal = read_fiscal(root)
    except WorkspaceError as error:
        return None, "workspace.md does not read: " + str(error).removeprefix("workspace.md").lstrip(": ")
    return fiscal.start, None


def read_ours(root: Path) -> Ours:
    """Who is on our side: your addresses from `profile.md` front matter
    (`identity.email`) and the workspace's own domains from `workspace.md` (`identity.domains`). A missing file leaves
    its part empty."""
    return Ours(_identity_list(root / "profile.md", "email"), _identity_list(root / "workspace.md", "domains"))


def zone_of(settings: dict[str, Any]) -> tzinfo:
    """The time zone `timezone` names, or UTC."""
    name = settings.get("timezone")
    if name is None:
        return UTC
    try:
        return ZoneInfo(str(name)) if isinstance(name, str) else ZoneInfo("")
    except (ZoneInfoNotFoundError, ValueError):
        raise WorkspaceError(f"settings.yaml timezone is {name!r}, which is no time zone: write an IANA name such as "
                         "America/Los_Angeles") from None


def _extensions(root: Path) -> tuple[list[Callable[..., Any]], list[App]]:
    """The functions `extensions.py` marks, and the apps it loads, as with `notes = load_app("apps/notes")`."""
    path = root / "extensions.py"
    if not path.is_file():
        return [], []
    name = "cerebro_extensions_" + hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module  # a dataclass in the module looks itself up here
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as error:  # noqa: BLE001 - any error in extensions.py is reported as one line
        del sys.modules[name]
        raise WorkspaceError(f"{path.name} does not import ({type(error).__name__}: {error}): fix it or move it aside")
    try:
        return marked_in(module), apps_in(module)
    except AppError as error:
        raise WorkspaceError(f"{path.name}: {error}") from None


def _infinite(value: float) -> bool:
    return isinstance(value, float) and not math.isfinite(value)  # a whole number of any size is finite


def _number(settings: dict[str, Any], block_name: str, key: str, *, whole: bool, low: float,
            high: float | None = None) -> None:
    block = settings.get(block_name)
    value = block.get(key) if isinstance(block, dict) else None
    if value is None:
        return
    kind = int if whole else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or _infinite(value) or value < low or (
            high is not None and value > high):
        span = f"from {low} to {high}" if high is not None else f"above {low}" if low else "of 0 or more"
        raise WorkspaceError(f"settings.yaml {block_name}.{key} is {value!r}: write a {'whole ' if whole else ''}number "
                         f"{span}")


@dataclass(frozen=True)
class Schedule:
    """The clocks of `serve`. `sources` is how often a source is checked, which its
    file's `freshness:` overrides, and `settle` how long a file is left alone before the files source reads it.
    `articles` is the cooldown of every writer: after an agent's run, new evidence waits that long before it runs
    again, 0 for none, which an agent's own `heartbeat: { cooldown: }` overrides. It is
    counted from each agent's last run on each article."""

    sources: timedelta = timedelta(minutes=15)
    settle: timedelta = timedelta(minutes=2)
    articles: timedelta = timedelta(hours=4)


def read_schedule(settings: dict[str, Any]) -> Schedule:
    """The `schedule:` block of settings: `sources`, `settle` and `articles`, each a duration such as `15m`, `settle`
    and `articles` 0 for none, and `articles` a minute or more. One that does not read is refused with a line that
    names it."""
    from .source import span

    block = settings.get("schedule")
    if block is None:
        return Schedule()
    if not isinstance(block, dict) or set(block) - set(SCHEDULE_KEYS):
        raise WorkspaceError("settings.yaml schedule takes sources, settle and articles, each a duration: write "
                         "schedule: { sources: 15m, settle: 2m, articles: 4h }")
    found = {}
    for key, value in block.items():
        if value is None:
            continue  # a null is unset
        text = str(value).strip() if key == "articles" else str(value)  # articles alone is trimmed of spaces
        if key == "articles" and not text:
            continue
        if key in ("settle", "articles") and ZERO.fullmatch(text):
            found[key] = timedelta(0)
            continue
        whole = DURATION.fullmatch(text)
        digits = whole.group(1).lstrip("0") if whole else ""
        if whole and (len(digits) > 19 or int(digits or "0") > (2**63 - 1) // (UNITS[whole.group(2)] * 10**9)):
            raise WorkspaceError(f"settings.yaml schedule.{key}: {text!r} is longer than a duration can be: write a "
                             "shorter one, as 90d")
        try:
            if not whole:  # span's own pattern lets a line break end it
                raise ValueError(f"{text!r} is not a duration: write a whole number above zero and a unit, s, m, h or "
                                 "d, as 90d")
            found[key] = span(f"{digits or 0}{whole.group(2)}" if len(text) > 40 else text)  # int() stops at 4300
        except ValueError as error:
            raise WorkspaceError(f"settings.yaml schedule.{key}: {error}") from None
        if key == "articles" and found[key] < timedelta(minutes=1):
            raise WorkspaceError(f"settings.yaml schedule.articles: {value!r} is under a minute: write a minute or more, "
                             "as 4h, or 0 to rewrite a changed article on every pass")
    return Schedule(**found)


@functools.cache
def known_keys() -> dict[str, Any]:
    """Every key settings.yaml takes, as a tree: each reader's `SETTINGS_KEYS`, merged. None is a value with no keys
    under it, and "*" stands for any name."""
    from . import budget, facts, gateway, models, rendered, rerank, store, tokens

    tree: dict[str, Any] = {}
    for part in (SETTINGS_KEYS, models.SETTINGS_KEYS, budget.SETTINGS_KEYS, rerank.SETTINGS_KEYS, rendered.SETTINGS_KEYS,
                 facts.SETTINGS_KEYS, store.SETTINGS_KEYS, tokens.SETTINGS_KEYS, gateway.SETTINGS_KEYS):
        _merge(tree, part)
    return tree


def _merge(into: dict[str, Any], part: dict[str, Any]) -> None:
    for key, below in part.items():
        if isinstance(below, dict) and isinstance(into.get(key), dict):
            _merge(into[key], below)
        else:
            into[key] = dict(below) if isinstance(below, dict) else below


def _at(line: int | None, said: str) -> str:
    return f"settings.yaml: line {line}: {said}" if line else f"settings.yaml: {said}"


def _shown(key: Any) -> str:
    said = "on" if key is True else "off" if key is False else "null" if key is None else str(key)  # as YAML 1.1,
    return shown_path(json.dumps(said, ensure_ascii=False))  # which reads store:, reads on:, off: and ~:


PLAIN = "!plain"  # a plain scalar's tag until `_plain` reads it
STR, MERGE = "tag:yaml.org,2002:str", "tag:yaml.org,2002:merge"
WORDS: dict[str, Any] = {**dict.fromkeys(("", "~", "null", "Null", "NULL")), **dict.fromkeys(("true", "True", "TRUE"), True),
                         **dict.fromkeys(("false", "False", "FALSE"), False), ".nan": math.nan, ".NaN": math.nan,
                         ".NAN": math.nan, **dict.fromkeys((".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF"), math.inf),
                         **dict.fromkeys(("-.inf", "-.Inf", "-.INF"), -math.inf)}
FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
BOOLS = {"y": True, "Y": True, "yes": True, "Yes": True, "YES": True, "on": True, "On": True, "ON": True,
         "n": False, "N": False, "no": False, "No": False, "NO": False, "off": False, "Off": False, "OFF": False}
NODES = 100_000  # the most nodes a file's aliases may build again: past it the file is refused


class _SettingsLoader(yaml.SafeLoader):
    """PyYAML's parser with the tab rule of libyaml: a tab is a space inside a flow, after a
    key's colon and between words, and never in an indent. A plain scalar keeps `PLAIN` for `_plain`."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.aliases: dict[tuple[int, int], int] = {}  # the line of each alias written as a map's value

    def scan_to_next_token(self) -> None:
        if self.index == 0 and self.peek() == "\ufeff":
            self.forward()
        while True:
            while self.peek() == " " or self.peek() == "\t" and (self.flow_level or not self.allow_simple_key):
                self.forward()
            if self.peek() == "#":
                while self.peek() not in "\0\r\n\x85\u2028\u2029":
                    self.forward()
            if not self.scan_line_break():
                return
            if not self.flow_level:
                self.allow_simple_key = True

    def scan_plain(self) -> Any:
        """PyYAML's plain scalar, ended at a colon only before a space, a tab or a line break."""
        chunks: list[str] = []
        start_mark = end_mark = self.get_mark()
        indent, spaces = self.indent + 1, []
        while self.peek() != "#":
            length = 0
            while True:
                ch = self.peek(length)
                if ch in "\0 \t\r\n\x85\u2028\u2029" or ch == ":" and self.peek(length + 1) in "\0 \t\r\n\x85\u2028\u2029" \
                        or self.flow_level and ch in ",?[]{}":
                    break
                length += 1
            if length == 0:
                break
            self.allow_simple_key = False
            chunks.extend(spaces)
            chunks.append(self.prefix(length))
            self.forward(length)
            end_mark = self.get_mark()
            spaces = self.scan_plain_spaces(indent, start_mark)
            if not spaces or self.peek() == "#" or not self.flow_level and self.column < indent:
                break
        return yaml.tokens.ScalarToken("".join(chunks), True, start_mark, end_mark)

    def scan_plain_spaces(self, indent: int, start_mark: Any) -> list[str]:
        """PyYAML's spaces in a plain scalar, where a tab is a space too, as in libyaml, but in a line's indent."""
        length = 0
        while self.peek(length) in " \t":
            length += 1
        whitespaces = self.prefix(length)
        self.forward(length)
        if self.peek() not in "\r\n\x85\u2028\u2029":
            return [whitespaces] if whitespaces else []
        line_break = self.scan_line_break()
        self.allow_simple_key = True
        if self.prefix(3) in ("---", "...") and self.peek(3) in "\0 \t\r\n\x85\u2028\u2029":
            return []
        breaks = []
        while self.peek() in " \t\r\n\x85\u2028\u2029" and not (self.peek() == "\t" and self.column < indent):
            if self.peek() in " \t":
                self.forward()
            else:
                breaks.append(self.scan_line_break())
                if self.prefix(3) in ("---", "...") and self.peek(3) in "\0 \t\r\n\x85\u2028\u2029":
                    return []
        chunks = [line_break] if line_break != "\n" else [] if breaks else [" "]
        return chunks + breaks

    def scan_block_scalar_indentation(self) -> Any:
        found = super().scan_block_scalar_indentation()
        self._no_tab(None)
        return found

    def scan_block_scalar_breaks(self, indent: int) -> Any:
        found = super().scan_block_scalar_breaks(indent)
        self._no_tab(indent)
        return found

    def _no_tab(self, indent: int | None) -> None:
        """libyaml refuses a tab where a block value's indent is still to come."""
        if self.peek() == "\t" and (indent is None or self.column < indent):
            raise yaml.scanner.ScannerError(None, None, "found a tab character where an indentation space is expected",
                                            self.get_mark())

    def scan_tag(self) -> Any:
        """PyYAML's tag, which a tab ends as a space does."""
        start_mark = self.get_mark()
        ch = self.peek(1)
        if ch == "<":
            self.forward(2)
            handle, suffix = None, self.scan_tag_uri("tag", start_mark)
            if self.peek() != ">":
                raise yaml.scanner.ScannerError("while parsing a tag", start_mark,
                                                f"expected '>', but found {self.peek()!r}", self.get_mark())
            self.forward()
        elif ch in "\0 \t\r\n\x85\u2028\u2029":
            handle, suffix = None, "!"
            self.forward()
        else:
            length, named = 1, False
            while ch not in "\0 \t\r\n\x85\u2028\u2029":
                if ch == "!":
                    named = True
                    break
                length += 1
                ch = self.peek(length)
            if named:
                handle = self.scan_tag_handle("tag", start_mark)
            else:
                handle = "!"
                self.forward()
            suffix = self.scan_tag_uri("tag", start_mark)
        if self.peek() not in "\0 \t\r\n\x85\u2028\u2029":
            raise yaml.scanner.ScannerError("while scanning a tag", start_mark,
                                            f"expected ' ', but found {self.peek()!r}", self.get_mark())
        return yaml.tokens.TagToken((handle, suffix), start_mark, self.get_mark())

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, yaml.events.AliasEvent) and isinstance(parent, yaml.MappingNode) and index is not None:
            self.aliases[id(parent), id(index)] = event.start_mark.line + 1  # where the alias stands, for NODES_KEPT
        if not isinstance(event, yaml.events.AliasEvent) and event.anchor is not None:
            self.anchors.pop(event.anchor, None)  # a name given again names the later value
        return super().compose_node(parent, index)

    def resolve(self, kind: Any, value: Any, implicit: Any) -> str:
        if kind is yaml.ScalarNode and implicit[0]:
            return PLAIN
        return super().resolve(kind, value, implicit)  # type: ignore[no-any-return]


def _whole(text: str) -> int | None:
    """`text` as a whole number within 64 bits, signed, or unsigned when it has no sign: base 2, 8 or 16 after `0b`,
    `0o` or `0x`, base 8 after a bare leading 0, else base 10. None when it is not one."""
    sign, body = (text[0], text[1:]) if text[:1] in ("+", "-") else ("", text)
    base = 10
    if body[:1] == "0":
        prefix = body[1:2].lower()
        if len(body) >= 3 and prefix in ("b", "o", "x"):
            base, body = {"b": 2, "o": 8, "x": 16}[prefix], body[2:]
        else:
            base, body = 8, body[1:]  # a bare 0 is the number 0
    if not text or not body and base != 8 or any(c not in "0123456789abcdef"[:base] for c in body.lower()):
        return None
    if len(body.lstrip("0")) > 64:  # past 64 bits in any base, and past what int() reads quickly
        return None
    value = int(body or "0", base)
    if sign == "-":
        return -value if value <= 2**63 else None
    return value if value < (2**63 if sign else 2**64) else None


def _plain(text: str) -> Any:
    """A plain scalar's value: YAML 1.2's null, true and false in three spellings, whole numbers, with
    `0b`, `0o`, `0x` and a leading 0 for octal, and its decimal floats. Anything else, a date included, is text."""
    if text in WORDS:
        return WORDS[text]
    if text[:1] == ".":
        try:
            found = float(text)
        except ValueError:
            return text
        return found if math.isfinite(found) else text
    if text[:1] not in tuple("+-0123456789"):
        return text
    bare = text.replace("_", "")
    whole = _whole(bare)
    if whole is not None:
        return whole
    if FLOAT.fullmatch(bare):
        found = float(bare)
        if math.isfinite(found):
            return found
    return text


@dataclass
class Read:
    """settings.yaml as `_read` reads it: the value, the line of each key and of each value by its
    path of keys, for each map that holds a key twice the first such key, in the order they stand, and each
    value whose tag it does not fit, which `check_settings` refuses."""

    value: Any = None
    keys: dict[tuple[Any, ...], int] = field(default_factory=dict)
    values: dict[tuple[Any, ...], int] = field(default_factory=dict)
    twice: dict[tuple[Any, ...], str] = field(default_factory=dict)
    tags: dict[tuple[Any, ...], str] = field(default_factory=dict)


class _Refused(Exception):
    """A file that does not read, with the line at fault."""


TEXT_KEYS = {("schedule", key) for key in ("sources", "settle", "articles")} | {("id",)} | {
    ("models", job, part) for job in JOBS for part in ("provider", "model", "effort")}
NOT_EMPTY = {("timezone",)} | {("models", job, part) for job in JOBS
                               for part in ("provider", "model", "effort")}  # an empty string is unset
FLAGS = {("search", "rerank")}
NODES_KEPT = {("budget", "daily_usd"), ("budget", "first_run_usd"), ("compile", "concurrency")}  # read
# from the node itself, so a value tagged !!null is unset whatever it says


def _typed(path: tuple[Any, ...], node: yaml.ScalarNode, value: Any) -> Any:
    """A scalar under `path` read into the type it holds there: its text where it holds text, a whole float as a
    port, `_atoi` as `compile.concurrency`, and YAML 1.1's yes and no as a flag."""
    if value is None:
        return None
    if path in TEXT_KEYS:
        value = node.value
    if path in NOT_EMPTY and value == "":
        return None
    if path == ("port",) and isinstance(value, float) and value.is_integer() and -2**63 <= value < 2**63:
        return int(value)
    if path == ("compile", "concurrency") and isinstance(value, int) and not isinstance(value, bool):
        return _atoi(node.value)
    if path in FLAGS and node.tag == PLAIN and value in BOOLS:
        return BOOLS[value]
    return value


def _atoi(text: str) -> int | str:
    """`text` as a whole number within 64 bits, in decimal digits with an optional sign, or the text as written when
    it is not one."""
    found = re.fullmatch(r"([-+]?)0*([0-9]+)", text)
    value = int(found.group(1) + found.group(2)) if found and len(found.group(2)) <= 19 else None
    return value if value is not None and -2**63 <= value < 2**63 else text


def _old_key(node: yaml.Node) -> Any:
    """A key as YAML 1.1 reads it, as `agent.read_yaml` does for the trust check and the secrets store."""
    if node.tag != PLAIN:
        return node.value
    tag = yaml.resolver.Resolver().resolve(yaml.ScalarNode, node.value, (True, False))
    try:
        return yaml.SafeLoader("").construct_object(yaml.ScalarNode(tag, node.value))
    except Exception:  # noqa: BLE001 - YAML 1.1 cannot build it, as a date with no 13th month, and neither can read_yaml
        return node.value


def _read(text: str) -> Read:
    """Read settings.yaml as YAML 1.2: its scalars, libyaml's tabs, `<<` merge keys, each
    key as its text, and a key given twice kept for `check_settings` to name in its order. Raises `_Refused`."""
    loader = None
    try:
        loader = _SettingsLoader(text)
        root = loader.get_node() if loader.check_node() else None  # the first document alone
        aliases = loader.aliases
    except RecursionError:
        raise _Refused("it nests too deeply to read: write it flatter") from None
    except yaml.reader.ReaderError:  # read_yaml names the character and its line, as every file's reader does
        raise _Refused(read_yaml(text)[1] or "it holds a character YAML does not allow, such as a NUL") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        problem = getattr(error, "problem", None) or "it does not parse"
        raise _Refused(f"line {mark.line + 1}: {problem}" if mark is not None else problem) from None
    finally:
        if loader is not None:
            loader.dispose()
    out = Read()
    if root is None:
        return out
    tops: dict[int, int] = {}  # a value at the top of the file takes its key's line
    for key, value in root.value if isinstance(root, yaml.MappingNode) else ():
        if value.start_mark.index > key.start_mark.index:  # not an alias, whose node stands earlier, at its anchor
            tops.setdefault(id(value), key.start_mark.line + 1)
    count, built = [0], set()

    def line(node: yaml.Node) -> int:
        return tops.get(id(node), node.start_mark.line + 1)

    def build(node: yaml.Node, path: tuple[Any, ...], old: tuple[Any, ...], stack: frozenset[int]) -> Any:
        count[0] += id(node) in built
        built.add(id(node))
        if count[0] > NODES:
            raise _Refused("its aliases stand for too many values: write each value out")
        if id(node) in stack:
            raise _Refused(f"line {node.start_mark.line + 1}: an anchor holds itself: write the value out")
        out.values.setdefault(path, line(node))
        out.values.setdefault(old, line(node))
        if isinstance(node, yaml.ScalarNode):
            if path in NODES_KEPT and node.tag == "tag:yaml.org,2002:null":
                return None
            if path == ("timezone",) and node.tag not in (PLAIN, STR, "tag:yaml.org,2002:null"):
                out.tags.setdefault(path, f"line {line(node)}: timezone: a name takes no tag {node.tag}: remove it")
                return node.value
            try:
                return _typed(path, node, _scalar(node))
            except _Refused as error:
                out.tags.setdefault(path, f"line {line(node)}: {'.'.join(map(str, path))}: {error}")
                return node.value
        stack = stack | {id(node)}
        if isinstance(node, yaml.SequenceNode):
            return [build(item, (*path, i), (*old, i), stack) for i, item in enumerate(node.value)]
        return mapping(node, path, old, stack)

    def mapping(node: yaml.MappingNode, path: tuple[Any, ...], old: tuple[Any, ...], stack: frozenset[int]) -> dict:
        found: dict[Any, Any] = {}
        merges: list[tuple[yaml.Node, yaml.Node]] = []
        pairs = []
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode):
                raise _Refused(f"line {key.start_mark.line + 1}: a key is a list or a map: write a plain word")
            if key.value == "<<" and key.tag in (PLAIN, MERGE):
                merges.append((key, value))
            pairs.append(key)
        if path not in out.twice:
            twice = [(b, a) for i, a in enumerate(pairs) for b in pairs[i + 1:] if a.value == b.value]
            if twice:
                b, a = twice[0]
                out.twice[path] = (f"line {b.start_mark.line + 1}: the key {_shown(b.value)} is given twice, first on "
                                   f"line {a.start_mark.line + 1}: keep one")
        for key, value in node.value:
            if key.value == "<<" and key.tag in (PLAIN, MERGE):
                continue
            at, was = (*path, key.value), (*old, _old_key(key))
            out.keys.setdefault(at, key.start_mark.line + 1)
            out.keys.setdefault(was, key.start_mark.line + 1)
            if at in NODES_KEPT and (id(node), id(key)) in aliases:  # these are read from the alias's own node
                out.values.setdefault(at, aliases[id(node), id(key)])
                out.values.setdefault(was, aliases[id(node), id(key)])
            found[key.value] = build(value, at, was, stack)
        for key, value in merges:
            items = value.value if isinstance(value, yaml.SequenceNode) else [value]
            if not all(isinstance(item, yaml.MappingNode) for item in items):
                raise _Refused(f"line {key.start_mark.line + 1}: a << merge key takes a map or a list of maps")
            for item in items:
                if id(item) in stack or item is node:
                    raise _Refused(f"line {key.start_mark.line + 1}: an anchor holds itself: write the value out")
                for name, below in mapping(item, path, old, stack).items():
                    found.setdefault(name, below)
        return found

    try:
        out.value = build(root, (), (), frozenset())
    except RecursionError:
        raise _Refused("it nests too deeply to read: write it flatter") from None
    return out


def _scalar(node: yaml.ScalarNode) -> Any:
    if node.tag == PLAIN:
        return _plain(node.value)
    tag = node.tag.rsplit(":", 1)[-1]
    if tag not in ("int", "float", "bool", "null"):
        return node.value  # a quoted scalar, !!str, or any other tag is read as text
    found = _plain(node.value)
    kind = {"int": int, "float": (int, float), "bool": bool, "null": type(None)}[tag]
    if isinstance(found, bool) != (tag == "bool") or not isinstance(found, kind):
        raise _Refused(f"!!{tag} does not fit {node.value!r}: remove the tag or fix the value")
    return float(found) if tag == "float" else found


def _check_keys(value: Any, tree: Any, lines: dict[tuple[Any, ...], int], at: tuple[Any, ...] = ()) -> None:
    if not isinstance(value, dict) or not isinstance(tree, dict):
        return
    other = at == ("store",) and value.get("class", "sqlite") != "sqlite"
    for key, below in value.items():
        if other and isinstance(key, str):
            continue  # another store class takes the keys its constructor takes, which store.open_store checks
        path = (*at, key)
        if not (isinstance(key, str) and key in tree) and "*" not in tree:
            raise WorkspaceError(_at(lines.get(path), f"unknown key {_shown(key)}"))
        _check_keys(below, tree[key] if isinstance(key, str) and key in tree else tree["*"], lines, path)


def _placed(message: str, lines: dict[tuple[Any, ...], int]) -> str:
    """A refusal that names a key, as `settings.yaml budget.daily_usd is 0` or `serve.allowed_hosts in settings.yaml
    ...`, with the key's line, or the refusal as it was."""
    rest = message.removeprefix("settings.yaml ")
    if rest == message and " in settings.yaml" not in message:
        return message
    rest = rest.replace(" in settings.yaml", "", 1)
    named = sorted(((".".join(map(str, path)), line) for path, line in lines.items()), key=lambda n: -len(n[0]))
    for name, line in named:
        if rest.startswith(name) and rest[len(name):len(name) + 1] in ("", " ", ":", ",", "."):
            return _at(line, rest)
    return message


WITH_TOP = ("schedule",)  # blocks whose keys are checked with the top level's, before any value
LAST = ("id", "port", "models")  # checked after every block, in this order
LATER = ("sources", "settle")  # schedule keys whose list or map is refused after every block; articles' in its block


# The maps in which a key given twice refuses settings.yaml, at the second one's line, as does any map inside a block of
# `OWN`. A map anywhere else is refused for being a map. Either way no value given twice is silently dropped.
DUPLICATE_CHECKED = {(), ("id",), ("port",), ("schedule",), *(("schedule", key) for key in ("sources", "settle")),
           ("models",), *(("models", job) for job in JOBS), ("budget",), ("compile",),
           *(("models", job, part) for job in JOBS for part in ("provider", "model", "effort"))}
OWN = ("store", "serve", "facts", "search", "collections", "providers")  # blocks refused for a key twice at any depth


def check_settings(settings: dict[str, Any], lines: dict[tuple[Any, ...], int] | None = None, *,
                   values: dict[tuple[Any, ...], int] | None = None, twice: dict[tuple[Any, ...], str] | None = None,
                   tags: dict[tuple[Any, ...], str] | None = None) -> None:
    """Refuse settings the workspace cannot run on, with a line that names the key, in this order: a key unknown at
    the top or under `schedule`, then a key given twice at the top,
    then each block in the file's order, its own unknown keys before its values, then a value of the wrong kind
    under `id`, `port`, a model job or `schedule`, or a key given twice in a block, in the file's order, and last the
    rest of the checks of `id`, `port` and `models`. `lines` gives each key's line in the file, `values` each value's
    line, which a refusal of a value names, `twice` the first key given twice in each map, and `tags` each value its tag does not
    fit, which refuses its block, as `_read` reads them."""
    lines = lines or {}
    values = values or lines
    twice = {path: said for path, said in (twice or {}).items()
             if path in DUPLICATE_CHECKED or path[:1] and path[0] in OWN}
    tree = known_keys()
    _check_keys(settings, {key: below if key in WITH_TOP else None for key, below in tree.items()}, lines)
    if () in twice:
        raise WorkspaceError(f"settings.yaml: {twice[()]}")
    for key, value in settings.items():
        _check_keys(value, tree[key], lines, (key,))
        if _given_twice(key, twice):
            continue
        tagged = {path: said for path, said in (tags or {}).items() if path[0] == key}
        if key == "schedule" and isinstance(value, dict):  # its keys are checked in the file's order
            for entry in value:
                if said := next((said for path, said in tagged.items() if path[:2] == (key, entry)), None):
                    raise WorkspaceError(f"settings.yaml: {said}")
                _placing(functools.partial(_schedule, only=entry), settings, values)
            continue
        if tagged:
            raise WorkspaceError(f"settings.yaml: {next(iter(tagged.values()))}")
        if key not in LAST and key in CHECKS:
            _placing(CHECKS[key], settings, values)
    for key, value in settings.items():
        for check in _of_wrong_kind(key, value, twice, values):
            _placing(check, settings, values)
    for key in LAST:
        _placing(CHECKS[key], settings, lines)


def _given_twice(key: Any, twice: dict[tuple[Any, ...], str]) -> str | None:
    """The key given twice that refuses the block `key` before its values are checked: one in the
    block's own map, or anywhere in a block of `OWN`."""
    if key in OWN:
        return next((said for path, said in twice.items() if path[0] == key), None)
    return twice.get((key,))


def _refuse(said: str, settings: dict[str, Any]) -> None:
    raise WorkspaceError(f"settings.yaml: {said}")


def _of_wrong_kind(key: Any, value: Any, twice: dict[tuple[Any, ...], str] | None = None,
                   values: dict[tuple[Any, ...], int] | None = None) -> list[Callable[[dict[str, Any]], object]]:
    """The checks that refuse `value` under `key` when it is of a kind refused on every
    command: a list or a map as `id`, what is no number of 64 bits as `port`, as a model job anything but a map, or
    a map that holds a list or a map, and as `schedule` anything but a map, or a list or a map as its `sources` or
    `settle`. A key given twice in the block is refused in its place."""
    twice = twice or {}
    if said := _given_twice(key, twice):
        return [functools.partial(_refuse, said)]
    if key == "id":
        return [_id] if isinstance(value, (dict, list)) else []
    if key == "port":
        odd = isinstance(value, bool) or not isinstance(value, (int, float)) or not -2**63 <= value < 2**63
        return [_port] if value is not None and odd else []
    if key == "schedule" and value is not None:
        if not isinstance(value, dict):
            return [read_schedule]
        return [functools.partial(_refuse, twice[("schedule", entry)]) if ("schedule", entry) in twice else
                functools.partial(_schedule_entry, entry) for entry, item in value.items()
                if entry in LATER and isinstance(item, (dict, list))]
    if key != "models" or value is None:
        return []
    if not isinstance(value, dict):
        return [read_models]
    found: list[Callable[[dict[str, Any]], object]] = []
    for job, entry in value.items():
        if job not in JOBS or entry is None:
            continue
        if ("models", job) in twice:
            found.append(functools.partial(_refuse, twice[("models", job)]))
        elif not isinstance(entry, dict) or any(isinstance(item, (dict, list)) for item in entry.values()):
            first = next(part for part, item in entry.items() if isinstance(item, (dict, list))) \
                if isinstance(entry, dict) else None
            found.append(functools.partial(_refuse, twice[("models", job, first)]) if ("models", job, first) in twice
                         else functools.partial(_job, job, at=(values or {}).get(("models", job, first))
                                                if first in ("provider", "model") else None))
    return found


def _job(job: str, settings: dict[str, Any], at: int | None = None) -> None:
    """Check one model job; `at` is the line of the list or the map it holds as a value, which the refusal names."""
    try:
        read_model(settings["models"][job], f"settings.yaml models.{job}", job)
    except CerebroError as error:
        if at is None:
            raise
        raise WorkspaceError(_at(at, str(error).removeprefix("settings.yaml "))) from None


def _schedule_entry(entry: str, settings: dict[str, Any]) -> None:
    read_schedule({"schedule": {entry: settings["schedule"][entry]}})


def _schedule(settings: dict[str, Any], only: str | None = None) -> None:
    block = settings.get("schedule")
    if isinstance(block, dict):  # any other kind is named with the values of the wrong kind, as in _of_wrong_kind
        read_schedule({"schedule": {entry: item for entry, item in block.items() if only in (None, entry)
                                    and not (entry in LATER and isinstance(item, (dict, list)))}})


def _placing(check: Callable[[dict[str, Any]], object], settings: dict[str, Any],
             lines: dict[tuple[Any, ...], int]) -> None:
    try:
        check(settings)
    except CerebroError as error:
        placed = _placed(str(error), lines)
        if placed != str(error):
            raise WorkspaceError(placed) from None
        raise


def _budget(settings: dict[str, Any]) -> None:
    read_caps(settings)
    block = settings.get("budget")
    value = block.get("first_run_usd") if isinstance(block, dict) else None
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                              or _infinite(value) or not LOWEST <= value <= HIGHEST):
        raise WorkspaceError(f"settings.yaml budget.first_run_usd is {value!r}: write a number of dollars from "
                         f"{LOWEST:.9f} to {HIGHEST:.0f}, or leave the key out for ${FIRST_RUN_USD:g}")


def _search(settings: dict[str, Any]) -> None:
    from .rerank import RerankSettingsError, read_rerank  # it imports search and models, which import this module's kin

    try:
        read_rerank(settings)
    except RerankSettingsError as error:
        raise WorkspaceError(str(error)) from None


def _facts(settings: dict[str, Any]) -> None:
    if settings.get("facts") is not None and not isinstance(settings["facts"], dict):
        raise WorkspaceError("settings.yaml facts is not a map: write facts: then revisions_kept: under it")
    _number(settings, "facts", "revisions_kept", whole=True, low=0, high=1000)


def _compile(settings: dict[str, Any]) -> None:
    if settings.get("compile") is not None and not isinstance(settings["compile"], dict):
        raise WorkspaceError("settings.yaml compile is not a map: write compile: then concurrency: under it")
    _number(settings, "compile", "concurrency", whole=True, low=1, high=64)


def min_grounding(settings: dict[str, Any]) -> float:
    """The score under which a sentence's grounding is marked, `min_grounding`, or `ground.FLOOR` when it is left out.
    An agent file's own takes its place."""
    from .ground import FLOOR

    value = settings.get("min_grounding")
    if value is None:
        return FLOOR
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise WorkspaceError(f"settings.yaml min_grounding is {value!r}: write a number from 0 to 1, such as 0.6")
    return float(value)


def _serve(settings: dict[str, Any]) -> None:
    serve = settings.get("serve")
    if serve is not None and not isinstance(serve, dict):
        raise WorkspaceError("settings.yaml serve is not a map: write serve: then host:, port: or interval: under it")
    host = (serve or {}).get("host")
    if host is not None and not isinstance(host, str):
        raise WorkspaceError(f"settings.yaml serve.host is {host!r}: write an address, as host: 127.0.0.1")
    _number(settings, "serve", "port", whole=True, low=1, high=65535)
    _number(settings, "serve", "interval", whole=False, low=1, high=86_400)
    _number(settings, "serve", "concurrency", whole=True, low=1, high=10_000)
    for key in SERVE_LIMITS[1:]:
        _number(settings, "serve", key, whole=False, low=1, high=3600)
    from .tokens import HostError, OriginError, allowed_hosts, allowed_origins

    try:
        allowed_hosts(None, settings)
        allowed_origins(None, settings)
    except (HostError, OriginError) as error:
        raise WorkspaceError(str(error)) from None


def _collections(settings: dict[str, Any]) -> None:
    try:
        descriptions(settings)
    except RenderError as error:
        raise WorkspaceError(str(error)) from None


def _port(settings: dict[str, Any]) -> None:
    port = settings.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
        raise WorkspaceError(f"settings.yaml port is {port!r}: write a whole number from 1 to 65535")


def _id(settings: dict[str, Any]) -> None:
    found = settings.get("id")
    if found is not None and (isinstance(found, bool) or not isinstance(found, (str, int))):
        raise WorkspaceError(f"settings.yaml id is {found!r}: write the id cerebro init wrote, a word such as "
                         "notes-wiki-36fe74")
    try:
        if found:  # an empty id opens no secrets store
            check_id(str(found))
    except SecretsError:
        raise WorkspaceError(f"settings.yaml id is {found!r}: write 1 to 64 letters, digits, _ and -, as the id cerebro "
                         "init wrote, such as notes-wiki-36fe74") from None


def _store(settings: dict[str, Any]) -> None:
    if settings.get("store") is not None and not isinstance(settings["store"], dict):
        raise WorkspaceError("settings.yaml store is not a map: write store: then path: or class: under it")


# The check of each top-level key's value. A store's own keys are checked when it opens, by its class.
CHECKS: dict[str, Callable[[dict[str, Any]], object]] = {
    "models": read_models, "budget": _budget, "schedule": _schedule, "timezone": zone_of, "search": _search,
    "facts": _facts, "compile": _compile, "serve": _serve, "collections": _collections, "port": _port, "id": _id,
    "store": _store, "providers": read_providers, "min_grounding": min_grounding,
}


def _parsed(text: str) -> dict[str, Any]:
    """The checked settings in `text`, read by `_read`. `id` and `store` keep the value YAML 1.1
    gives them, as the trust check and the secrets store read the file, so all three name one id and one store; a
    file those cannot read is refused while it holds either key, with the line they cannot read."""
    try:
        read = _read(text)
    except _Refused as problem:
        said = f"at {problem}" if str(problem).startswith("line ") else f"here: {problem}"
        raise WorkspaceError(_told(f"settings.yaml is not YAML {said}", "a tab stands where YAML wants a space")) from None
    settings = {} if read.value is None else read.value
    if not isinstance(settings, dict):
        raise WorkspaceError("settings.yaml is not a map of settings: write key: value lines")
    try:
        old, problem = read_yaml(text)
    except Exception:  # noqa: BLE001 - a number past Python's 4300 digits, or a tag PyYAML fails to construct
        old, problem = None, "a value YAML 1.1 cannot read: write it plainly"
    for key in ("id", "store"):
        if problem is None and isinstance(old, dict) and key in old:
            settings[key] = old[key]
    check_settings(settings, read.keys, values=read.values, twice=read.twice, tags=read.tags)
    # the file reads as YAML 1.2, but the YAML 1.1 reader of the trust check and the secrets store cannot read it
    if problem is not None and ("id" in settings or "store" in settings):
        raise WorkspaceError(_told(f"settings.yaml: {problem}", "a tab here is not read while the file holds id or store",
                               "write that line plainly"))
    return settings


TAB = "found character '\\t' that cannot start any token"  # PyYAML's words for a tab it does not read
TOLD = re.compile(r": (write|remove|keep|delete|fix|check|quote)\b")


def _told(said: str, tab: str, fix: str = "fix that line or quote its value") -> str:
    """A refusal of a file that does not read, ending with what to write: a tab is named in plain words, and a
    problem that already says what to write keeps its own words alone."""
    if TAB in said:
        return said.replace(TAB, tab) + ": write a space in place of the tab"
    return said if TOLD.search(said) else f"{said}: {fix}"


def _text(found: Path) -> str:
    if not (found / "settings.yaml").is_file():
        raise WorkspaceError(f"{found} is not a workspace (no settings.yaml in it): run in the workspace's folder, or name it "
                         "with --workspace")
    try:
        return (found / "settings.yaml").read_text(encoding="utf-8").removeprefix("\ufeff")
    except (OSError, UnicodeDecodeError) as error:
        raise WorkspaceError(f"settings.yaml cannot be read as UTF-8 text ({type(error).__name__}): save it as UTF-8")


def checked_settings(root: Path) -> dict[str, Any]:
    """The settings of the workspace at `root`, checked as `load` checks them, whether or not its owner trusts it: the
    file is read as text, and nothing of the workspace is imported or run."""
    return _parsed(_text(Path(root).resolve()))


def read_settings(root: Path, *, trusted: bool | None = None) -> dict[str, Any]:
    """The checked settings of the workspace at `root`, which is refused as `load` refuses it when its owner has not
    trusted it, unless `trusted` is True. The file is checked before trust when `trusted` is True or a trust record
    names the folder, since the trust check opens the store the settings name; for any other folder, the trust
    refusal comes first."""
    found = Path(root).resolve()
    text = _text(found)
    first = trusted or any(entry["path"] == str(found) for entry in trust_records())
    settings = _parsed(text) if first else {}
    check_trust(found, trusted)  # before anything in settings.yaml is acted on
    return settings if first else _parsed(text)


def load(root: Path | None = None, *, trusted: bool | None = None) -> Project:
    """Load the workspace at `root`, or the current folder. Folders above are never looked in, so no other folder's
    `extensions.py` runs. It loads only when `trusted` is True or its owner trusts it, as `trust.check` says, and
    that is checked before anything in settings.yaml is acted on."""
    found = Path(root if root is not None else Path.cwd()).resolve()
    settings = read_settings(found, trusted=trusted)
    agents, refused = load_agents(found / "agents")
    functions, apps = _extensions(found)
    notices = agent_notices(agents)
    return Project(root=found, settings=settings, agents=agents, refused=refused, notices=notices,
                   extensions=functions, apps=apps, models=read_models(settings), caps=read_caps(settings),
                   zone=zone_of(settings), kinds=load_kinds(found))
